from collections.abc import Iterable

from app.api.grok.client import GrokClient
from app.api.grok.routing import resolve_model_route
from app.api.grok.schemas import OBJECT_TYPE_ENUM, PLANNER_JSON_SCHEMA
from app.core.settings import get_settings
from app.helpers.json_parser import extract_json_payload
from app.helpers.prompts import build_planner_messages
from app.loggers.logger import get_logger
from app.validation.payloads import ensure_prompt_is_valid

logger = get_logger(__name__)

OBJECT_TYPE_NORMALIZATION = {
    "trigger": "triggers",
    "triggers": "triggers",
    "automation": "automations",
    "automations": "automations",
    "macro": "macros",
    "macros": "macros",
    "view": "views",
    "views": "views",
    "group": "groups",
    "groups": "groups",
    "ticket_field": "ticket_fields",
    "ticket_fields": "ticket_fields",
    "ticket_form": "ticket_forms",
    "ticket_forms": "ticket_forms",
    "article": "articles",
    "articles": "articles",
}


def _normalize_object_type(value: str) -> str:
    normalized = value.strip().lower()
    resolved = OBJECT_TYPE_NORMALIZATION.get(normalized)
    if resolved:
        return resolved
    return OBJECT_TYPE_ENUM[0]


def _as_string_list(value: object, *, limit: int = 6) -> list[str]:
    if isinstance(value, str):
        cleaned = value.strip()
        return [cleaned] if cleaned else []
    if not isinstance(value, Iterable):
        return []
    output: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            output.append(text)
    return output[:limit]


async def run_planner(
    prompt: str,
    *,
    dependency_mode: str = "match_existing_or_create_new",
    related_objects: list[dict] | None = None,
    reference_catalog: dict | None = None,
    recent_batch_context: list[str] | None = None,
    context_notes: str | None = None,
) -> dict:
    cleaned_prompt = ensure_prompt_is_valid(prompt)
    settings = get_settings()
    route = resolve_model_route(settings, "planner")
    fallback_plan = {
        "object_type": "triggers",
        "intent": cleaned_prompt,
        "confidence": 0.7,
        "ambiguity_score": 0.5,
        "ambiguity_reasons": ["Planner fallback used due to model output parse failure."],
        "clarification_questions": [],
        "dependency_notes": "",
        "llm": {
            "task": route.task,
            "model": route.model,
            "strict_schema": route.strict_schema,
        },
    }

    client = GrokClient()
    messages = build_planner_messages(
        cleaned_prompt,
        dependency_mode=dependency_mode,
        related_objects=related_objects,
        reference_catalog=reference_catalog,
        recent_batch_context=recent_batch_context,
        context_notes=context_notes,
    )

    try:
        raw = await client.chat(
            messages,
            temperature=0.0,
            model=route.model,
            max_output_tokens=route.max_output_tokens,
            response_schema=PLANNER_JSON_SCHEMA,
            response_schema_name="planner_result",
            strict_schema=route.strict_schema,
        )
        payload = extract_json_payload(raw)
        if not isinstance(payload, dict):
            raise ValueError("Planner response must be a JSON object.")

        confidence = float(payload.get("confidence", 0.7))
        confidence = min(max(confidence, 0.0), 1.0)
        ambiguity_score = float(payload.get("ambiguity_score", 0.5))
        ambiguity_score = min(max(ambiguity_score, 0.0), 1.0)

        return {
            "object_type": _normalize_object_type(str(payload.get("object_type", "triggers"))),
            "intent": str(payload.get("intent", cleaned_prompt)),
            "confidence": confidence,
            "ambiguity_score": ambiguity_score,
            "ambiguity_reasons": _as_string_list(payload.get("ambiguity_reasons"), limit=12),
            "clarification_questions": _as_string_list(
                payload.get("clarification_questions"),
                limit=6,
            ),
            "dependency_notes": str(payload.get("dependency_notes", "")),
            "llm": {
                "task": route.task,
                "model": route.model,
                "strict_schema": route.strict_schema,
            },
        }
    except Exception as exc:
        logger.warning("Planner fallback in use: %s", exc)
        return fallback_plan
