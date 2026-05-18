import argparse
import asyncio
import csv
import json
import os
import subprocess
import time
import traceback
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from app.core.settings import get_settings
from app.models.schemas import ImportAssistantGenerateRequest
from app.services.batch_store import reset_batch_store
from app.services.import_assistant_service import GenerateFailureError, generate_import_assistant_batch
from app.services.perf_capture import reset_perf_capture_state_for_tests


def _load_suite(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _to_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _summarize(results: list[dict]) -> dict:
    total = len(results)
    preview_ready = sum(1 for item in results if item.get("status") == "preview_ready")
    latencies = sorted(_to_float(item.get("duration_ms")) for item in results)
    p95_index = int((len(latencies) - 1) * 0.95) if latencies else 0
    p95 = latencies[p95_index] if latencies else 0.0
    failures = Counter(
        (item.get("failure_code") or "unknown")
        for item in results
        if item.get("status") != "preview_ready"
    )
    return {
        "total": total,
        "preview_ready": preview_ready,
        "success_rate_pct": round((preview_ready / total) * 100.0, 2) if total else 0.0,
        "p95_duration_ms": p95,
        "failures": dict(failures),
    }


def _is_rate_limit_saturation(summary: dict) -> bool:
    total = int(summary.get("total", 0) or 0)
    if total <= 0:
        return False
    failure_counts = summary.get("failures", {}) or {}
    rate_limited = int(failure_counts.get("rate_limited", 0) or 0)
    return (rate_limited / total) >= 0.4


async def _run_suite(
    suite: list[dict],
    *,
    requester: str,
    pace_seconds: float,
) -> list[dict]:
    results: list[dict] = []
    for index, row in enumerate(suite, start=1):
        prompt = str(row.get("prompt", "")).strip()
        focus = str(row.get("focus", "")).strip()
        focus_types = [focus] if focus else []
        request = ImportAssistantGenerateRequest(
            prompt=prompt,
            requester=requester,
            target_environment="sandbox",
            mode="generate_validate_preview",
            focus_object_types=focus_types,
        )
        started = time.time()
        result = {
            "idx": index,
            "id": row.get("id", f"P{index:02d}"),
            "focus": focus,
            "prompt": prompt,
            "batch_id": "",
            "status": "failed",
            "failure_code": "",
            "failure_reason": "",
            "next_step": "",
            "preview_ready": False,
            "blocked": "",
            "warnings": "",
            "passed": "",
            "duration_ms": 0.0,
        }
        try:
            response = await generate_import_assistant_batch(request)
            metadata = response.metadata or {}
            failure = metadata.get("failure", {}) if isinstance(metadata, dict) else {}
            result.update(
                {
                    "batch_id": response.batch_id,
                    "status": response.status,
                    "failure_code": failure.get("failure_code", ""),
                    "failure_reason": failure.get("failure_reason", ""),
                    "next_step": failure.get("next_step", ""),
                    "preview_ready": response.status == "preview_ready",
                    "blocked": response.validation_summary.blocked,
                    "warnings": response.validation_summary.warnings,
                    "passed": response.validation_summary.passed,
                }
            )
        except GenerateFailureError as exc:
            result.update(
                {
                    "status": "failed",
                    "failure_code": exc.code,
                    "failure_reason": exc.reason,
                    "next_step": exc.next_step,
                }
            )
        except Exception as exc:  # noqa: BLE001
            trace_text = traceback.format_exc(limit=5).strip()
            result.update(
                {
                    "status": "failed",
                    "failure_code": "request_error",
                    "failure_reason": str(exc),
                    "next_step": trace_text[:600],
                }
            )
        result["duration_ms"] = round((time.time() - started) * 1000.0, 2)
        print(
            f"[{index:02d}/{len(suite):02d}] {result['id']} -> {result['status']} "
            f"({result['failure_code'] or 'ok'}) {result['duration_ms']}ms"
        )
        results.append(result)
        if pace_seconds > 0 and index < len(suite):
            await asyncio.sleep(pace_seconds)
    return results


def _write_results(path: Path, results: list[dict]) -> None:
    if not results:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Generate->Preview suite with pacing.")
    parser.add_argument(
        "--suite",
        default="data/baselines/prompt-suite-30.csv",
        help="CSV file containing id,focus,prompt columns.",
    )
    parser.add_argument(
        "--out-root",
        default="data/live-prompt-runs",
        help="Output directory root for suite runs.",
    )
    parser.add_argument(
        "--pace-seconds",
        type=float,
        default=2.0,
        help="Delay between prompts to reduce provider burst pressure.",
    )
    parser.add_argument(
        "--rerun-on-rate-limit",
        action="store_true",
        help="Run one additional attempt when first attempt is rate-limit saturated.",
    )
    parser.add_argument(
        "--rerun-wait-seconds",
        type=float,
        default=45.0,
        help="Wait before rerun when rate-limit saturation is detected.",
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Enable diagnostics mode and perf capture.",
    )
    args = parser.parse_args()

    suite_path = Path(args.suite).resolve()
    if not suite_path.exists():
        raise FileNotFoundError(f"Suite file not found: {suite_path}")
    suite = _load_suite(suite_path)

    session_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_root = Path(args.out_root).resolve()
    session_dir = out_root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    if args.diagnostics:
        perf_dir = Path("data/perf-sessions").resolve() / session_id
        perf_dir.mkdir(parents=True, exist_ok=True)
        os.environ["DIAGNOSTICS_MODE"] = "true"
        os.environ["PERF_CAPTURE_ENABLED"] = "true"
        os.environ["PERF_CAPTURE_DIR"] = str(perf_dir)
    else:
        os.environ["DIAGNOSTICS_MODE"] = "false"
        os.environ["PERF_CAPTURE_ENABLED"] = "false"
        os.environ["PERF_CAPTURE_DIR"] = ""

    get_settings.cache_clear()
    reset_batch_store()
    reset_perf_capture_state_for_tests()

    attempts: list[dict] = []

    async def run_attempt(attempt_name: str) -> tuple[list[dict], dict]:
        rows = await _run_suite(
            suite,
            requester=f"quality-suite-{attempt_name}",
            pace_seconds=max(args.pace_seconds, 0.0),
        )
        summary = _summarize(rows)
        return rows, summary

    rows_1, summary_1 = asyncio.run(run_attempt("attempt-1"))
    attempt_1_dir = session_dir / "attempt-1"
    attempt_1_dir.mkdir(exist_ok=True)
    _write_results(attempt_1_dir / "results.csv", rows_1)
    (attempt_1_dir / "summary.json").write_text(json.dumps(summary_1, indent=2), encoding="utf-8")
    attempts.append({"name": "attempt-1", "summary": summary_1})

    chosen_rows = rows_1
    chosen_summary = summary_1
    chosen_attempt = "attempt-1"

    if args.rerun_on_rate_limit and _is_rate_limit_saturation(summary_1):
        wait_seconds = max(args.rerun_wait_seconds, 0.0)
        if wait_seconds > 0:
            print(f"Rate-limit saturation detected. Waiting {wait_seconds:.1f}s before rerun...")
            time.sleep(wait_seconds)
        rows_2, summary_2 = asyncio.run(run_attempt("attempt-2"))
        attempt_2_dir = session_dir / "attempt-2"
        attempt_2_dir.mkdir(exist_ok=True)
        _write_results(attempt_2_dir / "results.csv", rows_2)
        (attempt_2_dir / "summary.json").write_text(json.dumps(summary_2, indent=2), encoding="utf-8")
        attempts.append({"name": "attempt-2", "summary": summary_2})

        if float(summary_2.get("success_rate_pct", 0.0)) >= float(summary_1.get("success_rate_pct", 0.0)):
            chosen_rows = rows_2
            chosen_summary = summary_2
            chosen_attempt = "attempt-2"

    _write_results(session_dir / "results.csv", chosen_rows)
    final_summary = {
        "session": session_id,
        "output_dir": str(session_dir),
        "suite": str(suite_path),
        "diagnostics": bool(args.diagnostics),
        "pace_seconds": max(args.pace_seconds, 0.0),
        "chosen_attempt": chosen_attempt,
        "attempts": attempts,
        **chosen_summary,
    }

    if args.diagnostics:
        summarize_script = Path(__file__).resolve().parent / "summarize-perf-session.py"
        perf_summary = {
            "status": "not_run",
            "session_dir": str(perf_dir),
        }
        if summarize_script.exists():
            command = [
                "python",
                str(summarize_script),
                "--session-dir",
                str(perf_dir),
            ]
            summary_proc = subprocess.run(command, capture_output=True, text=True, check=False)
            if summary_proc.returncode == 0:
                perf_summary = {
                    "status": "ok",
                    "session_dir": str(perf_dir),
                    "stdout": summary_proc.stdout.strip(),
                }
            else:
                perf_summary = {
                    "status": "error",
                    "session_dir": str(perf_dir),
                    "returncode": summary_proc.returncode,
                    "stdout": summary_proc.stdout.strip(),
                    "stderr": summary_proc.stderr.strip(),
                }
        else:
            perf_summary = {
                "status": "missing_summarizer",
                "session_dir": str(perf_dir),
            }
        final_summary["perf_summary"] = perf_summary

    (session_dir / "summary.json").write_text(json.dumps(final_summary, indent=2), encoding="utf-8")
    print(json.dumps(final_summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
