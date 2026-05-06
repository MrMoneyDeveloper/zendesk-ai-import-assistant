from app.api.grok.client import GrokClient
from app.helpers.json_parser import extract_json_payload
from app.helpers.prompts import build_generator_messages
from app.loggers.logger import get_logger
from app.validation.payloads import normalize_generated_rows

logger = get_logger(__name__)


def _fallback_generated_rows() -> list[dict]:
    return [
        {
            "title": "Billing Routing",
            "conditions": [{"field": "group", "operator": "is", "value": "billing"}],
            "actions": [{"field": "assign", "value": "Billing Team"}],
        }
    ]


async def run_generator(plan: dict) -> list[dict]:
    client = GrokClient()
    messages = build_generator_messages(plan)

    try:
        raw = await client.chat(messages, temperature=0.1)
        payload = extract_json_payload(raw)

        records = payload.get("records") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            raise ValueError("Generator response must be a JSON array or {\"records\": [...]}")

        return normalize_generated_rows(records)
    except Exception as exc:
        logger.warning("Generator fallback in use: %s", exc)
        return _fallback_generated_rows()
