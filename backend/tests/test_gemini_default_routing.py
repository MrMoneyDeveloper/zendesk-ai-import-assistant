import asyncio
import json
from types import SimpleNamespace

import httpx

from app.api.gemini.client import GeminiInteractionClient, GeminiRequestError
from app.api.grok.client import GrokClient


def _gemini_settings(**overrides):
    values = {
        "gemini_api_key": "test-key",
        "gemini_default_model": "gemini-test",
        "gemini_default_max_concurrency": 1,
        "gemini_default_min_request_interval_seconds": 0.0,
        "gemini_default_failure_threshold": 3,
        "gemini_default_cooldown_seconds": 60,
        "gemini_default_max_retries": 2,
        "gemini_default_retry_max_delay_seconds": 0.5,
        "gemini_default_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_gemini_default_client_returns_structured_output_and_usage(monkeypatch):
    response_payload = {
        "output_text": json.dumps({"records": [], "generation_notes": []}),
        "usage": {
            "total_input_tokens": 100,
            "total_output_tokens": 20,
            "total_thought_tokens": 5,
            "total_tokens": 125,
        },
    }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return httpx.Response(200, json=response_payload)

    GeminiInteractionClient.reset_runtime_state()
    monkeypatch.setattr("app.api.gemini.client.httpx.AsyncClient", FakeAsyncClient)
    client = GeminiInteractionClient()
    client.settings = _gemini_settings()

    output = asyncio.run(
        client.chat(
            [{"role": "user", "content": "Return JSON"}],
            task="generator",
            require_json=True,
        )
    )

    assert json.loads(output)["generation_notes"] == []
    metrics = client.get_last_call_metrics("generator")
    assert metrics["final_status"] == "ok"
    assert metrics["attempt_count"] == 1
    assert metrics["total_tokens"] == 125


def test_gemini_circuit_opens_after_three_transient_failures(monkeypatch):
    calls = 0

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": {}})

    GeminiInteractionClient.reset_runtime_state()
    monkeypatch.setattr("app.api.gemini.client.httpx.AsyncClient", FakeAsyncClient)
    client = GeminiInteractionClient()
    client.settings = _gemini_settings()

    try:
        asyncio.run(
            client.chat(
                [{"role": "user", "content": "Return JSON"}],
                task="generator",
                require_json=True,
            )
        )
    except GeminiRequestError as exc:
        assert exc.error_class == "rate_limited"
    else:
        raise AssertionError("Expected GeminiRequestError")

    assert calls == 3
    try:
        asyncio.run(
            client.chat(
                [{"role": "user", "content": "Return JSON"}],
                task="generator",
                require_json=True,
            )
        )
    except GeminiRequestError as exc:
        assert exc.error_class == "circuit_open"
    else:
        raise AssertionError("Expected circuit-open GeminiRequestError")
    assert calls == 3


def test_groq_client_fails_over_after_gemini_exhaustion(monkeypatch):
    async def fail_gemini(self, *args, **kwargs):
        raise GeminiRequestError(
            "Gemini exhausted",
            error_class="rate_limited",
            http_status=429,
            telemetry={"attempt_count": 3},
        )

    async def return_groq(self, *args, **kwargs):
        self._LAST_CALL_METRICS["generator"] = {
            "task": "generator",
            "model": "openai/gpt-oss-20b",
            "final_status": "ok",
        }
        return '{"records":[],"generation_notes":[]}'

    monkeypatch.setattr(GeminiInteractionClient, "chat", fail_gemini)
    monkeypatch.setattr(GrokClient, "_chat_openai_compatible", return_groq)
    client = GrokClient()
    client.settings = SimpleNamespace(
        llm_default_provider="gemini",
        gemini_default_model="gemini-test",
    )
    client.provider_name = "Groq"

    output = asyncio.run(
        client.chat(
            [{"role": "user", "content": "Return JSON"}],
            task="generator",
            model="openai/gpt-oss-20b",
            response_format_override="json_object",
        )
    )

    assert json.loads(output)["records"] == []
    metrics = client.get_last_call_metrics("generator")
    assert metrics["fallback_used"] is True
    assert metrics["fallback_from"] == "Gemini"
    assert metrics["fallback_reason"] == "rate_limited"
    assert metrics["provider_chain"] == ["Gemini", "Groq"]
    assert metrics["gemini_attempt_count"] == 3
