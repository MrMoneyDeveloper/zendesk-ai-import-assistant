import asyncio

from app.services import zendesk
from app.services.zendesk import _build_ticket_field_payload, _build_ticket_form_payload


def test_build_ticket_field_payload_accepts_aliases_and_sets_defaults():
    record = {
        "title": "Test",
        "conditions": [],
        "actions": [
            {"field": "type", "value": "dropdown"},
            {"field": "options", "value": "tested, not tested"},
        ],
    }

    payload = _build_ticket_field_payload(record)
    ticket_field = payload["ticket_field"]
    assert ticket_field["title"] == "Test"
    assert ticket_field["type"] == "tagger"
    assert ticket_field["custom_field_options"] == [
        {"name": "tested", "value": "tested"},
        {"name": "not tested", "value": "not_tested"},
    ]
    assert ticket_field["agent_can_edit"] is True
    assert ticket_field["visible_in_portal"] is True
    assert ticket_field["editable_in_portal"] is False
    assert ticket_field["required"] is False
    assert ticket_field["required_in_portal"] is False


def test_build_ticket_form_payload_collects_numeric_ids_and_named_refs():
    record = {
        "title": "Claims Form",
        "conditions": [],
        "actions": [
            {"field": "ticket_fields", "value": ["123", "Claim Number", 456]},
            {"field": "ticket_field_names", "value": "Policy Number"},
        ],
    }
    payload, names = _build_ticket_form_payload(record)

    assert payload["ticket_form"]["name"] == "Claims Form"
    assert payload["ticket_form"]["ticket_field_ids"] == [123, 456]
    assert names == ["Claim Number", "Policy Number"]


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.is_success = 200 <= status_code < 300
        self.text = "{}"

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        self.created_groups = {}
        self.group_id_seed = 900

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        if url.endswith("/api/v2/groups.json"):
            groups = [
                {"id": int(group_id), "name": name}
                for name, group_id in self.created_groups.items()
            ]
            return _FakeResponse(200, {"groups": groups})
        return _FakeResponse(200, {})

    async def post(self, url, **kwargs):
        payload = kwargs.get("json", {})
        if url.endswith("/api/v2/groups.json"):
            name = str(payload.get("group", {}).get("name", "")).strip()
            self.group_id_seed += 1
            created_id = str(self.group_id_seed)
            if name:
                self.created_groups[name.lower()] = created_id
            return _FakeResponse(201, {"group": {"id": int(created_id), "name": name}})
        if url.endswith("/api/v2/triggers.json"):
            trigger = payload.get("trigger", {})
            return _FakeResponse(201, {"trigger": {"id": 555, "title": trigger.get("title", "")}})
        return _FakeResponse(400, {"error": "unsupported"})

    async def put(self, url, **kwargs):
        return _FakeResponse(200, {})


def test_deploy_records_auto_creates_missing_group_dependency(monkeypatch):
    monkeypatch.setattr(zendesk.httpx, "AsyncClient", _FakeAsyncClient)

    records = [
        {
            "record_id": "REC-0001",
            "object_type": "triggers",
            "title": "Route Billing",
            "import_decision": "approved",
            "deployable": True,
            "conditions": [{"field": "status", "operator": "is", "value": "new"}],
            "actions": [{"field": "group_id", "value": "Billing"}],
        }
    ]

    result = asyncio.run(
        zendesk.deploy_records_to_zendesk(
            subdomain="acme",
            email="admin@acme.com",
            api_token="tok_test",
            records=records,
            dry_run=False,
            on_existing="create_new",
        )
    )

    assert result["summary"]["deployed"] == 1
    assert result["summary"]["failed"] == 0
    assert result["dependency_auto_create"]["created_count"] >= 1
    assert any(
        event.get("object_type") == "groups"
        and str(event.get("title", "")).lower() == "billing"
        for event in result["dependency_auto_create"]["events"]
    )


def test_deploy_records_fails_with_explicit_fix_for_non_creatable_dependency(monkeypatch):
    monkeypatch.setattr(zendesk.httpx, "AsyncClient", _FakeAsyncClient)

    records = [
        {
            "record_id": "REC-0001",
            "object_type": "triggers",
            "title": "Brand Scoped Trigger",
            "import_decision": "approved",
            "deployable": True,
            "conditions": [{"field": "brand_id", "operator": "is", "value": "Brand Alpha"}],
            "actions": [{"field": "set_tags", "value": "brand_alpha"}],
        }
    ]

    result = asyncio.run(
        zendesk.deploy_records_to_zendesk(
            subdomain="acme",
            email="admin@acme.com",
            api_token="tok_test",
            records=records,
            dry_run=False,
            on_existing="create_new",
        )
    )

    assert result["summary"]["failed"] == 1
    assert result["results"][0]["deployment_status"] == "failed"
    assert "cannot be auto-created" in result["results"][0]["execution_message"].lower()
