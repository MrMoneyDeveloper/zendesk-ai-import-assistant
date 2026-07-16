import asyncio

from app.services.progress_narrator import NarrationResult, ProgressNarrator
from app.services.progress_narrator_benchmark import (
    ProgressNarratorBenchmarkConfig,
    choose_narrator,
    run_progress_narrator_benchmark,
)


def test_choose_narrator_prefers_valid_isolated_groq_lane():
    summary = [
        {
            "provider": "gemini",
            "valid_rate": 1.0,
            "model_response_count": 3,
            "average_descriptive_score": 0.8,
            "average_wall_time_ms": 900,
        },
        {
            "provider": "groq",
            "valid_rate": 1.0,
            "model_response_count": 3,
            "average_descriptive_score": 0.8,
            "average_wall_time_ms": 850,
        },
    ]

    assert choose_narrator(summary)["provider"] == "groq"


def test_benchmark_writes_results_without_deployment(monkeypatch, tmp_path):
    async def fake_narrate(self, *, deterministic_message, event_context):
        return NarrationResult(
            message=deterministic_message,
            source="model_narrator" if self.enabled else "deterministic",
            provider=(self.settings.progress_narrator_provider.capitalize() if self.enabled else None),
            model="test-model" if self.enabled else None,
            fallback_used=False,
            telemetry={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    monkeypatch.setattr(ProgressNarrator, "narrate", fake_narrate)
    result = asyncio.run(
        run_progress_narrator_benchmark(
            ProgressNarratorBenchmarkConfig(output_root=tmp_path)
        )
    )

    assert result["deployment_enabled"] is False
    assert result["synthetic_data_only"] is True
    assert (tmp_path / result["run_id"] / "results.json").exists()
    assert len(result["results"]) == 9
