import argparse
import asyncio
import csv
import json
import os
import random
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.api.grok.client import GrokClient
from app.api.grok.routing import resolve_model_route
from app.core.settings import get_settings
from app.models.schemas import ImportAssistantGenerateRequest
from app.routes.import_assistant import generate as generate_endpoint
from app.services.generator import run_generator
from app.services.import_assistant_service import (
    _build_chunk_instruction,
    _build_chunk_plan,
    _dedupe_generated_rows,
    _estimate_requested_record_count,
)
from app.services.planner import run_planner

DEFAULT_OBJECT_TYPES = [
    "triggers",
    "macros",
    "views",
    "ticket_forms",
    "ticket_fields",
    "articles",
    "automations",
    "groups",
]
DEFAULT_LEVELS = [1, 3, 6, 12, 24, 36, 48, 72]
DEFAULT_PATHS = ["generate_endpoint", "raw_model"]

BENCHMARK_PROFILE_OVERRIDES = {
    "BENCHMARK_MODE": "true",
    "LLM_RATE_GUARD_ENABLED": "false",
    "LLM_RETRY_MAX_ATTEMPTS": "1",
    "LLM_RETRY_BACKOFF_SECONDS": "0",
    "LLM_STRICT_SCHEMA_MODE": "false",
    "LLM_STRICT_SCHEMA_PLANNER": "false",
    "LLM_STRICT_SCHEMA_CLARIFIER": "false",
    "LLM_STRICT_SCHEMA_GENERATOR": "false",
    "LLM_FALLBACK_TO_JSON_OBJECT": "false",
    "LLM_AUTO_CHUNK_ENABLED": "true",
    "LLM_AUTO_CHUNK_SIZE": "6",
    "LLM_AUTO_CHUNK_MAX_CHUNKS": "12",
    # Keep external destinations disabled in benchmark runs to avoid side effects.
    "APPS_SCRIPT_WEB_APP_URL": "",
    "APPS_SCRIPT_API_KEY": "",
    "GOOGLE_SERVICE_ACCOUNT_FILE": "",
}


@dataclass
class BenchmarkConfig:
    output_root: Path
    object_types: list[str]
    levels: list[int]
    paths: list[str]
    attempts_per_level: int
    max_attempts: int
    max_runtime_seconds: int
    random_seed: int


@contextmanager
def benchmark_profile(overrides: dict[str, str] | None = None):
    overrides = overrides or BENCHMARK_PROFILE_OVERRIDES
    previous = {key: os.getenv(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _coerce_csv_list(value: str | None, *, cast=int) -> list:
    if not value:
        return []
    parts = [item.strip() for item in value.split(",")]
    cleaned = [item for item in parts if item]
    if cast is str:
        return cleaned
    return [cast(item) for item in cleaned]


def _slug(text: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return lowered or "item"


def _build_prompt(object_type: str, count: int) -> str:
    label = {
        "triggers": "triggers",
        "macros": "macros",
        "views": "views",
        "ticket_forms": "ticket forms",
        "ticket_fields": "ticket fields",
        "articles": "help center articles",
        "automations": "automations",
        "groups": "groups",
    }.get(object_type, object_type)
    return (
        f"Create {count} {label} for benchmark load testing. "
        f"Use unique numbered titles with prefix BENCH-{_slug(object_type)} and valid JSON output only."
    )


def _parse_provider_status(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"\((\d{3})\)", text)
    if match:
        return int(match.group(1))
    match = re.search(r"\bHTTP\s+(\d{3})\b", text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"\b(\d{3})\s+Bad Request\b", text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def _safe_avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def _derive_provider_status_from_metadata(metadata: dict[str, Any]) -> int | None:
    runtime = metadata.get("llm_runtime", {}) if isinstance(metadata, dict) else {}
    chunks = runtime.get("generator_chunks", []) if isinstance(runtime, dict) else []
    statuses: list[int] = []
    if isinstance(chunks, list):
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            status = chunk.get("http_status")
            if isinstance(status, int):
                statuses.append(status)
    generator = runtime.get("generator", {}) if isinstance(runtime, dict) else {}
    if isinstance(generator, dict):
        status = generator.get("http_status")
        if isinstance(status, int):
            statuses.append(status)
    non_200 = [status for status in statuses if status != 200]
    if non_200:
        return non_200[0]
    if statuses:
        return statuses[0]
    return None


async def _run_generate_endpoint_attempt(
    *,
    object_type: str,
    requested_count: int,
    requester: str,
) -> dict[str, Any]:
    prompt = _build_prompt(object_type, requested_count)
    request = ImportAssistantGenerateRequest(
        prompt=prompt,
        mode="benchmark",
        requester=requester,
        dependency_mode="match_existing_or_create_new",
        focus_object_types=[object_type],
        related_objects=[],
        reference_catalog={},
        recent_batch_context=[],
        context_notes="benchmark-mode",
    )
    started = time.perf_counter()
    try:
        response = await generate_endpoint(request)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        metadata = dict(response.metadata or {})
        llm_routes = metadata.get("llm_routes", {}) if isinstance(metadata, dict) else {}
        llm_runtime = metadata.get("llm_runtime", {}) if isinstance(metadata, dict) else {}
        chunking = metadata.get("chunking", {}) if isinstance(metadata, dict) else {}
        provider_http_status = _derive_provider_status_from_metadata(metadata)
        retries = 0
        pre_wait = 0.0
        chunks = llm_runtime.get("generator_chunks", []) if isinstance(llm_runtime, dict) else []
        if isinstance(chunks, list):
            for chunk in chunks:
                if not isinstance(chunk, dict):
                    continue
                retries += int(chunk.get("retry_count") or 0)
                pre_wait += float(chunk.get("pre_request_wait_ms") or 0.0)
        generator_runtime = llm_runtime.get("generator", {}) if isinstance(llm_runtime, dict) else {}
        if isinstance(generator_runtime, dict) and not retries:
            retries = int(generator_runtime.get("retry_count") or 0)
            pre_wait = float(generator_runtime.get("pre_request_wait_ms") or 0.0)

        return {
            "status": "ok",
            "http_status": 200,
            "provider_http_status": provider_http_status or 200,
            "error_text": "",
            "latency_ms": elapsed_ms,
            "batch_id": response.batch_id,
            "pipeline_status": response.status,
            "planner_model": str((llm_routes.get("planner") or {}).get("model") or ""),
            "generator_model": str((llm_routes.get("generator") or {}).get("model") or ""),
            "retry_count": retries,
            "pre_request_wait_ms": round(pre_wait, 2),
            "estimated_tokens": int((generator_runtime or {}).get("estimated_tokens") or 0),
            "used_tokens": int((generator_runtime or {}).get("used_tokens") or 0),
            "chunking": chunking if isinstance(chunking, dict) else {},
        }
    except HTTPException as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        detail = exc.detail
        detail_text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=True)
        provider_status = _parse_provider_status(detail_text)
        return {
            "status": "error",
            "http_status": int(exc.status_code),
            "provider_http_status": provider_status,
            "error_text": detail_text,
            "latency_ms": elapsed_ms,
            "batch_id": "",
            "pipeline_status": "error",
            "planner_model": "",
            "generator_model": "",
            "retry_count": 0,
            "pre_request_wait_ms": 0.0,
            "estimated_tokens": 0,
            "used_tokens": 0,
            "chunking": {},
        }


async def _run_raw_model_attempt(
    *,
    object_type: str,
    requested_count: int,
    requester: str,
) -> dict[str, Any]:
    prompt = _build_prompt(object_type, requested_count)
    settings = get_settings()
    planner_route = resolve_model_route(settings, "planner")
    generator_route = resolve_model_route(settings, "generator")
    started = time.perf_counter()
    retry_count_total = 0
    pre_wait_total = 0.0
    estimated_tokens_total = 0
    used_tokens_total = 0
    generated_rows: list[dict] = []

    try:
        plan = await run_planner(
            prompt,
            dependency_mode="match_existing_or_create_new",
            focus_object_types=[object_type],
            related_objects=[],
            reference_catalog={},
            recent_batch_context=[],
            context_notes="benchmark-mode",
            allow_fallback=False,
        )
        planner_metrics = GrokClient.get_last_call_metrics("planner")
        retry_count_total += int(planner_metrics.get("retry_count") or 0)
        pre_wait_total += float(planner_metrics.get("pre_request_wait_ms") or 0.0)
        estimated_tokens_total += int(planner_metrics.get("estimated_tokens") or 0)
        used_tokens_total += int(planner_metrics.get("used_tokens") or 0)

        estimate = _estimate_requested_record_count(prompt)
        chunk_plan = _build_chunk_plan(
            settings=settings,
            estimated_count=int(estimate.get("estimated_count", requested_count) or requested_count),
        )
        chunk_targets = list(chunk_plan.get("chunk_targets", [requested_count]) or [requested_count])
        if not chunk_plan.get("activated", False):
            chunk_targets = [requested_count]

        total_chunks = len(chunk_targets)
        for chunk_index, target_count in enumerate(chunk_targets, start=1):
            if chunk_index > 1:
                wait_seconds = max(settings.llm_auto_chunk_pacing_seconds, 0.0)
                if settings.llm_auto_chunk_pacing_jitter_seconds > 0:
                    wait_seconds += random.uniform(0.0, settings.llm_auto_chunk_pacing_jitter_seconds)
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)

            chunk_instruction = _build_chunk_instruction(
                chunk_index=chunk_index,
                chunk_total=total_chunks,
                target_count=int(target_count),
            )
            rows = await run_generator(
                plan,
                dependency_mode="match_existing_or_create_new",
                focus_object_types=[object_type],
                related_objects=[],
                reference_catalog={},
                recent_batch_context=[],
                context_notes="benchmark-mode",
                chunk_instruction=chunk_instruction,
                chunk_target_count=int(target_count),
                chunk_index=chunk_index,
                chunk_total=total_chunks,
                existing_titles=[str(item.get("title", "")).strip() for item in generated_rows[-100:]],
                allow_fallback=False,
            )
            generated_rows.extend(rows)
            generated_rows, _ = _dedupe_generated_rows(generated_rows)

            metrics = GrokClient.get_last_call_metrics("generator")
            retry_count_total += int(metrics.get("retry_count") or 0)
            pre_wait_total += float(metrics.get("pre_request_wait_ms") or 0.0)
            estimated_tokens_total += int(metrics.get("estimated_tokens") or 0)
            used_tokens_total += int(metrics.get("used_tokens") or 0)

        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return {
            "status": "ok",
            "http_status": 200,
            "provider_http_status": 200,
            "error_text": "",
            "latency_ms": elapsed_ms,
            "batch_id": "",
            "pipeline_status": "ok",
            "planner_model": planner_route.model,
            "generator_model": generator_route.model,
            "retry_count": retry_count_total,
            "pre_request_wait_ms": round(pre_wait_total, 2),
            "estimated_tokens": estimated_tokens_total,
            "used_tokens": used_tokens_total,
            "chunking": {
                "activated": bool(chunk_plan.get("activated", False)),
                "chunk_size": int(chunk_plan.get("chunk_size", settings.llm_auto_chunk_size)),
                "total_chunks": int(chunk_plan.get("total_chunks", total_chunks)),
                "chunk_targets": chunk_targets,
                "total_generated_after_dedupe": len(generated_rows),
            },
        }
    except RuntimeError as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        detail_text = str(exc)
        provider_status = _parse_provider_status(detail_text)
        return {
            "status": "error",
            "http_status": 502,
            "provider_http_status": provider_status,
            "error_text": detail_text,
            "latency_ms": elapsed_ms,
            "batch_id": "",
            "pipeline_status": "error",
            "planner_model": planner_route.model,
            "generator_model": generator_route.model,
            "retry_count": retry_count_total,
            "pre_request_wait_ms": round(pre_wait_total, 2),
            "estimated_tokens": estimated_tokens_total,
            "used_tokens": used_tokens_total,
            "chunking": {},
        }


def _is_hard_failure(event: dict[str, Any]) -> bool:
    provider = event.get("provider_http_status")
    http = event.get("http_status")
    if isinstance(provider, int) and provider >= 400:
        return True
    if isinstance(http, int) and http >= 400:
        return True
    return str(event.get("status", "ok")).lower() != "ok"


def detect_threshold_breach(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {
            "breached": False,
            "breach_reason": "",
            "breach_level": None,
            "prior_stable_level": None,
            "breach_index": None,
            "rate_429_window10": 0.0,
            "rate_400_cumulative": 0.0,
        }

    consecutive_failures = 0
    cumulative_400 = 0
    breach_index: int | None = None
    breach_reason = ""
    breach_level: int | None = None
    rate_429_window10 = 0.0
    rate_400_cumulative = 0.0

    for index, event in enumerate(events):
        provider = event.get("provider_http_status")
        if provider == 400:
            cumulative_400 += 1

        if _is_hard_failure(event):
            consecutive_failures += 1
        else:
            consecutive_failures = 0

        window = events[max(0, index - 9) : index + 1]
        if len(window) == 10:
            count_429 = sum(1 for item in window if item.get("provider_http_status") == 429)
            rate_429_window10 = count_429 / 10.0
        else:
            rate_429_window10 = 0.0

        rate_400_cumulative = cumulative_400 / float(index + 1)

        breached = False
        if len(window) == 10 and rate_429_window10 >= 0.15:
            breached = True
            breach_reason = "429_rate_window10"
        elif rate_400_cumulative >= 0.20:
            breached = True
            breach_reason = "400_rate_cumulative"
        elif consecutive_failures >= 3:
            breached = True
            breach_reason = "consecutive_failures"

        if breached:
            breach_index = index
            breach_level = int(event.get("requested_count") or 0)
            break

    if breach_index is None:
        return {
            "breached": False,
            "breach_reason": "",
            "breach_level": None,
            "prior_stable_level": max(int(item.get("requested_count") or 0) for item in events),
            "breach_index": None,
            "rate_429_window10": round(rate_429_window10, 4),
            "rate_400_cumulative": round(rate_400_cumulative, 4),
        }

    stable_events = [
        event
        for event in events[:breach_index]
        if str(event.get("status", "")).lower() == "ok"
        and int(event.get("provider_http_status") or 200) < 400
    ]
    prior_stable_level = max((int(item.get("requested_count") or 0) for item in stable_events), default=0)
    return {
        "breached": True,
        "breach_reason": breach_reason,
        "breach_level": breach_level,
        "prior_stable_level": prior_stable_level,
        "breach_index": breach_index,
        "rate_429_window10": round(rate_429_window10, 4),
        "rate_400_cumulative": round(rate_400_cumulative, 4),
    }


def _group_summary(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for event in events:
        key = (
            str(event.get("object_type", "")),
            str(event.get("path", "")),
            str(event.get("planner_model", "")),
            str(event.get("generator_model", "")),
        )
        groups.setdefault(key, []).append(event)

    rows: list[dict[str, Any]] = []
    for (object_type, path, planner_model, generator_model), items in sorted(groups.items()):
        attempts = len(items)
        ok_count = sum(1 for item in items if str(item.get("status", "")).lower() == "ok")
        provider_400 = sum(1 for item in items if item.get("provider_http_status") == 400)
        provider_429 = sum(1 for item in items if item.get("provider_http_status") == 429)
        rows.append(
            {
                "object_type": object_type,
                "path": path,
                "planner_model": planner_model,
                "generator_model": generator_model,
                "attempts": attempts,
                "success_count": ok_count,
                "error_count": attempts - ok_count,
                "success_rate": round((ok_count / attempts) if attempts else 0.0, 4),
                "provider_400_rate": round((provider_400 / attempts) if attempts else 0.0, 4),
                "provider_429_rate": round((provider_429 / attempts) if attempts else 0.0, 4),
                "avg_latency_ms": _safe_avg([float(item.get("latency_ms") or 0.0) for item in items]),
                "avg_retry_count": _safe_avg([float(item.get("retry_count") or 0.0) for item in items]),
                "avg_pre_request_wait_ms": _safe_avg(
                    [float(item.get("pre_request_wait_ms") or 0.0) for item in items]
                ),
                "avg_estimated_tokens": _safe_avg(
                    [float(item.get("estimated_tokens") or 0.0) for item in items]
                ),
                "avg_used_tokens": _safe_avg([float(item.get("used_tokens") or 0.0) for item in items]),
                "max_requested_count": max(int(item.get("requested_count") or 0) for item in items),
            }
        )
    return rows


def _build_recommendations(
    events: list[dict[str, Any]],
    threshold_rows: list[dict[str, Any]],
    *,
    default_chunk_size: int,
    default_pacing_seconds: float,
) -> list[dict[str, Any]]:
    object_types = sorted({str(item.get("object_type", "")) for item in events})
    recommendations: list[dict[str, Any]] = []

    for object_type in object_types:
        candidates = [item for item in events if str(item.get("object_type", "")) == object_type]
        if not candidates:
            continue

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in candidates:
            key = (str(item.get("path", "")), str(item.get("generator_model", "")))
            grouped.setdefault(key, []).append(item)

        best_key = None
        best_score = (-1, -1.0)
        for key, items in grouped.items():
            ok_levels = [
                int(item.get("requested_count") or 0)
                for item in items
                if str(item.get("status", "")).lower() == "ok"
                and int(item.get("provider_http_status") or 200) < 400
            ]
            max_ok = max(ok_levels, default=0)
            success_rate = sum(1 for item in items if str(item.get("status", "")).lower() == "ok") / float(len(items))
            score = (max_ok, success_rate)
            if score > best_score:
                best_score = score
                best_key = key

        preferred_path, preferred_model = best_key or ("", "")
        threshold = next(
            (
                row
                for row in threshold_rows
                if row.get("object_type") == object_type and row.get("path") == preferred_path
            ),
            None,
        )
        safe_max = int((threshold or {}).get("prior_stable_level") or best_score[0])
        provider_429_seen = any(
            item.get("provider_http_status") == 429
            for item in candidates
            if str(item.get("path", "")) == preferred_path
        )
        suggested_chunk_size = max(default_chunk_size - 1, 2) if provider_429_seen else default_chunk_size
        suggested_pacing = round(default_pacing_seconds + 0.25, 2) if provider_429_seen else default_pacing_seconds
        note = (
            f"Preferred path/model={preferred_path}/{preferred_model}. "
            f"Use safe_max={safe_max}, chunk_size={suggested_chunk_size}, pacing={suggested_pacing}s."
        )
        recommendations.append(
            {
                "object_type": object_type,
                "preferred_path": preferred_path,
                "preferred_model": preferred_model,
                "safe_max_requested_count": safe_max,
                "suggested_chunk_size": suggested_chunk_size,
                "suggested_pacing_seconds": suggested_pacing,
                "note": note,
            }
        )
    return recommendations


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True))
            handle.write("\n")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["empty"])
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _prepare_run_config(config: BenchmarkConfig, settings_snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": _utc_now(),
        "config": {
            "output_root": str(config.output_root),
            "object_types": config.object_types,
            "levels": config.levels,
            "paths": config.paths,
            "attempts_per_level": config.attempts_per_level,
            "max_attempts": config.max_attempts,
            "max_runtime_seconds": config.max_runtime_seconds,
            "random_seed": config.random_seed,
        },
        "benchmark_profile_overrides": {
            key: ("<redacted>" if "KEY" in key or "TOKEN" in key else value)
            for key, value in BENCHMARK_PROFILE_OVERRIDES.items()
        },
        "settings_snapshot": settings_snapshot,
    }


async def run_benchmark(config: BenchmarkConfig) -> dict[str, Any]:
    random.seed(config.random_seed)
    run_id = _run_id()
    run_dir = config.output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    events: list[dict[str, Any]] = []
    started = time.monotonic()
    attempt_counter = 0

    with benchmark_profile():
        settings = get_settings()
        settings_snapshot = {
            "llm_provider": settings.llm_provider,
            "llm_model_planner": settings.llm_model_planner,
            "llm_model_clarifier": settings.llm_model_clarifier,
            "llm_model_generator": settings.llm_model_generator,
            "llm_rate_guard_enabled": settings.llm_rate_guard_enabled,
            "llm_retry_max_attempts": settings.llm_retry_max_attempts,
            "llm_strict_schema_mode": settings.llm_strict_schema_mode,
            "llm_auto_chunk_enabled": settings.llm_auto_chunk_enabled,
            "llm_auto_chunk_size": settings.llm_auto_chunk_size,
            "llm_auto_chunk_max_chunks": settings.llm_auto_chunk_max_chunks,
            "benchmark_mode_enabled": settings.benchmark_mode_enabled,
        }

        for object_type in config.object_types:
            for path in config.paths:
                scenario_events: list[dict[str, Any]] = []
                stop_scenario = False
                for requested_count in config.levels:
                    if stop_scenario:
                        break
                    for _ in range(config.attempts_per_level):
                        if attempt_counter >= config.max_attempts:
                            stop_scenario = True
                            break
                        elapsed_seconds = time.monotonic() - started
                        if elapsed_seconds >= config.max_runtime_seconds:
                            stop_scenario = True
                            break

                        attempt_counter += 1
                        if path == "generate_endpoint":
                            outcome = await _run_generate_endpoint_attempt(
                                object_type=object_type,
                                requested_count=requested_count,
                                requester=f"benchmark-{run_id}",
                            )
                        else:
                            outcome = await _run_raw_model_attempt(
                                object_type=object_type,
                                requested_count=requested_count,
                                requester=f"benchmark-{run_id}",
                            )

                        event = {
                            "run_id": run_id,
                            "recorded_at": _utc_now(),
                            "attempt_index": attempt_counter,
                            "path": path,
                            "object_type": object_type,
                            "requested_count": requested_count,
                            **outcome,
                        }
                        events.append(event)
                        scenario_events.append(event)

                        breach = detect_threshold_breach(scenario_events)
                        if breach.get("breached"):
                            stop_scenario = True
                            break

        summary_rows = _group_summary(events)

        threshold_rows: list[dict[str, Any]] = []
        for object_type in config.object_types:
            for path in config.paths:
                scenario_events = [
                    event
                    for event in events
                    if event.get("object_type") == object_type and event.get("path") == path
                ]
                breach = detect_threshold_breach(scenario_events)
                threshold_rows.append(
                    {
                        "object_type": object_type,
                        "path": path,
                        "breached": breach.get("breached"),
                        "breach_reason": breach.get("breach_reason"),
                        "breach_level": breach.get("breach_level"),
                        "prior_stable_level": breach.get("prior_stable_level"),
                        "rate_429_window10": breach.get("rate_429_window10"),
                        "rate_400_cumulative": breach.get("rate_400_cumulative"),
                    }
                )

        recommendations = _build_recommendations(
            events,
            threshold_rows,
            default_chunk_size=settings.llm_auto_chunk_size,
            default_pacing_seconds=settings.llm_auto_chunk_pacing_seconds,
        )

        _write_jsonl(run_dir / "events.jsonl", events)
        _write_csv(run_dir / "summary.csv", summary_rows)
        _write_csv(run_dir / "thresholds.csv", threshold_rows)
        _write_csv(run_dir / "recommendations.csv", recommendations)
        (run_dir / "run_config.json").write_text(
            json.dumps(_prepare_run_config(config, settings_snapshot), indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

    return {
        "run_id": run_id,
        "output_dir": str(run_dir),
        "event_count": len(events),
        "summary_rows": len(summary_rows),
        "threshold_rows": len(threshold_rows),
        "recommendation_rows": len(recommendations),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run benchmark matrix for free-tier limit discovery (guardrails off, chunking on).",
    )
    parser.add_argument(
        "--output-root",
        default=str((Path(__file__).resolve().parents[2] / "data" / "benchmarks").resolve()),
        help="Output root directory for benchmark artifacts.",
    )
    parser.add_argument(
        "--object-types",
        default=",".join(DEFAULT_OBJECT_TYPES),
        help="Comma-separated object types.",
    )
    parser.add_argument(
        "--levels",
        default=",".join(str(item) for item in DEFAULT_LEVELS),
        help="Comma-separated staircase load levels.",
    )
    parser.add_argument(
        "--paths",
        default=",".join(DEFAULT_PATHS),
        help="Comma-separated paths: generate_endpoint,raw_model.",
    )
    parser.add_argument("--attempts-per-level", type=int, default=2)
    parser.add_argument("--max-attempts", type=int, default=240)
    parser.add_argument("--max-runtime-seconds", type=int, default=1800)
    parser.add_argument("--seed", type=int, default=42)
    return parser


async def _async_main(args: argparse.Namespace) -> int:
    object_types = _coerce_csv_list(args.object_types, cast=str) or DEFAULT_OBJECT_TYPES
    levels = _coerce_csv_list(args.levels, cast=int) or DEFAULT_LEVELS
    paths = _coerce_csv_list(args.paths, cast=str) or DEFAULT_PATHS
    config = BenchmarkConfig(
        output_root=Path(args.output_root).resolve(),
        object_types=object_types,
        levels=levels,
        paths=paths,
        attempts_per_level=max(int(args.attempts_per_level), 1),
        max_attempts=max(int(args.max_attempts), 1),
        max_runtime_seconds=max(int(args.max_runtime_seconds), 60),
        random_seed=int(args.seed),
    )
    result = await run_benchmark(config)
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    return asyncio.run(_async_main(args))
