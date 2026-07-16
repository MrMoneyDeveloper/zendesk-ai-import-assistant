from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.api.gemini.client import GeminiInteractionClient
from app.core.settings import get_settings
from app.services.progress_narrator import ProgressNarrator, _validate_narration


BENCHMARK_CASES = (
    {
        "message": (
            "Wave 1/5 completed with 12 record(s) from 6 chunk(s) and 0 blocked item(s); "
            "the cumulative operating model now contains 12 record(s). Supervisor and deterministic "
            "gates verified the Help Center destinations. Next: creating shared fields and department groups."
        ),
        "context": {"wave": 1, "generated_records": 12, "chunks": 6, "blocked": 0},
    },
    {
        "message": (
            "Wave 3/5 completed with 21 record(s) from 7 chunk(s) and 1 blocked item(s); "
            "the cumulative operating model now contains 49 record(s). Form dependencies and view columns "
            "were checked. Next: routing triggers, time-based automations, and agent macros."
        ),
        "context": {"wave": 3, "generated_records": 21, "chunks": 7, "blocked": 1},
    },
    {
        "message": (
            "Wave 5/5 completed with 14 record(s) from 7 chunk(s) and 0 blocked item(s); "
            "the cumulative operating model now contains 119 record(s). Article section references and body "
            "coverage were checked. Next: final validation and staging for human review."
        ),
        "context": {"wave": 5, "generated_records": 14, "chunks": 7, "blocked": 0},
    },
)


@dataclass(frozen=True)
class ProgressNarratorBenchmarkConfig:
    output_root: Path
    providers: tuple[str, ...] = ("deterministic", "gemini", "groq")


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")


def _safe_number(value: object) -> float:
    try:
        return max(float(value), 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _descriptive_score(message: str) -> float:
    lowered = str(message or "").lower()
    groups = (
        ("built", "created", "generated", "drafted", "completed"),
        ("checked", "verified", "review", "gate", "validated"),
        ("next", "then", "after this"),
        ("dependency", "coverage", "routing", "section", "field", "form", "article"),
    )
    matched = sum(1 for terms in groups if any(term in lowered for term in terms))
    word_count = len(lowered.split())
    length_bonus = 1 if 20 <= word_count <= 70 else 0
    return round((matched + length_bonus) / (len(groups) + 1), 3)


def _summarize_provider(provider: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [_safe_number(item.get("wall_time_ms")) for item in cases]
    valid_count = sum(1 for item in cases if item.get("valid"))
    model_count = sum(1 for item in cases if item.get("source") == "model_narrator")
    total_tokens = sum(int(_safe_number(item.get("total_tokens"))) for item in cases)
    return {
        "provider": provider,
        "cases": len(cases),
        "valid_count": valid_count,
        "valid_rate": round(valid_count / len(cases), 3) if cases else 0.0,
        "model_response_count": model_count,
        "fallback_count": sum(1 for item in cases if item.get("fallback_used")),
        "average_wall_time_ms": round(statistics.mean(latencies), 2) if latencies else 0.0,
        "p95_wall_time_ms": round(max(latencies), 2) if latencies else 0.0,
        "total_tokens": total_tokens,
        "average_descriptive_score": round(
            statistics.mean(_safe_number(item.get("descriptive_score")) for item in cases),
            3,
        )
        if cases
        else 0.0,
    }


def choose_narrator(summary: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [item for item in summary if item.get("provider") in {"gemini", "groq"}]
    if not candidates:
        return {
            "provider": "deterministic",
            "reason": "No model narrator was benchmarked; deterministic progress remains active.",
        }

    eligible = [
        item
        for item in candidates
        if float(item.get("valid_rate", 0.0) or 0.0) >= 0.67
        and int(item.get("model_response_count", 0) or 0) > 0
    ]
    if not eligible:
        return {
            "provider": "deterministic",
            "reason": "Neither model produced enough valid public summaries; use verified deterministic events.",
        }

    max_latency = max(float(item.get("average_wall_time_ms", 0.0) or 0.0) for item in eligible) or 1.0
    for item in eligible:
        validity = float(item.get("valid_rate", 0.0) or 0.0)
        descriptive = float(item.get("average_descriptive_score", 0.0) or 0.0)
        latency_score = 1.0 - min(
            float(item.get("average_wall_time_ms", 0.0) or 0.0) / max_latency,
            1.0,
        )
        quota_isolation = 1.0 if item.get("provider") == "groq" else 0.0
        item["selection_score"] = round(
            validity * 0.45
            + descriptive * 0.25
            + latency_score * 0.15
            + quota_isolation * 0.15,
            3,
        )

    winner = max(eligible, key=lambda item: float(item.get("selection_score", 0.0)))
    reason = (
        "Use Groq for low-volume wave narration while Gemini remains dedicated to planning, generation, and supervision; "
        "verified deterministic events remain the fallback."
        if winner.get("provider") == "groq"
        else "Use Gemini narration because it produced the strongest valid summaries in this run; deterministic events remain the fallback."
    )
    return {"provider": winner.get("provider"), "reason": reason}


async def run_progress_narrator_benchmark(
    config: ProgressNarratorBenchmarkConfig,
) -> dict[str, Any]:
    run_id = _run_id()
    output_dir = config.output_root / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = get_settings()
    results: list[dict[str, Any]] = []

    for provider in config.providers:
        if provider == "gemini":
            GeminiInteractionClient.reset_runtime_state()
        narrator = ProgressNarrator()
        narrator.settings = replace(
            settings,
            progress_narrator_enabled=provider != "deterministic",
            progress_narrator_provider=provider if provider != "deterministic" else "groq",
            progress_narrator_max_calls_per_batch=len(BENCHMARK_CASES),
        )
        for index, case in enumerate(BENCHMARK_CASES, start=1):
            started = time.perf_counter()
            result = await narrator.narrate(
                deterministic_message=case["message"],
                event_context=case["context"],
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            telemetry = result.telemetry if isinstance(result.telemetry, dict) else {}
            output = {
                "provider": provider,
                "case": index,
                "message": result.message,
                "source": result.source,
                "model": result.model,
                "fallback_used": bool(result.fallback_used),
                "valid": _validate_narration(result.message, case["message"]) is not None,
                "descriptive_score": _descriptive_score(result.message),
                "wall_time_ms": elapsed_ms,
                "input_tokens": int(_safe_number(telemetry.get("input_tokens"))),
                "output_tokens": int(_safe_number(telemetry.get("output_tokens"))),
                "thought_tokens": int(_safe_number(telemetry.get("thought_tokens"))),
                "total_tokens": int(_safe_number(telemetry.get("total_tokens"))),
                "attempt_count": int(_safe_number(telemetry.get("attempt_count"))),
                "error_class": telemetry.get("error_class") or telemetry.get("error"),
            }
            results.append(output)

    summary = [
        _summarize_provider(
            provider,
            [item for item in results if item.get("provider") == provider],
        )
        for provider in config.providers
    ]
    recommendation = choose_narrator(summary)
    payload = {
        "run_id": run_id,
        "deployment_enabled": False,
        "synthetic_data_only": True,
        "results": results,
        "summary": summary,
        "recommendation": recommendation,
    }
    (output_dir / "results.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    lines = [
        "# Progress Narrator Benchmark",
        "",
        "Synthetic orchestration events only. No Zendesk deployment was attempted.",
        "",
        "| Provider | Valid | Model responses | Fallbacks | Avg ms | Tokens | Descriptive |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in summary:
        lines.append(
            f"| {item['provider']} | {item['valid_count']}/{item['cases']} | "
            f"{item['model_response_count']} | {item['fallback_count']} | "
            f"{item['average_wall_time_ms']} | {item['total_tokens']} | "
            f"{item['average_descriptive_score']} |"
        )
    lines.extend(
        [
            "",
            f"Recommendation: **{recommendation['provider']}**",
            "",
            str(recommendation["reason"]),
        ]
    )
    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {**payload, "output_dir": str(output_dir)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark public progress narration providers.")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/benchmarks/progress-narrator"),
    )
    args = parser.parse_args()
    result = asyncio.run(
        run_progress_narrator_benchmark(
            ProgressNarratorBenchmarkConfig(output_root=args.output_root)
        )
    )
    print(json.dumps({"output_dir": result["output_dir"], **result["recommendation"]}, indent=2))
    return 0

