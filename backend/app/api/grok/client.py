import httpx

from app.api.grok.models import RECOMMENDED_MODEL, SUPPORTED_MODELS
from app.core.settings import get_settings
from app.loggers.logger import get_logger

logger = get_logger(__name__)


class GrokClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_url = self.settings.xai_base_url
        self.provider_name = "Groq" if self.settings.llm_provider == "groq" else "xAI"

    async def chat(self, messages: list[dict], temperature: float | None = None) -> str:
        if not self.settings.xai_enabled:
            raise RuntimeError("xAI integration is disabled via XAI_ENABLED.")
        if not self.settings.xai_api_key:
            raise RuntimeError("XAI_API_KEY is missing from environment.")

        model = self.settings.xai_model or RECOMMENDED_MODEL
        if model not in SUPPORTED_MODELS:
            logger.warning(
                "Model '%s' is not in SUPPORTED_MODELS metadata, continuing anyway.",
                model,
            )

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.settings.xai_temperature,
            "max_tokens": self.settings.xai_max_output_tokens,
        }

        headers = {
            "Authorization": f"Bearer {self.settings.xai_api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.settings.xai_request_timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = response.text
                try:
                    body = response.json()
                    detail = body.get("error") or body.get("message") or response.text
                except Exception:
                    pass
                raise RuntimeError(
                    f"{self.provider_name} API request failed ({response.status_code}): {detail}"
                ) from exc

        data = response.json()
        return data["choices"][0]["message"]["content"]
