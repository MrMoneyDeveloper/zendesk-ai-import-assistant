from __future__ import annotations

import threading
import time
from contextvars import ContextVar, Token
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_int(value: object) -> int:
    try:
        return max(int(value), 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _safe_float(value: object) -> float:
    try:
        return max(float(value), 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _parse_timestamp(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _first_status_time(history: list[dict[str, Any]], statuses: set[str]) -> float | None:
    for item in history:
        if str(item.get("status", "")).strip() not in statuses:
            continue
        parsed = _parse_timestamp(item.get("at"))
        if parsed is not None:
            return parsed
    return None


def _phase_timings(
    status_history: list[dict[str, Any]],
    *,
    started_at_epoch: float,
    finished_at_epoch: float,
) -> dict[str, float]:
    generation_start = _first_status_time(status_history, {"wave_execution", "generating"})
    generated_at = _first_status_time(status_history, {"generated"})
    staging_start = _first_status_time(status_history, {"staging"})
    preview_ready = _first_status_time(status_history, {"preview_ready"}) or finished_at_epoch

    generation_start = generation_start or started_at_epoch
    generated_at = generated_at or generation_start
    staging_start = staging_start or generated_at
    preview_ready = max(preview_ready, staging_start)

    return {
        "planning_and_backlog_ms": round(max(generation_start - started_at_epoch, 0.0) * 1000.0, 2),
        "generation_and_supervision_ms": round(max(generated_at - generation_start, 0.0) * 1000.0, 2),
        "post_processing_ms": round(max(staging_start - generated_at, 0.0) * 1000.0, 2),
        "staging_and_validation_ms": round(max(preview_ready - staging_start, 0.0) * 1000.0, 2),
    }


@dataclass
class UsageSession:
    label: str
    started_at: str = field(default_factory=_utc_now)
    started_at_epoch: float = field(default_factory=time.time)
    started_monotonic: float = field(default_factory=time.perf_counter)
    calls: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, payload: dict[str, Any]) -> None:
        event = {
            "recorded_at": _utc_now(),
            "provider": str(payload.get("provider") or "unknown").strip() or "unknown",
            "task": str(payload.get("task") or "unknown").strip() or "unknown",
            "model": str(payload.get("model") or "unknown").strip() or "unknown",
            "api_key_profile": str(payload.get("api_key_profile") or "").strip() or None,
            "final_status": str(payload.get("final_status") or "unknown").strip() or "unknown",
            "http_status": payload.get("http_status"),
            "attempt_count": max(_safe_int(payload.get("attempt_count")), 1),
            "retry_count": _safe_int(payload.get("retry_count")),
            "estimated_tokens": _safe_int(payload.get("estimated_tokens")),
            "input_tokens": _safe_int(payload.get("input_tokens")),
            "output_tokens": _safe_int(payload.get("output_tokens")),
            "thought_tokens": _safe_int(payload.get("thought_tokens")),
            "cached_tokens": _safe_int(payload.get("cached_tokens")),
            "tool_use_tokens": _safe_int(payload.get("tool_use_tokens")),
            "total_tokens": _safe_int(
                payload.get("total_tokens")
                if payload.get("total_tokens") is not None
                else payload.get("used_tokens")
            ),
            "elapsed_ms": round(_safe_float(payload.get("elapsed_ms")), 2),
            "pre_request_wait_ms": round(_safe_float(payload.get("pre_request_wait_ms")), 2),
            "error_class": str(payload.get("error_class") or "").strip() or None,
        }
        with self._lock:
            self.calls.append(event)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self.calls)


_ACTIVE_USAGE_SESSION: ContextVar[UsageSession | None] = ContextVar(
    "active_usage_session",
    default=None,
)


def start_usage_session(label: str) -> tuple[UsageSession, Token]:
    session = UsageSession(label=str(label or "generation").strip() or "generation")
    token = _ACTIVE_USAGE_SESSION.set(session)
    return session, token


def reset_usage_session(token: Token) -> None:
    _ACTIVE_USAGE_SESSION.reset(token)


def record_model_call(payload: dict[str, Any]) -> None:
    session = _ACTIVE_USAGE_SESSION.get()
    if session is not None:
        session.record(payload)


def _aggregate_calls(calls: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for call in calls:
        name = str(call.get(key) or "unknown")
        bucket = grouped.setdefault(
            name,
            {
                key: name,
                "calls": 0,
                "retries": 0,
                "estimated_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "thought_tokens": 0,
                "total_tokens": 0,
                "elapsed_ms_sum": 0.0,
            },
        )
        bucket["calls"] += 1
        bucket["retries"] += _safe_int(call.get("retry_count"))
        for token_key in (
            "estimated_tokens",
            "input_tokens",
            "output_tokens",
            "thought_tokens",
            "total_tokens",
        ):
            bucket[token_key] += _safe_int(call.get(token_key))
        bucket["elapsed_ms_sum"] += _safe_float(call.get("elapsed_ms"))

    rows = []
    for bucket in grouped.values():
        bucket["elapsed_ms_sum"] = round(bucket["elapsed_ms_sum"], 2)
        rows.append(bucket)
    return sorted(rows, key=lambda item: str(item.get(key, "")))


def build_usage_report(
    session: UsageSession,
    *,
    status_history: list[dict[str, Any]] | None = None,
    total_elapsed_ms: float | None = None,
) -> dict[str, Any]:
    calls = session.snapshot()
    finished_at_epoch = time.time()
    wall_time_ms = (
        _safe_float(total_elapsed_ms)
        if total_elapsed_ms is not None
        else max((time.perf_counter() - session.started_monotonic) * 1000.0, 0.0)
    )
    totals = {
        "calls": len(calls),
        "successful_calls": sum(
            1 for call in calls if str(call.get("final_status", "")).lower() in {"ok", "reviewed"}
        ),
        "failed_calls": sum(
            1 for call in calls if str(call.get("final_status", "")).lower() not in {"ok", "reviewed"}
        ),
        "retries": sum(_safe_int(call.get("retry_count")) for call in calls),
        "estimated_tokens": sum(_safe_int(call.get("estimated_tokens")) for call in calls),
        "input_tokens": sum(_safe_int(call.get("input_tokens")) for call in calls),
        "output_tokens": sum(_safe_int(call.get("output_tokens")) for call in calls),
        "thought_tokens": sum(_safe_int(call.get("thought_tokens")) for call in calls),
        "cached_tokens": sum(_safe_int(call.get("cached_tokens")) for call in calls),
        "tool_use_tokens": sum(_safe_int(call.get("tool_use_tokens")) for call in calls),
        "total_tokens": sum(_safe_int(call.get("total_tokens")) for call in calls),
        "model_call_elapsed_ms_sum": round(
            sum(_safe_float(call.get("elapsed_ms")) for call in calls),
            2,
        ),
        "pre_request_wait_ms_sum": round(
            sum(_safe_float(call.get("pre_request_wait_ms")) for call in calls),
            2,
        ),
        "pipeline_wall_time_ms": round(wall_time_ms, 2),
    }
    if wall_time_ms > 0:
        totals["model_elapsed_to_wall_ratio"] = round(
            totals["model_call_elapsed_ms_sum"] / wall_time_ms,
            3,
        )

    return {
        "schema_version": 1,
        "label": session.label,
        "started_at": session.started_at,
        "finished_at": _utc_now(),
        "token_source": "provider_response_when_available",
        "totals": totals,
        "phase_timings": _phase_timings(
            list(status_history or []),
            started_at_epoch=session.started_at_epoch,
            finished_at_epoch=finished_at_epoch,
        ),
        "by_provider": _aggregate_calls(calls, "provider"),
        "by_model": _aggregate_calls(calls, "model"),
        "by_task": _aggregate_calls(calls, "task"),
        "calls": calls[-200:],
    }
