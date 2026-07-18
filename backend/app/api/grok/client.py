import asyncio
import hashlib
import json
import random
import time
from copy import deepcopy
from typing import Any, ClassVar

import httpx

from app.api.gemini.client import GeminiInteractionClient, GeminiRequestError
from app.api.grok.models import RECOMMENDED_MODEL, SUPPORTED_MODELS
from app.api.grok.schemas import PLANNER_JSON_SCHEMA
from app.core.settings import get_settings
from app.loggers.logger import get_logger
from app.services.perf_capture import emit_perf_event
from app.services.usage_telemetry import record_model_call

logger = get_logger(__name__)


class LLMRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_class: str,
        http_status: int | None = None,
        provider_detail: str | None = None,
        error_code: str | None = None,
        failed_generation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.http_status = http_status
        self.provider_detail = provider_detail or message
        self.error_code = (error_code or "").strip() or None
        self.failed_generation = failed_generation or None


class GrokClient:
    _MODEL_TPM_DEFAULTS: ClassVar[dict[str, int]] = {
        "qwen/qwen3-32b": 3000,
        "openai/gpt-oss-20b": 8000,
    }
    _MODEL_USAGE_STATE: ClassVar[dict[str, dict[str, float | int | None]]] = {}
    _LAST_CALL_METRICS: ClassVar[dict[str, dict[str, Any]]] = {}
    _CIRCUIT_STATE: ClassVar[dict[tuple[str, str, str], dict[str, Any]]] = {}
    _DETERMINISTIC_ERROR_CLASSES: ClassVar[set[str]] = {
        "unsupported_response_format",
        "schema_validation_failure",
        "model_permission_blocked",
        "other_invalid_request",
    }

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
    def _extract_error_payload(response: httpx.Response) -> tuple[str, str | None, str | None]:
        detail = response.text
        error_code: str | None = None
        failed_generation: str | None = None
        try:
            payload = response.json()
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    raw_message = str(error.get("message") or "").strip()
                    raw_code = str(error.get("code") or "").strip()
                    raw_failed = error.get("failed_generation")
                    if raw_code:
                        error_code = raw_code
                    if isinstance(raw_failed, str) and raw_failed.strip():
                        failed_generation = raw_failed.strip()
                    return (raw_message or raw_code or detail), error_code, failed_generation
                if error:
                    return str(error), error_code, failed_generation
                return str(payload.get("message") or detail), error_code, failed_generation
        except Exception:
            pass
        return detail, error_code, failed_generation

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
        if parsed > now_epoch + 60:
            return max(parsed - now_epoch, 0.0)
        return parsed

    @staticmethod
    def _extract_usage(data: dict[str, Any]) -> dict[str, int]:
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            }

        def positive_int(*keys: str) -> int:
            for key in keys:
                try:
                    parsed = int(usage.get(key))
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    return parsed
            return 0

        input_tokens = positive_int("prompt_tokens", "input_tokens", "total_input_tokens")
        output_tokens = positive_int("completion_tokens", "output_tokens", "total_output_tokens")
        total_tokens = positive_int("total_tokens") or (input_tokens + output_tokens)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }

    @classmethod
    def _extract_usage_tokens(cls, data: dict[str, Any]) -> int | None:
        total_tokens = cls._extract_usage(data)["total_tokens"]
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

    @staticmethod
    def _as_error_text(payload: dict[str, Any], detail: str) -> str:
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or detail or "")
        if error:
            return str(error)
        return str(payload.get("message") or detail or "")

    def _classify_http_error(
        self,
        response: httpx.Response,
    ) -> tuple[str, str, str | None, str | None]:
        status = int(response.status_code)
        detail, error_code, failed_generation = self._extract_error_payload(response)
        text = detail.lower()
        normalized_error_code = str(error_code or "").strip().lower()
        if status == 429:
            return "rate_limited", detail, error_code, failed_generation

        if status == 403 and "model" in text and (
            "permission" in text or "not allowed" in text or "not available" in text
        ):
            return "model_permission_blocked", detail, error_code, failed_generation

        if status == 400:
            if (
                normalized_error_code == "json_validate_failed"
                or "failed to generate json" in text
                or "failed_generation" in text
                or "json_validate_failed" in text
                or "generated json does not match the expected schema" in text
            ):
                return "schema_validation_failure", detail, error_code, failed_generation
            if ("response_format" in text or "json_schema" in text) and (
                "unsupported" in text or "not support" in text or "invalid" in text
            ):
                return "unsupported_response_format", detail, error_code, failed_generation
            if "schema" in text and (
                "validation" in text or "invalid" in text or "expected" in text
            ):
                return "schema_validation_failure", detail, error_code, failed_generation
            if "model" in text and (
                "permission" in text
                or "not allowed" in text
                or "not available" in text
                or "not found" in text
            ):
                return "model_permission_blocked", detail, error_code, failed_generation
            return "other_invalid_request", detail, error_code, failed_generation

        if 400 <= status < 500:
            return "other_invalid_request", detail, error_code, failed_generation
        return "http_error", detail, error_code, failed_generation

    def _model_supports_json_schema(self, model: str) -> bool:
        allowed = tuple(item.strip() for item in self.settings.llm_json_schema_supported_models if item.strip())
        if not allowed:
            return False
        for entry in allowed:
            if entry.endswith("*") and model.startswith(entry[:-1]):
                return True
            if model == entry:
                return True
        return False

    @staticmethod
    def _usage_state_key(model: str, api_key_profile: str | None) -> str:
        profile = str(api_key_profile or "default").strip() or "default"
        return f"{model}::{profile}"

    def _get_model_usage_state(
        self,
        model: str,
        api_key_profile: str | None = None,
    ) -> dict[str, float | int | None]:
        now_mono = time.monotonic()
        state_key = self._usage_state_key(model, api_key_profile)
        state = self._MODEL_USAGE_STATE.get(state_key)
        if not state:
            state = {
                "window_started_at": now_mono,
                "window_used_tokens": 0.0,
                "remaining_tokens": None,
                "reset_tokens_seconds": None,
                "limit_tokens": None,
            }
            self._MODEL_USAGE_STATE[state_key] = state
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
        api_key_profile: str | None = None,
        state: dict[str, float | int | None] | None = None,
    ) -> int:
        working_state = state or self._get_model_usage_state(model, api_key_profile)
        header_limit = working_state.get("limit_tokens")
        if isinstance(header_limit, (int, float)) and header_limit > 0:
            return int(header_limit)
        return self._MODEL_TPM_DEFAULTS.get(model, 2500)

    def _apply_rate_headers(
        self,
        model: str,
        response: httpx.Response,
        *,
        api_key_profile: str | None = None,
    ) -> None:
        state = self._get_model_usage_state(model, api_key_profile)
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

    def _record_usage(
        self,
        model: str,
        tokens_used: int,
        *,
        api_key_profile: str | None = None,
    ) -> None:
        if tokens_used <= 0:
            return
        state = self._get_model_usage_state(model, api_key_profile)
        window_used = float(state.get("window_used_tokens") or 0.0)
        state["window_used_tokens"] = max(window_used + float(tokens_used), 0.0)
        remaining = state.get("remaining_tokens")
        if isinstance(remaining, (int, float)):
            state["remaining_tokens"] = max(int(remaining) - int(tokens_used), 0)

    def _wait_cap_for_task(self, task: str | None) -> float:
        if task in {"planner", "clarifier"}:
            return self.settings.llm_prewait_max_seconds_planner
        return self.settings.llm_prewait_max_seconds_generator

    def _compute_pre_request_wait_seconds(
        self,
        model: str,
        estimated_tokens: int,
        *,
        task: str | None,
        api_key_profile: str | None = None,
    ) -> tuple[float, str | None]:
        if not self.settings.llm_rate_guard_enabled:
            return 0.0, None

        state = self._get_model_usage_state(model, api_key_profile)
        now_mono = time.monotonic()
        tpm_budget = max(
            self._resolve_model_tpm_budget(
                model,
                api_key_profile=api_key_profile,
                state=state,
            ),
            1,
        )
        safety_ratio = self.settings.llm_rate_guard_safety_ratio
        min_headroom = self.settings.llm_rate_guard_min_headroom_tokens

        safe_budget = max(int(tpm_budget * safety_ratio), 1)
        local_used = float(state.get("window_used_tokens") or 0.0)
        local_projected = local_used + float(max(estimated_tokens, 1))
        local_threshold = max(safe_budget - min_headroom, min_headroom)

        local_wait = 0.0
        wait_reason: str | None = None
        if local_projected > local_threshold:
            elapsed = now_mono - float(state.get("window_started_at") or now_mono)
            local_wait = max(60.0 - elapsed, 0.0)
            wait_reason = "local_budget"

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
                wait_reason = "header_budget"

        wait_seconds = max(local_wait, header_wait, 0.0)
        if wait_seconds <= 0.0:
            return 0.0, None
        max_cap = max(self._wait_cap_for_task(task), 0.0)
        jittered = wait_seconds + random.uniform(0.05, 0.3)
        if max_cap <= 0:
            return 0.0, None
        return min(jittered, max_cap), wait_reason

    def _breaker_key(self, task: str | None, model: str, error_class: str) -> tuple[str, str, str]:
        return (str(task or "default"), model, error_class)

    def _breaker_entry(self, key: tuple[str, str, str]) -> dict[str, Any]:
        entry = self._CIRCUIT_STATE.get(key)
        if entry is None:
            entry = {"failures": [], "cooldown_until": 0.0}
            self._CIRCUIT_STATE[key] = entry
        return entry

    def _record_breaker_failure(self, task: str | None, model: str, error_class: str) -> None:
        if not self.settings.llm_circuit_breaker_enabled:
            return
        key = self._breaker_key(task, model, error_class)
        entry = self._breaker_entry(key)
        now = time.monotonic()
        failures = [ts for ts in entry.get("failures", []) if now - float(ts) <= self.settings.llm_circuit_breaker_window_seconds]
        failures.append(now)
        entry["failures"] = failures
        threshold = max(self.settings.llm_circuit_breaker_failures, 1)
        if len(failures) >= threshold:
            entry["cooldown_until"] = now + float(self.settings.llm_circuit_breaker_cooldown_seconds)

    def _is_breaker_open(self, task: str | None, model: str, error_class: str) -> bool:
        if not self.settings.llm_circuit_breaker_enabled:
            return False
        key = self._breaker_key(task, model, error_class)
        entry = self._breaker_entry(key)
        now = time.monotonic()
        cooldown_until = float(entry.get("cooldown_until") or 0.0)
        return cooldown_until > now

    def _record_breaker_success(self, task: str | None, model: str) -> None:
        if not self.settings.llm_circuit_breaker_enabled:
            return
        task_name = str(task or "default")
        keys_to_delete = [
            key
            for key in self._CIRCUIT_STATE.keys()
            if key[0] == task_name and key[1] == model
        ]
        for key in keys_to_delete:
            self._CIRCUIT_STATE.pop(key, None)

    def _record_call_metrics(self, task: str | None, metrics: dict[str, Any]) -> None:
        if task:
            self._LAST_CALL_METRICS[task] = metrics
        record_model_call({"provider": self.provider_name, **metrics})
        emit_perf_event(
            "llm_call",
            {
                "provider": self.provider_name,
                "task": task,
                "model": metrics.get("model"),
                "http_status": metrics.get("http_status"),
                "final_status": metrics.get("final_status"),
                "estimated_tokens": metrics.get("estimated_tokens"),
                "used_tokens": metrics.get("used_tokens"),
                "input_tokens": metrics.get("input_tokens"),
                "output_tokens": metrics.get("output_tokens"),
                "total_tokens": metrics.get("total_tokens"),
                "elapsed_ms": metrics.get("elapsed_ms"),
                "attempt_count": metrics.get("attempt_count"),
                "retry_count": metrics.get("retry_count"),
                "pre_request_wait_ms": metrics.get("pre_request_wait_ms"),
                "wait_reason": metrics.get("wait_reason"),
                "error_class": metrics.get("error_class"),
                "breaker_state": metrics.get("breaker_state"),
                "api_key_profile": metrics.get("api_key_profile"),
                "provider_error_code": metrics.get("provider_error_code"),
                "provider_failed_generation_excerpt": metrics.get("provider_failed_generation_excerpt"),
            },
        )

    async def _post_completion(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        task: str | None = None,
        initial_breaker_state: str = "closed",
        api_key_profile: str | None = None,
        initial_retry_count: int = 0,
    ) -> dict[str, Any]:
        attempts = max(self.settings.llm_retry_max_attempts, 1)
        backoff = max(self.settings.llm_retry_backoff_seconds, 0.0)
        last_response: httpx.Response | None = None
        model = str(payload.get("model") or "")
        estimated_tokens = self._estimate_payload_tokens(payload)
        total_wait_seconds = 0.0
        retry_count = max(int(initial_retry_count), 0)
        wait_reason: str | None = None
        breaker_state = initial_breaker_state
        call_started = time.perf_counter()

        for attempt in range(1, attempts + 1):
            pre_wait_seconds, pre_wait_reason = self._compute_pre_request_wait_seconds(
                model,
                estimated_tokens,
                task=task,
                api_key_profile=api_key_profile,
            )
            if pre_wait_reason:
                wait_reason = pre_wait_reason
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
            self._apply_rate_headers(
                model,
                response,
                api_key_profile=api_key_profile,
            )

            error_class, detail, error_code, failed_generation = self._classify_http_error(response)
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
                wait_reason = "retry_after" if retry_after is not None else "backoff_retry"
                self._record_usage(
                    model,
                    estimated_tokens,
                    api_key_profile=api_key_profile,
                )
                await asyncio.sleep(sleep_seconds)
                continue

            if not response.is_success:
                self._record_breaker_failure(task, model, error_class)
                if self._is_breaker_open(task, model, error_class):
                    breaker_state = f"open:{error_class}"
                else:
                    breaker_state = f"closed:{error_class}"
                self._record_call_metrics(
                    task,
                    {
                        "task": task,
                        "model": model,
                        "estimated_tokens": estimated_tokens,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "elapsed_ms": round((time.perf_counter() - call_started) * 1000.0, 2),
                        "attempt_count": attempt,
                        "pre_request_wait_ms": round(total_wait_seconds * 1000.0, 2),
                        "retry_count": retry_count,
                        "final_status": "http_error",
                        "http_status": response.status_code,
                        "wait_reason": wait_reason,
                        "error_class": error_class,
                        "breaker_state": breaker_state,
                        "api_key_profile": api_key_profile,
                        "provider_error_code": error_code,
                        "provider_failed_generation_excerpt": (
                            failed_generation[:500] if isinstance(failed_generation, str) else None
                        ),
                    },
                )
                raise LLMRequestError(
                    f"{self.provider_name} API request failed ({response.status_code}) [{error_class}]: {detail}",
                    error_class=error_class,
                    http_status=response.status_code,
                    provider_detail=detail,
                    error_code=error_code,
                    failed_generation=failed_generation,
                )

            data = response.json()
            usage = self._extract_usage(data)
            used_tokens = usage["total_tokens"] or estimated_tokens
            self._record_usage(
                model,
                used_tokens,
                api_key_profile=api_key_profile,
            )
            self._record_breaker_success(task, model)
            self._record_call_metrics(
                task,
                {
                    "task": task,
                    "model": model,
                    "estimated_tokens": estimated_tokens,
                    "used_tokens": used_tokens,
                    "input_tokens": usage["input_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "total_tokens": used_tokens,
                    "elapsed_ms": round((time.perf_counter() - call_started) * 1000.0, 2),
                    "attempt_count": attempt,
                    "pre_request_wait_ms": round(total_wait_seconds * 1000.0, 2),
                    "retry_count": retry_count,
                    "final_status": "ok",
                    "http_status": response.status_code,
                    "wait_reason": wait_reason,
                    "error_class": "none",
                    "breaker_state": breaker_state,
                    "api_key_profile": api_key_profile,
                    "provider_error_code": None,
                    "provider_failed_generation_excerpt": None,
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
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "elapsed_ms": round((time.perf_counter() - call_started) * 1000.0, 2),
                    "attempt_count": attempts,
                    "pre_request_wait_ms": round(total_wait_seconds * 1000.0, 2),
                    "retry_count": retry_count,
                    "final_status": "error",
                    "http_status": None,
                    "wait_reason": wait_reason,
                    "error_class": "transport_error",
                    "breaker_state": breaker_state,
                    "api_key_profile": api_key_profile,
                    "provider_error_code": None,
                    "provider_failed_generation_excerpt": None,
                },
            )
            raise LLMRequestError(
                f"{self.provider_name} API request failed: no response received.",
                error_class="transport_error",
                http_status=None,
            )
        error_class, detail, error_code, failed_generation = self._classify_http_error(last_response)
        self._record_call_metrics(
            task,
            {
                "task": task,
                "model": model,
                "estimated_tokens": estimated_tokens,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "elapsed_ms": round((time.perf_counter() - call_started) * 1000.0, 2),
                "attempt_count": attempts,
                "pre_request_wait_ms": round(total_wait_seconds * 1000.0, 2),
                "retry_count": retry_count,
                "final_status": "http_error",
                "http_status": last_response.status_code,
                "wait_reason": wait_reason,
                "error_class": error_class,
                "breaker_state": breaker_state,
                "api_key_profile": api_key_profile,
                "provider_error_code": error_code,
                "provider_failed_generation_excerpt": (
                    failed_generation[:500] if isinstance(failed_generation, str) else None
                ),
            },
        )
        raise LLMRequestError(
            f"{self.provider_name} API request failed ({last_response.status_code}) [{error_class}]: {detail}",
            error_class=error_class,
            http_status=last_response.status_code,
            provider_detail=detail,
            error_code=error_code,
            failed_generation=failed_generation,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        *,
        model: str | None = None,
        max_output_tokens: int | None = None,
        response_schema: dict | None = None,
        response_schema_name: str = "structured_response",
        strict_schema: bool | None = None,
        task: str | None = None,
        response_format_override: str | None = None,
        api_key_override: str | None = None,
        reasoning_effort: str | None = None,
        reasoning_format: str | None = None,
        prefer_provider: str | None = None,
    ) -> str:
        selected_preference = str(
            prefer_provider or getattr(self.settings, "llm_default_provider", "groq") or "groq"
        ).strip().lower()
        gemini_tasks = {"planner", "generator", "clarifier", "healthcheck"}
        gemini_failure: GeminiRequestError | None = None
        task_name = str(task or "default")

        if selected_preference == "gemini" and task_name in gemini_tasks:
            gemini_schema = response_schema
            if task_name == "planner" and gemini_schema is None:
                gemini_schema = PLANNER_JSON_SCHEMA
            require_json = bool(
                gemini_schema is not None
                or str(response_format_override or "").strip().lower() == "json_object"
            )
            try:
                content = await GeminiInteractionClient().chat(
                    messages,
                    task=task_name,
                    response_schema=gemini_schema,
                    require_json=require_json,
                    model=str(getattr(self.settings, "gemini_default_model", "") or "").strip()
                    or None,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                )
                gemini_metrics = GeminiInteractionClient.get_last_call_metrics(task_name)
                gemini_metrics.update(
                    {
                        "provider": "Gemini",
                        "selected_provider": "gemini",
                        "fallback_used": False,
                    }
                )
                if task:
                    self._LAST_CALL_METRICS[task] = gemini_metrics
                return content
            except GeminiRequestError as exc:
                gemini_failure = exc
                logger.warning(
                    "Gemini default route failed for task '%s' (%s); switching to %s fallback.",
                    task_name,
                    exc.error_class,
                    self.provider_name,
                )

        try:
            result = await self._chat_openai_compatible(
                messages,
                temperature,
                model=model,
                max_output_tokens=max_output_tokens,
                response_schema=response_schema,
                response_schema_name=response_schema_name,
                strict_schema=strict_schema,
                task=task,
                response_format_override=response_format_override,
                api_key_override=api_key_override,
                reasoning_effort=reasoning_effort,
                reasoning_format=reasoning_format,
            )
        except Exception:
            if task and gemini_failure is not None:
                latest = deepcopy(self._LAST_CALL_METRICS.get(task, {}))
                gemini_metrics = (
                    gemini_failure.telemetry
                    or GeminiInteractionClient.get_last_call_metrics(task_name)
                )
                latest.update(
                    {
                        "provider": self.provider_name,
                        "selected_provider": self.provider_name.lower(),
                        "fallback_used": True,
                        "fallback_from": "Gemini",
                        "fallback_reason": gemini_failure.error_class,
                        "gemini_attempt_count": int(
                            gemini_metrics.get("attempt_count", 0) or 0
                        ),
                        "gemini_http_status": gemini_failure.http_status,
                        "provider_chain": ["Gemini", self.provider_name],
                    }
                )
                self._LAST_CALL_METRICS[task] = latest
            raise
        if task:
            latest = deepcopy(self._LAST_CALL_METRICS.get(task, {}))
            latest.update(
                {
                    "provider": self.provider_name,
                    "selected_provider": self.provider_name.lower(),
                    "fallback_used": gemini_failure is not None,
                }
            )
            if gemini_failure is not None:
                gemini_metrics = (
                    gemini_failure.telemetry
                    or GeminiInteractionClient.get_last_call_metrics(task_name)
                )
                latest.update(
                    {
                        "fallback_from": "Gemini",
                        "fallback_reason": gemini_failure.error_class,
                        "gemini_attempt_count": int(
                            gemini_metrics.get("attempt_count", 0) or 0
                        ),
                        "gemini_http_status": gemini_failure.http_status,
                        "provider_chain": ["Gemini", self.provider_name],
                    }
                )
            self._LAST_CALL_METRICS[task] = latest
        return result

    async def _chat_openai_compatible(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        *,
        model: str | None = None,
        max_output_tokens: int | None = None,
        response_schema: dict | None = None,
        response_schema_name: str = "structured_response",
        strict_schema: bool | None = None,
        task: str | None = None,
        response_format_override: str | None = None,
        api_key_override: str | None = None,
        reasoning_effort: str | None = None,
        reasoning_format: str | None = None,
    ) -> str:
        if not self.settings.xai_enabled:
            raise RuntimeError("xAI integration is disabled via XAI_ENABLED.")
        active_api_key = str(api_key_override or self.settings.xai_api_key or "").strip()
        if not active_api_key:
            raise RuntimeError("XAI_API_KEY is missing from environment.")
        api_key_profile = f"key_{hashlib.sha256(active_api_key.encode('utf-8')).hexdigest()[:10]}"

        selected_model = model or self.settings.xai_model or RECOMMENDED_MODEL
        breaker_state = "closed"

        if self._is_breaker_open(task, selected_model, "model_permission_blocked"):
            if task in {"planner", "clarifier"} and selected_model != self.settings.llm_model_generator:
                logger.warning(
                    "Circuit breaker open for %s/%s model_permission_blocked. Routing to fallback model '%s'.",
                    task,
                    selected_model,
                    self.settings.llm_model_generator,
                )
                selected_model = self.settings.llm_model_generator
                breaker_state = "open:model_permission_blocked"

        if selected_model not in SUPPORTED_MODELS:
            logger.warning(
                "Model '%s' is not in SUPPORTED_MODELS metadata, continuing anyway.",
                selected_model,
            )

        payload: dict[str, Any] = {
            "model": selected_model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.settings.xai_temperature,
            "max_tokens": max_output_tokens or self.settings.xai_max_output_tokens,
        }
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        if reasoning_format:
            payload["reasoning_format"] = reasoning_format
        strict = self.settings.llm_strict_schema_mode if strict_schema is None else strict_schema
        response_format_mode = "none"
        override = (response_format_override or "").strip().lower()
        if override == "json_object":
            payload["response_format"] = {"type": "json_object"}
            response_format_mode = "json_object_forced"
        elif override == "none":
            response_format_mode = "none_forced"
        elif response_schema:
            if self._model_supports_json_schema(selected_model):
                payload["response_format"] = self._build_response_format(
                    response_schema_name,
                    response_schema,
                    strict=strict,
                )
                response_format_mode = "json_schema"
            else:
                payload["response_format"] = {"type": "json_object"}
                response_format_mode = "json_object"
                breaker_state = "open:unsupported_response_format"
                if self.settings.llm_circuit_breaker_enabled:
                    self._record_breaker_failure(task, selected_model, "unsupported_response_format")
                logger.info(
                    "Model '%s' does not use json_schema in current config. Using json_object for task '%s'.",
                    selected_model,
                    task or "default",
                )

        headers = {
            "Authorization": f"Bearer {active_api_key}",
            "Content-Type": "application/json",
        }

        try:
            data = await self._post_completion(
                payload,
                headers,
                task=task,
                initial_breaker_state=breaker_state,
                api_key_profile=api_key_profile,
            )
            if task:
                latest = deepcopy(self._LAST_CALL_METRICS.get(task, {}))
                latest["response_format_mode"] = response_format_mode
                latest["model"] = selected_model
                latest["api_key_profile"] = api_key_profile
                self._LAST_CALL_METRICS[task] = latest
            return self._extract_response_text(data)
        except LLMRequestError as exc:
            if not (
                response_schema
                and self.settings.llm_fallback_to_json_object
                and exc.error_class in {"unsupported_response_format", "schema_validation_failure"}
                and response_format_mode == "json_schema"
            ):
                raise

            logger.warning(
                "Structured response failed for model '%s' task '%s' (%s). Retrying with json_object.",
                selected_model,
                task or "default",
                exc.error_class,
            )
            relaxed_payload = dict(payload)
            relaxed_payload["response_format"] = {"type": "json_object"}
            data = await self._post_completion(
                relaxed_payload,
                headers,
                task=task,
                initial_breaker_state=f"open:{exc.error_class}",
                api_key_profile=api_key_profile,
                initial_retry_count=1,
            )
            if task:
                latest = deepcopy(self._LAST_CALL_METRICS.get(task, {}))
                latest["response_format_mode"] = "json_object_fallback"
                latest["model"] = selected_model
                latest["error_class"] = exc.error_class
                latest["api_key_profile"] = api_key_profile
                self._LAST_CALL_METRICS[task] = latest
            return self._extract_response_text(data)
