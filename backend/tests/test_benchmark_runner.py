import asyncio
import csv
import json

from app.core.settings import get_settings
from app.services.benchmark_runner import (
    BenchmarkConfig,
    benchmark_profile,
    detect_threshold_breach,
    run_benchmark,
)


def test_benchmark_profile_overrides_and_restores(monkeypatch):
    monkeypatch.setenv("LLM_RATE_GUARD_ENABLED", "true")
    monkeypatch.setenv("BENCHMARK_MODE", "false")
    get_settings.cache_clear()
    before = get_settings()
    assert before.llm_rate_guard_enabled is True
    assert before.benchmark_mode_enabled is False

    with benchmark_profile(
        {
            "LLM_RATE_GUARD_ENABLED": "false",
            "BENCHMARK_MODE": "true",
        }
    ):
        current = get_settings()
        assert current.llm_rate_guard_enabled is False
        assert current.benchmark_mode_enabled is True

    after = get_settings()
    assert after.llm_rate_guard_enabled is True
    assert after.benchmark_mode_enabled is False


def test_detect_threshold_breach_429_window():
    events = []
    for index in range(12):
        status = 429 if index in {8, 9} else 200
        events.append(
            {
                "status": "error" if status >= 400 else "ok",
                "http_status": 200 if status < 400 else 502,
                "provider_http_status": status,
                "requested_count": 24,
            }
        )

    breach = detect_threshold_breach(events)
    assert breach["breached"] is True
    assert breach["breach_reason"] in {"429_rate_window10", "400_rate_cumulative", "consecutive_failures"}
    assert breach["breach_level"] == 24


def test_run_benchmark_writes_artifacts(monkeypatch, tmp_path):
    async def fake_generate_attempt(*, object_type, requested_count, requester):
        if requested_count >= 12:
            return {
                "status": "error",
                "http_status": 502,
                "provider_http_status": 429,
                "error_text": "Groq API request failed (429): rate limit",
                "latency_ms": 210.0,
                "batch_id": "",
                "pipeline_status": "error",
                "planner_model": "qwen/qwen3-32b",
                "generator_model": "openai/gpt-oss-20b",
                "retry_count": 0,
                "pre_request_wait_ms": 0.0,
                "estimated_tokens": 1500,
                "used_tokens": 0,
                "chunking": {"activated": True, "chunk_size": 6, "total_chunks": 2},
            }
        return {
            "status": "ok",
            "http_status": 200,
            "provider_http_status": 200,
            "error_text": "",
            "latency_ms": 120.0,
            "batch_id": f"BATCH-{requested_count}",
            "pipeline_status": "preview_ready",
            "planner_model": "qwen/qwen3-32b",
            "generator_model": "openai/gpt-oss-20b",
            "retry_count": 0,
            "pre_request_wait_ms": 0.0,
            "estimated_tokens": 800,
            "used_tokens": 700,
            "chunking": {"activated": requested_count >= 7, "chunk_size": 6, "total_chunks": 1},
        }

    async def fake_raw_attempt(*, object_type, requested_count, requester):
        return {
            "status": "ok",
            "http_status": 200,
            "provider_http_status": 200,
            "error_text": "",
            "latency_ms": 95.0,
            "batch_id": "",
            "pipeline_status": "ok",
            "planner_model": "qwen/qwen3-32b",
            "generator_model": "openai/gpt-oss-20b",
            "retry_count": 0,
            "pre_request_wait_ms": 0.0,
            "estimated_tokens": 600,
            "used_tokens": 520,
            "chunking": {"activated": requested_count >= 7, "chunk_size": 6, "total_chunks": 1},
        }

    monkeypatch.setattr("app.services.benchmark_runner._run_generate_endpoint_attempt", fake_generate_attempt)
    monkeypatch.setattr("app.services.benchmark_runner._run_raw_model_attempt", fake_raw_attempt)

    config = BenchmarkConfig(
        output_root=tmp_path / "benchmarks",
        object_types=["triggers"],
        levels=[1, 3, 6, 12],
        paths=["generate_endpoint", "raw_model"],
        attempts_per_level=2,
        max_attempts=20,
        max_runtime_seconds=600,
        random_seed=7,
    )
    result = asyncio.run(run_benchmark(config))
    output_dir = tmp_path / "benchmarks" / result["run_id"]

    assert output_dir.exists()
    assert (output_dir / "events.jsonl").exists()
    assert (output_dir / "summary.csv").exists()
    assert (output_dir / "thresholds.csv").exists()
    assert (output_dir / "recommendations.csv").exists()
    assert (output_dir / "run_config.json").exists()

    events = (output_dir / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(events) > 0

    with (output_dir / "summary.csv").open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert any(row["path"] == "generate_endpoint" for row in rows)
    assert any(row["path"] == "raw_model" for row in rows)

    run_config = json.loads((output_dir / "run_config.json").read_text(encoding="utf-8"))
    assert run_config["config"]["paths"] == ["generate_endpoint", "raw_model"]
