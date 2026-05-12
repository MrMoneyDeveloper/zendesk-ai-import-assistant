import asyncio

from app.api.grok.client import GrokClient
from app.api.grok.routing import resolve_model_route
from app.core.settings import get_settings


def test_groq_stage_model_defaults_and_conservative_context(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("XAI_API_KEY", "gsk_test_key")
    monkeypatch.delenv("XAI_MODEL", raising=False)
    monkeypatch.delenv("LLM_MODEL_PLANNER", raising=False)
    monkeypatch.delenv("LLM_MODEL_CLARIFIER", raising=False)
    monkeypatch.delenv("LLM_MODEL_GENERATOR", raising=False)
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.llm_model_planner == "qwen/qwen3-32b"
    assert settings.llm_model_clarifier == "qwen/qwen3-32b"
    assert settings.llm_model_generator == "openai/gpt-oss-20b"

    assert settings.llm_planner_max_output_tokens == 300
    assert settings.llm_clarifier_max_output_tokens == 240
    assert settings.llm_generator_max_output_tokens == 900

    assert settings.llm_strict_schema_planner is False
    assert settings.llm_strict_schema_clarifier is False
    assert settings.llm_strict_schema_generator is True

    assert settings.llm_context_max_related_objects == 16
    assert settings.llm_context_max_entries_per_catalog == 6
    assert settings.llm_context_max_catalog_entries == 36
    assert settings.llm_context_max_recent_items == 4
    assert settings.llm_context_max_recent_chars == 140
    assert settings.llm_context_max_notes_chars == 700
    assert settings.llm_auto_chunk_enabled is True
    assert settings.llm_auto_chunk_size == 6
    assert settings.llm_auto_chunk_max_chunks == 12
    assert settings.llm_auto_chunk_trigger_min_records == 7
    assert settings.llm_auto_chunk_pacing_seconds >= 0
    assert settings.llm_auto_chunk_pacing_jitter_seconds >= 0


def test_stage_strict_flags_can_inherit_global(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("XAI_API_KEY", "gsk_test_key")
    monkeypatch.setenv("LLM_STRICT_SCHEMA_MODE", "true")
    monkeypatch.setenv("LLM_STRICT_SCHEMA_PLANNER", "inherit")
    monkeypatch.setenv("LLM_STRICT_SCHEMA_CLARIFIER", "false")
    monkeypatch.setenv("LLM_STRICT_SCHEMA_GENERATOR", "true")
    get_settings.cache_clear()

    settings = get_settings()
    planner_route = resolve_model_route(settings, "planner")
    clarifier_route = resolve_model_route(settings, "clarifier")
    generator_route = resolve_model_route(settings, "generator")

    assert planner_route.strict_schema is True
    assert clarifier_route.strict_schema is False
    assert generator_route.strict_schema is True


def test_rate_guard_waits_when_remaining_tokens_are_low(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("XAI_API_KEY", "gsk_test_key")
    monkeypatch.setenv("LLM_RATE_GUARD_ENABLED", "true")
    monkeypatch.setenv("LLM_RATE_GUARD_SAFETY_RATIO", "0.8")
    monkeypatch.setenv("LLM_RATE_GUARD_MIN_HEADROOM_TOKENS", "250")
    get_settings.cache_clear()

    GrokClient._MODEL_USAGE_STATE.clear()
    client = GrokClient()
    model = "qwen/qwen3-32b"
    state = client._get_model_usage_state(model)
    state["remaining_tokens"] = 120
    state["reset_tokens_seconds"] = 0.5

    wait_seconds, wait_reason = client._compute_pre_request_wait_seconds(
        model,
        estimated_tokens=350,
        task="planner",
    )
    assert wait_seconds > 0.0
    assert wait_reason in {"header_budget", "local_budget"}


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.is_success = 200 <= status_code < 300
        self.text = "{}"

    def json(self):
        return self._payload


class _FakeAsyncClient:
    recorded_payloads: list[dict] = []
    responses: list[_FakeResponse] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.recorded_payloads.append(dict(json or {}))
        if _FakeAsyncClient.responses:
            return _FakeAsyncClient.responses.pop(0)
        return _FakeResponse(
            200,
            {
                "choices": [{"message": {"content": "{\"ok\":true}"}}],
                "usage": {"total_tokens": 120},
            },
        )


def test_planner_uses_json_object_when_model_not_in_schema_supported_list(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("XAI_API_KEY", "gsk_test_key")
    monkeypatch.setenv("LLM_JSON_SCHEMA_SUPPORTED_MODELS", "openai/gpt-oss-20b")
    get_settings.cache_clear()
    GrokClient._MODEL_USAGE_STATE.clear()
    GrokClient._LAST_CALL_METRICS.clear()
    _FakeAsyncClient.recorded_payloads = []
    _FakeAsyncClient.responses = [
        _FakeResponse(
            200,
            {
                "choices": [{"message": {"content": "{\"intent\":\"ok\"}"}}],
                "usage": {"total_tokens": 100},
            },
        )
    ]
    monkeypatch.setattr("app.api.grok.client.httpx.AsyncClient", _FakeAsyncClient)

    client = GrokClient()
    output = asyncio.run(
        client.chat(
            [{"role": "user", "content": "test"}],
            model="qwen/qwen3-32b",
            response_schema={"type": "object"},
            response_schema_name="planner",
            strict_schema=True,
            task="planner",
        )
    )
    assert output
    assert _FakeAsyncClient.recorded_payloads
    response_format = _FakeAsyncClient.recorded_payloads[-1].get("response_format", {})
    assert response_format.get("type") == "json_object"
    metrics = GrokClient.get_last_call_metrics("planner")
    assert metrics.get("response_format_mode") == "json_object"


def test_non_rate_limit_4xx_does_not_inflate_local_usage(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("XAI_API_KEY", "gsk_test_key")
    monkeypatch.setenv("LLM_JSON_SCHEMA_SUPPORTED_MODELS", "openai/gpt-oss-20b")
    monkeypatch.setenv("LLM_FALLBACK_TO_JSON_OBJECT", "false")
    get_settings.cache_clear()
    GrokClient._MODEL_USAGE_STATE.clear()
    GrokClient._LAST_CALL_METRICS.clear()
    _FakeAsyncClient.recorded_payloads = []
    _FakeAsyncClient.responses = [
        _FakeResponse(
            400,
            {"error": {"message": "response_format json_schema is not supported for this model"}},
        )
    ]
    monkeypatch.setattr("app.api.grok.client.httpx.AsyncClient", _FakeAsyncClient)

    client = GrokClient()
    model = "openai/gpt-oss-20b"
    state = client._get_model_usage_state(model)
    before = float(state.get("window_used_tokens") or 0.0)

    try:
        asyncio.run(
            client.chat(
                [{"role": "user", "content": "test"}],
                model=model,
                response_schema={"type": "object"},
                response_schema_name="generator_records",
                strict_schema=True,
                task="generator",
            )
        )
    except Exception:
        pass

    after_state = client._get_model_usage_state(model)
    after = float(after_state.get("window_used_tokens") or 0.0)
    assert after == before
    metrics = GrokClient.get_last_call_metrics("generator")
    assert metrics.get("error_class") in {"unsupported_response_format", "schema_validation_failure", "other_invalid_request"}


def test_circuit_breaker_opens_after_configured_failures(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("XAI_API_KEY", "gsk_test_key")
    monkeypatch.setenv("LLM_CIRCUIT_BREAKER_ENABLED", "true")
    monkeypatch.setenv("LLM_CIRCUIT_BREAKER_FAILURES", "2")
    monkeypatch.setenv("LLM_CIRCUIT_BREAKER_WINDOW_SECONDS", "300")
    monkeypatch.setenv("LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "120")
    get_settings.cache_clear()

    GrokClient._CIRCUIT_STATE.clear()
    client = GrokClient()
    task = "planner"
    model = "qwen/qwen3-32b"
    error_class = "model_permission_blocked"

    client._record_breaker_failure(task, model, error_class)
    assert client._is_breaker_open(task, model, error_class) is False
    client._record_breaker_failure(task, model, error_class)
    assert client._is_breaker_open(task, model, error_class) is True
