from typing import Any

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

    @staticmethod
    def _extract_response_text(data: dict[str, Any]) -> str:
        message = ((data.get("choices") or [{}])[0]).get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")).strip())
            return "\n".join(part for part in text_parts if part).strip()
        return str(content)

    @staticmethod
    def _extract_error_detail(response: httpx.Response) -> str:
        detail = response.text
        try:
            payload = response.json()
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    return str(error.get("message") or error.get("code") or detail)
                if error:
                    return str(error)
                return str(payload.get("message") or detail)
        except Exception:
            pass
        return detail

    @staticmethod
    def _build_response_format(schema_name: str, schema: dict, strict: bool) -> dict:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": strict,
                "schema": schema,
            },
        }

    async def _post_completion(self, payload: dict, headers: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.settings.xai_request_timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = self._extract_error_detail(response)
            raise RuntimeError(
                f"{self.provider_name} API request failed ({response.status_code}): {detail}"
            ) from exc
        return response.json()

    async def chat(
        self,
        messages: list[dict],
        temperature: float | None = None,
        *,
        model: str | None = None,
        max_output_tokens: int | None = None,
        response_schema: dict | None = None,
        response_schema_name: str = "structured_response",
        strict_schema: bool | None = None,
    ) -> str:
        if not self.settings.xai_enabled:
            raise RuntimeError("xAI integration is disabled via XAI_ENABLED.")
        if not self.settings.xai_api_key:
            raise RuntimeError("XAI_API_KEY is missing from environment.")

        selected_model = model or self.settings.xai_model or RECOMMENDED_MODEL
        if selected_model not in SUPPORTED_MODELS:
            logger.warning(
                "Model '%s' is not in SUPPORTED_MODELS metadata, continuing anyway.",
                selected_model,
            )

        payload = {
            "model": selected_model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.settings.xai_temperature,
            "max_tokens": max_output_tokens or self.settings.xai_max_output_tokens,
        }
        strict = self.settings.llm_strict_schema_mode if strict_schema is None else strict_schema
        if response_schema:
            payload["response_format"] = self._build_response_format(
                response_schema_name,
                response_schema,
                strict=strict,
            )

        headers = {
            "Authorization": f"Bearer {self.settings.xai_api_key}",
            "Content-Type": "application/json",
        }

        try:
            data = await self._post_completion(payload, headers)
            return self._extract_response_text(data)
        except RuntimeError as exc:
            if not (
                response_schema
                and strict
                and self.settings.llm_fallback_to_json_object
                and "400" in str(exc)
            ):
                raise
            logger.warning(
                "Strict schema request failed for model '%s'. Retrying with json_object mode. Error: %s",
                selected_model,
                exc,
            )
            relaxed_payload = dict(payload)
            relaxed_payload["response_format"] = {"type": "json_object"}
            data = await self._post_completion(relaxed_payload, headers)
            return self._extract_response_text(data)
