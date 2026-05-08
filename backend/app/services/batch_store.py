import json
import threading
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
        with self.path.open("r", encoding="utf-8-sig") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {"batches": {}}

    def _write(self, payload: dict[str, Any]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

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


_store: BatchStore | None = None


def get_batch_store() -> BatchStore:
    global _store
    if _store is None:
        _store = BatchStore(get_settings().batch_store_file)
    return _store


def reset_batch_store() -> None:
    global _store
    _store = None
