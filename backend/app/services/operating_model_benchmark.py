from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.settings import get_settings
from app.models.schemas import ImportAssistantGenerateRequest
from app.routes.import_assistant import generate as generate_endpoint
from app.services.batch_store import get_batch_store, reset_batch_store
from app.services.benchmark_runner import benchmark_profile
from app.services.perf_capture import reset_perf_capture_state_for_tests


APEX_OPERATING_MODEL_PROMPT = """Build a Zendesk support operating model for Apex Mobility Finance, a company that finances electric scooters, delivery e-bikes, and small EV fleets for gig workers and small businesses.

Business context:
Apex has these departments:
- Customer Support: first-line support for account questions, payment issues, login problems, and general requests.
- Claims & Incidents: handles damaged vehicles, theft reports, accident claims, insurance evidence, and urgent safety incidents.
- Finance Operations: handles failed payments, settlement disputes, refunds, payoff quotes, and billing corrections.
- Fleet Onboarding: helps business customers onboard multiple riders, verify documents, activate vehicles, and schedule handover.
- Technical Support: handles app bugs, GPS/device issues, charger problems, battery diagnostics, and telematics troubleshooting.
- Compliance: handles KYC document review, suspicious activity, regulatory complaints, and data/privacy requests.
- VIP / Enterprise Success: handles high-value fleet accounts, partner escalations, and white-glove support.

Create a complete Zendesk configuration in dependency order:
- Groups for the departments above, reusing existing matching groups if selected.
- Ticket fields for Department, Customer Segment, Vehicle Type, Issue Category, Incident Severity, Payment Status, KYC Status, Fleet Size, and Requested Outcome.
- Ticket forms for General Support, Claims & Incidents, Finance Operations, Fleet Onboarding, Technical Support, Compliance Review, and VIP Enterprise Support.
- Triggers to route tickets to the correct department based on form, issue category, customer segment, severity, and payment/KYC signals.
- Automations for stale urgent incidents, unresolved failed payments, pending KYC reviews, and enterprise escalations.
- Macros for first response, missing information request, payment dispute acknowledgement, incident claim acknowledgement, KYC document request, technical troubleshooting steps, and VIP escalation acknowledgement.
- Views for each department showing useful columns like requester, priority, status, vehicle type, issue category, severity, payment status, KYC status, assignee, and updated date.
- Help center categories, sections, and articles for payments, claims, onboarding, technical troubleshooting, compliance/KYC, and enterprise fleet support.

Operational rules:
- Use existing selected Zendesk context where appropriate, but create missing dependencies when needed.
- Keep every record deploy-safe and incremental.
- Add dependency notes when an object depends on a field, group, category, or section.
- Use clear Apex-specific tags.
- Do not invent external Zendesk IDs. Reference dependencies by clear names and notes.
- Make the setup practical for a real support team, not a demo.
"""


@dataclass(frozen=True)
class OperatingModelBenchmarkConfig:
    output_root: Path
    variants: tuple[str, ...] = ("template", "hybrid")
    prompt: str = APEX_OPERATING_MODEL_PROMPT
    timeout_seconds_per_variant: int = 900


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")


def _safe_average(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def _action_text(row: dict[str, Any], fields: set[str]) -> str:
    output: list[str] = []
    actions = row.get("actions", [])
    actions = actions if isinstance(actions, list) else []
    for action in actions:
        if not isinstance(action, dict):
            continue
        if str(action.get("field", "")).strip().lower() not in fields:
            continue
        value = action.get("value")
        if isinstance(value, str):
            output.append(value.strip())
    return "\n".join(part for part in output if part)


def _quality_metrics(batch: dict[str, Any]) -> dict[str, Any]:
    records = [item for item in list(batch.get("records", []) or []) if isinstance(item, dict)]
    metadata = batch.get("metadata", {}) if isinstance(batch.get("metadata"), dict) else {}
    supervisor = metadata.get("supervisor", {}) if isinstance(metadata.get("supervisor"), dict) else {}
    reviews = [item for item in list(supervisor.get("reviews", []) or []) if isinstance(item, dict)]
    coverage = (
        metadata.get("quality_gates", {}).get("coverage", {})
        if isinstance(metadata.get("quality_gates"), dict)
        else {}
    )
    runtime = metadata.get("llm_runtime", {}) if isinstance(metadata.get("llm_runtime"), dict) else {}
    chunks = [item for item in list(runtime.get("generator_chunks", []) or []) if isinstance(item, dict)]

    article_lengths = [
        len(_action_text(row, {"body", "article_body"}))
        for row in records
        if str(row.get("object_type", "")).strip() == "articles"
    ]
    macro_lengths = [
        len(_action_text(row, {"comment_value", "comment_value_html", "body"}))
        for row in records
        if str(row.get("object_type", "")).strip() == "macros"
    ]
    duplicate_action_fields = 0
    for row in records:
        seen: set[str] = set()
        actions = row.get("actions", [])
        actions = actions if isinstance(actions, list) else []
        for action in actions:
            if not isinstance(action, dict):
                continue
            field = str(action.get("field", "")).strip().lower()
            if field and field in seen:
                duplicate_action_fields += 1
            if field:
                seen.add(field)

    title_keys = [
        (
            str(row.get("object_type", "")).strip().lower(),
            str(row.get("title", "")).strip().lower(),
        )
        for row in records
        if str(row.get("title", "")).strip()
    ]
    lane_counts: dict[str, int] = {}
    for chunk in chunks:
        profile = str(chunk.get("api_key_profile") or "unknown")
        if chunk.get("template_first"):
            continue
        lane_counts[profile] = lane_counts.get(profile, 0) + 1

    effective_scores = [
        float(item.get("effective_quality_score"))
        for item in reviews
        if item.get("effective_quality_score") is not None
    ]
    return {
        "record_count": len(records),
        "generated_counts": dict(batch.get("generated_counts", {}) or {}),
        "validation": dict(batch.get("validation_summary", {}) or {}),
        "coverage_status": str(coverage.get("status") or "unknown"),
        "coverage_missing_count": len(list(coverage.get("missing", []) or [])),
        "supervisor_review_calls": int(supervisor.get("call_counts", {}).get("total", 0) or 0),
        "effective_approved_reviews": sum(1 for item in reviews if item.get("effective_approved")),
        "average_effective_quality_score": _safe_average(effective_scores),
        "patches_applied": int(supervisor.get("patch_counts", {}).get("applied", 0) or 0),
        "patches_rejected": int(supervisor.get("patch_counts", {}).get("rejected", 0) or 0),
        "blocked_chunks": len(list(supervisor.get("blocked_chunk_ids", []) or [])),
        "template_first_chunks": sum(1 for item in chunks if item.get("template_first")),
        "model_routed_chunks": sum(1 for item in chunks if not item.get("template_first")),
        "deterministic_fallback_chunks": sum(
            1
            for item in chunks
            if item.get("deterministic_fallback") and not item.get("template_first")
        ),
        "generator_lane_chunk_counts": lane_counts,
        "average_article_body_chars": _safe_average([float(value) for value in article_lengths]),
        "average_macro_body_chars": _safe_average([float(value) for value in macro_lengths]),
        "duplicate_titles": max(len(title_keys) - len(set(title_keys)), 0),
        "duplicate_action_fields": duplicate_action_fields,
    }


def _flatten_result(result: dict[str, Any]) -> dict[str, Any]:
    usage = result.get("usage_report", {}) if isinstance(result.get("usage_report"), dict) else {}
    totals = usage.get("totals", {}) if isinstance(usage.get("totals"), dict) else {}
    quality = result.get("quality", {}) if isinstance(result.get("quality"), dict) else {}
    validation = quality.get("validation", {}) if isinstance(quality.get("validation"), dict) else {}
    providers = [
        item
        for item in list(usage.get("by_provider", []) or [])
        if isinstance(item, dict)
    ]

    def provider_metric(provider: str, field: str) -> int | float:
        return sum(
            item.get(field, 0) or 0
            for item in providers
            if str(item.get("provider", "")).strip().lower() == provider.lower()
        )

    lane_counts = quality.get("generator_lane_chunk_counts", {})
    lane_counts = lane_counts if isinstance(lane_counts, dict) else {}
    return {
        "variant": result.get("variant"),
        "status": result.get("status"),
        "batch_id": result.get("batch_id"),
        "wall_time_seconds": round(float(result.get("wall_time_ms", 0.0) or 0.0) / 1000.0, 2),
        "record_count": quality.get("record_count", 0),
        "passed": validation.get("passed", 0),
        "warnings": validation.get("warnings", 0),
        "blocked": validation.get("blocked", 0),
        "coverage_status": quality.get("coverage_status"),
        "supervisor_calls": quality.get("supervisor_review_calls", 0),
        "effective_approved_reviews": quality.get("effective_approved_reviews", 0),
        "generator_calls": sum(
            int(item.get("calls", 0) or 0)
            for item in list(usage.get("by_task", []) or [])
            if item.get("task") == "generator"
        ),
        "total_model_calls": totals.get("calls", 0),
        "successful_model_calls": totals.get("successful_calls", 0),
        "failed_model_calls": totals.get("failed_calls", 0),
        "input_tokens": totals.get("input_tokens", 0),
        "output_tokens": totals.get("output_tokens", 0),
        "thought_tokens": totals.get("thought_tokens", 0),
        "total_tokens": totals.get("total_tokens", 0),
        "gemini_tokens": provider_metric("Gemini", "total_tokens"),
        "groq_tokens": provider_metric("Groq", "total_tokens"),
        "retries": totals.get("retries", 0),
        "model_call_elapsed_seconds": round(
            float(totals.get("model_call_elapsed_ms_sum", 0.0) or 0.0) / 1000.0,
            2,
        ),
        "pre_request_wait_seconds": round(
            float(totals.get("pre_request_wait_ms_sum", 0.0) or 0.0) / 1000.0,
            2,
        ),
        "template_first_chunks": quality.get("template_first_chunks", 0),
        "model_routed_chunks": quality.get("model_routed_chunks", 0),
        "fallback_chunks": quality.get("deterministic_fallback_chunks", 0),
        "generator_lane_chunks": ";".join(
            f"{name}:{lane_counts[name]}" for name in sorted(lane_counts)
        ),
        "avg_quality_score": quality.get("average_effective_quality_score", 0),
        "avg_article_body_chars": quality.get("average_article_body_chars", 0),
        "avg_macro_body_chars": quality.get("average_macro_body_chars", 0),
        "patches_applied": quality.get("patches_applied", 0),
        "patches_rejected": quality.get("patches_rejected", 0),
        "blocked_chunks": quality.get("blocked_chunks", 0),
        "duplicate_titles": quality.get("duplicate_titles", 0),
        "duplicate_action_fields": quality.get("duplicate_action_fields", 0),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = list(rows[0].keys()) if rows else ["empty"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_summary(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = [
        "variant",
        "status",
        "wall_time_seconds",
        "record_count",
        "generator_calls",
        "supervisor_calls",
        "total_tokens",
        "gemini_tokens",
        "groq_tokens",
        "fallback_chunks",
        "blocked",
        "avg_quality_score",
        "avg_article_body_chars",
        "avg_macro_body_chars",
    ]
    lines = [
        "# Operating Model Benchmark",
        "",
        "Zendesk deployment and external Sheet staging were disabled for every variant.",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def compare_saved_benchmark_results(
    result_files: tuple[Path, ...],
    output_dir: Path,
) -> dict[str, Any]:
    if len(result_files) < 2:
        raise ValueError("At least two saved benchmark result files are required.")

    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    sources: list[str] = []
    for source in result_files:
        result_path = source.resolve()
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError(f"Benchmark result must be a JSON object: {result_path}")

        batch_store_path = result_path.parent / "batches.json"
        if batch_store_path.exists():
            store_payload = json.loads(batch_store_path.read_text(encoding="utf-8"))
            batches = store_payload.get("batches", {}) if isinstance(store_payload, dict) else {}
            batch_id = str(result.get("batch_id", "")).strip()
            batch = batches.get(batch_id, {}) if isinstance(batches, dict) else {}
            if isinstance(batch, dict) and batch:
                result["quality"] = _quality_metrics(batch)

        result["source_result_file"] = str(result_path)
        results.append(result)
        sources.append(str(result_path))

    flat_rows = [_flatten_result(result) for result in results]
    _write_csv(output_dir / "comparison.csv", flat_rows)
    _write_summary(output_dir / "summary.md", flat_rows)
    payload = {
        "comparison_id": output_dir.name,
        "generated_at": datetime.now(UTC).isoformat(),
        "deployment_enabled": False,
        "source_result_files": sources,
        "results": results,
    }
    (output_dir / "results.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=True),
        encoding="utf-8",
    )
    return {
        "comparison_id": output_dir.name,
        "output_dir": str(output_dir.resolve()),
        "comparison": flat_rows,
    }


async def run_operating_model_benchmark(
    config: OperatingModelBenchmarkConfig,
) -> dict[str, Any]:
    run_id = _run_id()
    run_dir = config.output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.txt").write_text(config.prompt.strip() + "\n", encoding="utf-8")

    results: list[dict[str, Any]] = []
    for variant in config.variants:
        normalized_variant = str(variant).strip().lower()
        if normalized_variant not in {"template", "hybrid"}:
            raise ValueError(f"Unsupported benchmark variant: {variant}")
        variant_dir = run_dir / normalized_variant
        variant_dir.mkdir(parents=True, exist_ok=True)
        overrides = {
            "DEPARTMENT_GENERATION_STRATEGY": normalized_variant,
            "DEPARTMENT_CONTENT_DRAFT_MAX_OUTPUT_TOKENS": "2400",
            "APPS_SCRIPT_WEB_APP_URL": "",
            "APPS_SCRIPT_API_KEY": "",
            "GOOGLE_SHEET_ID": "",
            "GOOGLE_SERVICE_ACCOUNT_FILE": "",
            "ZENDESK_SUBDOMAIN": "",
            "ZENDESK_EMAIL": "",
            "ZENDESK_API_TOKEN": "",
            "BATCH_STORE_FILE": str((variant_dir / "batches.json").resolve()),
            "BATCH_STORE_TRIM_RUNTIME_METADATA": "false",
            "DIAGNOSTICS_MODE": "true",
            "PERF_CAPTURE_ENABLED": "true",
            "PERF_CAPTURE_DIR": str((variant_dir / "perf").resolve()),
        }

        started = time.perf_counter()
        with benchmark_profile(overrides):
            reset_batch_store()
            reset_perf_capture_state_for_tests()
            request = ImportAssistantGenerateRequest(
                prompt=config.prompt,
                requester=f"operating-model-benchmark-{run_id}-{normalized_variant}",
                dependency_mode="match_existing_or_create_new",
                focus_object_types=[],
                related_objects=[],
                reference_catalog={},
                recent_batch_context=[],
                context_notes=f"benchmark_variant={normalized_variant}; external_deployment=disabled",
            )
            try:
                response = await asyncio.wait_for(
                    generate_endpoint(request.model_dump()),
                    timeout=max(int(config.timeout_seconds_per_variant), 60),
                )
                wall_time_ms = round((time.perf_counter() - started) * 1000.0, 2)
                batch = get_batch_store().get_batch(response.batch_id) or {}
                metadata = batch.get("metadata", {}) if isinstance(batch.get("metadata"), dict) else {}
                result = {
                    "variant": normalized_variant,
                    "status": response.status,
                    "batch_id": response.batch_id,
                    "wall_time_ms": wall_time_ms,
                    "usage_report": metadata.get("usage_report", {}),
                    "quality": _quality_metrics(batch),
                    "error": "",
                }
            except Exception as exc:  # noqa: BLE001
                partial_batches = get_batch_store().list_batches()
                partial_batch = max(
                    partial_batches,
                    key=lambda item: str(item.get("created_at", "")),
                    default={},
                )
                partial_metadata = (
                    partial_batch.get("metadata", {})
                    if isinstance(partial_batch.get("metadata"), dict)
                    else {}
                )
                result = {
                    "variant": normalized_variant,
                    "status": "error",
                    "batch_id": str(partial_batch.get("batch_id", "")),
                    "wall_time_ms": round((time.perf_counter() - started) * 1000.0, 2),
                    "usage_report": partial_metadata.get("usage_report", {}),
                    "quality": _quality_metrics(partial_batch) if partial_batch else {},
                    "error": str(exc)[:1000],
                }
            results.append(result)
            (variant_dir / "result.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=True),
                encoding="utf-8",
            )
        reset_batch_store()
        reset_perf_capture_state_for_tests()
        get_settings.cache_clear()

    flat_rows = [_flatten_result(result) for result in results]
    _write_csv(run_dir / "comparison.csv", flat_rows)
    _write_summary(run_dir / "summary.md", flat_rows)
    (run_dir / "results.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "generated_at": datetime.now(UTC).isoformat(),
                "variants": list(config.variants),
                "deployment_enabled": False,
                "results": results,
            },
            indent=2,
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )
    return {
        "run_id": run_id,
        "output_dir": str(run_dir),
        "comparison": flat_rows,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare deterministic-template and hybrid Groq+Gemini operating-model generation.",
    )
    parser.add_argument(
        "--output-root",
        default=str(
            (Path(__file__).resolve().parents[2] / "data" / "benchmarks" / "operating-model").resolve()
        ),
    )
    parser.add_argument("--variants", default="template,hybrid")
    parser.add_argument("--timeout-seconds-per-variant", type=int, default=900)
    parser.add_argument(
        "--compare-result",
        action="append",
        default=[],
        help="Saved result.json path. Repeat at least twice to build a comparison without API calls.",
    )
    parser.add_argument("--comparison-output-dir", default="")
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    saved_results = tuple(Path(item).resolve() for item in list(args.compare_result or []))
    if saved_results:
        output_dir = (
            Path(args.comparison_output_dir).resolve()
            if str(args.comparison_output_dir).strip()
            else Path(args.output_root).resolve() / f"comparison-{_run_id()}"
        )
        result = compare_saved_benchmark_results(saved_results, output_dir)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0

    variants = tuple(item.strip() for item in str(args.variants).split(",") if item.strip())
    config = OperatingModelBenchmarkConfig(
        output_root=Path(args.output_root).resolve(),
        variants=variants or ("template", "hybrid"),
        timeout_seconds_per_variant=max(int(args.timeout_seconds_per_variant), 60),
    )
    result = await run_operating_model_benchmark(config)
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


def main() -> int:
    return asyncio.run(_async_main(build_arg_parser().parse_args()))
