from collections.abc import Iterable

from app.api.grok.client import GrokClient, LLMRequestError
from app.api.grok.routing import resolve_model_route
from app.api.grok.schemas import OBJECT_TYPE_ENUM
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


def _is_model_availability_error(exc: Exception) -> bool:
    text = str(exc).strip().lower()
    if not text:
        return False
    markers = (
        "model",
        "permission",
        "not available",
        "not found",
        "unsupported",
        "forbidden",
    )
    return any(marker in text for marker in markers)


def _is_json_generation_error(exc: Exception) -> bool:
    text = str(exc).strip().lower()
    if not text:
        return False
    return (
        "failed to generate json" in text
        or "failed_generation" in text
        or "json_validate_failed" in text
        or "[schema_validation_failure]" in text
        or "[other_invalid_request]" in text
    )


def _is_rate_limited_error(exc: Exception) -> bool:
    if isinstance(exc, LLMRequestError):
        return str(exc.error_class or "").strip().lower() == "rate_limited"
    text = str(exc).strip().lower()
    return "[rate_limited]" in text or "rate_limit" in text or "too many requests" in text


def _resolve_fallback_object_type(focus_object_types: list[str] | None) -> str:
    if not isinstance(focus_object_types, list):
        return "triggers"
    for raw in focus_object_types:
        normalized = _normalize_object_type(str(raw))
        if normalized in OBJECT_TYPE_ENUM:
            return normalized
    return "triggers"


def _build_plan_from_payload(payload: dict, cleaned_prompt: str, route) -> dict:
    confidence = float(payload.get("confidence", 0.7))
    confidence = min(max(confidence, 0.0), 1.0)
    ambiguity_score = float(payload.get("ambiguity_score", 0.5))
    ambiguity_score = min(max(ambiguity_score, 0.0), 1.0)
    telemetry = GrokClient.get_last_call_metrics(route.task)
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
            "model": str(telemetry.get("model") or route.model),
            "provider": str(telemetry.get("provider") or ""),
            "strict_schema": route.strict_schema,
            "telemetry": telemetry,
        },
    }


async def run_planner(
    prompt: str,
    *,
    dependency_mode: str = "match_existing_or_create_new",
    focus_object_types: list[str] | None = None,
    related_objects: list[dict] | None = None,
    reference_catalog: dict | None = None,
    recent_batch_context: list[str] | None = None,
    context_notes: str | None = None,
    allow_fallback: bool = True,
) -> dict:
    cleaned_prompt = ensure_prompt_is_valid(prompt)
    settings = get_settings()
    route = resolve_model_route(settings, "planner")
    fallback_object_type = _resolve_fallback_object_type(focus_object_types)
    fallback_plan = {
        "object_type": fallback_object_type,
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
            "telemetry": {},
        },
    }

    client = GrokClient()
    messages = build_planner_messages(
        cleaned_prompt,
        dependency_mode=dependency_mode,
        focus_object_types=focus_object_types,
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
            response_schema=None,
            strict_schema=False,
            task=route.task,
            response_format_override="json_object",
        )
        payload = extract_json_payload(raw)
        if not isinstance(payload, dict):
            raise ValueError("Planner response must be a JSON object.")
        return _build_plan_from_payload(payload, cleaned_prompt, route)
    except Exception as exc:
        if _is_rate_limited_error(exc):
            logger.warning("Planner rate limited; skipping planner fallback fanout.")
            raise RuntimeError(f"Planner model call failed: {exc}") from exc
        if (
            (_is_model_availability_error(exc) or _is_json_generation_error(exc))
            and not _is_rate_limited_error(exc)
            and route.model != settings.llm_model_generator
        ):
            logger.warning(
                "Planner model '%s' unavailable/unstable for this request, retrying with fallback model '%s'.",
                route.model,
                settings.llm_model_generator,
            )
            fallback_route = resolve_model_route(settings, "planner")
            fallback_route = type(fallback_route)(
                task=fallback_route.task,
                model=settings.llm_model_generator,
                max_output_tokens=min(
                    settings.llm_planner_max_output_tokens,
                    settings.llm_generator_max_output_tokens,
                ),
                strict_schema=True,
            )
            try:
                raw = await client.chat(
                    messages,
                    temperature=0.0,
                    model=fallback_route.model,
                    max_output_tokens=fallback_route.max_output_tokens,
                    response_schema=None,
                    strict_schema=False,
                    task=fallback_route.task,
                    response_format_override="json_object",
                )
                payload = extract_json_payload(raw)
                if not isinstance(payload, dict):
                    raise ValueError("Planner response must be a JSON object.")
                return _build_plan_from_payload(payload, cleaned_prompt, fallback_route)
            except Exception as fallback_exc:
                logger.warning("Planner fallback model call failed: %s", fallback_exc)
        logger.warning("Planner fallback in use: %s", exc)
        if allow_fallback:
            return fallback_plan
        raise RuntimeError(f"Planner model call failed: {exc}") from exc
