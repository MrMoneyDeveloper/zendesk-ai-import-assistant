from pathlib import Path

from fastapi.testclient import TestClient

from app.core.settings import get_settings
from app.services.batch_store import reset_batch_store


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
