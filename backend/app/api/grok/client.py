import asyncio
import json
import random
import time
from copy import deepcopy
from typing import ClassVar
from typing import Any

import httpx

from app.api.grok.models import RECOMMENDED_MODEL, SUPPORTED_MODELS
from app.core.settings import get_settings
from app.loggers.logger import get_logger

logger = get_logger(__name__)


class GrokClient:
    _MODEL_TPM_DEFAULTS: ClassVar[dict[str, int]] = {
        "qwen/qwen3-32b": 3000,
        "openai/gpt-oss-20b": 8000,
    }
    _MODEL_USAGE_STATE: ClassVar[dict[str, dict[str, float | int | None]]] = {}
    _LAST_CALL_METRICS: ClassVar[dict[str, dict[str, Any]]] = {}

    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_url = self.settings.xai_base_url
        self.provider_name = "Groq" if self.settings.llm_provider == "groq" else "xAI"

    @classmethod
    def get_last_call_metrics(cls, task: str) -> dict[str, Any]:
        metrics = cls._LAST_CALL_METRICS.get(task, {})
        return deepcopy(metrics)

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

    @staticmethod
    def _parse_int_header(value: str | None) -> int | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return int(float(cleaned))
        except ValueError:
            return None

    @staticmethod
    def _parse_reset_seconds(value: str | None) -> float | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if not cleaned:
            return None
        if cleaned.endswith("ms"):
            try:
                return max(float(cleaned[:-2]) / 1000.0, 0.0)
            except ValueError:
                return None
        if cleaned.endswith("s"):
            cleaned = cleaned[:-1]
        try:
            parsed = float(cleaned)
        except ValueError:
            return None
        if parsed < 0:
            return None
        now_epoch = time.time()
        # Some APIs return epoch timestamps for reset headers.
        if parsed > now_epoch + 60:
            return max(parsed - now_epoch, 0.0)
        return parsed

    @staticmethod
    def _extract_usage_tokens(data: dict[str, Any]) -> int | None:
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return None
        value = usage.get("total_tokens")
        try:
            total_tokens = int(value)
        except (TypeError, ValueError):
            return None
        return total_tokens if total_tokens > 0 else None

    @staticmethod
    def _estimate_payload_tokens(payload: dict[str, Any]) -> int:
        try:
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            serialized = str(payload)
        input_estimate = max(1, len(serialized) // 4)
        reserved_output = int(max(float(payload.get("max_tokens", 0) or 0.0), 0.0) * 0.7)
        return input_estimate + reserved_output

    def _get_model_usage_state(self, model: str) -> dict[str, float | int | None]:
        now_mono = time.monotonic()
        state = self._MODEL_USAGE_STATE.get(model)
        if not state:
            state = {
                "window_started_at": now_mono,
                "window_used_tokens": 0.0,
                "remaining_tokens": None,
                "reset_tokens_seconds": None,
                "limit_tokens": None,
            }
            self._MODEL_USAGE_STATE[model] = state
            return state

        started = float(state.get("window_started_at") or now_mono)
        if now_mono - started >= 60.0:
            state["window_started_at"] = now_mono
            state["window_used_tokens"] = 0.0
        return state

    def _resolve_model_tpm_budget(
        self,
        model: str,
        *,
        state: dict[str, float | int | None] | None = None,
    ) -> int:
        working_state = state or self._get_model_usage_state(model)
        header_limit = working_state.get("limit_tokens")
        if isinstance(header_limit, (int, float)) and header_limit > 0:
            return int(header_limit)
        return self._MODEL_TPM_DEFAULTS.get(model, 2500)

    def _apply_rate_headers(self, model: str, response: httpx.Response) -> None:
        state = self._get_model_usage_state(model)
        remaining = self._parse_int_header(
            response.headers.get("x-ratelimit-remaining-tokens")
            or response.headers.get("x-ratelimit-remaining-token")
        )
        reset_seconds = self._parse_reset_seconds(
            response.headers.get("x-ratelimit-reset-tokens")
            or response.headers.get("x-ratelimit-reset-token")
        )
        limit_tokens = self._parse_int_header(
            response.headers.get("x-ratelimit-limit-tokens")
            or response.headers.get("x-ratelimit-limit-token")
        )

        if remaining is not None:
            state["remaining_tokens"] = max(remaining, 0)
        if reset_seconds is not None:
            state["reset_tokens_seconds"] = max(reset_seconds, 0.0)
        if limit_tokens is not None and limit_tokens > 0:
            state["limit_tokens"] = limit_tokens

    def _record_usage(self, model: str, tokens_used: int) -> None:
        if tokens_used <= 0:
            return
        state = self._get_model_usage_state(model)
        window_used = float(state.get("window_used_tokens") or 0.0)
        state["window_used_tokens"] = max(window_used + float(tokens_used), 0.0)
        remaining = state.get("remaining_tokens")
        if isinstance(remaining, (int, float)):
            state["remaining_tokens"] = max(int(remaining) - int(tokens_used), 0)

    def _compute_pre_request_wait_seconds(self, model: str, estimated_tokens: int) -> float:
        if not self.settings.llm_rate_guard_enabled:
            return 0.0

        state = self._get_model_usage_state(model)
        now_mono = time.monotonic()
        tpm_budget = max(self._resolve_model_tpm_budget(model, state=state), 1)
        safety_ratio = self.settings.llm_rate_guard_safety_ratio
        min_headroom = self.settings.llm_rate_guard_min_headroom_tokens

        safe_budget = max(int(tpm_budget * safety_ratio), 1)
        local_used = float(state.get("window_used_tokens") or 0.0)
        local_projected = local_used + float(max(estimated_tokens, 1))
        local_threshold = max(safe_budget - min_headroom, min_headroom)

        local_wait = 0.0
        if local_projected > local_threshold:
            elapsed = now_mono - float(state.get("window_started_at") or now_mono)
            local_wait = max(60.0 - elapsed, 0.0)

        header_wait = 0.0
        remaining = state.get("remaining_tokens")
        if isinstance(remaining, (int, float)):
            remaining_threshold = max(int(remaining) - max(estimated_tokens, 1), 0)
            if remaining_threshold < min_headroom:
                reset_seconds = state.get("reset_tokens_seconds")
                if isinstance(reset_seconds, (int, float)):
                    header_wait = max(float(reset_seconds), 0.0)
                elif local_wait <= 0.0:
                    header_wait = 0.35

        wait_seconds = max(local_wait, header_wait, 0.0)
        if wait_seconds <= 0.0:
            return 0.0
        return min(wait_seconds + random.uniform(0.05, 0.3), 20.0)

    def _record_call_metrics(self, task: str | None, metrics: dict[str, Any]) -> None:
        if task:
            self._LAST_CALL_METRICS[task] = metrics

    async def _post_completion(
        self,
        payload: dict,
        headers: dict[str, str],
        *,
        task: str | None = None,
    ) -> dict[str, Any]:
        attempts = max(self.settings.llm_retry_max_attempts, 1)
        backoff = max(self.settings.llm_retry_backoff_seconds, 0.0)
        last_response: httpx.Response | None = None
        model = str(payload.get("model") or "")
        estimated_tokens = self._estimate_payload_tokens(payload)
        total_wait_seconds = 0.0
        retry_count = 0

        for attempt in range(1, attempts + 1):
            pre_wait_seconds = self._compute_pre_request_wait_seconds(model, estimated_tokens)
            if pre_wait_seconds > 0:
                logger.info(
                    "%s proactive rate guard delaying %.2fs before %s request.",
                    self.provider_name,
                    pre_wait_seconds,
                    model,
                )
                await asyncio.sleep(pre_wait_seconds)
                total_wait_seconds += pre_wait_seconds

            async with httpx.AsyncClient(timeout=self.settings.xai_request_timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
            last_response = response
            self._apply_rate_headers(model, response)
            if response.status_code == 429 and attempt < attempts:
                retry_count += 1
                retry_after_header = response.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_header) if retry_after_header else None
                except (TypeError, ValueError):
                    retry_after = None
                sleep_seconds = retry_after if retry_after is not None else max(backoff * attempt, 0.2)
                sleep_seconds = min(max(sleep_seconds + random.uniform(0.05, 0.3), 0.2), 10.0)
                logger.warning(
                    "%s API rate limited (429). Retrying attempt %s/%s after %.2fs.",
                    self.provider_name,
                    attempt + 1,
                    attempts,
                    sleep_seconds,
                )
                total_wait_seconds += sleep_seconds
                self._record_usage(model, estimated_tokens)
                await asyncio.sleep(sleep_seconds)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = self._extract_error_detail(response)
                self._record_usage(model, estimated_tokens)
                self._record_call_metrics(
                    task,
                    {
                        "task": task,
                        "model": model,
                        "estimated_tokens": estimated_tokens,
                        "pre_request_wait_ms": round(total_wait_seconds * 1000.0, 2),
                        "retry_count": retry_count,
                        "final_status": "http_error",
                        "http_status": response.status_code,
                    },
                )
                raise RuntimeError(
                    f"{self.provider_name} API request failed ({response.status_code}): {detail}"
                ) from exc
            data = response.json()
            used_tokens = self._extract_usage_tokens(data) or estimated_tokens
            self._record_usage(model, used_tokens)
            self._record_call_metrics(
                task,
                {
                    "task": task,
                    "model": model,
                    "estimated_tokens": estimated_tokens,
                    "used_tokens": used_tokens,
                    "pre_request_wait_ms": round(total_wait_seconds * 1000.0, 2),
                    "retry_count": retry_count,
                    "final_status": "ok",
                    "http_status": response.status_code,
                },
            )
            return data

        if last_response is None:
            self._record_call_metrics(
                task,
                {
                    "task": task,
                    "model": model,
                    "estimated_tokens": estimated_tokens,
                    "pre_request_wait_ms": round(total_wait_seconds * 1000.0, 2),
                    "retry_count": retry_count,
                    "final_status": "error",
                    "http_status": None,
                },
            )
            raise RuntimeError(f"{self.provider_name} API request failed: no response received.")
        detail = self._extract_error_detail(last_response)
        self._record_usage(model, estimated_tokens)
        self._record_call_metrics(
            task,
            {
                "task": task,
                "model": model,
                "estimated_tokens": estimated_tokens,
                "pre_request_wait_ms": round(total_wait_seconds * 1000.0, 2),
                "retry_count": retry_count,
                "final_status": "http_error",
                "http_status": last_response.status_code,
            },
        )
        raise RuntimeError(
            f"{self.provider_name} API request failed ({last_response.status_code}): {detail}"
        )

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
        task: str | None = None,
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
            data = await self._post_completion(payload, headers, task=task)
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
            data = await self._post_completion(relaxed_payload, headers, task=task)
            return self._extract_response_text(data)
