from app.services.usage_telemetry import (
    build_usage_report,
    record_model_call,
    reset_usage_session,
    start_usage_session,
)


def test_usage_report_aggregates_provider_tokens_and_elapsed_time():
    session, token = start_usage_session("pytest")
    try:
        record_model_call(
            {
                "provider": "Groq",
                "task": "generator",
                "model": "model-a",
                "final_status": "ok",
                "attempt_count": 1,
                "retry_count": 0,
                "input_tokens": 100,
                "output_tokens": 40,
                "total_tokens": 140,
                "elapsed_ms": 250,
            }
        )
        record_model_call(
            {
                "provider": "Gemini",
                "task": "supervisor",
                "model": "model-b",
                "final_status": "reviewed",
                "attempt_count": 2,
                "retry_count": 1,
                "input_tokens": 200,
                "output_tokens": 30,
                "thought_tokens": 10,
                "total_tokens": 240,
                "elapsed_ms": 400,
            }
        )
        report = build_usage_report(session, total_elapsed_ms=1000)
    finally:
        reset_usage_session(token)

    assert report["totals"]["calls"] == 2
    assert report["totals"]["retries"] == 1
    assert report["totals"]["input_tokens"] == 300
    assert report["totals"]["output_tokens"] == 70
    assert report["totals"]["thought_tokens"] == 10
    assert report["totals"]["total_tokens"] == 380
    assert report["totals"]["model_call_elapsed_ms_sum"] == 650
    assert {item["provider"] for item in report["by_provider"]} == {"Gemini", "Groq"}


def test_usage_report_derives_pipeline_phase_timings():
    session, token = start_usage_session("pytest-phases")
    try:
        session.started_at_epoch = 100.0
        history = [
            {"status": "wave_execution", "at": "1970-01-01T00:01:41+00:00"},
            {"status": "generated", "at": "1970-01-01T00:01:44+00:00"},
            {"status": "staging", "at": "1970-01-01T00:01:45+00:00"},
            {"status": "preview_ready", "at": "1970-01-01T00:01:47+00:00"},
        ]
        report = build_usage_report(session, status_history=history, total_elapsed_ms=7000)
    finally:
        reset_usage_session(token)

    assert report["phase_timings"] == {
        "planning_and_backlog_ms": 1000.0,
        "generation_and_supervision_ms": 3000.0,
        "post_processing_ms": 1000.0,
        "staging_and_validation_ms": 2000.0,
    }
