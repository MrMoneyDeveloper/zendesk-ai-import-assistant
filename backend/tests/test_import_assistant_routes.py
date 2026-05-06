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


def test_generate_preview_and_approve_flow(monkeypatch, tmp_path):
    store_file = tmp_path / "batches.json"
    monkeypatch.setenv("BATCH_STORE_FILE", str(store_file))
    get_settings.cache_clear()
    reset_batch_store()

    async def fake_planner(prompt: str):
        return {"object_type": "triggers", "intent": prompt, "confidence": 0.91}

    async def fake_generator(plan: dict):
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

    assert Path(store_file).exists()
