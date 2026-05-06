from app.api.grok.client import GrokClient
from app.helpers.json_parser import extract_json_payload
from app.helpers.prompts import build_planner_messages
from app.loggers.logger import get_logger
from app.validation.payloads import ensure_prompt_is_valid

logger = get_logger(__name__)


async def run_planner(prompt: str) -> dict:
    cleaned_prompt = ensure_prompt_is_valid(prompt)
    fallback_plan = {
        "object_type": "trigger",
        "intent": cleaned_prompt,
        "confidence": 0.7,
    }

    client = GrokClient()
    messages = build_planner_messages(cleaned_prompt)

    try:
        raw = await client.chat(messages, temperature=0.0)
        payload = extract_json_payload(raw)
        if not isinstance(payload, dict):
            raise ValueError("Planner response must be a JSON object.")

        confidence = float(payload.get("confidence", 0.7))
        confidence = min(max(confidence, 0.0), 1.0)

        return {
            "object_type": str(payload.get("object_type", "trigger")),
            "intent": str(payload.get("intent", cleaned_prompt)),
            "confidence": confidence,
        }
    except Exception as exc:
        logger.warning("Planner fallback in use: %s", exc)
        return fallback_plan
