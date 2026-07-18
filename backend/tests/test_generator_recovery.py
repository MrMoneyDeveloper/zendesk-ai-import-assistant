import asyncio

from app.api.grok.client import GrokClient, LLMRequestError
from app.core.settings import get_settings
from app.models.schemas import ImportAssistantGenerateRequest
from app.services.batch_store import get_batch_store, reset_batch_store
from app.services.generator import GeneratorStructuredOutputError, run_generator


class _StubSheetsService:
    @property
    def enabled(self):
        return False

    def stage_batch(self, batch, planning, preview_records):
        return {"sheet_enabled": False, "message": "stubbed"}

    def write_validation_log(self, batch_id, records):
        return 0

    def write_approval_log(self, batch_id, decisions):
        return 0


class _StubAppScriptBridgeService:
    @property
    def enabled(self):
        return False

    async def invoke(self, action, payload=None, method="POST", timeout_seconds=None):
        return {
            "action": action,
            "status": "skipped",
            "detail": "stubbed",
            "http_status": None,
            "data": {},
        }


def test_generator_retry_ladder_reaches_json_object_and_succeeds(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("XAI_API_KEY", "gsk_test_key")
    monkeypatch.setenv("LLM_MODEL_GENERATOR", "openai/gpt-oss-20b")
    monkeypatch.setenv("LLM_JSON_SCHEMA_SUPPORTED_MODELS", "openai/gpt-oss-20b")
    get_settings.cache_clear()
    GrokClient._LAST_CALL_METRICS.clear()

    calls: list[dict] = []

    async def fake_chat(self, messages, temperature=None, **kwargs):  # noqa: ANN001
        calls.append(dict(kwargs))
        call_no = len(calls)
        if call_no <= 2:
            GrokClient._LAST_CALL_METRICS["generator"] = {
                "task": "generator",
                "model": kwargs.get("model"),
                "response_format_mode": "json_schema",
                "http_status": 400,
                "error_class": "schema_validation_failure",
                "provider_error_code": "json_validate_failed",
                "provider_failed_generation_excerpt": '{"records":[{"object_type":"triggers"}]}',
            }
            raise LLMRequestError(
                "Groq API request failed (400) [schema_validation_failure]: Failed to validate JSON.",
                error_class="schema_validation_failure",
                http_status=400,
                error_code="json_validate_failed",
                failed_generation='{"records":[{"object_type":"triggers"}]}',
            )
        return (
            '{"records":[{"object_type":"triggers","title":"Escalate Finance",'
            '"conditions":[{"field":"status","operator":"is","value":"open"}],'
            '"actions":[{"field":"set_tags","value":"escalated"}],'
            '"dependency_notes":[]}],"generation_notes":[]}'
        )

    monkeypatch.setattr(GrokClient, "chat", fake_chat)

    rows = asyncio.run(
        run_generator(
            {"object_type": "triggers", "intent": "Create escalation trigger"},
            allow_fallback=False,
        )
    )

    assert len(rows) == 1
    assert rows[0]["title"] == "Escalate Finance"
    assert len(calls) == 3
    assert calls[0].get("strict_schema") is True
    assert calls[1].get("strict_schema") is False
    assert calls[2].get("response_format_override") == "json_object"


def test_generator_routes_next_format_attempt_to_groq_after_invalid_gemini_payload(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("XAI_API_KEY", "gsk_test_key")
    monkeypatch.setenv("LLM_MODEL_GENERATOR", "openai/gpt-oss-20b")
    get_settings.cache_clear()
    GrokClient._LAST_CALL_METRICS.clear()
    calls: list[dict] = []

    async def fake_chat(self, messages, temperature=None, **kwargs):  # noqa: ANN001
        calls.append(dict(kwargs))
        if len(calls) == 1:
            GrokClient._LAST_CALL_METRICS["generator"] = {
                "task": "generator",
                "selected_provider": "gemini",
                "error_class": "none",
            }
            return '{"records":[{"object_type":"triggers","title":123}],"generation_notes":[]}'
        return (
            '{"records":[{"object_type":"triggers","title":"Route Claims",'
            '"conditions":[{"field":"status","operator":"is","value":"new"}],'
            '"actions":[{"field":"group_id","value":"Claims"}],'
            '"dependency_notes":[]}],"generation_notes":[]}'
        )

    monkeypatch.setattr(GrokClient, "chat", fake_chat)
    rows = asyncio.run(
        run_generator(
            {"object_type": "triggers", "intent": "Route claims"},
            allow_fallback=False,
        )
    )

    assert rows[0]["title"] == "Route Claims"
    assert calls[0].get("prefer_provider") is None
    assert calls[1].get("prefer_provider") == "groq"


def test_generate_fails_with_failed_generation_metadata(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {
            "object_type": "triggers",
            "intent": prompt,
            "confidence": 0.9,
            "ambiguity_score": 0.1,
            "ambiguity_reasons": [],
            "clarification_questions": [],
            "dependency_notes": "",
        }

    async def broken_generator(plan: dict, **kwargs):
        raise GeneratorStructuredOutputError(
            "Generator JSON validation failed after retry ladder.",
            attempts=[{"mode": "json_schema_strict", "http_status": 400}],
            provider_error_code="json_validate_failed",
            failed_generation_excerpt='{"records":[{"object_type":"triggers","title":123}]}',
            validator_reason="title must be a string",
            corrective_question="Should I return one minimal trigger record only?",
            corrective_example="Yes, one trigger record only with strict JSON keys.",
            error_class="schema_validation_failure",
        )

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", broken_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", _StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", _StubAppScriptBridgeService)

    from app.services.import_assistant_service import generate_import_assistant_batch

    try:
        asyncio.run(
            generate_import_assistant_batch(
                ImportAssistantGenerateRequest(
                    prompt=(
                        "Create escalation trigger for finance tickets: conditions status=open "
                        "and group=Finance Support; actions add tag escalated and keep status open."
                    ),
                    requester="pytest-user",
                    target_environment="sandbox",
                    mode="generate_validate_preview",
                )
            )
        )
    except RuntimeError:
        pass

    store = get_batch_store()
    batches = list(store.list_batches())
    assert batches
    latest = sorted(batches, key=lambda item: item.get("updated_at", ""), reverse=True)[0]
    metadata = latest.get("metadata", {})
    assert metadata["failure"]["failure_code"] == "generator_json_validation_failed"
    assert metadata["llm_runtime"]["generator_error"]["provider_error_code"] == "json_validate_failed"
    assert "failed_generation_excerpt" not in metadata["llm_runtime"]["generator_error"]


def test_non_chunked_failure_message_is_not_chunking(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {
            "object_type": "triggers",
            "intent": prompt,
            "confidence": 0.9,
            "ambiguity_score": 0.1,
            "ambiguity_reasons": [],
            "clarification_questions": [],
            "dependency_notes": "",
        }

    async def broken_generator(plan: dict, **kwargs):
        raise GeneratorStructuredOutputError(
            "Generator JSON validation failed after retry ladder.",
            attempts=[{"mode": "json_schema_strict", "http_status": 400}],
            provider_error_code="json_validate_failed",
            failed_generation_excerpt='{"records":[{"object_type":"triggers"}]}',
            validator_reason="missing title",
            corrective_question=None,
            corrective_example=None,
            error_class="schema_validation_failure",
        )

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", broken_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", _StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", _StubAppScriptBridgeService)

    from app.services.import_assistant_service import generate_import_assistant_batch

    try:
        asyncio.run(
            generate_import_assistant_batch(
                ImportAssistantGenerateRequest(
                    prompt=(
                        "Create escalation trigger for finance tickets: conditions status=open "
                        "and group=Finance Support; actions add tag escalated and keep status open."
                    ),
                    requester="pytest-user",
                    target_environment="sandbox",
                    mode="generate_validate_preview",
                )
            )
        )
    except RuntimeError:
        pass

    store = get_batch_store()
    batches = list(store.list_batches())
    assert batches
    latest = sorted(batches, key=lambda item: item.get("updated_at", ""), reverse=True)[0]
    history_messages = [item.get("message", "") for item in latest.get("status_history", [])]
    assert any(message.startswith("Generator JSON validation failed") for message in history_messages)


def test_generator_single_item_compatibility_mode_starts_with_json_object(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("XAI_API_KEY", "gsk_test_key")
    monkeypatch.setenv("LLM_MODEL_GENERATOR", "openai/gpt-oss-20b")
    monkeypatch.setenv("LLM_JSON_SCHEMA_SUPPORTED_MODELS", "openai/gpt-oss-20b")
    get_settings.cache_clear()
    GrokClient._LAST_CALL_METRICS.clear()

    calls: list[dict] = []

    async def fake_chat(self, messages, temperature=None, **kwargs):  # noqa: ANN001
        calls.append(dict(kwargs))
        return (
            '{"records":[{"object_type":"triggers","title":"Escalate Finance",'
            '"conditions":[{"field":"status","operator":"is","value":"open"}],'
            '"actions":[{"field":"set_tags","value":"escalated"}],'
            '"dependency_notes":[]}],"generation_notes":[]}'
        )

    monkeypatch.setattr(GrokClient, "chat", fake_chat)

    rows = asyncio.run(
        run_generator(
            {"object_type": "triggers", "intent": "Create escalation trigger"},
            allow_fallback=False,
            compatibility_first=True,
        )
    )

    assert len(rows) == 1
    assert len(calls) == 1
    assert calls[0].get("response_format_override") == "json_object"


def test_generator_uses_model_override_when_provided(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("XAI_API_KEY", "gsk_test_key")
    monkeypatch.setenv("LLM_MODEL_GENERATOR", "openai/gpt-oss-20b")
    monkeypatch.setenv("LLM_JSON_SCHEMA_SUPPORTED_MODELS", "openai/gpt-oss-20b")
    get_settings.cache_clear()
    GrokClient._LAST_CALL_METRICS.clear()

    captured_models: list[str] = []
    captured_keys: list[str | None] = []

    async def fake_chat(self, messages, temperature=None, **kwargs):  # noqa: ANN001
        captured_models.append(str(kwargs.get("model")))
        captured_keys.append(kwargs.get("api_key_override"))
        return (
            '{"records":[{"object_type":"views","title":"Finance Queue",'
            '"conditions":[{"field":"status","operator":"less_than","value":"solved"}],'
            '"actions":[{"field":"output_columns","value":["status","updated","subject"]}],'
            '"dependency_notes":[]}],"generation_notes":[]}'
        )

    monkeypatch.setattr(GrokClient, "chat", fake_chat)

    rows = asyncio.run(
        run_generator(
            {"object_type": "views", "intent": "Create finance unresolved view"},
            allow_fallback=False,
            compatibility_first=True,
            model_override="qwen/qwen3-32b",
            api_key_override="gsk_wave3",
        )
    )

    assert len(rows) == 1
    assert captured_models == ["qwen/qwen3-32b"]
    assert captured_keys == ["gsk_wave3"]


def test_generator_fail_fast_on_rate_limited(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("XAI_API_KEY", "gsk_test_key")
    monkeypatch.setenv("LLM_MODEL_GENERATOR", "openai/gpt-oss-20b")
    monkeypatch.setenv("LLM_JSON_SCHEMA_SUPPORTED_MODELS", "openai/gpt-oss-20b")
    get_settings.cache_clear()
    GrokClient._LAST_CALL_METRICS.clear()

    calls = {"count": 0}

    async def fake_chat(self, messages, temperature=None, **kwargs):  # noqa: ANN001
        calls["count"] += 1
        GrokClient._LAST_CALL_METRICS["generator"] = {
            "task": "generator",
            "model": kwargs.get("model"),
            "response_format_mode": "json_object_forced",
            "http_status": 429,
            "error_class": "rate_limited",
            "provider_error_code": "rate_limit_exceeded",
            "provider_failed_generation_excerpt": None,
        }
        raise LLMRequestError(
            "Groq API request failed (429) [rate_limited]: too many requests.",
            error_class="rate_limited",
            http_status=429,
            error_code="rate_limit_exceeded",
        )

    monkeypatch.setattr(GrokClient, "chat", fake_chat)

    try:
        asyncio.run(
            run_generator(
                {"object_type": "triggers", "intent": "Create escalation trigger"},
                allow_fallback=False,
                compatibility_first=True,
            )
        )
    except GeneratorStructuredOutputError as exc:
        assert exc.error_class == "rate_limited"
        assert exc.mode_order[:2] == ["json_object", "no_response_format"]
        assert len(exc.attempts) == 1
    else:
        raise AssertionError("Expected GeneratorStructuredOutputError for rate-limited fail-fast path.")

    assert calls["count"] == 1


def test_generator_compatibility_only_skips_schema_modes(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("XAI_API_KEY", "gsk_test_key")
    monkeypatch.setenv("LLM_MODEL_GENERATOR", "openai/gpt-oss-20b")
    monkeypatch.setenv("LLM_JSON_SCHEMA_SUPPORTED_MODELS", "openai/gpt-oss-20b")
    get_settings.cache_clear()
    GrokClient._LAST_CALL_METRICS.clear()

    calls: list[dict] = []

    async def fake_chat(self, messages, temperature=None, **kwargs):  # noqa: ANN001
        calls.append(dict(kwargs))
        call_no = len(calls)
        if call_no == 1:
            GrokClient._LAST_CALL_METRICS["generator"] = {
                "task": "generator",
                "model": kwargs.get("model"),
                "response_format_mode": "json_object_forced",
                "http_status": 400,
                "error_class": "schema_validation_failure",
                "provider_error_code": "json_validate_failed",
                "provider_failed_generation_excerpt": '{"records":[{"object_type":"ticket_fields"}]}',
            }
            raise LLMRequestError(
                "Groq API request failed (400) [schema_validation_failure]: Failed to validate JSON.",
                error_class="schema_validation_failure",
                http_status=400,
                error_code="json_validate_failed",
                failed_generation='{"records":[{"object_type":"ticket_fields"}]}',
            )
        return (
            '{"records":[{"object_type":"ticket_fields","title":"Medication Type",'
            '"conditions":[],"actions":[{"field":"field_type","value":"tagger"},'
            '{"field":"custom_field_options","value":[{"name":"Acute","value":"acute"}]}],'
            '"dependency_notes":[]}],"generation_notes":[]}'
        )

    monkeypatch.setattr(GrokClient, "chat", fake_chat)

    rows = asyncio.run(
        run_generator(
            {"object_type": "ticket_fields", "intent": "Create dropdown field"},
            allow_fallback=False,
            compatibility_first=True,
            compatibility_only=True,
        )
    )

    assert len(rows) == 1
    assert len(calls) == 2
    assert calls[0].get("response_format_override") == "json_object"
    assert calls[1].get("response_format_override") == "none"
    assert calls[0].get("strict_schema") is False
    assert calls[1].get("strict_schema") is False
