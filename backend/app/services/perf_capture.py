import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.settings import get_settings

_WRITE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "initialized": False,
    "enabled": False,
    "events_file": None,
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _initialize_state() -> None:
    if _STATE["initialized"]:
        return
    settings = get_settings()
    enabled = bool(settings.perf_capture_enabled and settings.perf_capture_dir)
    _STATE["enabled"] = enabled
    if not enabled:
        _STATE["events_file"] = None
        _STATE["initialized"] = True
        return

    target_dir = Path(settings.perf_capture_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    events_file = target_dir / "events.jsonl"
    _STATE["events_file"] = str(events_file)
    _STATE["initialized"] = True


def reset_perf_capture_state_for_tests() -> None:
    _STATE["initialized"] = False
    _STATE["enabled"] = False
    _STATE["events_file"] = None


def perf_capture_enabled() -> bool:
    _initialize_state()
    return bool(_STATE["enabled"])


def emit_perf_event(event_type: str, payload: dict[str, Any] | None = None) -> None:
    _initialize_state()
    if not _STATE["enabled"]:
        return

    payload = payload or {}
    events_file = _STATE["events_file"]
    if not events_file:
        return

    event = {
        "at": _utc_now(),
        "event_type": str(event_type or "").strip() or "event",
        "request_id": "-",
        "pid": _safe_int(os.getpid()),
        "payload": payload,
    }
    try:
        from app.middlewares.request_context import get_request_id

        event["request_id"] = get_request_id()
    except Exception:
        event["request_id"] = "-"
    line = json.dumps(event, ensure_ascii=True)
    with _WRITE_LOCK:
        with Path(events_file).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
