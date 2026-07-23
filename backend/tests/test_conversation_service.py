import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api.grok.client import GrokClient
from app.core.settings import get_settings
from app.main import app
from app.routes import import_assistant as import_assistant_routes
from app.services.batch_store import get_batch_store, reset_batch_store
from app.services.conversation_service import (
    append_conversation_message,
    assign_ai_conversation_title,
    attach_batch_to_conversation,
    create_conversation,
    get_conversation,
    list_conversations,
)
from app.services.conversation_store import ConversationStore, reset_conversation_store


@pytest.fixture
def conversation_files(monkeypatch, tmp_path):
    monkeypatch.setenv("CONVERSATION_STORE_FILE", str(tmp_path / "conversations.json"))
    monkeypatch.setenv("BATCH_STORE_FILE", str(tmp_path / "batches.json"))
    get_settings.cache_clear()
    reset_conversation_store()
    reset_batch_store()
    yield tmp_path
    reset_conversation_store()
    reset_batch_store()
    get_settings.cache_clear()


def test_conversation_store_enforces_unique_titles_and_nested_batches(conversation_files):
    store = ConversationStore(str(conversation_files / "direct-conversations.json"))
    base = {
        "title": "Claims Routing Review",
        "title_source": "deterministic",
        "created_at": "2026-07-23T10:00:00+00:00",
        "updated_at": "2026-07-23T10:00:00+00:00",
        "requester": "local-user",
        "operation_mode": "create",
        "active_batch_id": None,
        "batch_ids": [],
        "state": {},
        "messages": [],
    }
    first = store.create({"conversation_id": "CHAT-1", **base})
    second = store.create({"conversation_id": "CHAT-2", **base})

    assert first["title"] == "Claims Routing Review"
    assert second["title"] == "Claims Routing Review (2)"

    attached = store.attach_batch("CHAT-1", "BATCH-1")
    assert attached["active_batch_id"] == "BATCH-1"
    assert attached["batch_ids"] == ["BATCH-1"]


def test_conversation_history_restores_messages_and_redacts_sensitive_metadata(conversation_files):
    conversation = create_conversation(
        first_message="Create a claims routing trigger for broker submissions.",
        operation_mode="create",
        state={
            "operation_mode": "create",
            "focus_object_types": ["triggers"],
            "api_token": "must-not-be-stored",
        },
    )
    conversation_id = conversation["conversation_id"]
    append_conversation_message(
        conversation_id,
        role="assistant",
        kind="context_answer",
        content="The trigger can route broker claims.",
        metadata={
            "question_mode": "instance",
            "api_token": "must-not-be-stored",
            "usage": {"total_tokens": 120},
        },
    )

    batch = {
        "batch_id": "BATCH-1",
        "status": "preview_ready",
        "prompt": "Create a claims routing trigger for broker submissions.",
        "requester": "local-user",
        "target_environment": "sandbox",
        "mode": "generate_validate_preview",
        "created_at": "2026-07-23T10:00:00+00:00",
        "updated_at": "2026-07-23T10:01:00+00:00",
        "status_history": [],
        "records": [],
        "generated_counts": {},
        "validation_summary": {"passed": 0, "warnings": 0, "blocked": 0},
        "planning_summary": {},
        "metadata": {},
        "conversation_id": conversation_id,
    }
    get_batch_store().save_batch(batch)
    attach_batch_to_conversation(conversation_id, "BATCH-1")

    detail = get_conversation(conversation_id)
    assert detail["active_batch_id"] == "BATCH-1"
    assert detail["batches"][0]["batch_id"] == "BATCH-1"
    assert detail["state"]["focus_object_types"] == ["triggers"]
    assert "api_token" not in detail["state"]
    assert "api_token" not in detail["messages"][-1]["metadata"]
    assert detail["messages"][-1]["metadata"]["usage"]["total_tokens"] == 120
    assert list_conversations()[0]["conversation_id"] == conversation_id


def test_ai_title_assignment_is_unique_and_uses_configured_model_route(
    monkeypatch,
    conversation_files,
):
    first = create_conversation(
        first_message="Create claims routing and broker escalation rules.",
        operation_mode="create",
    )
    second = create_conversation(
        first_message="Create a second claims routing and broker escalation workflow.",
        operation_mode="create",
    )

    calls = []

    async def fake_chat(self, messages, **kwargs):  # noqa: ANN001
        calls.append({"messages": messages, **kwargs})
        return '{"title":"Broker Claims Escalation"}'

    monkeypatch.setattr(GrokClient, "chat", fake_chat)
    monkeypatch.setattr(
        GrokClient,
        "get_last_call_metrics",
        lambda self, task: {"provider": "Gemini", "model": "gemini-test", "task": task},
    )

    asyncio.run(assign_ai_conversation_title(first["conversation_id"], "Create claims routing."))
    asyncio.run(assign_ai_conversation_title(second["conversation_id"], "Create claims routing."))

    first_detail = get_conversation(first["conversation_id"])
    second_detail = get_conversation(second["conversation_id"])
    assert first_detail["title"] == "Broker Claims Escalation"
    assert first_detail["title_source"] == "ai"
    assert second_detail["title"] == "Broker Claims Escalation (2)"
    assert calls[0]["task"] == "conversation_title"
    assert calls[0]["prefer_provider"] == get_settings().llm_default_provider


def test_conversation_routes_create_append_list_and_resume(
    monkeypatch,
    conversation_files,
):
    monkeypatch.setattr(
        import_assistant_routes,
        "_schedule_conversation_title",
        lambda conversation_id, first_message: None,
    )
    client = TestClient(app)
    created_response = client.post(
        "/api/import-assistant/conversations",
        json={
            "first_message": "Create a finance escalation workflow.",
            "operation_mode": "create",
            "requester": "local-user",
            "state": {
                "operation_mode": "create",
                "focus_object_types": ["triggers", "macros"],
            },
        },
    )
    assert created_response.status_code == 201
    created = created_response.json()
    conversation_id = created["conversation_id"]
    assert created["message_count"] == 1

    appended_response = client.post(
        f"/api/import-assistant/conversations/{conversation_id}/messages",
        json={
            "role": "assistant",
            "kind": "message",
            "content": "The finance workflow is ready to continue.",
            "metadata": {},
        },
    )
    assert appended_response.status_code == 200
    assert appended_response.json()["message_count"] == 2

    list_response = client.get("/api/import-assistant/conversations")
    assert list_response.status_code == 200
    assert list_response.json()["conversations"][0]["conversation_id"] == conversation_id

    detail_response = client.get(
        f"/api/import-assistant/conversations/{conversation_id}"
    )
    assert detail_response.status_code == 200
    assert detail_response.json()["messages"][-1]["role"] == "assistant"
