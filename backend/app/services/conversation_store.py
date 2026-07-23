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


class ConversationStore:
    def __init__(self, path: str, *, max_entries: int = 100, max_messages: int = 80) -> None:
        self.path = Path(path)
        self.max_entries = max(max_entries, 1)
        self.max_messages = max(max_messages, 10)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"conversations": {}})

    def _read(self) -> dict[str, Any]:
        for attempt in range(1, 4):
            try:
                with self.path.open("r", encoding="utf-8-sig") as handle:
                    try:
                        payload = json.load(handle)
                    except json.JSONDecodeError:
                        return {"conversations": {}}
                if not isinstance(payload, dict):
                    return {"conversations": {}}
                payload.setdefault("conversations", {})
                return payload
            except OSError as exc:
                if attempt >= 3:
                    raise
                if getattr(exc, "errno", None) not in {5, 13, 22, 32}:
                    raise
                time.sleep(0.05 * attempt)
        return {"conversations": {}}

    @staticmethod
    def _timestamp(value: object) -> float:
        text = str(value or "").strip()
        if not text:
            return 0.0
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return 0.0

    def _apply_hygiene(self, payload: dict[str, Any]) -> dict[str, Any]:
        conversations = payload.get("conversations")
        if not isinstance(conversations, dict):
            payload["conversations"] = {}
            return payload

        for conversation in conversations.values():
            if not isinstance(conversation, dict):
                continue
            messages = conversation.get("messages")
            if isinstance(messages, list) and len(messages) > self.max_messages:
                conversation["messages"] = messages[-self.max_messages :]

        if len(conversations) > self.max_entries:
            ordered = sorted(
                conversations.items(),
                key=lambda item: self._timestamp(
                    (item[1] or {}).get("updated_at") or (item[1] or {}).get("created_at")
                ),
                reverse=True,
            )
            payload["conversations"] = dict(ordered[: self.max_entries])
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        payload = self._apply_hygiene(payload)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        for attempt in range(1, 4):
            try:
                with temp_path.open("w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2)
                os.replace(temp_path, self.path)
                return
            except OSError as exc:
                if attempt >= 3:
                    raise
                if getattr(exc, "errno", None) not in {5, 13, 22, 32}:
                    raise
                time.sleep(0.05 * attempt)
            finally:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _unique_title(
        conversations: dict[str, Any],
        requested_title: str,
        *,
        exclude_id: str | None = None,
    ) -> str:
        base = "Zendesk Configuration"
        cleaned = " ".join(str(requested_title or "").split()).strip(" .-_")
        if cleaned:
            base = cleaned[:80].rstrip()

        used = {
            str(item.get("title") or "").strip().casefold()
            for conversation_id, item in conversations.items()
            if conversation_id != exclude_id and isinstance(item, dict)
        }
        if base.casefold() not in used:
            return base
        suffix = 2
        while True:
            suffix_text = f" ({suffix})"
            candidate = f"{base[: 80 - len(suffix_text)].rstrip()}{suffix_text}"
            if candidate.casefold() not in used:
                return candidate
            suffix += 1

    def create(self, conversation: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            payload = self._read()
            conversations = payload.setdefault("conversations", {})
            conversation_id = str(conversation.get("conversation_id") or "").strip()
            if not conversation_id:
                raise ValueError("conversation_id is required.")
            if conversation_id in conversations:
                raise ValueError(f"Conversation {conversation_id} already exists.")
            stored = dict(conversation)
            stored["title"] = self._unique_title(conversations, str(stored.get("title") or ""))
            conversations[conversation_id] = stored
            self._write(payload)
            return dict(stored)

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        with _lock:
            payload = self._read()
            value = payload.get("conversations", {}).get(conversation_id)
            return dict(value) if isinstance(value, dict) else None

    def list(self) -> list[dict[str, Any]]:
        with _lock:
            payload = self._read()
            conversations = payload.get("conversations", {})
            if not isinstance(conversations, dict):
                return []
            return [dict(item) for item in conversations.values() if isinstance(item, dict)]

    def update(self, conversation_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            payload = self._read()
            conversations = payload.setdefault("conversations", {})
            current = conversations.get(conversation_id)
            if not isinstance(current, dict):
                raise KeyError(conversation_id)
            next_value = dict(current)
            next_value.update(updates)
            if "title" in updates:
                next_value["title"] = self._unique_title(
                    conversations,
                    str(updates.get("title") or ""),
                    exclude_id=conversation_id,
                )
            next_value["updated_at"] = _utc_now()
            conversations[conversation_id] = next_value
            self._write(payload)
            return dict(next_value)

    def append_message(self, conversation_id: str, message: dict[str, Any]) -> dict[str, Any]:
        with _lock:
            payload = self._read()
            conversations = payload.setdefault("conversations", {})
            current = conversations.get(conversation_id)
            if not isinstance(current, dict):
                raise KeyError(conversation_id)
            messages = list(current.get("messages", []) or [])
            messages.append(dict(message))
            current["messages"] = messages[-self.max_messages :]
            current["updated_at"] = _utc_now()
            conversations[conversation_id] = current
            self._write(payload)
            return dict(current)

    def attach_batch(self, conversation_id: str, batch_id: str) -> dict[str, Any]:
        with _lock:
            payload = self._read()
            conversations = payload.setdefault("conversations", {})
            current = conversations.get(conversation_id)
            if not isinstance(current, dict):
                raise KeyError(conversation_id)
            batch_ids = [
                str(item).strip()
                for item in list(current.get("batch_ids", []) or [])
                if str(item).strip()
            ]
            if batch_id not in batch_ids:
                batch_ids.append(batch_id)
            current["batch_ids"] = batch_ids
            current["active_batch_id"] = batch_id
            current["updated_at"] = _utc_now()
            conversations[conversation_id] = current
            self._write(payload)
            return dict(current)


_store: ConversationStore | None = None


def get_conversation_store() -> ConversationStore:
    global _store
    if _store is None:
        settings = get_settings()
        _store = ConversationStore(
            settings.conversation_store_file,
            max_entries=settings.conversation_store_max_entries,
            max_messages=settings.conversation_store_max_messages,
        )
    return _store


def reset_conversation_store() -> None:
    global _store
    _store = None
