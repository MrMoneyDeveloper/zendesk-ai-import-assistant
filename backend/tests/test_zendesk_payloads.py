import asyncio
from urllib.parse import urlparse

from app.services import zendesk
from app.services.zendesk import (
    _build_article_payload,
    _build_macro_payload,
    _build_ticket_field_payload,
    _build_ticket_form_payload,
    _build_view_payload,
)
from app.core.settings import get_settings


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


def test_build_article_payload_defaults_to_draft_and_accepts_section_name():
    record = {
        "title": "Submit a Claim",
        "conditions": [],
        "actions": [
            {"field": "section_name", "value": "Claims"},
            {"field": "body", "value": "<p>Follow the claims intake steps.</p>"},
        ],
    }

    draft_payload, section_reference = _build_article_payload(record)
    publish_payload, _ = _build_article_payload(record, article_mode="publish")

    assert section_reference == "Claims"
    assert draft_payload["article"]["draft"] is True
    assert publish_payload["article"]["draft"] is False


def test_build_macro_payload_preserves_private_internal_note_mode():
    payload = _build_macro_payload(
        {
            "title": "Escalate High Value Claim",
            "conditions": [],
            "actions": [
                {"field": "comment_value", "value": "Assign a senior assessor."},
                {"field": "comment_mode_is_public", "value": False},
                {"field": "group_id", "value": "123"},
            ],
        }
    )

    assert payload["macro"]["actions"] == [
        {"field": "comment_value", "value": "Assign a senior assessor."},
        {"field": "comment_mode_is_public", "value": False},
        {"field": "group_id", "value": "123"},
    ]


def test_build_view_payload_preserves_unassigned_filter_and_sorting():
    payload = _build_view_payload(
        {
            "title": "All Unassigned Tickets",
            "conditions": [
                {"field": "status", "operator": "is", "value": "open"},
                {"field": "assignee_id", "operator": "is", "value": ""},
            ],
            "actions": [
                {"field": "output_columns", "value": ["status", "created", "description"]},
                {"field": "sort_by", "value": "created"},
                {"field": "sort_order", "value": "asc"},
            ],
        }
    )

    assert payload["view"]["all"][-1] == {
        "field": "assignee_id",
        "operator": "is",
        "value": "",
    }
    assert payload["view"]["output"] == {
        "columns": ["status", "created", "description"],
        "sort_by": "created",
        "sort_order": "asc",
    }


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


class _ExactUpdateClient:
    put_urls: list[str] = []
    post_urls: list[str] = []
    put_payloads: list[dict] = []
    live_updated_at = "2026-07-18T10:00:00Z"

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        if url.endswith("/api/v2/triggers/321.json"):
            return _FakeResponse(
                200,
                {
                    "trigger": {
                        "id": 321,
                        "title": "Route Enterprise Tickets",
                        "updated_at": self.live_updated_at,
                    }
                },
            )
        return _FakeResponse(200, {"triggers": []})

    async def post(self, url, **kwargs):
        self.post_urls.append(url)
        return _FakeResponse(500, {"error": "Exact update must not POST"})

    async def put(self, url, **kwargs):
        self.put_urls.append(url)
        self.put_payloads.append(kwargs.get("json", {}))
        return _FakeResponse(200, {"trigger": {"id": 321, "title": "Route Enterprise Tickets"}})


class _PaginatedCatalogClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        parsed = urlparse(url)
        path = parsed.path
        query = parsed.query
        if path == "/api/v2/triggers.json":
            if "page=2" in query:
                return _FakeResponse(
                    200,
                    {
                        "triggers": [
                            {
                                "id": 2,
                                "title": "Second trigger",
                                "active": True,
                                "conditions": {"all": [], "any": []},
                                "actions": [{"field": "set_tags", "value": "second"}],
                            }
                        ],
                        "next_page": None,
                    },
                )
            return _FakeResponse(
                200,
                {
                    "triggers": [
                        {
                            "id": 1,
                            "title": "First trigger",
                            "active": True,
                            "conditions": {
                                "all": [{"field": "status", "operator": "is", "value": "new"}],
                                "any": [],
                            },
                            "actions": [{"field": "set_tags", "value": "first"}],
                        }
                    ],
                    "next_page": "https://acme.zendesk.com/api/v2/triggers.json?page=2",
                },
            )
        roots = {
            "/api/v2/brands.json": "brands",
            "/api/v2/groups.json": "groups",
            "/api/v2/ticket_forms.json": "ticket_forms",
            "/api/v2/automations.json": "automations",
            "/api/v2/macros.json": "macros",
            "/api/v2/views.json": "views",
            "/api/v2/ticket_fields.json": "ticket_fields",
            "/api/v2/help_center/articles.json": "articles",
            "/api/v2/help_center/help_centers.json": "help_centers",
            "/api/v2/help_center/categories.json": "categories",
            "/api/v2/help_center/sections.json": "sections",
            "/api/v2/slas/policies.json": "sla_policies",
            "/api/v2/business_hours/schedules.json": "schedules",
            "/api/v2/user_fields.json": "user_fields",
            "/api/v2/organization_fields.json": "organization_fields",
            "/api/v2/custom_objects": "custom_objects",
        }
        root = roots.get(path)
        if root == "sla_policies":
            return _FakeResponse(200, {root: [{"id": 88, "title": "VIP SLA", "policy_metrics": []}]})
        if root:
            return _FakeResponse(200, {root: []})
        return _FakeResponse(404, {"error": "Not Found"})


class _Fallback404Client:
    fallback_hits = {
        "/api/v2/help_center/help_center.json": 0,
        "/api/v2/help_center.json": 0,
    }

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        path = urlparse(url).path
        if path in self.fallback_hits:
            self.fallback_hits[path] += 1
            return _FakeResponse(404, {"error": "Not Found"})
        if path == "/api/v2/help_center/help_centers.json":
            return _FakeResponse(404, {"error": "Not Found"})
        key = path.rsplit("/", 1)[-1]
        payload_key = key.replace(".json", "")
        if payload_key == "articles":
            return _FakeResponse(200, {"articles": []})
        if payload_key == "categories":
            return _FakeResponse(200, {"categories": []})
        if payload_key == "sections":
            return _FakeResponse(200, {"sections": []})
        if payload_key == "brands":
            return _FakeResponse(200, {"brands": []})
        if payload_key == "groups":
            return _FakeResponse(200, {"groups": []})
        if payload_key == "ticket_forms":
            return _FakeResponse(200, {"ticket_forms": []})
        if payload_key == "triggers":
            return _FakeResponse(200, {"triggers": []})
        if payload_key == "automations":
            return _FakeResponse(200, {"automations": []})
        if payload_key == "macros":
            return _FakeResponse(200, {"macros": []})
        if payload_key == "views":
            return _FakeResponse(200, {"views": []})
        if payload_key == "ticket_fields":
            return _FakeResponse(200, {"ticket_fields": []})
        return _FakeResponse(200, {})


class _HelpCenterHierarchyClient:
    posted_paths: list[str] = []
    posted_payloads: list[dict] = []
    posted_hosts: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        path = urlparse(url).path
        if path.endswith("categories.json"):
            return _FakeResponse(200, {"categories": []})
        if path.endswith("sections.json"):
            return _FakeResponse(200, {"sections": []})
        if path.endswith("articles.json"):
            return _FakeResponse(200, {"articles": []})
        return _FakeResponse(200, {})

    async def post(self, url, **kwargs):
        path = urlparse(url).path
        payload = kwargs.get("json", {})
        self.posted_paths.append(path)
        self.posted_payloads.append(payload)
        self.posted_hosts.append(str(urlparse(url).hostname or ""))
        if path == "/api/v2/help_center/categories.json":
            return _FakeResponse(201, {"category": {"id": 101, "name": "Claims Support"}})
        if path == "/api/v2/help_center/categories/101/sections.json":
            return _FakeResponse(201, {"section": {"id": 201, "name": "Claims"}})
        if path == "/api/v2/help_center/sections/201/articles.json":
            return _FakeResponse(201, {"article": {"id": 301, "title": "Submit a Claim"}})
        return _FakeResponse(400, {"error": "unsupported"})

    async def put(self, url, **kwargs):
        return _FakeResponse(200, {})


class _HelpCenterReadinessClient:
    has_help_center = True
    help_center_state = "enabled"
    guide_status = 200
    brand_subdomain = "acme"
    requested_hosts: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        parsed = urlparse(url)
        path = parsed.path
        self.requested_hosts.append(str(parsed.hostname or ""))
        if path == "/api/v2/brands.json":
            return _FakeResponse(
                200,
                {
                    "brands": [
                        {
                            "id": 77,
                            "name": "Main brand",
                            "subdomain": self.brand_subdomain,
                            "default": True,
                            "has_help_center": self.has_help_center,
                            "help_center_state": self.help_center_state,
                        }
                    ]
                },
            )
        if path.endswith("/categories.json"):
            return _FakeResponse(self.guide_status, {"categories": []})
        return _FakeResponse(404, {"error": "Not Found"})


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


def test_reference_catalog_paginates_and_keeps_editable_snapshots(monkeypatch):
    monkeypatch.setattr(zendesk.httpx, "AsyncClient", _PaginatedCatalogClient)
    zendesk._HELP_CENTER_FALLBACK_404_COOLDOWN.clear()

    result = asyncio.run(
        zendesk.fetch_zendesk_reference_catalog(
            subdomain="acme",
            email="admin@acme.com",
            api_token="tok_test",
        )
    )

    assert result["ok"] is True
    assert result["complete"] is True
    assert result["page_counts"]["triggers"] == 2
    assert result["catalog_counts"]["triggers"] == 2
    assert result["catalogs"]["triggers"][0]["snapshot"]["conditions"]["all"]
    assert result["catalogs"]["triggers"][0]["editable"] is True
    assert result["catalogs"]["sla_policies"][0]["editable"] is False
    assert result["sync_id"].startswith("SYNC-")


def test_exact_update_uses_bound_id_and_never_posts(monkeypatch):
    _ExactUpdateClient.put_urls = []
    _ExactUpdateClient.post_urls = []
    _ExactUpdateClient.put_payloads = []
    _ExactUpdateClient.live_updated_at = "2026-07-18T10:00:00Z"
    monkeypatch.setattr(zendesk.httpx, "AsyncClient", _ExactUpdateClient)
    records = [
        {
            "record_id": "REC-UPDATE",
            "object_type": "triggers",
            "title": "Route Enterprise Tickets",
            "operation_mode": "update",
            "target_object_id": "321",
            "target_object_type": "trigger",
            "target_updated_at": "2026-07-18T10:00:00Z",
            "import_decision": "approved",
            "deployable": True,
            "conditions": [{"field": "status", "operator": "is", "value": "new", "scope": "all"}],
            "actions": [{"field": "set_tags", "value": "enterprise vip"}],
        }
    ]

    result = asyncio.run(
        zendesk.deploy_records_to_zendesk(
            subdomain="acme",
            email="admin@acme.com",
            api_token="tok_test",
            records=records,
            on_existing="overwrite_existing",
        )
    )

    assert result["summary"]["deployed"] == 1
    assert result["results"][0]["zendesk_object_id"] == "321"
    assert _ExactUpdateClient.post_urls == []
    assert _ExactUpdateClient.put_urls[0].endswith("/api/v2/triggers/321.json")


def test_exact_update_blocks_when_live_object_changed_after_sync(monkeypatch):
    _ExactUpdateClient.put_urls = []
    _ExactUpdateClient.post_urls = []
    _ExactUpdateClient.put_payloads = []
    _ExactUpdateClient.live_updated_at = "2026-07-18T10:05:00Z"
    monkeypatch.setattr(zendesk.httpx, "AsyncClient", _ExactUpdateClient)
    records = [
        {
            "record_id": "REC-UPDATE",
            "object_type": "triggers",
            "title": "Route Enterprise Tickets",
            "operation_mode": "update",
            "target_object_id": "321",
            "target_object_type": "trigger",
            "target_updated_at": "2026-07-18T10:00:00Z",
            "import_decision": "approved",
            "deployable": True,
            "conditions": [{"field": "status", "operator": "is", "value": "new"}],
            "actions": [{"field": "set_tags", "value": "enterprise vip"}],
        }
    ]

    result = asyncio.run(
        zendesk.deploy_records_to_zendesk(
            subdomain="acme",
            email="admin@acme.com",
            api_token="tok_test",
            records=records,
            on_existing="overwrite_existing",
        )
    )

    assert result["summary"]["failed"] == 1
    assert "changed after synchronization" in result["results"][0]["execution_message"]
    assert _ExactUpdateClient.put_urls == []


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


def test_deploy_help_center_hierarchy_resolves_same_batch_names_and_keeps_articles_draft(monkeypatch):
    _HelpCenterHierarchyClient.posted_paths = []
    _HelpCenterHierarchyClient.posted_payloads = []
    _HelpCenterHierarchyClient.posted_hosts = []
    monkeypatch.setattr(zendesk.httpx, "AsyncClient", _HelpCenterHierarchyClient)
    records = [
        {
            "record_id": "REC-CAT",
            "object_type": "categories",
            "title": "Claims Support",
            "import_decision": "approved",
            "deployable": True,
            "conditions": [],
            "actions": [{"field": "locale", "value": "en-us"}],
        },
        {
            "record_id": "REC-SEC",
            "object_type": "sections",
            "title": "Claims",
            "import_decision": "approved",
            "deployable": True,
            "conditions": [],
            "actions": [{"field": "category_name", "value": "Claims Support"}],
        },
        {
            "record_id": "REC-ART",
            "object_type": "articles",
            "title": "Submit a Claim",
            "import_decision": "approved",
            "deployable": True,
            "conditions": [],
            "actions": [
                {"field": "section_name", "value": "Claims"},
                {"field": "body", "value": "<p>Follow the claims intake steps.</p>"},
            ],
        },
    ]

    result = asyncio.run(
        zendesk.deploy_records_to_zendesk(
            subdomain="acme",
            email="admin@acme.com",
            api_token="tok_test",
            records=records,
            article_mode="draft",
            help_center_base_url="https://claims-brand.zendesk.com",
        )
    )

    assert result["summary"] == {"attempted": 3, "deployed": 3, "failed": 0, "skipped": 0}
    assert _HelpCenterHierarchyClient.posted_paths == [
        "/api/v2/help_center/categories.json",
        "/api/v2/help_center/categories/101/sections.json",
        "/api/v2/help_center/sections/201/articles.json",
    ]
    article_payload = _HelpCenterHierarchyClient.posted_payloads[-1]
    assert article_payload["article"]["draft"] is True
    assert _HelpCenterHierarchyClient.posted_hosts == [
        "claims-brand.zendesk.com",
        "claims-brand.zendesk.com",
        "claims-brand.zendesk.com",
    ]


def test_help_center_readiness_requires_manual_enablement_when_brand_is_disabled(monkeypatch):
    _HelpCenterReadinessClient.has_help_center = False
    _HelpCenterReadinessClient.help_center_state = "disabled"
    _HelpCenterReadinessClient.guide_status = 404
    _HelpCenterReadinessClient.brand_subdomain = "acme"
    _HelpCenterReadinessClient.requested_hosts = []
    monkeypatch.setattr(zendesk.httpx, "AsyncClient", _HelpCenterReadinessClient)

    result = asyncio.run(
        zendesk.check_zendesk_help_center_readiness(
            subdomain="acme",
            email="admin@acme.com",
            api_token="tok_test",
            help_center_url="https://acme.zendesk.com/hc/en-us",
        )
    )

    assert result["ready"] is False
    assert result["state"] == "manual_enablement_required"
    assert result["brand"]["name"] == "Main brand"
    assert len(result["instructions"]) >= 3


def test_help_center_readiness_passes_authenticated_brand_and_guide_checks(monkeypatch):
    _HelpCenterReadinessClient.has_help_center = True
    _HelpCenterReadinessClient.help_center_state = "enabled"
    _HelpCenterReadinessClient.guide_status = 200
    _HelpCenterReadinessClient.brand_subdomain = "brand-one"
    _HelpCenterReadinessClient.requested_hosts = []
    monkeypatch.setattr(zendesk.httpx, "AsyncClient", _HelpCenterReadinessClient)

    result = asyncio.run(
        zendesk.check_zendesk_help_center_readiness(
            subdomain="acme",
            email="admin@acme.com",
            api_token="tok_test",
            help_center_url="https://brand-one.zendesk.com/hc/en-us",
        )
    )

    assert result["ready"] is True
    assert result["state"] == "ready"
    assert result["can_create_structure"] is True
    assert result["can_create_articles"] is True
    assert result["help_center_api_base_url"] == "https://brand-one.zendesk.com"
    assert _HelpCenterReadinessClient.requested_hosts[-1] == "brand-one.zendesk.com"


def test_help_center_fallback_404_paths_are_suppressed_after_first_failure(monkeypatch):
    monkeypatch.setenv("ZENDESK_FALLBACK_404_COOLDOWN_SECONDS", "3600")
    get_settings.cache_clear()
    _Fallback404Client.fallback_hits = {
        "/api/v2/help_center/help_center.json": 0,
        "/api/v2/help_center.json": 0,
    }
    monkeypatch.setattr(zendesk.httpx, "AsyncClient", _Fallback404Client)
    zendesk._HELP_CENTER_FALLBACK_404_COOLDOWN.clear()

    first = asyncio.run(
        zendesk.fetch_zendesk_reference_catalog(
            subdomain="acme",
            email="admin@acme.com",
            api_token="tok_test",
        )
    )
    second = asyncio.run(
        zendesk.fetch_zendesk_reference_catalog(
            subdomain="acme",
            email="admin@acme.com",
            api_token="tok_test",
        )
    )

    assert first["ok"] is False
    assert second["ok"] is False
    assert _Fallback404Client.fallback_hits["/api/v2/help_center/help_center.json"] == 1
    assert _Fallback404Client.fallback_hits["/api/v2/help_center.json"] == 1
    assert any("cooldown active" in warning.lower() for warning in second.get("warnings", []))
