import asyncio
import json
from types import SimpleNamespace

from app.core.settings import get_settings
from app.services.batch_store import get_batch_store
from app.services.operating_model_benchmark import (
    OperatingModelBenchmarkConfig,
    build_arg_parser,
    compare_saved_benchmark_results,
    run_operating_model_benchmark,
)


def test_operating_model_benchmark_writes_isolated_comparison_artifacts(monkeypatch, tmp_path):
    async def fake_generate(_payload):
        strategy = get_settings().department_generation_strategy
        batch_id = f"BATCH-{strategy}"
        model_calls = 2 if strategy == "hybrid" else 0
        get_batch_store().save_batch(
            {
                "batch_id": batch_id,
                "status": "preview_ready",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:04+00:00",
                "status_history": [],
                "records": [
                    {
                        "object_type": "articles",
                        "title": f"{strategy} shared title",
                        "actions": [{"field": "body", "value": "A substantive article body."}],
                    },
                    {
                        "object_type": "macros",
                        "title": f"{strategy} shared title",
                        "actions": [{"field": "comment_value", "value": "A useful macro response."}],
                    },
                ],
                "generated_counts": {"articles": 1, "macros": 1},
                "validation_summary": {"passed": 2, "warnings": 0, "blocked": 0},
                "metadata": {
                    "usage_report": {
                        "totals": {
                            "calls": model_calls + 1,
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "thought_tokens": 0,
                            "total_tokens": 120,
                            "retries": 0,
                        },
                        "by_task": [
                            {"task": "generator", "calls": model_calls},
                            {"task": "supervisor", "calls": 1},
                        ],
                    },
                    "quality_gates": {"coverage": {"status": "passed", "missing": []}},
                    "supervisor": {
                        "call_counts": {"total": 1},
                        "reviews": [{"effective_approved": True, "effective_quality_score": 0.9}],
                        "patch_counts": {"applied": 1, "rejected": 0},
                        "blocked_chunk_ids": [],
                    },
                    "llm_runtime": {
                        "generator_chunks": [
                            {
                                "template_first": strategy == "template",
                                "api_key_profile": "primary",
                                "deterministic_fallback": False,
                            }
                        ]
                    },
                },
            }
        )
        return SimpleNamespace(batch_id=batch_id, status="preview_ready")

    monkeypatch.setattr(
        "app.services.operating_model_benchmark.generate_endpoint",
        fake_generate,
    )
    result = asyncio.run(
        run_operating_model_benchmark(
            OperatingModelBenchmarkConfig(
                output_root=tmp_path,
                variants=("template", "hybrid"),
                prompt="Build an operating model for benchmark departments.",
                timeout_seconds_per_variant=60,
            )
        )
    )

    output_dir = tmp_path / result["run_id"]
    assert (output_dir / "comparison.csv").exists()
    assert (output_dir / "summary.md").exists()
    assert (output_dir / "template" / "result.json").exists()
    assert (output_dir / "hybrid" / "result.json").exists()
    payload = json.loads((output_dir / "results.json").read_text(encoding="utf-8"))
    assert payload["deployment_enabled"] is False
    assert [item["variant"] for item in payload["results"]] == ["template", "hybrid"]
    assert result["comparison"][0]["generator_calls"] == 0
    assert result["comparison"][1]["generator_calls"] == 2
    assert payload["results"][0]["quality"]["duplicate_titles"] == 0
    assert payload["results"][1]["quality"]["duplicate_titles"] == 0

    saved_output = tmp_path / "saved-comparison"
    saved = compare_saved_benchmark_results(
        (
            output_dir / "template" / "result.json",
            output_dir / "hybrid" / "result.json",
        ),
        saved_output,
    )
    assert len(saved["comparison"]) == 2
    assert (saved_output / "comparison.csv").exists()
    assert "gemini_tokens" in saved["comparison"][0]
    assert "duplicate_action_fields" in saved["comparison"][1]


def test_operating_model_benchmark_accepts_external_prompt_file(tmp_path):
    prompt_file = tmp_path / "customer-prompt.txt"
    prompt_file.write_text("Build the exact customer operating model.", encoding="utf-8")
    args = build_arg_parser().parse_args(
        ["--prompt-file", str(prompt_file), "--variants", "hybrid"]
    )
    assert args.prompt_file == str(prompt_file)
    assert args.variants == "hybrid"
