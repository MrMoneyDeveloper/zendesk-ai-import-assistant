import time

from app.api.grok.client import GrokClient
from app.core.settings import get_settings


def _provider_label(provider: str) -> str:
    if provider == "groq":
        return "Groq (OpenAI-compatible)"
    return "xAI (OpenAI-compatible)"


async def run_api_test() -> dict:
    settings = get_settings()
    provider = _provider_label(settings.llm_provider)

    if not settings.xai_enabled:
        return {
            "provider": provider,
            "base_url": settings.xai_base_url,
            "model": settings.xai_model,
            "status": "skipped",
            "detail": "XAI_ENABLED is false.",
        }

    if not settings.xai_api_key:
        return {
            "provider": provider,
            "base_url": settings.xai_base_url,
            "model": settings.xai_model,
            "status": "skipped",
            "detail": "XAI_API_KEY is not configured yet.",
        }

    client = GrokClient()
    start = time.perf_counter()

    try:
        content = await client.chat(
            [
                {
                    "role": "system",
                    "content": "You are a health-check assistant. Reply in under 12 words.",
                },
                {"role": "user", "content": "Return a short API connectivity confirmation."},
            ],
            temperature=0.0,
        )
        latency_ms = (time.perf_counter() - start) * 1000

        return {
            "provider": provider,
            "base_url": settings.xai_base_url,
            "model": settings.xai_model,
            "status": "ok",
            "latency_ms": round(latency_ms, 2),
            "output_preview": content[:220],
            "detail": "Request completed successfully.",
        }
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        return {
            "provider": provider,
            "base_url": settings.xai_base_url,
            "model": settings.xai_model,
            "status": "error",
            "latency_ms": round(latency_ms, 2),
            "detail": str(exc),
        }
