import asyncio
from types import SimpleNamespace

from app.api.grok.client import GrokClient
from app.services.progress_narrator import (
    ProgressNarrator,
    _validate_narration,
    build_chunk_message,
    build_wave_complete_message,
    build_wave_start_message,
)


def test_deterministic_progress_messages_include_operational_context():
    items = [
        {"object_type": "groups", "department_name": "Customer Support"},
        {"object_type": "groups", "department_name": "Finance Operations"},
    ]
    start = build_wave_start_message(wave=2, wave_position=2, total_waves=5, items=items)
    chunk = build_chunk_message(
        wave_position=2,
        total_waves=5,
        item=items[0],
        object_type="groups",
        chunk_index=1,
        chunk_total=1,
        target_count=1,
        mode="gemini",
    )
    complete = build_wave_complete_message(
        wave=2,
        wave_position=2,
        total_waves=5,
        wave_meta={"generated_records": 9, "chunks": 8, "blocked": 0},
        cumulative_counts={"groups": 7, "ticket_fields": 9},
        supervisor_summary="All required names were verified.",
    )

    assert "shared fields and department groups" in start
    assert "Customer Support" in chunk
    assert "Gemini as the primary drafting model" in chunk
    assert "16 record(s)" in complete
    assert "All required names were verified" in complete

    final_complete = build_wave_complete_message(
        wave=4,
        wave_position=1,
        total_waves=1,
        wave_meta={"generated_records": 6, "chunks": 2, "blocked": 0},
        cumulative_counts={"triggers": 6},
        supervisor_summary="All triggers were staged and validated.",
    )
    assert "final validation and staging" in final_complete
    assert "articles" not in final_complete.lower()
    assert "were reviewed and checked" in final_complete


def test_narration_validator_rejects_invented_numbers_and_approval():
    source = "Wave 1/5 completed with 12 records and 0 blocked items."
    assert _validate_narration("Wave 1/5 completed with 12 records and 0 blocked items.", source)
    assert _validate_narration("Wave 1/5 completed with 13 records.", source) is None
    assert _validate_narration("All 12 records were approved in wave 1/5.", source) is None
    assert _validate_narration("All 12 records were staged in wave 1/5.", source) is None


def test_groq_progress_narrator_polishes_verified_event(monkeypatch):
    async def fake_chat(self, *args, **kwargs):
        return "Wave 2/5 completed with 9 records across 8 chunks and 0 blocked items; the next dependency layer can now begin."

    monkeypatch.setattr(GrokClient, "chat", fake_chat)
    narrator = ProgressNarrator()
    narrator.settings = SimpleNamespace(
        progress_narrator_enabled=True,
        progress_narrator_max_calls_per_batch=6,
        progress_narrator_provider="groq",
        progress_narrator_model="openai/gpt-oss-20b",
        progress_narrator_max_output_tokens=120,
        xai_api_key_tertiary="key-3",
        xai_api_key_secondary="key-2",
        xai_api_key="key-1",
        gemini_default_model="gemini-test",
    )
    source = (
        "Wave 2/5 completed with 9 record(s) from 8 chunk(s) and 0 blocked item(s); "
        "the cumulative operating model now contains 16 record(s). Next: wave 3/5."
    )

    result = asyncio.run(
        narrator.narrate(
            deterministic_message=source,
            event_context={"wave": 2, "generated_records": 9, "chunks": 8, "blocked": 0},
        )
    )

    assert result.source == "model_narrator"
    assert result.provider == "Groq"
    assert result.fallback_used is False
