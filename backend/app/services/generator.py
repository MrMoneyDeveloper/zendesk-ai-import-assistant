from app.api.grok.client import GrokClient
from app.api.grok.routing import resolve_model_route
from app.api.grok.schemas import GENERATOR_JSON_SCHEMA
from app.core.settings import get_settings
from app.helpers.json_parser import extract_json_payload
from app.helpers.prompts import build_generator_messages
from app.loggers.logger import get_logger
from app.validation.payloads import normalize_generated_rows

logger = get_logger(__name__)


def _fallback_generated_rows() -> list[dict]:
    return [
        {
            "object_type": "triggers",
            "title": "Default Intake Triage",
            "conditions": [{"field": "status", "operator": "is", "value": "new"}],
            "actions": [
                {"field": "status", "value": "open"},
                {"field": "set_tags", "value": "ai_import_generated"},
            ],
            "dependency_notes": ["Generator fallback output used."],
        }
    ]


async def run_generator(
    plan: dict,
    *,
    dependency_mode: str = "match_existing_or_create_new",
    related_objects: list[dict] | None = None,
    reference_catalog: dict | None = None,
    recent_batch_context: list[str] | None = None,
    context_notes: str | None = None,
) -> list[dict]:
    settings = get_settings()
    route = resolve_model_route(settings, "generator")
    client = GrokClient()
    messages = build_generator_messages(
        plan,
        dependency_mode=dependency_mode,
        related_objects=related_objects,
        reference_catalog=reference_catalog,
        recent_batch_context=recent_batch_context,
        context_notes=context_notes,
    )

    try:
        raw = await client.chat(
            messages,
            temperature=0.1,
            model=route.model,
            max_output_tokens=route.max_output_tokens,
            response_schema=GENERATOR_JSON_SCHEMA,
            response_schema_name="generator_records",
            strict_schema=route.strict_schema,
        )
        payload = extract_json_payload(raw)

        records: list[dict] | None = None
        if isinstance(payload, dict):
            payload_records = payload.get("records")
            if isinstance(payload_records, list):
                records = payload_records
        elif isinstance(payload, list):
            records = payload

        if not isinstance(records, list):
            raise ValueError('Generator response must be {"records":[...]} or a JSON array.')

        return normalize_generated_rows(records)
    except Exception as exc:
        logger.warning("Generator fallback in use: %s", exc)
        return _fallback_generated_rows()
