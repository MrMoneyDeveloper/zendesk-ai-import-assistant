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

    wait_seconds = client._compute_pre_request_wait_seconds(model, estimated_tokens=350)
    assert wait_seconds > 0.0
