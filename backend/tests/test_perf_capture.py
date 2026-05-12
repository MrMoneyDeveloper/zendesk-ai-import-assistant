import json

from app.core.settings import get_settings
from app.services.perf_capture import emit_perf_event, reset_perf_capture_state_for_tests


def test_perf_capture_writes_jsonl_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("PERF_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("PERF_CAPTURE_DIR", str(tmp_path))
    get_settings.cache_clear()
    reset_perf_capture_state_for_tests()

    emit_perf_event("test_event", {"value": 42})

    events_file = tmp_path / "events.jsonl"
    assert events_file.exists()
    lines = events_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event_type"] == "test_event"
    assert payload["payload"]["value"] == 42


def test_perf_capture_noop_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("PERF_CAPTURE_ENABLED", "false")
    monkeypatch.setenv("PERF_CAPTURE_DIR", str(tmp_path))
    get_settings.cache_clear()
    reset_perf_capture_state_for_tests()

    emit_perf_event("test_event", {"value": 99})
    assert not (tmp_path / "events.jsonl").exists()
