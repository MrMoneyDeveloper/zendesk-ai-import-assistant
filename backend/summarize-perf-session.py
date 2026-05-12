import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path


def _safe_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _load_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _write_csv(path: Path, rows: list[dict]) -> None:
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


def summarize(session_dir: Path) -> dict:
    events_file = session_dir / "events.jsonl"
    events = _load_events(events_file)
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    error_class_counts: dict[str, int] = {}
    for event in events:
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        event_type = str(event.get("event_type", "")).strip() or "event"
        operation = str(payload.get("operation", payload.get("action", ""))).strip()
        path = str(payload.get("path", "")).strip()
        key = (event_type, operation, path)
        grouped.setdefault(key, []).append(event)
        error_class = str(payload.get("error_class", "")).strip().lower()
        if error_class and error_class not in {"none", "ok"}:
            error_class_counts[error_class] = error_class_counts.get(error_class, 0) + 1

    summary_rows: list[dict] = []
    for (event_type, operation, path), rows in sorted(grouped.items()):
        durations = []
        errors = 0
        status_400 = 0
        status_429 = 0
        for row in rows:
            payload = row.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}
            duration = _safe_float(payload.get("duration_ms"))
            if duration > 0:
                durations.append(duration)
            http_status = payload.get("http_status")
            if isinstance(http_status, int):
                if http_status >= 400:
                    errors += 1
                if http_status == 400:
                    status_400 += 1
                if http_status == 429:
                    status_429 += 1
            if payload.get("status") == "error" or payload.get("final_status") in {"http_error", "error"}:
                errors += 1

        avg_duration = (sum(durations) / len(durations)) if durations else 0.0
        max_duration = max(durations) if durations else 0.0
        summary_rows.append(
            {
                "event_type": event_type,
                "operation": operation,
                "path": path,
                "count": len(rows),
                "errors": errors,
                "status_400": status_400,
                "status_429": status_429,
                "avg_duration_ms": round(avg_duration, 2),
                "max_duration_ms": round(max_duration, 2),
            }
        )

    bottlenecks = sorted(summary_rows, key=lambda row: row.get("avg_duration_ms", 0.0), reverse=True)[:30]

    request_rows = [row for row in summary_rows if row.get("event_type") == "request" and row.get("path")]
    request_rows.sort(key=lambda row: row.get("avg_duration_ms", 0.0), reverse=True)
    top_bottleneck_path = request_rows[0]["path"] if request_rows else ""
    top_failure_class = ""
    if error_class_counts:
        top_failure_class = max(error_class_counts.items(), key=lambda item: item[1])[0]

    recommended_profile = "balanced_default"
    if top_failure_class in {"unsupported_response_format", "schema_validation_failure", "model_permission_blocked"}:
        recommended_profile = "compatibility_safe"
    elif top_failure_class == "rate_limited":
        recommended_profile = "rate_limited_conservative"
    elif top_bottleneck_path == "/api/import-assistant/integrations/status":
        recommended_profile = "cached_status_light_polling"

    verdict = {
        "generated_at": datetime.now(UTC).isoformat(),
        "top_failure_class": top_failure_class or "none",
        "top_bottleneck_path": top_bottleneck_path or "none",
        "recommended_immediate_config_profile": recommended_profile,
    }
    _write_csv(session_dir / "summary.csv", summary_rows)
    _write_csv(session_dir / "bottlenecks_top30.csv", bottlenecks)
    (session_dir / "session_verdict.json").write_text(
        json.dumps(verdict, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    return {
        "session_dir": str(session_dir),
        "events": len(events),
        "summary_rows": len(summary_rows),
        "bottlenecks": len(bottlenecks),
        "top_failure_class": verdict["top_failure_class"],
        "top_bottleneck_path": verdict["top_bottleneck_path"],
        "recommended_immediate_config_profile": verdict["recommended_immediate_config_profile"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize perf session events.jsonl into CSV outputs.")
    parser.add_argument("--session-dir", required=True)
    args = parser.parse_args()
    session_dir = Path(args.session_dir).resolve()
    result = summarize(session_dir)
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
