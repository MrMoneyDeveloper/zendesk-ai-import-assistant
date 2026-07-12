import json
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.settings import get_settings

_lock = threading.RLock()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class BatchStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"batches": {}})

    def _read(self) -> dict[str, Any]:
        for attempt in range(1, 4):
            try:
                with self.path.open("r", encoding="utf-8-sig") as f:
                    try:
                        return json.load(f)
                    except json.JSONDecodeError:
                        return {"batches": {}}
            except OSError as exc:
                if attempt >= 3:
                    raise
                if getattr(exc, "errno", None) not in {22, 13, 5, 32}:
                    raise
                time.sleep(0.05 * attempt)
        return {"batches": {}}

    def _write(self, payload: dict[str, Any]) -> None:
        payload = self._apply_hygiene(payload)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        for attempt in range(1, 4):
            try:
                with temp_path.open("w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                os.replace(temp_path, self.path)
                return
            except OSError as exc:
                if attempt >= 3:
                    raise
                if getattr(exc, "errno", None) not in {22, 13, 5, 32}:
                    raise
                time.sleep(0.05 * attempt)
            finally:
                try:
                    if temp_path.exists():
                        temp_path.unlink(missing_ok=True)
                except Exception:
                    pass

    @staticmethod
    def _safe_numeric(value: object) -> float:
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(text).timestamp()
            except ValueError:
                pass
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    def _compact_runtime_metadata(self, metadata: dict[str, Any]) -> None:
        llm_runtime = metadata.get("llm_runtime")
        if not isinstance(llm_runtime, dict):
            return

        planner = llm_runtime.get("planner", {})
        generator = llm_runtime.get("generator", {})
        terminal_error_class = llm_runtime.get("terminal_error_class")
        generator_error = llm_runtime.get("generator_error", {})

        compact_generator_error: dict[str, Any] = {}
        if isinstance(generator_error, dict):
            for key in ("error_class", "provider_error_code", "validator_reason"):
                value = generator_error.get(key)
                if value not in (None, "", []):
                    compact_generator_error[key] = value

        compact = {
            "planner": planner if isinstance(planner, dict) else {},
            "generator": (
                {
                    "model": generator.get("model"),
                    "http_status": generator.get("http_status"),
                    "final_status": generator.get("final_status"),
                    "retry_count": generator.get("retry_count"),
                    "pre_request_wait_ms": generator.get("pre_request_wait_ms"),
                    "wait_reason": generator.get("wait_reason"),
                    "error_class": generator.get("error_class"),
                    "provider_error_code": generator.get("provider_error_code"),
                }
                if isinstance(generator, dict)
                else {}
            ),
        }
        if terminal_error_class not in (None, ""):
            compact["terminal_error_class"] = terminal_error_class
        if compact_generator_error:
            compact["generator_error"] = compact_generator_error
        metadata["llm_runtime"] = compact

    def _apply_hygiene(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return payload
        batches = payload.get("batches")
        if not isinstance(batches, dict):
            return payload

        settings = get_settings()
        items = list(batches.items())
        if settings.batch_store_max_entries > 0 and len(items) > settings.batch_store_max_entries:
            items.sort(
                key=lambda kv: self._safe_numeric((kv[1] or {}).get("updated_at") or (kv[1] or {}).get("created_at")),
                reverse=True,
            )
            kept = dict(items[: settings.batch_store_max_entries])
            payload["batches"] = kept
            batches = kept

        if settings.batch_store_trim_runtime_metadata and not settings.diagnostics_mode_enabled:
            for batch in batches.values():
                if not isinstance(batch, dict):
                    continue
                metadata = batch.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                self._compact_runtime_metadata(metadata)
        return payload

    def save_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            data = self._read()
            data.setdefault("batches", {})
            data["batches"][batch["batch_id"]] = batch
            self._write(data)
        return batch

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with _lock:
            data = self._read()
            return data.get("batches", {}).get(batch_id)

    def list_batches(self) -> list[dict[str, Any]]:
        with _lock:
            data = self._read()
            batches = data.get("batches", {})
            if not isinstance(batches, dict):
                return []
            return list(batches.values())

    def update_batch(self, batch_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            data = self._read()
            batches = data.setdefault("batches", {})
            batch = batches.get(batch_id)
            if not batch:
                raise KeyError(batch_id)
            batch.update(updates)
            batch["updated_at"] = _utc_now()
            batches[batch_id] = batch
            self._write(data)
        return batch

    def append_status(
        self,
        batch_id: str,
        status: str,
        message: str = "",
    ) -> dict[str, Any]:
        with _lock:
            data = self._read()
            batches = data.setdefault("batches", {})
            batch = batches.get(batch_id)
            if not batch:
                raise KeyError(batch_id)

            history = batch.setdefault("status_history", [])
            history.append({"status": status, "message": message, "at": _utc_now()})
            batch["status"] = status
            batch["updated_at"] = _utc_now()
            batches[batch_id] = batch
            self._write(data)
        return batch

    def append_status_event(
        self,
        batch_id: str,
        status: str,
        message: str = "",
    ) -> dict[str, Any]:
        with _lock:
            data = self._read()
            batches = data.setdefault("batches", {})
            batch = batches.get(batch_id)
            if not batch:
                raise KeyError(batch_id)

            history = batch.setdefault("status_history", [])
            history.append({"status": status, "message": message, "at": _utc_now()})
            batch["updated_at"] = _utc_now()
            batches[batch_id] = batch
            self._write(data)
        return batch


_store: BatchStore | None = None


def get_batch_store() -> BatchStore:
    global _store
    if _store is None:
        _store = BatchStore(get_settings().batch_store_file)
    return _store


def reset_batch_store() -> None:
    global _store
    _store = None
