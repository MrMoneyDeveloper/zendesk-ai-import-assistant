import asyncio
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


class RecoveryAppScriptBridgeService:
    @property
    def enabled(self):
        return True

    async def invoke(self, action, payload=None, method="POST", timeout_seconds=None):
        batch_id = str((payload or {}).get("batch_id") or "")
        if action == "get_batch_preview":
            return {
                "action": action,
                "status": "ok",
                "detail": None,
                "http_status": 200,
                "data": {
                    "ok": True,
                    "batch_id": batch_id,
                    "planning_summary": {
                        "object_type": "groups",
                        "intent": "Recovered operating model",
                        "confidence": "0.9",
                    },
                    "generated_counts": {"groups": 1},
                    "validation_summary": {"passed": 1, "warnings": 0, "blocked": 0},
                    "records": [
                        {
                            "record_id": "REC-0001",
                            "object_type": "groups",
                            "title": "Claims Operations",
                            "preview_summary": "Claims support group.",
                            "validation_status": "passed",
                            "warnings": [],
                            "blocked_reason": "",
                            "import_decision": "pending_review",
                            "deployable": True,
                            "conditions": [],
                            "actions": [],
                            "deployment_status": "pending",
                            "zendesk_object_id": "",
                        }
                    ],
                },
            }
        if action == "get_batch_operational_state":
            return {
                "action": action,
                "status": "ok",
                "detail": None,
                "http_status": 200,
                "data": {
                    "ok": True,
                    "batch_id": batch_id,
                    "metadata": {
                        "status": "generating_wave_checkpoint",
                        "coverage_manifest_json": '{"enabled":true}',
                        "supervisor_json": '{"enabled":true,"call_counts":{"total":2}}',
                        "updated_at": "2026-07-25T00:00:00+00:00",
                    },
                    "progress_events": [
                        {
                            "event_id": "EVT-RECOVERY",
                            "status": "wave_execution",
                            "message": "Wave 2 completed.",
                            "wave": "2",
                            "at": "2026-07-25T00:00:00+00:00",
                        }
                    ],
                },
            }
        return {
            "action": action,
            "status": "error",
            "detail": "unsupported",
            "http_status": 400,
            "data": {},
        }


def test_missing_job_recovers_last_appscript_wave_checkpoint(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("APPS_SCRIPT_BATCH_RECOVERY_ENABLED", "true")
    monkeypatch.setenv("APPS_SCRIPT_WEB_APP_URL", "https://example.com/apps-script")
    monkeypatch.setenv("APPS_SCRIPT_API_KEY", "test-key")
    get_settings.cache_clear()
    reset_batch_store()
    monkeypatch.setattr(
        "app.routes.import_assistant.AppScriptBridgeService",
        RecoveryAppScriptBridgeService,
    )

    from app.main import app

    client = TestClient(app)
    batch_id = "BATCH-RECOVERED-CHECKPOINT"
    job_response = client.get(f"/api/import-assistant/jobs/{batch_id}")

    assert job_response.status_code == 200
    job = job_response.json()
    assert job["status"] == "failed"
    assert job["generated_counts"] == {"groups": 1}
    assert job["validation_summary"]["blocked"] == 1
    assert job["metadata"]["failure"]["failure_code"] == (
        "generation_interrupted_after_checkpoint"
    )
    assert job["metadata"]["recovery"]["source"] == "appscript_wave_checkpoint"

    preview_response = client.get(f"/api/import-assistant/preview/{batch_id}")
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["records"][0]["import_decision"] == "blocked"
    assert "runtime restarted" in preview["records"][0]["warnings"][0]


def test_generate_async_reserves_pollable_batch_before_generation(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()
    scheduled = {}

    def fake_schedule(request, batch_id):
        scheduled["batch_id"] = batch_id
        scheduled["prompt"] = request.prompt

    monkeypatch.setattr(
        "app.routes.import_assistant._schedule_reserved_generation",
        fake_schedule,
    )

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate-async",
        json={
            "prompt": "Build a department operating model for claims and finance.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "async-test-user",
        },
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "received"
    assert payload["batch_id"] == scheduled["batch_id"]
    assert scheduled["prompt"].startswith("Build a department operating model")
    assert "department manifest" in payload["status_history"][0]["message"]

    job_response = client.get(f"/api/import-assistant/jobs/{payload['batch_id']}")
    assert job_response.status_code == 200
    assert job_response.json()["status"] == "received"


def test_interrupted_async_generation_marks_reserved_batch_failed(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    from app.models.schemas import ImportAssistantGenerateRequest
    from app.routes import import_assistant as route_module
    from app.services.import_assistant_service import reserve_import_assistant_batch

    request = ImportAssistantGenerateRequest(
        prompt="Create one trigger for interruption testing.",
        target_environment="sandbox",
        mode="generate_validate_preview",
        requester="async-cancel-test",
    )
    reserved = reserve_import_assistant_batch(request)

    async def slow_preflight():
        await asyncio.sleep(60)
        return {"status": "ok"}

    monkeypatch.setattr(route_module, "_sync_schema_preflight_if_enabled", slow_preflight)

    async def cancel_run():
        task = asyncio.create_task(
            route_module._run_reserved_generation(request, reserved.batch_id)
        )
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(cancel_run())

    failed_batch = get_batch_store().get_batch(reserved.batch_id)
    assert failed_batch["status"] == "failed"
    assert failed_batch["metadata"]["failure"]["failure_code"] == "generation_interrupted"


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
        *,
        subdomain,
        email,
        api_token,
        records,
        dry_run=False,
        on_existing="create_new",
        article_mode="draft",
        help_center_base_url=None,
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


def test_deploy_runs_support_then_resumable_help_center_phase(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    deployed_type_batches: list[list[str]] = []

    async def fake_deploy_records_to_zendesk(
        *,
        subdomain,
        email,
        api_token,
        records,
        dry_run=False,
        on_existing="create_new",
        article_mode="draft",
        help_center_base_url=None,
    ):
        deployed_type_batches.append([str(row.get("object_type")) for row in records])
        results = [
            {
                "record_id": row["record_id"],
                "object_type": row["object_type"],
                "title": row["title"],
                "deployment_status": "deployed",
                "zendesk_object_id": f"ZD-{row['record_id']}",
                "execution_message": "created",
                "executed_at": "2026-01-01T00:00:00Z",
            }
            for row in records
        ]
        return {
            "summary": {
                "attempted": len(results),
                "deployed": len(results),
                "failed": 0,
                "skipped": 0,
            },
            "results": results,
            "base_url": "https://acme.zendesk.com",
            "dependency_auto_create": {"events": [], "created_count": 0},
            "sanitization_stats": {},
        }

    async def fake_help_center_readiness(**kwargs):
        return {
            "ready": True,
            "state": "ready",
            "detail": "Help Center is ready.",
            "base_url": "https://acme.zendesk.com",
            "help_center_api_base_url": "https://acme.zendesk.com",
            "help_center_url": "https://acme.zendesk.com/hc/en-us",
            "locale": "en-us",
            "brand": {"id": "7", "name": "Main brand", "has_help_center": True},
            "available_brands": [],
            "checks": [],
            "instructions": [],
            "can_create_structure": True,
            "can_create_articles": True,
        }

    monkeypatch.setattr(
        "app.services.import_assistant_service.deploy_records_to_zendesk",
        fake_deploy_records_to_zendesk,
    )
    monkeypatch.setattr(
        "app.services.import_assistant_service.check_zendesk_help_center_readiness",
        fake_help_center_readiness,
    )

    store = get_batch_store()
    records = [
        {
            "record_id": "REC-SUPPORT",
            "object_type": "triggers",
            "title": "Route Claims",
            "import_decision": "approved",
            "deployable": True,
            "deployment_status": "pending",
        },
        {
            "record_id": "REC-CATEGORY",
            "object_type": "categories",
            "title": "Claims Support",
            "import_decision": "approved",
            "deployable": True,
            "deployment_status": "pending",
        },
        {
            "record_id": "REC-SECTION",
            "object_type": "sections",
            "title": "Claims",
            "import_decision": "approved",
            "deployable": True,
            "deployment_status": "pending",
        },
        {
            "record_id": "REC-ARTICLE",
            "object_type": "articles",
            "title": "Submit a Claim",
            "import_decision": "approved",
            "deployable": True,
            "deployment_status": "pending",
        },
    ]
    store.save_batch(
        {
            "batch_id": "BATCH-PHASES",
            "status": "approved",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "records": records,
            "metadata": {},
            "status_history": [],
        }
    )

    from app.main import app

    client = TestClient(app)
    credentials = {
        "batch_id": "BATCH-PHASES",
        "subdomain": "acme",
        "email": "admin@acme.com",
        "api_token": "tok_test_123",
    }
    support_response = client.post(
        "/api/import-assistant/deploy",
        json={**credentials, "deployment_scope": "support"},
    )
    assert support_response.status_code == 200
    assert support_response.json()["status"] == "deployed_partial"
    assert support_response.json()["metadata"]["pending_help_center_count"] == 3
    assert deployed_type_batches[0] == ["triggers"]

    help_center_response = client.post(
        "/api/import-assistant/deploy",
        json={
            **credentials,
            "deployment_scope": "help_center",
            "help_center_url": "https://acme.zendesk.com/hc/en-us",
            "confirm_help_center_deploy": True,
            "article_mode": "draft",
        },
    )
    assert help_center_response.status_code == 200
    assert help_center_response.json()["status"] == "deployed"
    assert deployed_type_batches[1] == ["categories", "sections", "articles"]
    saved_records = get_batch_store().get_batch("BATCH-PHASES")["records"]
    support_record = next(row for row in saved_records if row["record_id"] == "REC-SUPPORT")
    assert support_record["zendesk_object_id"] == "ZD-REC-SUPPORT"


def test_help_center_readiness_route_returns_manual_handoff(monkeypatch):
    async def fake_readiness(**kwargs):
        return {
            "ready": False,
            "state": "manual_enablement_required",
            "detail": "Enable Help Center for Main brand.",
            "base_url": "https://acme.zendesk.com",
            "help_center_api_base_url": "https://brand-one.zendesk.com",
            "help_center_url": "https://brand-one.zendesk.com/hc/en-us",
            "locale": "en-us",
            "brand": {
                "id": "7",
                "name": "Main brand",
                "subdomain": "brand-one",
                "has_help_center": False,
            },
            "available_brands": [],
            "checks": [
                {
                    "name": "brand_help_center",
                    "status": "failed",
                    "detail": "Zendesk reports Help Center is disabled.",
                }
            ],
            "instructions": ["Enable Help Center in Zendesk Guide, then verify again."],
            "can_create_structure": False,
            "can_create_articles": False,
        }

    monkeypatch.setattr(
        "app.routes.import_assistant.check_zendesk_help_center_readiness",
        fake_readiness,
    )
    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/zendesk/help-center/readiness",
        json={
            "subdomain": "acme",
            "email": "admin@acme.com",
            "api_token": "tok_test_123",
            "help_center_url": "https://brand-one.zendesk.com/hc/en-us",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "manual_enablement_required"
    assert payload["ready"] is False
    assert payload["instructions"]


def test_generate_returns_structured_request_validation_failure(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create trigger",
            "focus_object_types": ["invalid_focus"],
        },
    )
    assert response.status_code == 422
    payload = response.json()
    detail = payload["detail"]
    assert detail["failure_stage"] == "request"
    assert detail["failure_code"] == "request_validation_failed"
    assert isinstance(detail["validation_errors"], list)
    assert detail["validation_errors"][0]["path"].startswith("focus_object_types")
    assert isinstance(detail.get("batch_id"), str)
    assert isinstance(detail.get("compaction"), dict)

    store = get_batch_store()
    failed_batch = store.get_batch(detail["batch_id"])
    assert failed_batch is not None
    assert failed_batch["status"] == "failed"
    request_validation_meta = failed_batch.get("metadata", {}).get("request_validation", {})
    assert isinstance(request_validation_meta.get("validation_errors"), list)


def test_generate_auto_compacts_oversized_reference_payload(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "triggers", "intent": prompt, "confidence": 0.9}

    async def fake_generator(plan: dict, **kwargs):
        return [
            {
                "title": "Compaction Trigger",
                "conditions": [{"field": "status", "operator": "is", "value": "new"}],
                "actions": [{"field": "set_tags", "value": "compaction_test"}],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    oversized_name = "A" * 700
    oversized_description = "B" * 2500
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create one trigger",
            "related_objects": [
                {
                    "object_type": "group",
                    "id": "g1",
                    "name": oversized_name,
                    "description": oversized_description,
                }
            ],
            "reference_catalog": {
                "groups": [
                    {
                        "object_type": "group",
                        "id": "group-1",
                        "name": oversized_name,
                        "description": oversized_description,
                    }
                ]
            },
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"


def test_job_control_updates_run_control(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "triggers", "intent": prompt, "confidence": 0.9}

    async def fake_generator(plan: dict, **kwargs):
        return [
            {
                "title": "Control Test Trigger",
                "conditions": [{"field": "status", "operator": "is", "value": "new"}],
                "actions": [{"field": "set_tags", "value": "control_test"}],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    generate_resp = client.post(
        "/api/import-assistant/generate",
        json={"prompt": "create one control test trigger"},
    )
    assert generate_resp.status_code == 200
    batch_id = generate_resp.json()["batch_id"]

    pause_resp = client.post(
        f"/api/import-assistant/jobs/{batch_id}/control",
        json={"action": "pause", "requested_by": "pytest"},
    )
    assert pause_resp.status_code == 200
    pause_payload = pause_resp.json()
    assert pause_payload["run_control"]["pause_requested"] is True
    assert pause_payload["run_control"]["updated_by"] == "pytest"

    resume_resp = client.post(
        f"/api/import-assistant/jobs/{batch_id}/control",
        json={"action": "resume", "requested_by": "pytest"},
    )
    assert resume_resp.status_code == 200
    assert resume_resp.json()["run_control"]["pause_requested"] is False


def test_checkpoint_endpoints_list_and_reject(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    store = get_batch_store()
    batch_id = "BATCH-CHECKPOINT-001"
    now = "2026-05-23T10:00:00Z"
    store.save_batch(
        {
            "batch_id": batch_id,
            "status": "generating",
            "prompt": "Create a full business setup.",
            "requester": "pytest-user",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "created_at": now,
            "updated_at": now,
            "status_history": [{"status": "generating", "message": "Wave execution in progress.", "at": now}],
            "records": [
                {
                    "record_id": "REC-0001",
                    "object_type": "groups",
                    "title": "Billing Team",
                    "preview_summary": "Group record.",
                    "validation_status": "passed",
                    "warnings": [],
                    "blocked_reason": None,
                    "import_decision": "pending_review",
                    "deployable": True,
                    "conditions": [],
                    "actions": [],
                    "deployment_status": "pending",
                    "zendesk_object_id": None,
                    "execution_message": "",
                }
            ],
            "generated_counts": {"groups": 1},
            "validation_summary": {"passed": 1, "warnings": 0, "blocked": 0},
            "planning_summary": {"object_type": "groups"},
            "metadata": {
                "run_control": {
                    "pause_requested": False,
                    "pause_after_wave": False,
                    "cancel_requested": False,
                    "cancel_reason": "",
                    "updated_at": now,
                    "updated_by": "pytest-user",
                },
                "checkpoints": [
                    {
                        "checkpoint_id": "CHK-01-AAAA1111",
                        "wave": 1,
                        "created_at": now,
                        "status": "pending",
                        "summary": {"wave": 1, "generated_counts": {"groups": 1}},
                        "preview_snapshot_ref": f"/api/import-assistant/preview/{batch_id}?checkpoint=CHK-01-AAAA1111",
                        "decision_at": None,
                        "decision_by": None,
                        "decision_note": None,
                    }
                ],
                "rollback": {},
            },
        }
    )

    checkpoints_resp = client.get(f"/api/import-assistant/jobs/{batch_id}/checkpoints")
    assert checkpoints_resp.status_code == 200
    checkpoints_payload = checkpoints_resp.json()
    assert len(checkpoints_payload["checkpoints"]) == 1
    assert checkpoints_payload["checkpoints"][0]["checkpoint_id"] == "CHK-01-AAAA1111"

    reject_resp = client.post(
        f"/api/import-assistant/jobs/{batch_id}/checkpoints/CHK-01-AAAA1111/decision",
        json={"decision": "reject", "requested_by": "pytest-user", "note": "Reject wave 1"},
    )
    assert reject_resp.status_code == 200
    reject_payload = reject_resp.json()
    assert reject_payload["checkpoint"]["status"] == "rejected"
    assert reject_payload["rollback"]["status"] in {"rollback_completed", "rollback_partial"}

    job_resp = client.get(f"/api/import-assistant/jobs/{batch_id}")
    assert job_resp.status_code == 200
    job_payload = job_resp.json()
    assert job_payload["status"] == "failed"
    assert job_payload["metadata"]["run_control"]["cancel_requested"] is True
    preview_resp = client.get(f"/api/import-assistant/preview/{batch_id}")
    assert preview_resp.status_code == 200
    assert preview_resp.json()["records"] == []


def test_generate_recovers_from_planner_failed_generation(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()
    planner_calls = {"count": 0}

    async def flaky_planner(prompt: str, **kwargs):
        planner_calls["count"] += 1
        if kwargs.get("allow_fallback") is False:
            raise RuntimeError(
                "Planner model call failed: Groq API request failed (400) [other_invalid_request]: "
                "Failed to generate JSON. See failed_generation for more details."
            )
        return {
            "object_type": "groups",
            "intent": prompt,
            "confidence": 0.92,
            "ambiguity_score": 0.1,
            "ambiguity_reasons": [],
            "clarification_questions": [],
            "dependency_notes": "",
        }

    async def fake_generator(plan: dict, **kwargs):
        return [
            {
                "object_type": "groups",
                "title": "Recovered Ops Group",
                "conditions": [],
                "actions": [],
                "dependency_notes": [],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", flaky_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create 2 support groups and include one named Recovered Ops Group.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert payload["generated_counts"]["groups"] == 1
    assert planner_calls["count"] >= 2

    job = client.get(f"/api/import-assistant/jobs/{payload['batch_id']}")
    assert job.status_code == 200
    messages = [item.get("message", "") for item in job.json().get("status_history", [])]
    assert any("deterministic JSON failure" in message for message in messages)


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


def test_generate_infers_vague_macro_without_clarification(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "macros", "intent": prompt, "confidence": 0.88}

    async def fake_generator(plan: dict, **kwargs):
        return [
            {
                "object_type": "macros",
                "title": "Reply in 25 hours",
                "conditions": [],
                "actions": [{"field": "set_tags", "value": "follow_up_25h"}],
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
            "prompt": "create reply in 25 hours macro",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert payload["needs_clarification"] is False
    assumptions = payload.get("metadata", {}).get("inference_assumptions", [])
    assert isinstance(assumptions, list)


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


def test_generate_continues_on_high_ambiguity_score(monkeypatch, tmp_path):
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
        return [
            {
                "object_type": "triggers",
                "title": "Tag new tickets",
                "conditions": [{"field": "status", "operator": "is", "value": "new"}],
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
            "prompt": "Create a trigger when status is new and add a tag.",
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


def test_generate_chunk_cap_returns_failed_with_split_guidance(monkeypatch, tmp_path):
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
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["failure_stage"] == "generate"
    assert "split this into multiple requests" in detail["failure_reason"].lower()


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
    assert detail["failure_code"] == "chunk_generation_failed"
    assert "synthetic generator chunk failure" in detail["failure_reason"].lower()


def test_generate_chunked_ticket_fields_forces_compatibility_first(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AUTO_CHUNK_ENABLED", "true")
    monkeypatch.setenv("LLM_AUTO_CHUNK_SIZE", "3")
    monkeypatch.setenv("LLM_AUTO_CHUNK_MAX_CHUNKS", "12")
    monkeypatch.setenv("LLM_AUTO_CHUNK_TRIGGER_MIN_RECORDS", "2")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_SECONDS", "0")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_JITTER_SECONDS", "0")
    get_settings.cache_clear()
    reset_batch_store()

    compatibility_flags: list[bool] = []
    compatibility_only_flags: list[bool] = []
    seen_chunks: list[int] = []

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "ticket_fields", "intent": prompt, "confidence": 0.95}

    async def fake_generator(plan: dict, **kwargs):
        chunk_index = int(kwargs.get("chunk_index") or 1)
        seen_chunks.append(chunk_index)
        compatibility_flags.append(bool(kwargs.get("compatibility_first")))
        compatibility_only_flags.append(bool(kwargs.get("compatibility_only")))
        return [
            {
                "object_type": "ticket_fields",
                "title": f"Chunk Field {chunk_index}",
                "conditions": [],
                "actions": [
                    {"field": "field_type", "value": "tagger"},
                    {
                        "field": "custom_field_options",
                        "value": [
                            {"name": "Option A", "value": "option_a"},
                            {"name": "Option B", "value": "option_b"},
                        ],
                    },
                ],
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
            "prompt": "Create 8 ticket fields for claims processing as dropdowns.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
            "focus_object_types": ["ticket_fields"],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert seen_chunks == [1, 2, 3]
    assert compatibility_flags
    assert all(compatibility_flags)
    assert compatibility_only_flags
    assert all(compatibility_only_flags)
    chunking = payload.get("metadata", {}).get("chunking", {})
    assert chunking.get("activated") is True
    for item in chunking.get("chunks", []):
        assert item.get("mode_order", []) == ["json_object", "no_response_format"]


def test_generate_business_blueprint_ticket_forms_forces_compatibility_first(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_SECONDS", "0")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_JITTER_SECONDS", "0")
    get_settings.cache_clear()
    reset_batch_store()

    compatibility_by_object: dict[str, list[bool]] = {}

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "triggers", "intent": prompt, "confidence": 0.9, "ambiguity_score": 0.21}

    async def fake_blueprint_compiler(**kwargs):  # noqa: ANN001
        return (
            {
                "mode": "compiler",
                "target_objects": [
                    {"object_type": "ticket_fields", "target_count": 2, "priority": 1, "wave": 1},
                    {"object_type": "ticket_forms", "target_count": 2, "priority": 2, "wave": 2},
                ],
                "assumptions": ["Generated from business brief"],
            },
            {"model": "test-planner"},
        )

    async def fake_generator(plan: dict, **kwargs):
        object_type = str(plan.get("object_type", "triggers"))
        compatibility_by_object.setdefault(object_type, []).append(bool(kwargs.get("compatibility_first")))
        if object_type == "ticket_fields":
            return [
                {
                    "object_type": "ticket_fields",
                    "title": "Medication Type",
                    "conditions": [],
                    "actions": [
                        {"field": "field_type", "value": "tagger"},
                        {
                            "field": "custom_field_options",
                            "value": [
                                {"name": "Acute", "value": "acute"},
                                {"name": "Chronic", "value": "chronic"},
                            ],
                        },
                    ],
                    "dependency_notes": [],
                }
            ]
        return [
            {
                "object_type": "ticket_forms",
                "title": "Medication Intake",
                "conditions": [],
                "actions": [{"field": "ticket_field_names", "value": ["Medication Type"]}],
                "dependency_notes": [],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service._run_business_blueprint_compiler", fake_blueprint_compiler)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": (
                "For medication support, create 4 ticket fields and 3 ticket forms "
                "with field mapping for intake and request routing."
            ),
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert compatibility_by_object.get("ticket_forms")
    assert all(compatibility_by_object["ticket_forms"])
    chunking = payload.get("metadata", {}).get("chunking", {})
    form_chunks = [item for item in chunking.get("chunks", []) if item.get("object_type") == "ticket_forms"]
    assert form_chunks
    for item in form_chunks:
        assert item.get("mode_order", [])[0] == "json_object"


def test_generate_business_blueprint_views_forces_compatibility_first(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_SECONDS", "0")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_JITTER_SECONDS", "0")
    get_settings.cache_clear()
    reset_batch_store()

    compatibility_by_object: dict[str, list[bool]] = {}

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "triggers", "intent": prompt, "confidence": 0.9, "ambiguity_score": 0.2}

    async def fake_blueprint_compiler(**kwargs):  # noqa: ANN001
        return (
            {
                "mode": "compiler",
                "target_objects": [
                    {"object_type": "groups", "target_count": 1, "priority": 1, "wave": 1},
                    {"object_type": "views", "target_count": 3, "priority": 2, "wave": 2},
                ],
                "assumptions": ["Generated from business brief"],
            },
            {"model": "test-planner"},
        )

    async def fake_generator(plan: dict, **kwargs):
        object_type = str(plan.get("object_type", "triggers"))
        compatibility_by_object.setdefault(object_type, []).append(bool(kwargs.get("compatibility_first")))
        if object_type == "groups":
            return [
                {
                    "object_type": "groups",
                    "title": "Claims Team",
                    "conditions": [],
                    "actions": [],
                    "dependency_notes": [],
                }
            ]
        return [
            {
                "object_type": "views",
                "title": "Claims Open Tickets",
                "conditions": [{"field": "status", "operator": "less_than", "value": "solved"}],
                "actions": [{"field": "output_columns", "value": ["status", "updated", "subject"]}],
                "dependency_notes": [],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service._run_business_blueprint_compiler", fake_blueprint_compiler)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "For claims support, create 1 group and 3 views for open work queues end-to-end.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert compatibility_by_object.get("views")
    assert all(compatibility_by_object["views"])
    chunking = payload.get("metadata", {}).get("chunking", {})
    view_chunks = [item for item in chunking.get("chunks", []) if item.get("object_type") == "views"]
    assert view_chunks
    for item in view_chunks:
        assert item.get("mode_order", [])[0] == "json_object"


def test_generate_business_blueprint_wave3_triggers_force_compatibility_only(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_SECONDS", "0")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_JITTER_SECONDS", "0")
    get_settings.cache_clear()
    reset_batch_store()

    compatibility_only_by_object: dict[str, list[bool]] = {}

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "triggers", "intent": prompt, "confidence": 0.9, "ambiguity_score": 0.2}

    async def fake_blueprint_compiler(**kwargs):  # noqa: ANN001
        return (
            {
                "mode": "compiler",
                "target_objects": [
                    {"object_type": "groups", "target_count": 1, "priority": 1, "wave": 1},
                    {"object_type": "triggers", "target_count": 4, "priority": 3, "wave": 3},
                ],
                "assumptions": ["Generated from business brief"],
            },
            {"model": "test-planner"},
        )

    async def fake_generator(plan: dict, **kwargs):
        object_type = str(plan.get("object_type", "triggers"))
        compatibility_only_by_object.setdefault(object_type, []).append(bool(kwargs.get("compatibility_only")))
        if object_type == "groups":
            return [
                {
                    "object_type": "groups",
                    "title": "Claims Team",
                    "conditions": [],
                    "actions": [],
                    "dependency_notes": [],
                }
            ]
        return [
            {
                "object_type": "triggers",
                "title": "Claims Escalation Trigger",
                "conditions": [{"field": "status", "operator": "less_than", "value": "solved"}],
                "actions": [{"field": "set_tags", "value": "claims_escalation"}],
                "dependency_notes": [],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service._run_business_blueprint_compiler", fake_blueprint_compiler)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "For claims support, create 1 group and 4 escalation triggers.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert compatibility_only_by_object.get("triggers")
    assert all(compatibility_only_by_object["triggers"])
    chunking = payload.get("metadata", {}).get("chunking", {})
    trigger_chunks = [item for item in chunking.get("chunks", []) if item.get("object_type") == "triggers"]
    assert trigger_chunks
    for item in trigger_chunks:
        assert item.get("mode_order", []) == ["json_object", "no_response_format"]


def test_generate_business_blueprint_wave3_automations_use_deterministic_fallback(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_SECONDS", "0")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_JITTER_SECONDS", "0")
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "automations", "intent": prompt, "confidence": 0.9, "ambiguity_score": 0.2}

    async def fake_blueprint_compiler(**kwargs):  # noqa: ANN001
        return (
            {
                "mode": "compiler",
                "target_objects": [
                    {"object_type": "groups", "target_count": 1, "priority": 1, "wave": 1},
                    {"object_type": "automations", "target_count": 3, "priority": 3, "wave": 3},
                ],
                "assumptions": ["Generated from business brief"],
            },
            {"model": "test-planner"},
        )

    async def failing_generator(plan: dict, **kwargs):
        object_type = str(plan.get("object_type", "triggers"))
        if object_type == "groups":
            return [
                {
                    "object_type": "groups",
                    "title": "Claims Team",
                    "conditions": [],
                    "actions": [],
                    "dependency_notes": [],
                }
            ]
        raise RuntimeError("synthetic schema failure in automations")

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service._run_business_blueprint_compiler", fake_blueprint_compiler)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", failing_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "For claims support, create 1 group and 3 automations for open ticket follow-ups.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert payload["generated_counts"].get("automations", 0) >= 1
    batch_id = payload["batch_id"]
    preview_response = client.get(f"/api/import-assistant/preview/{batch_id}")
    assert preview_response.status_code == 200
    preview_records = preview_response.json().get("records", [])
    automation_rows = [row for row in preview_records if row.get("object_type") == "automations"]
    assert automation_rows
    for row in automation_rows:
        assert row.get("actions")
        assert row.get("conditions")

    chunking = payload.get("metadata", {}).get("chunking", {})
    automation_chunks = [item for item in chunking.get("chunks", []) if item.get("object_type") == "automations"]
    assert automation_chunks
    assert all(item.get("deterministic_fallback") for item in automation_chunks)
    assert all(item.get("fallback_reason") for item in automation_chunks)
    assert all(item.get("mode_order", []) == ["json_object", "no_response_format"] for item in automation_chunks)


def test_generate_business_blueprint_wave3_rule_fails_when_fallback_builder_returns_empty(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_SECONDS", "0")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_JITTER_SECONDS", "0")
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "automations", "intent": prompt, "confidence": 0.9, "ambiguity_score": 0.2}

    async def fake_blueprint_compiler(**kwargs):  # noqa: ANN001
        return (
            {
                "mode": "compiler",
                "target_objects": [
                    {"object_type": "groups", "target_count": 1, "priority": 1, "wave": 1},
                    {"object_type": "automations", "target_count": 3, "priority": 3, "wave": 3},
                ],
                "assumptions": ["Generated from business brief"],
            },
            {"model": "test-planner"},
        )

    async def failing_generator(plan: dict, **kwargs):
        object_type = str(plan.get("object_type", "triggers"))
        if object_type == "groups":
            return [
                {
                    "object_type": "groups",
                    "title": "Claims Team",
                    "conditions": [],
                    "actions": [],
                    "dependency_notes": [],
                }
            ]
        raise RuntimeError("synthetic schema failure in automations")

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service._run_business_blueprint_compiler", fake_blueprint_compiler)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", failing_generator)
    monkeypatch.setattr("app.services.import_assistant_service._build_deterministic_chunk_rows", lambda **kwargs: [])
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "For claims support, create 1 group and 3 automations for open ticket follow-ups.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["failure_stage"] == "generate"
    assert detail["failure_code"] == "wave_generation_failed"
    assert "Deterministic fallback could not build valid automations rows" in detail["failure_reason"]


def test_generate_business_blueprint_ticket_fields_uses_deterministic_fallback_on_chunk_failure(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_SECONDS", "0")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_JITTER_SECONDS", "0")
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "triggers", "intent": prompt, "confidence": 0.9, "ambiguity_score": 0.2}

    async def fake_blueprint_compiler(**kwargs):  # noqa: ANN001
        return (
            {
                "mode": "compiler",
                "target_objects": [
                    {"object_type": "groups", "target_count": 1, "priority": 1, "wave": 1},
                    {"object_type": "ticket_fields", "target_count": 4, "priority": 1, "wave": 1},
                ],
                "assumptions": ["Generated from business brief"],
            },
            {"model": "test-planner"},
        )

    async def failing_generator(plan: dict, **kwargs):
        object_type = str(plan.get("object_type", "triggers"))
        if object_type == "groups":
            return [
                {
                    "object_type": "groups",
                    "title": "Claims Group",
                    "conditions": [],
                    "actions": [],
                    "dependency_notes": [],
                }
            ]
        raise RuntimeError("synthetic json failure in ticket_fields")

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service._run_business_blueprint_compiler", fake_blueprint_compiler)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", failing_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "For claims, create 1 group and 4 ticket fields as dropdown values.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert payload["generated_counts"].get("ticket_fields", 0) >= 1
    chunking = payload.get("metadata", {}).get("chunking", {})
    field_chunks = [item for item in chunking.get("chunks", []) if item.get("object_type") == "ticket_fields"]
    assert field_chunks
    assert all(item.get("deterministic_fallback") for item in field_chunks)
    assert all(item.get("mode_order", []) == ["json_object", "no_response_format"] for item in field_chunks)


def test_generate_business_blueprint_orchestration_uses_single_parent_batch(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_SECONDS", "0")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_JITTER_SECONDS", "0")
    get_settings.cache_clear()
    reset_batch_store()
    generator_calls: list[str] = []

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "triggers", "intent": prompt, "confidence": 0.88, "ambiguity_score": 0.22}

    async def fake_blueprint_compiler(**kwargs):  # noqa: ANN001
        return (
            {
                "mode": "compiler",
                "target_objects": [
                    {"object_type": "groups", "target_count": 2, "priority": 1, "wave": 1},
                    {"object_type": "ticket_fields", "target_count": 2, "priority": 1, "wave": 1},
                    {"object_type": "triggers", "target_count": 2, "priority": 3, "wave": 3},
                ],
                "assumptions": ["Generated from business brief"],
            },
            {"model": "test-planner"},
        )

    async def fake_generator(plan: dict, **kwargs):
        object_type = str(plan.get("object_type", "triggers"))
        generator_calls.append(object_type)
        return [
            {
                "object_type": object_type,
                "title": f"{object_type}-item-{len(generator_calls)}",
                "conditions": [],
                "actions": [{"field": "set_tags", "value": f"{object_type}_generated"}],
                "dependency_notes": [],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service._run_business_blueprint_compiler", fake_blueprint_compiler)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": (
                "For our claims business, create 2 groups, 2 ticket fields, and 2 triggers "
                "as an end-to-end setup."
            ),
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    orchestration = payload.get("metadata", {}).get("orchestration", {})
    assert orchestration.get("mode") == "business_blueprint"
    assert len(orchestration.get("waves", [])) >= 2
    assert payload["generated_counts"].get("groups", 0) >= 1
    assert payload["generated_counts"].get("ticket_fields", 0) >= 1
    assert payload["generated_counts"].get("triggers", 0) >= 1
    assert "groups" in generator_calls
    assert "ticket_fields" in generator_calls
    assert "triggers" in generator_calls


def test_department_supervisor_retries_only_rejected_source_chunk(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("GEMINI_SUPERVISOR_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_SUPERVISOR_REVIEW_GROUPING", "department")
    monkeypatch.setenv("GEMINI_SUPERVISOR_MAX_REGENERATION_RETRIES", "1")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_SECONDS", "0")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_JITTER_SECONDS", "0")
    get_settings.cache_clear()
    reset_batch_store()
    review_counts: dict[str, int] = {}

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "triggers", "intent": prompt, "confidence": 0.9, "ambiguity_score": 0.1}

    async def fake_blueprint_compiler(**kwargs):  # noqa: ANN001
        from app.services.import_assistant_service import _build_department_coverage_manifest

        manifest = _build_department_coverage_manifest(
            prompt=kwargs["prompt"],
            focus_object_types=["groups", "ticket_forms", "triggers"],
        )
        return (
            {
                "mode": "compiler",
                "coverage_manifest": manifest,
                "target_objects": [],
                "assumptions": [],
            },
            {"model": "test-planner"},
        )

    async def fake_supervisor_review(self, **kwargs):  # noqa: ANN001
        review_scope = kwargs.get("review_scope") or {}
        department = str(review_scope.get("department_name") or "unknown")
        scope_types = ",".join(review_scope.get("object_types", []) or [])
        review_key = f"{department}:{scope_types}"
        review_counts[review_key] = review_counts.get(review_key, 0) + 1
        reject_initial_claims = (
            department == "Claims & Incidents"
            and scope_types == "triggers"
            and review_counts[review_key] == 1
        )
        chunk_specs = list(review_scope.get("chunk_requirements", []) or [])
        assessments = [
            {
                "chunk_id": spec["chunk_id"],
                "approved": not reject_initial_claims,
                "quality_score": 0.95 if not reject_initial_claims else 0.4,
                "blocking_issues": ["Retry this source chunk."] if reject_initial_claims else [],
                "requires_regeneration": reject_initial_claims,
            }
            for spec in chunk_specs
        ]
        return {
            "status": "reviewed",
            "reason": "",
            "records": kwargs["records"],
            "review": {
                "approved": not reject_initial_claims,
                "quality_score": 0.95 if not reject_initial_claims else 0.4,
                "context_gaps": [],
                "dependency_issues": [],
                "chunk_assessments": assessments,
                "patches": [],
                "memory_delta": ["Untrusted model memory must not be committed."],
                "public_reasoning_summary": "Department trigger review.",
                "requires_regeneration": reject_initial_claims,
            },
            "patch_summary": {"applied": 0, "rejected": 0, "patch_results": []},
            "latency_ms": 1,
        }

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    async def fail_generator(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("Use deterministic department fallback in this test.")

    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fail_generator)
    monkeypatch.setattr(
        "app.services.import_assistant_service._run_business_blueprint_compiler",
        fake_blueprint_compiler,
    )
    monkeypatch.setattr(
        "app.services.gemini_supervisor.GeminiSupervisor.review_and_patch_chunk",
        fake_supervisor_review,
    )
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr(
        "app.services.import_assistant_service.AppScriptBridgeService",
        StubAppScriptBridgeService,
    )
    monkeypatch.setattr(
        "app.routes.import_assistant.AppScriptBridgeService",
        StubAppScriptBridgeService,
    )

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": (
                "Build a Zendesk operating model.\n"
                "Departments:\n"
                "- Customer Support: handles general customer requests.\n"
                "- Claims & Incidents: handles claims and safety incidents.\n\n"
                "Ticket forms for General Support and Claims & Incidents.\n"
                "Create triggers to route each department."
            ),
            "focus_object_types": ["groups", "ticket_forms", "triggers"],
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    supervisor = payload["metadata"]["supervisor"]
    assert payload["generated_counts"]["triggers"] == 6
    assert review_counts["Customer Support:triggers"] == 1
    assert review_counts["Claims & Incidents:triggers"] == 2
    assert supervisor["call_counts"]["consolidated"] == 6
    assert supervisor["call_counts"]["retry"] == 1
    assert supervisor["blocked_chunk_ids"] == []
    assert all("Untrusted model memory" not in item for item in supervisor["memory"])
    assert len(supervisor["verified_memory"]) == 10
    get_settings.cache_clear()


def test_generate_business_blueprint_wave_failure_uses_deterministic_fallback(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_SECONDS", "0")
    monkeypatch.setenv("LLM_AUTO_CHUNK_PACING_JITTER_SECONDS", "0")
    get_settings.cache_clear()
    reset_batch_store()
    stage_calls = {"count": 0}

    class StageTrackingSheetsService(StubSheetsService):
        def stage_batch(self, batch, planning, preview_records):  # noqa: ANN001
            stage_calls["count"] += 1
            return {"sheet_enabled": False, "message": "unexpected"}

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "triggers", "intent": prompt, "confidence": 0.9, "ambiguity_score": 0.2}

    async def fake_blueprint_compiler(**kwargs):  # noqa: ANN001
        return (
            {
                "mode": "compiler",
                "target_objects": [
                    {"object_type": "groups", "target_count": 1, "priority": 1, "wave": 1},
                    {"object_type": "triggers", "target_count": 1, "priority": 3, "wave": 3},
                ],
                "assumptions": [],
            },
            {"model": "test-planner"},
        )

    async def failing_wave_generator(plan: dict, **kwargs):
        object_type = str(plan.get("object_type", "triggers"))
        if object_type == "groups":
            raise RuntimeError("synthetic wave generation failure")
        return [
            {
                "object_type": "triggers",
                "title": "Claims Trigger",
                "conditions": [{"field": "status", "operator": "less_than", "value": "solved"}],
                "actions": [{"field": "set_tags", "value": "claims_route"}],
                "dependency_notes": [],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service._run_business_blueprint_compiler", fake_blueprint_compiler)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", failing_wave_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StageTrackingSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "For this business create one group and one trigger for claims routing.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert payload["generated_counts"].get("groups", 0) >= 1
    assert payload["generated_counts"].get("triggers", 0) >= 1
    assert stage_calls["count"] == 1

    batches = get_batch_store().list_batches()
    assert batches
    latest = sorted(batches, key=lambda item: item.get("updated_at", ""), reverse=True)[0]
    assert latest["status"] == "preview_ready"
    statuses = [item.get("status") for item in latest.get("status_history", [])]
    assert "staging" in statuses


def test_generate_single_item_explicit_prompt_bypasses_planner(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    async def fail_if_planner_called(prompt: str, **kwargs):
        raise AssertionError("Planner should be bypassed for explicit single-item prompts.")

    async def fake_generator(plan: dict, **kwargs):
        return [
            {
                "object_type": "triggers",
                "title": "Billing Route",
                "conditions": [{"field": "status", "operator": "is", "value": "open"}],
                "actions": [{"field": "set_tags", "value": "billing_open"}],
                "dependency_notes": [],
            }
        ]

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fail_if_planner_called)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create one trigger named Billing Route that adds tag billing_open on open tickets.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert payload.get("metadata", {}).get("planning", {}).get("planner_bypassed") is True


def test_generate_planner_rate_limited_fails_fast_without_generator(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()
    planner_calls = {"count": 0}

    async def rate_limited_planner(prompt: str, **kwargs):
        planner_calls["count"] += 1
        raise RuntimeError("Planner model call failed: Groq API request failed (429) [rate_limited]: too many requests")

    async def fail_if_generator_called(plan: dict, **kwargs):
        raise AssertionError("Generator should not be called when planner is rate-limited.")

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", rate_limited_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fail_if_generator_called)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create billing views for high priority unresolved tickets",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["failure_code"] == "rate_limited"
    assert planner_calls["count"] == 1


def test_generate_generator_rate_limited_maps_failure_code(monkeypatch, tmp_path):
    from app.services.generator import GeneratorStructuredOutputError

    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "triggers", "intent": prompt, "confidence": 0.95, "ambiguity_score": 0.1}

    async def rate_limited_generator(plan: dict, **kwargs):
        raise GeneratorStructuredOutputError(
            "Generator rate limited before producing valid output.",
            attempts=[{"mode": "json_object", "http_status": 429, "error_class": "rate_limited"}],
            error_class="rate_limited",
            provider_error_code="rate_limit_exceeded",
        )

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", rate_limited_generator)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create one trigger named Billing Route and add tag billing_open.",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["failure_code"] == "rate_limited"
    assert "rate-limit" in detail["next_step"].lower()


def test_generate_post_generated_canonicalization_error_is_typed_failure(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "triggers", "intent": prompt, "confidence": 0.95, "ambiguity_score": 0.1}

    async def fake_generator(plan: dict, **kwargs):
        return [
            {
                "object_type": "triggers",
                "title": "Route Billing",
                "conditions": [{"field": "status", "operator": "is", "value": "open"}],
                "actions": [{"field": "set_tags", "value": "billing"}],
            }
        ]

    def fail_canonicalize(**kwargs):  # noqa: ANN001
        raise ValueError("synthetic canonicalization failure")

    monkeypatch.setattr("app.services.import_assistant_service.run_planner", fake_planner)
    monkeypatch.setattr("app.services.import_assistant_service.run_generator", fake_generator)
    monkeypatch.setattr("app.services.import_assistant_service._canonicalize_generated_rows", fail_canonicalize)
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create one billing trigger with tag billing",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["failure_stage"] == "generate"
    assert detail["failure_code"] == "generate_canonicalization_failed"
    assert "canonicalization failed" in detail["failure_reason"].lower()

    batches = get_batch_store().list_batches()
    assert batches
    latest = sorted(batches, key=lambda item: item.get("updated_at", ""), reverse=True)[0]
    assert latest["status"] == "failed"
    assert latest["metadata"]["failure"]["failure_code"] == "generate_canonicalization_failed"


def test_generate_route_catch_all_returns_typed_runtime_error(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    async def boom(_request):  # noqa: ANN001
        raise ValueError("unexpected route-level failure")

    monkeypatch.setattr("app.routes.import_assistant.generate_import_assistant_batch", boom)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create one trigger",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["failure_stage"] == "generate"
    assert detail["failure_code"] == "generate_runtime_error"
    assert "unhandled generation error" in detail["failure_reason"].lower()


def test_generate_pre_request_validated_failure_marks_batch_failed(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    def fail_context_bundle(**kwargs):  # noqa: ANN001
        raise RuntimeError("synthetic pre-validation context failure")

    monkeypatch.setattr(
        "app.services.import_assistant_service._build_llm_context_bundle",
        fail_context_bundle,
    )
    monkeypatch.setattr("app.services.import_assistant_service.SheetsService", StubSheetsService)
    monkeypatch.setattr("app.services.import_assistant_service.AppScriptBridgeService", StubAppScriptBridgeService)
    monkeypatch.setattr("app.routes.import_assistant.AppScriptBridgeService", StubAppScriptBridgeService)

    from app.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-assistant/generate",
        json={
            "prompt": "Create one trigger",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
        },
    )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["failure_code"] == "generate_runtime_error"
    assert "initialization failed" in detail["failure_reason"].lower()

    batches = get_batch_store().list_batches()
    assert batches
    latest = sorted(batches, key=lambda item: item.get("updated_at", ""), reverse=True)[0]
    assert latest["status"] == "failed"
    assert latest["metadata"]["failure"]["failure_code"] == "generate_runtime_error"
    statuses = [item.get("status") for item in latest.get("status_history", [])]
    assert "failed" in statuses


def test_generate_ticket_form_sparse_output_infers_fields_from_prompt(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str, **kwargs):
        return {"object_type": "ticket_forms", "intent": prompt, "confidence": 0.91}

    async def fake_generator(plan: dict, **kwargs):
        return [
            {
                "object_type": "ticket_forms",
                "title": "Claims Intake Form",
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
            "prompt": "Create ticket form called Claims Intake Form including fields Claim Status and Claim Number",
            "target_environment": "sandbox",
            "mode": "generate_validate_preview",
            "requester": "pytest-user",
            "reference_catalog": {
                "ticket_fields": [
                    {"id": "101", "name": "Claim Status", "object_type": "ticket_field"},
                    {"id": "102", "name": "Claim Number", "object_type": "ticket_field"},
                ]
            },
            "focus_object_types": ["ticket_forms"],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "preview_ready"
    assert payload["validation_summary"]["blocked"] == 0

    preview = client.get(f"/api/import-assistant/preview/{payload['batch_id']}")
    assert preview.status_code == 200
    records = preview.json()["records"]
    assert records
    form_record = records[0]
    assert form_record["object_type"] == "ticket_forms"
    assert form_record["blocked_reason"] is None
    action_fields = {item["field"] for item in form_record.get("actions", [])}
    assert "ticket_field_ids" in action_fields or "ticket_field_names" in action_fields
