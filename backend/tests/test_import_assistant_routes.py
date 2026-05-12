from pathlib import Path

from fastapi.testclient import TestClient

from app.core.settings import get_settings
from app.services.batch_store import get_batch_store, reset_batch_store


class StubSheetsService:
    @property
    def enabled(self):
        return False

    def stage_batch(self, batch, planning, preview_records):
        return {"sheet_enabled": False, "message": "stubbed"}

    def write_validation_log(self, batch_id, records):
        return 0

    def write_approval_log(self, batch_id, decisions):
        return 0


class StubAppScriptBridgeService:
    @property
    def enabled(self):
        return False

    async def invoke(self, action, payload=None, method="POST"):
        return {
            "action": action,
            "status": "skipped",
            "detail": "stubbed",
            "http_status": None,
            "data": {},
        }


class CountingAppScriptBridgeService:
    call_count = 0

    @property
    def enabled(self):
        return True

    async def invoke(self, action, payload=None, method="POST", timeout_seconds=None):
        if action == "health":
            CountingAppScriptBridgeService.call_count += 1
        return {
            "action": action,
            "status": "ok",
            "detail": None,
            "http_status": 200,
            "data": {},
        }


def test_generate_preview_and_approve_flow(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "triggers", "intent": prompt, "confidence": 0.91}

    async def fake_generator(plan: dict, **kwargs):
        return [
            {
                "title": "Route Claims",
                "conditions": [{"field": "group", "operator": "is", "value": "claims"}],
                "actions": [{"field": "assign", "value": "Claims Team"}],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    async def fake_deploy_records_to_zendesk(
        *, subdomain, email, api_token, records, dry_run=False, on_existing="create_new"
    ):
        return {
            "summary": {
                "attempted": 1,
                "deployed": 1,
                "failed": 0,
                "skipped": 0,
            },
            "results": [
                {
                    "record_id": "REC-0001",
                    "object_type": "triggers",
                    "title": "Route Claims",
                    "deployment_status": "deployed",
                    "zendesk_object_id": "123456",
                    "execution_message": "Trigger created successfully.",
                    "executed_at": "2026-01-01T00:00:00Z",
                }
            ],
            "base_url": "https://acme.zendesk.com",
        }

    monkeypatch.setattr(
        "app.services.import_assistant_service.deploy_records_to_zendesk",
        fake_deploy_records_to_zendesk,
    )

    from app.main import app

    client = TestClient(app)

    generate_resp = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create claims routing setup",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert generate_resp.status_code == 200
    payload = generate_resp.json()
    batch_id = payload["batch_id"]
    assert payload["status"] == "preview_ready"
    assert payload["generated_counts"]["triggers"] == 1
    assert "llm_routes" in payload.get("metadata", {})
    assert "clarifier" in payload["metadata"]["llm_routes"]
    assert "llm_runtime" in payload.get("metadata", {})

    job_resp = client.get(f"/api/import-assistant/jobs/{batch_id}")
    assert job_resp.status_code == 200
    assert job_resp.json()["status"] == "preview_ready"

    preview_resp = client.get(f"/api/import-assistant/preview/{batch_id}")
    assert preview_resp.status_code == 200
    records = preview_resp.json()["records"]
    assert len(records) == 1
    assert records[0]["record_id"].startswith("REC-")

    approve_resp = client.post(
        "/api/import-assistant/approve",
        json={
            "batch_id": batch_id,
            "approved_by": "pytest-user",
            "records": [{"record_id": records[0]["record_id"], "import_decision": "approved"}],
        },
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["summary"]["approved"] == 1

    deploy_resp = client.post(
        "/api/import-assistant/deploy",
        json={
            "batch_id": batch_id,
            "subdomain": "acme",
            "email": "admin@acme.com",
            "api_token": "tok_test_123",
            "dry_run": False,
        },
    )
    assert deploy_resp.status_code == 200
    deploy_payload = deploy_resp.json()
    assert deploy_payload["status"] == "deployed"
    assert deploy_payload["summary"]["deployed"] == 1

    assert Path(store_file).exists()


def test_integrations_status_uses_cached_health(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("INTEGRATIONS_HEALTH_CACHE_SECONDS", "60")
    monkeypatch.setenv("APPS_SCRIPT_HEALTH_TIMEOUT_SECONDS", "1")
    get_settings.cache_clear()
    reset_batch_store()
    CountingAppScriptBridgeService.call_count = 0

    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", CountingAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.SheetsService", StubSheetsService)

    from app.main import app
    from app.routes import import_assistant as route_module

    route_module._INTEGRATIONS_HEALTH_CACHE["at_monotonic"] = 0.0
    route_module._INTEGRATIONS_HEALTH_CACHE["at_iso"] = None
    route_module._INTEGRATIONS_HEALTH_CACHE["payload"] = None

    client = TestClient(app)
    first = client.get("/api/import-assistant/integrations/status")
    second = client.get("/api/import-assistant/integrations/status")

    assert first.status_code == 200
    assert second.status_code == 200
    assert CountingAppScriptBridgeService.call_count == 1
    second_payload = second.json()
    assert second_payload["appscript"]["health_source"] == "cache"


def test_generate_requires_clarification_for_vague_macro(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "macros", "intent": prompt, "confidence": 0.88}

    async def fake_generator(plan: dict, **kwargs):
        raise AssertionError("Generator should not run when clarification is required.")

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "create reply in 25 hours macro",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "clarification_required"
    assert payload["needs_clarification"] is True
    assert len(payload["clarification_questions"]) >= 1
    first_question = payload["clarification_questions"][0]
    assert "understood so far" in first_question["reason"].lower()
    assert len(first_question.get("examples", [])) == 1


def test_benchmark_mode_bypasses_clarification_and_generation_safety(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("BENCHMARK_MODE", "true")
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {
            "object_type": "triggers",
            "intent": prompt,
            "confidence": 0.2,
            "ambiguity_score": 0.99,
            "ambiguity_reasons": ["Synthetic ambiguity for benchmark test."],
            "clarification_questions": [],
        }

    async def fake_generator(plan: dict, **kwargs):
        return [
            {
                "title": "BENCH Trigger 1",
                "object_type": "triggers",
                "conditions": [{"field": "status", "operator": "is", "value": "new"}],
                "actions": [{"field": "set_tags", "value": "bench_tag"}],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "benchmark prompt intentionally vague",
            "mode": "benchmark",
            "target_environment": "sandbox",
            "requester": "pytest-benchmark",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert payload["needs_clarification"] is False
    assert payload["metadata"]["benchmark"]["enabled"] is True
    assert payload["metadata"]["generation_safety"]["blocked"] is False
    assert payload["metadata"]["generation_safety"]["bypassed"] is True


def test_generate_requires_clarification_on_high_ambiguity_score(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AMBIGUITY_THRESHOLD", "0.55")
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {
            "object_type": "triggers",
            "intent": prompt,
            "confidence": 0.86,
            "ambiguity_score": 0.92,
            "ambiguity_reasons": ["No explicit target group or ownership strategy was provided."],
            "clarification_questions": [],
        }

    async def fake_generator(plan: dict, **kwargs):
        raise AssertionError("Generator should not run when ambiguity threshold is exceeded.")

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create a trigger when status is new and add a tag.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "clarification_required"
    assert payload["needs_clarification"] is True
    assert len(payload["clarification_questions"]) >= 1


def test_explicit_trigger_prompt_bypasses_clarification_loop(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AMBIGUITY_THRESHOLD", "0.55")
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {
            "object_type": "triggers",
            "intent": prompt,
            "confidence": 0.66,
            "ambiguity_score": 0.93,
            "ambiguity_reasons": ["Model marked this as ambiguous."],
            "clarification_questions": [
                "Should this apply to all groups or only specific groups?",
                "Should this apply to all forms or only specific forms?",
            ],
        }

    async def fake_generator(plan: dict, **kwargs):
        return [
            {
                "object_type": "triggers",
                "title": "Apply test tag to open tickets",
                "conditions": [{"field": "status", "operator": "is", "value": "open"}],
                "actions": [{"field": "set_tags", "value": "test"}],
                "dependency_notes": [],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Make a trigger for any open ticket, all groups, all forms, all statuses, and set tag is test.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert payload["needs_clarification"] is False
    assert payload["generated_counts"]["triggers"] == 1


def test_attachment_extract_endpoint_supports_text(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/attachments/extract",
        files={"file": ("context.txt", b"line 1\nline 2", "text/plain")},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"] == "context.txt"
    assert payload["mime_type"] == "text/plain"
    assert payload["char_count"] > 0
    assert "line 1" in payload["extracted_text"]
    assert payload["truncated"] is False


def test_deploy_blocks_when_all_approved_rows_are_non_deployable(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    store = get_batch_store()
    store.save_batch(
        {
            "batch_id": "BATCH-SAFETY-001",
            "status": "approved",
            "created_at": "2026-05-10T00:00:00Z",
            "updated_at": "2026-05-10T00:00:00Z",
            "requester": "pytest-user",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "status_history": [],
            "generated_counts": {"triggers": 1},
            "validation_summary": {"passed": 0, "warnings": 0, "blocked": 1},
            "records": [
                {
                    "record_id": "REC-0001",
                    "object_type": "triggers",
                    "title": "Fallback Trigger",
                    "preview_summary": "Trigger configuration record.",
                    "validation_status": "failed",
                    "warnings": [],
                    "blocked_reason": "Generation safety gate blocked deployment.",
                    "import_decision": "approved",
                    "deployable": False,
                    "conditions": [{"field": "status", "operator": "is", "value": "new"}],
                    "actions": [{"field": "set_tags", "value": "fallback"}],
                    "deployment_status": "pending",
                    "zendesk_object_id": None,
                    "execution_message": "",
                }
            ],
            "metadata": {
                "generation_safety": {
                    "blocked": True,
                    "reasons": ["Generator fallback output detected. Regenerate before deployment."],
                }
            },
        }
    )

    from app.main import app

    client = TestClient(app)
    deploy_response = client.post(
        "/api/import-assistant/deploy",
        json={
            "batch_id": "BATCH-SAFETY-001",
            "subdomain": "acme",
            "email": "admin@acme.com",
            "api_token": "tok_test_123",
            "dry_run": False,
        },
    )
    assert deploy_response.status_code == 400
    detail = deploy_response.json()["detail"]
    assert detail["failure_stage"] == "deploy"
    assert "Deployment blocked" in detail["failure_reason"]


def test_generate_dropdown_field_prompt_is_canonicalized_to_tagger(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "ticket_fields", "intent": prompt, "confidence": 0.93}

    async def fake_generator(plan: dict, **kwargs):
        return [
            {
                "object_type": "ticket_fields",
                "title": "Test",
                "conditions": [],
                "actions": [
                    {"field": "type", "value": "dropdown"},
                    {"field": "options", "value": "tested, not tested"},
                ],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Make me a field called Test make it a dropdown and the 2 values are tested and not tested",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert payload["generated_counts"]["ticket_fields"] == 1

    preview = client.get(f"/api/import-assistant/preview/{payload['batch_id']}")
    assert preview.status_code == 200
    record = preview.json()["records"][0]
    actions = {item["field"]: item["value"] for item in record["actions"]}
    assert actions["field_type"] == "tagger"
    assert actions["custom_field_options"] == [
        {"name": "tested", "value": "tested"},
        {"name": "not tested", "value": "not_tested"},
    ]
    assert record["deployable"] is True


def test_generate_single_item_prompt_stays_non_chunked(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AUTO_CHUNK_ENABLED", "true")
    monkeypatch.setenv("LLM_AUTO_CHUNK_SIZE", "6")
    monkeypatch.setenv("LLM_AUTO_CHUNK_MAX_CHUNKS", "12")
    monkeypatch.setenv("LLM_AUTO_CHUNK_TRIGGER_MIN_RECORDS", "7")
    get_settings.cache_clear()
    reset_batch_store()
    call_count = {"value": 0}

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "groups", "intent": prompt, "confidence": 0.95}

    async def fake_generator(plan: dict, **kwargs):
        call_count["value"] += 1
        return [
            {
                "object_type": "groups",
                "title": "Claims Team",
                "conditions": [],
                "actions": [],
                "dependency_notes": [],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create one support group named Claims Team for local routing.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert call_count["value"] == 1
    chunking = payload.get("metadata", {}).get("chunking", {})
    assert chunking.get("activated") is False
    assert chunking.get("final_status") == "single_pass"


def test_generate_multi_item_prompt_runs_chunked(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AUTO_CHUNK_ENABLED", "true")
    monkeypatch.setenv("LLM_AUTO_CHUNK_SIZE", "6")
    monkeypatch.setenv("LLM_AUTO_CHUNK_MAX_CHUNKS", "12")
    monkeypatch.setenv("LLM_AUTO_CHUNK_TRIGGER_MIN_RECORDS", "7")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_SECONDS", "0")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_JITTER_SECONDS", "0")
    get_settings.cache_clear()
    reset_batch_store()
    seen_chunks = []

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "groups", "intent": prompt, "confidence": 0.95}

    async def fake_generator(plan: dict, **kwargs):
        chunk_index = int(kwargs.get("chunk_index") or 1)
        seen_chunks.append(chunk_index)
        return [
            {
                "object_type": "groups",
                "title": f"Chunk Group {chunk_index}",
                "conditions": [],
                "actions": [],
                "dependency_notes": [],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create 20 support groups for staged rollout with consistent naming.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert seen_chunks == [1, 2, 3, 4]
    chunking = payload.get("metadata", {}).get("chunking", {})
    assert chunking.get("activated") is True
    assert chunking.get("total_chunks") == 4
    assert len(chunking.get("chunks", [])) == 4
    assert chunking.get("final_status") == "ok"


def test_generate_chunk_cap_returns_clarification(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AUTO_CHUNK_ENABLED", "true")
    monkeypatch.setenv("LLM_AUTO_CHUNK_SIZE", "6")
    monkeypatch.setenv("LLM_AUTO_CHUNK_MAX_CHUNKS", "12")
    monkeypatch.setenv("LLM_AUTO_CHUNK_TRIGGER_MIN_RECORDS", "7")
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "groups", "intent": prompt, "confidence": 0.95}

    async def fake_generator(plan: dict, **kwargs):
        raise AssertionError("Generator should not run when chunk cap is exceeded.")

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create 100 support groups for staged rollout with consistent naming.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "clarification_required"
    assert payload["needs_clarification"] is True
    assert "split this into multiple requests" in payload["clarification_questions"][0]["question"].lower()


def test_generate_chunk_failure_aborts_whole_run(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AUTO_CHUNK_ENABLED", "true")
    monkeypatch.setenv("LLM_AUTO_CHUNK_SIZE", "6")
    monkeypatch.setenv("LLM_AUTO_CHUNK_MAX_CHUNKS", "12")
    monkeypatch.setenv("LLM_AUTO_CHUNK_TRIGGER_MIN_RECORDS", "7")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_SECONDS", "0")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_JITTER_SECONDS", "0")
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "groups", "intent": prompt, "confidence": 0.95}

    async def fake_generator(plan: dict, **kwargs):
        chunk_index = int(kwargs.get("chunk_index") or 1)
        if chunk_index == 2:
            raise RuntimeError("Synthetic generator chunk failure.")
        return [
            {
                "object_type": "groups",
                "title": f"Chunk Group {chunk_index}",
                "conditions": [],
                "actions": [],
                "dependency_notes": [],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create 20 support groups for staged rollout with consistent naming.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["failure_stage"] == "generate"
    assert "chunked generation aborted" in detail["failure_reason"].lower()
