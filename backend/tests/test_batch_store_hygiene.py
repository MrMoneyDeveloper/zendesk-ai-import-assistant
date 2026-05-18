from app.core.settings import get_settings
from app.services.batch_store import BatchStore, get_batch_store, reset_batch_store


def test_batch_store_prunes_to_max_entries(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("BATCH_STORE_MAX_ENTRIES", "25")
    monkeypatch.setenv("DIAGNOSTICS_MODE", "false")
    monkeypatch.setenv("BATCH_STORE_TRIM_RUNTIME_METADATA", "true")
    get_settings.cache_clear()
    reset_batch_store()
    store = get_batch_store()

    for idx in range(1, 27):
        minute = f"{idx:02d}"
        store.save_batch(
            {
                "batch_id": f"B{idx}",
                "status": "preview_ready",
                "created_at": f"2026-05-10T00:{minute}:00+00:00",
                "updated_at": f"2026-05-10T00:{minute}:00+00:00",
                "status_history": [],
                "metadata": {},
            }
        )

    ids = {item.get("batch_id") for item in store.list_batches()}
    assert len(ids) == 25
    assert "B1" not in ids
    assert "B26" in ids


def test_batch_store_compacts_llm_runtime_in_lean_mode(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("BATCH_STORE_MAX_ENTRIES", "20")
    monkeypatch.setenv("DIAGNOSTICS_MODE", "false")
    monkeypatch.setenv("BATCH_STORE_TRIM_RUNTIME_METADATA", "true")
    get_settings.cache_clear()
    reset_batch_store()
    store = get_batch_store()

    store.save_batch(
        {
            "batch_id": "B1",
            "status": "failed",
            "created_at": "2026-05-10T00:00:00+00:00",
            "updated_at": "2026-05-10T00:00:00+00:00",
            "status_history": [],
            "metadata": {
                "llm_runtime": {
                    "planner": {"model": "qwen/qwen3-32b"},
                    "generator": {
                        "model": "openai/gpt-oss-20b",
                        "http_status": 400,
                        "final_status": "http_error",
                        "retry_count": 1,
                        "pre_request_wait_ms": 12000.0,
                        "wait_reason": "header_budget",
                        "error_class": "schema_validation_failure",
                        "provider_error_code": "json_validate_failed",
                    },
                    "generator_error": {
                        "attempts": [{"mode": "json_object"}],
                        "failed_generation_excerpt": "raw payload",
                        "provider_error_code": "json_validate_failed",
                        "validator_reason": "not parseable",
                    },
                    "terminal_error_class": "schema_validation_failure",
                }
            },
        }
    )

    batch = store.get_batch("B1")
    assert batch
    runtime = batch["metadata"]["llm_runtime"]
    assert "planner" in runtime
    assert "generator" in runtime
    assert "generator_error" in runtime
    assert runtime["generator_error"]["provider_error_code"] == "json_validate_failed"
    assert "attempts" not in runtime["generator_error"]
    assert "failed_generation_excerpt" not in runtime["generator_error"]


def test_batch_store_keeps_verbose_runtime_in_diagnostics_mode(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("BATCH_STORE_MAX_ENTRIES", "20")
    monkeypatch.setenv("DIAGNOSTICS_MODE", "true")
    monkeypatch.setenv("BATCH_STORE_TRIM_RUNTIME_METADATA", "true")
    get_settings.cache_clear()
    reset_batch_store()
    store = get_batch_store()

    store.save_batch(
        {
            "batch_id": "B1",
            "status": "failed",
            "created_at": "2026-05-10T00:00:00+00:00",
            "updated_at": "2026-05-10T00:00:00+00:00",
            "status_history": [],
            "metadata": {
                "llm_runtime": {
                    "generator_error": {
                        "attempts": [{"mode": "json_object"}],
                        "failed_generation_excerpt": "raw payload",
                    }
                }
            },
        }
    )

    batch = store.get_batch("B1")
    assert batch
    runtime = batch["metadata"]["llm_runtime"]
    assert runtime["generator_error"]["attempts"][0]["mode"] == "json_object"
    assert runtime["generator_error"]["failed_generation_excerpt"] == "raw payload"
