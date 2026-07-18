from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import threading
import time
from copy import deepcopy
from typing import Any, ClassVar

import httpx

from app.core.settings import get_settings
from app.helpers.json_parser import extract_json_payload
from app.services.perf_capture import emit_perf_event
from app.services.usage_telemetry import record_model_call


GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1/interactions"
_TRANSIENT_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class GeminiRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_class: str,
        http_status: int | None = None,
        telemetry: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.http_status = http_status
        self.telemetry = telemetry or {}


def _extract_interaction_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    candidates: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                candidates.append(cleaned)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        step_type = str(value.get("type") or value.get("step_type") or "").lower()
        if "thought" in step_type:
            return
        for key in ("output_text", "text"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                candidates.append(raw.strip())
        for key in ("model_output", "output", "parts", "items", "message", "content"):
            if key in value:
                visit(value.get(key))

    visit(payload.get("steps"))
    visit(payload.get("output"))
    if candidates:
        return candidates[-1]
    return ""


def _extract_usage(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    def value(key: str) -> int:
        try:
            return max(int(usage.get(key) or 0), 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = value("total_input_tokens")
    output_tokens = value("total_output_tokens")
    thought_tokens = value("total_thought_tokens")
    cached_tokens = value("total_cached_tokens")
    tool_use_tokens = value("total_tool_use_tokens")
    total_tokens = value("total_tokens") or (
        input_tokens + output_tokens + thought_tokens + tool_use_tokens
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thought_tokens": thought_tokens,
        "cached_tokens": cached_tokens,
        "tool_use_tokens": tool_use_tokens,
        "total_tokens": total_tokens,
    }


def _retry_after_seconds(response: httpx.Response, *, fallback: float, cap: float) -> float:
    candidates: list[object] = [response.headers.get("retry-after")]
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    details = error.get("details", []) if isinstance(error, dict) else []
    for detail in details if isinstance(details, list) else []:
        if isinstance(detail, dict):
            candidates.append(detail.get("retryDelay"))
    for candidate in candidates:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*s?\s*", str(candidate or ""))
        if match:
            return min(max(float(match.group(1)), 0.1), cap)
    return min(max(float(fallback), 0.1), cap)


class GeminiInteractionClient:
    _LAST_CALL_METRICS: ClassVar[dict[str, dict[str, Any]]] = {}
    _CIRCUIT_STATE: ClassVar[dict[str, dict[str, Any]]] = {}
    _STATE_LOCK: ClassVar[threading.Lock] = threading.Lock()
    _NEXT_REQUEST_AT: ClassVar[float] = 0.0
    _SEMAPHORES: ClassVar[dict[int, asyncio.Semaphore]] = {}

    def __init__(self) -> None:
        self.settings = get_settings()

    @classmethod
    def get_last_call_metrics(cls, task: str) -> dict[str, Any]:
        return deepcopy(cls._LAST_CALL_METRICS.get(task, {}))

    @classmethod
    def reset_runtime_state(cls) -> None:
        with cls._STATE_LOCK:
            cls._LAST_CALL_METRICS.clear()
            cls._CIRCUIT_STATE.clear()
            cls._NEXT_REQUEST_AT = 0.0
            cls._SEMAPHORES.clear()

    def _semaphore(self) -> asyncio.Semaphore:
        loop_key = id(asyncio.get_running_loop())
        with self._STATE_LOCK:
            semaphore = self._SEMAPHORES.get(loop_key)
            if semaphore is None:
                semaphore = asyncio.Semaphore(
                    max(int(self.settings.gemini_default_max_concurrency), 1)
                )
                self._SEMAPHORES[loop_key] = semaphore
            return semaphore

    async def _wait_for_request_slot(self) -> float:
        interval = max(
            float(self.settings.gemini_default_min_request_interval_seconds),
            0.0,
        )
        if interval <= 0:
            return 0.0
        now = time.monotonic()
        with self._STATE_LOCK:
            wait_seconds = max(self._NEXT_REQUEST_AT - now, 0.0)
            reserved_at = max(self._NEXT_REQUEST_AT, now) + interval
            self.__class__._NEXT_REQUEST_AT = reserved_at
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        return wait_seconds

    def _circuit_open(self, task: str) -> bool:
        now = time.monotonic()
        with self._STATE_LOCK:
            state = self._CIRCUIT_STATE.get(task, {})
            return float(state.get("cooldown_until") or 0.0) > now

    def _record_failure(self, task: str) -> None:
        now = time.monotonic()
        threshold = max(int(self.settings.gemini_default_failure_threshold), 1)
        window_seconds = max(float(self.settings.gemini_default_cooldown_seconds), 15.0)
        with self._STATE_LOCK:
            state = self._CIRCUIT_STATE.setdefault(
                task,
                {"failures": [], "cooldown_until": 0.0},
            )
            failures = [
                float(item)
                for item in state.get("failures", [])
                if now - float(item) <= window_seconds
            ]
            failures.append(now)
            state["failures"] = failures
            if len(failures) >= threshold:
                state["cooldown_until"] = now + float(
                    self.settings.gemini_default_cooldown_seconds
                )

    def _record_success(self, task: str) -> None:
        with self._STATE_LOCK:
            self._CIRCUIT_STATE.pop(task, None)

    def _record_metrics(self, task: str, metrics: dict[str, Any]) -> None:
        self._LAST_CALL_METRICS[task] = deepcopy(metrics)
        payload = {"provider": "Gemini", **metrics}
        record_model_call(payload)
        emit_perf_event("llm_call", payload)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        task: str,
        response_schema: dict[str, Any] | None = None,
        require_json: bool = False,
        model: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        api_key = str(self.settings.gemini_api_key or "").strip()
        selected_model = str(model or self.settings.gemini_default_model or "").strip()
        if not api_key:
            raise GeminiRequestError(
                "GEMINI_API_KEY is missing from environment.",
                error_class="missing_api_key",
            )
        if not selected_model:
            raise GeminiRequestError(
                "GEMINI_DEFAULT_MODEL is not configured.",
                error_class="missing_model",
            )
        if self._circuit_open(task):
            metrics = {
                "task": task,
                "model": selected_model,
                "api_key_profile": "gemini_default",
                "final_status": "circuit_open",
                "http_status": None,
                "attempt_count": 0,
                "retry_count": 0,
                "estimated_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "thought_tokens": 0,
                "total_tokens": 0,
                "elapsed_ms": 0.0,
                "pre_request_wait_ms": 0.0,
                "error_class": "circuit_open",
                "breaker_state": "open",
            }
            self._LAST_CALL_METRICS[task] = deepcopy(metrics)
            raise GeminiRequestError(
                f"Gemini circuit is open for task '{task}'.",
                error_class="circuit_open",
                telemetry=metrics,
            )

        input_payload = {
            "instructions": (
                "Follow the role-ordered messages. Return only the requested public response; "
                "do not expose hidden chain-of-thought."
            ),
            "messages": messages,
        }
        request_body: dict[str, Any] = {
            "model": selected_model,
            "input": json.dumps(input_payload, ensure_ascii=False),
            "store": False,
            "generation_config": {
                "temperature": min(max(float(temperature if temperature is not None else 0.1), 0.0), 2.0),
                "thinking_level": "minimal" if task == "generator" else "low",
            },
        }
        if max_output_tokens is not None:
            request_body["generation_config"]["max_output_tokens"] = max(
                int(max_output_tokens),
                64,
            )
        if response_schema is not None:
            request_body["response_format"] = {
                "type": "text",
                "mime_type": "application/json",
                "schema": response_schema,
            }
        elif require_json:
            request_body["response_format"] = {
                "type": "text",
                "mime_type": "application/json",
            }

        started = time.perf_counter()
        estimated_tokens = max(len(str(request_body["input"])) // 4, 1)
        max_retries = max(int(self.settings.gemini_default_max_retries), 0)
        retry_delay_cap = max(
            float(self.settings.gemini_default_retry_max_delay_seconds),
            0.5,
        )
        retry_wait_seconds = 0.0
        status_codes: list[int] = []
        last_error_class = "unknown"
        last_status: int | None = None
        usage_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "thought_tokens": 0,
            "cached_tokens": 0,
            "tool_use_tokens": 0,
            "total_tokens": 0,
        }

        def metrics(final_status: str, attempt_count: int) -> dict[str, Any]:
            return {
                "task": task,
                "model": selected_model,
                "api_key_profile": (
                    "gemini_" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:10]
                ),
                "final_status": final_status,
                "http_status": last_status,
                "attempt_count": max(attempt_count, 1),
                "retry_count": max(attempt_count - 1, 0),
                "estimated_tokens": estimated_tokens,
                **usage_totals,
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "pre_request_wait_ms": round(retry_wait_seconds * 1000.0, 2),
                "error_class": last_error_class,
                "breaker_state": "open" if self._circuit_open(task) else "closed",
                "http_statuses": status_codes[-8:],
            }

        async with self._semaphore():
            for attempt in range(max_retries + 1):
                retry_wait_seconds += await self._wait_for_request_slot()
                try:
                    async with httpx.AsyncClient(
                        timeout=self.settings.gemini_default_timeout_seconds
                    ) as client:
                        response = await client.post(
                            GEMINI_INTERACTIONS_URL,
                            headers={
                                "x-goog-api-key": api_key,
                                "Content-Type": "application/json",
                            },
                            json=request_body,
                        )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error_class = "transport_error"
                    last_status = None
                    self._record_failure(task)
                    if attempt >= max_retries or self._circuit_open(task):
                        final_metrics = metrics("error", attempt + 1)
                        self._record_metrics(task, final_metrics)
                        raise GeminiRequestError(
                            f"Gemini transport failed after {attempt + 1} attempt(s): {type(exc).__name__}.",
                            error_class=last_error_class,
                            telemetry=final_metrics,
                        ) from exc
                    delay = min((2**attempt) + random.uniform(0.05, 0.3), retry_delay_cap)
                    retry_wait_seconds += delay
                    await asyncio.sleep(delay)
                    continue

                last_status = int(response.status_code)
                status_codes.append(last_status)
                if not response.is_success:
                    last_error_class = (
                        "rate_limited"
                        if response.status_code == 429
                        else "transient_http_error"
                        if response.status_code in _TRANSIENT_STATUS_CODES
                        else "http_error"
                    )
                    self._record_failure(task)
                    can_retry = (
                        response.status_code in _TRANSIENT_STATUS_CODES
                        and attempt < max_retries
                        and not self._circuit_open(task)
                    )
                    if can_retry:
                        delay = _retry_after_seconds(
                            response,
                            fallback=(2**attempt) + random.uniform(0.05, 0.3),
                            cap=retry_delay_cap,
                        )
                        retry_wait_seconds += delay
                        await asyncio.sleep(delay)
                        continue
                    final_metrics = metrics("error", attempt + 1)
                    self._record_metrics(task, final_metrics)
                    detail = response.text[:300].strip()
                    raise GeminiRequestError(
                        f"Gemini request failed ({response.status_code}) after {attempt + 1} attempt(s): {detail}",
                        error_class=last_error_class,
                        http_status=response.status_code,
                        telemetry=final_metrics,
                    )

                try:
                    payload = response.json()
                except ValueError as exc:
                    payload = {}
                    output_text = ""
                    last_error_class = "malformed_output"
                else:
                    usage = _extract_usage(payload)
                    for key in usage_totals:
                        usage_totals[key] += int(usage.get(key, 0) or 0)
                    output_text = _extract_interaction_text(payload)
                    if not output_text:
                        last_error_class = "malformed_output"
                    elif require_json or response_schema is not None:
                        try:
                            extract_json_payload(output_text)
                        except Exception:
                            last_error_class = "malformed_output"
                        else:
                            last_error_class = "none"
                    else:
                        last_error_class = "none"

                if last_error_class == "none":
                    self._record_success(task)
                    final_metrics = metrics("ok", attempt + 1)
                    self._record_metrics(task, final_metrics)
                    return output_text

                self._record_failure(task)
                # A second identical structured-output request rarely repairs a
                # syntactically malformed result. Hand control back to the
                # provider router so it can use the configured Groq fallback.
                if last_error_class == "malformed_output":
                    final_metrics = metrics("error", attempt + 1)
                    self._record_metrics(task, final_metrics)
                    raise GeminiRequestError(
                        f"Gemini returned malformed output after {attempt + 1} attempt(s).",
                        error_class="malformed_output",
                        http_status=response.status_code,
                        telemetry=final_metrics,
                    )
                if attempt >= max_retries or self._circuit_open(task):
                    final_metrics = metrics("error", attempt + 1)
                    self._record_metrics(task, final_metrics)
                    raise GeminiRequestError(
                        f"Gemini returned malformed output after {attempt + 1} attempt(s).",
                        error_class="malformed_output",
                        http_status=response.status_code,
                        telemetry=final_metrics,
                    )

        final_metrics = metrics("error", max_retries + 1)
        self._record_metrics(task, final_metrics)
        raise GeminiRequestError(
            "Gemini request failed without a terminal response.",
            error_class=last_error_class,
            http_status=last_status,
            telemetry=final_metrics,
        )
