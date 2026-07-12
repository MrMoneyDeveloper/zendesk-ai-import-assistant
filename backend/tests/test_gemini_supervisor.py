import asyncio
import json
from types import SimpleNamespace

import httpx

from app.services.gemini_supervisor import (
    GeminiSupervisor,
    _retry_delay_seconds,
    apply_supervisor_patches,
    evaluate_supervisor_bundle,
)


def test_retry_delay_uses_gemini_retry_info():
    response = httpx.Response(
        429,
        json={
            "error": {
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "4.75s",
                    }
                ]
            }
        },
    )

    assert _retry_delay_seconds(response, fallback=1.0) == 4.75


def test_supervisor_retries_rate_limit_before_parsing_review(monkeypatch):
    reviewed_payload = {
        "approved": True,
        "quality_score": 0.9,
        "context_gaps": [],
        "dependency_issues": [],
        "chunk_assessments": [],
        "patches": [],
        "memory_delta": [],
        "public_reasoning_summary": "Coverage and dependencies checked.",
        "requires_regeneration": False,
    }
    responses = [
        httpx.Response(429, headers={"Retry-After": "0"}, json={"error": {}}),
        httpx.Response(200, json={"output_text": json.dumps(reviewed_payload)}),
    ]

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return responses.pop(0)

    monkeypatch.setattr("app.services.gemini_supervisor.httpx.AsyncClient", FakeAsyncClient)
    supervisor = GeminiSupervisor()
    supervisor.settings = SimpleNamespace(
        gemini_api_key="test-key",
        gemini_supervisor_model="test-model",
        gemini_supervisor_timeout_seconds=1.0,
        gemini_supervisor_min_request_interval_seconds=0.0,
        gemini_supervisor_rate_limit_retries=1,
    )

    review = asyncio.run(
        supervisor._call_review(
            prompt="Review records",
            records=[],
            object_type="groups",
            wave=2,
            wave_position=2,
            chunk_index=1,
            chunk_total=1,
            blueprint={},
            supervisor_memory=[],
            reference_catalog={},
            review_scope={},
            cumulative_records=[],
            remaining_manifest_coverage={},
        )
    )

    assert review["approved"] is True
    assert review["quality_score"] == 0.9
    assert responses == []


def test_supervisor_retries_malformed_structured_output(monkeypatch):
    reviewed_payload = {
        "approved": True,
        "quality_score": 0.88,
        "context_gaps": [],
        "dependency_issues": [],
        "chunk_assessments": [],
        "patches": [],
        "memory_delta": [],
        "public_reasoning_summary": "Review recovered after malformed output.",
        "requires_regeneration": False,
    }
    responses = [
        httpx.Response(200, json={"output_text": "not-json"}),
        httpx.Response(200, json={"output_text": json.dumps(reviewed_payload)}),
    ]

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return responses.pop(0)

    monkeypatch.setattr("app.services.gemini_supervisor.httpx.AsyncClient", FakeAsyncClient)
    supervisor = GeminiSupervisor()
    supervisor.settings = SimpleNamespace(
        gemini_api_key="test-key",
        gemini_supervisor_model="test-model",
        gemini_supervisor_timeout_seconds=1.0,
        gemini_supervisor_min_request_interval_seconds=0.0,
        gemini_supervisor_rate_limit_retries=1,
    )

    review = asyncio.run(
        supervisor._call_review(
            prompt="Review records",
            records=[],
            object_type="groups",
            wave=2,
            wave_position=2,
            chunk_index=1,
            chunk_total=1,
            blueprint={},
            supervisor_memory=[],
            reference_catalog={},
            review_scope={},
            cumulative_records=[],
            remaining_manifest_coverage={},
        )
    )

    assert review["quality_score"] == 0.88
    assert responses == []


def test_supervisor_retries_transport_timeout(monkeypatch):
    reviewed_payload = {
        "approved": True,
        "quality_score": 0.91,
        "context_gaps": [],
        "dependency_issues": [],
        "chunk_assessments": [],
        "patches": [],
        "memory_delta": [],
        "public_reasoning_summary": "Review recovered after timeout.",
        "requires_regeneration": False,
    }
    responses = [
        httpx.ReadTimeout("synthetic timeout"),
        httpx.Response(200, json={"output_text": json.dumps(reviewed_payload)}),
    ]

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr("app.services.gemini_supervisor.httpx.AsyncClient", FakeAsyncClient)
    supervisor = GeminiSupervisor()
    supervisor.settings = SimpleNamespace(
        gemini_api_key="test-key",
        gemini_supervisor_model="test-model",
        gemini_supervisor_timeout_seconds=1.0,
        gemini_supervisor_min_request_interval_seconds=0.0,
        gemini_supervisor_rate_limit_retries=1,
    )

    review = asyncio.run(
        supervisor._call_review(
            prompt="Review records",
            records=[],
            object_type="groups",
            wave=2,
            wave_position=2,
            chunk_index=1,
            chunk_total=1,
            blueprint={},
            supervisor_memory=[],
            reference_catalog={},
            review_scope={},
            cumulative_records=[],
            remaining_manifest_coverage={},
        )
    )

    assert review["quality_score"] == 0.91
    assert responses == []


def test_supervisor_applies_safe_tag_and_dependency_note():
    rows = [
        {
            "object_type": "triggers",
            "title": "Route Claims",
            "conditions": [],
            "actions": [{"field": "set_tags", "value": "claims"}],
            "dependency_notes": [],
        }
    ]
    review = {
        "patches": [
            {
                "operation": "add_tag",
                "target_index": 0,
                "value": "vip claims",
                "reason": "Prompt references VIP claim handling.",
            },
            {
                "operation": "add_dependency_note",
                "target_title": "Route Claims",
                "value": "Depends on the VIP field created earlier.",
                "reason": "Carry dependency context forward.",
            },
        ]
    }

    patched, summary = apply_supervisor_patches(rows, review, auto_apply=True)

    assert summary["applied"] == 2
    assert summary["rejected"] == 0
    assert patched[0]["actions"][0]["value"] == "claims vip_claims"
    assert patched[0]["dependency_notes"] == [
        "Gemini supervisor: Depends on the VIP field created earlier."
    ]


def test_supervisor_invalid_record_key_does_not_fallback_to_index():
    rows = [
        {
            "object_type": "articles",
            "title": "Payment Help",
            "conditions": [],
            "actions": [{"field": "body", "value": "Original body"}],
            "_supervisor_record_key": "BL-1:1:0",
        }
    ]
    review = {
        "patches": [
            {
                "operation": "set_article_body",
                "record_key": "BL-unknown:1:0",
                "target_index": 0,
                "value": "Wrong target body",
            }
        ]
    }

    patched, summary = apply_supervisor_patches(rows, review, auto_apply=True)

    assert patched == rows
    assert summary["applied"] == 0
    assert summary["patch_results"][0]["reject_reason"] == "target_not_found"


def test_supervisor_rejects_title_collision_with_prior_bundle():
    rows = [
        {
            "object_type": "articles",
            "title": "Enterprise Fleet Support: Required Information 2",
            "conditions": [],
            "actions": [{"field": "body", "value": "Substantive body"}],
        }
    ]
    review = {
        "patches": [
            {
                "operation": "normalize_title",
                "target_index": 0,
                "value": "Enterprise Fleet Support: Required Information",
            }
        ]
    }

    patched, summary = apply_supervisor_patches(
        rows,
        review,
        auto_apply=True,
        reserved_titles={
            "articles": ["Enterprise Fleet Support: Required Information"],
        },
    )

    assert patched == rows
    assert summary["applied"] == 0
    assert summary["patch_results"][0]["reject_reason"] == "duplicate_title"


def test_supervisor_rejects_unsafe_action_patch():
    rows = [
        {
            "object_type": "triggers",
            "title": "Route Claims",
            "conditions": [],
            "actions": [],
        }
    ]
    review = {
        "patches": [
            {
                "operation": "add_action",
                "target_index": 0,
                "field": "group_id",
                "value": "12345",
                "reason": "Unsafe ID replacement should not auto-apply.",
            }
        ]
    }

    patched, summary = apply_supervisor_patches(rows, review, auto_apply=True)

    assert patched == rows
    assert summary["applied"] == 0
    assert summary["rejected"] == 1
    assert summary["patch_results"][0]["reject_reason"] == "unsafe_action"


def test_supervisor_normalizes_title():
    rows = [
        {
            "object_type": "macros",
            "title": "refund macro",
            "conditions": [],
            "actions": [],
        }
    ]
    review = {
        "patches": [
            {
                "operation": "normalize_title",
                "target_index": 0,
                "value": "Refund Follow-up Macro",
                "reason": "Use a clearer title.",
            }
        ]
    }

    patched, summary = apply_supervisor_patches(rows, review, auto_apply=True)

    assert summary["applied"] == 1
    assert patched[0]["title"] == "Refund Follow-up Macro"


def test_supervisor_merges_typed_actions_without_duplicates():
    rows = [
        {
            "object_type": "views",
            "title": "Claims Queue",
            "conditions": [{"field": "group_id", "operator": "is", "value": "Claims"}],
            "actions": [
                {"field": "output_columns", "value": ["status", "priority"]},
                {"field": "output_columns", "value": ["status"]},
            ],
        }
    ]
    review = {
        "patches": [
            {
                "operation": "add_view_output_columns",
                "target_index": 0,
                "value": ["priority", "assignee"],
                "reason": "Complete the view columns.",
            }
        ]
    }

    patched, summary = apply_supervisor_patches(rows, review, auto_apply=True)

    assert summary["applied"] == 1
    output_actions = [item for item in patched[0]["actions"] if item["field"] == "output_columns"]
    assert output_actions == [{"field": "output_columns", "value": ["status", "priority", "assignee"]}]


def test_supervisor_accepts_list_tags_and_replaces_article_body():
    rows = [
        {
            "object_type": "articles",
            "title": "Payment Help",
            "conditions": [],
            "actions": [
                {"field": "body", "value": "old"},
                {"field": "set_tags", "value": "payments"},
            ],
        }
    ]
    review = {
        "patches": [
            {"operation": "add_tag", "target_index": 0, "value": ["billing support", "payments"]},
            {"operation": "set_article_body", "target_index": 0, "value": "new body"},
        ]
    }

    patched, summary = apply_supervisor_patches(rows, review, auto_apply=True)

    assert summary["applied"] == 2
    assert [item for item in patched[0]["actions"] if item["field"] == "body"] == [
        {"field": "body", "value": "new body"}
    ]
    assert [item for item in patched[0]["actions"] if item["field"] == "set_tags"] == [
        {"field": "set_tags", "value": "payments billing_support"}
    ]


def test_supervisor_name_routing_requires_verified_reference():
    rows = [{"object_type": "triggers", "title": "Route Claims", "conditions": [], "actions": []}]
    review = {
        "patches": [
            {
                "operation": "set_group_action_by_name",
                "target_index": 0,
                "value": "Claims & Incidents",
            },
            {
                "operation": "set_ticket_form_condition_by_name",
                "target_index": 0,
                "value": "Claims Intake",
            },
        ]
    }

    patched, summary = apply_supervisor_patches(
        rows,
        review,
        auto_apply=True,
        allowed_references={
            "groups": ["Claims & Incidents"],
            "ticket_forms": ["Claims Intake"],
        },
    )

    assert summary["applied"] == 2
    assert {"field": "group_id", "value": "Claims & Incidents"} in patched[0]["actions"]
    assert {
        "field": "ticket_form_id",
        "operator": "is",
        "value": "Claims Intake",
    } in patched[0]["conditions"]


def test_supervisor_gate_caps_high_score_when_mandatory_structure_is_missing():
    rows = [
        {
            "object_type": "triggers",
            "title": "Claims Marker",
            "conditions": [{"field": "status", "operator": "is", "value": "new"}],
            "actions": [{"field": "set_tags", "value": "claims"}],
            "_supervisor_chunk_id": "BL-1:1",
            "_supervisor_record_key": "BL-1:1:0",
        }
    ]
    review = {
        "approved": True,
        "quality_score": 0.95,
        "requires_regeneration": False,
        "chunk_assessments": [
            {
                "chunk_id": "BL-1:1",
                "approved": True,
                "quality_score": 0.95,
                "blocking_issues": [],
                "requires_regeneration": False,
            }
        ],
    }

    gate = evaluate_supervisor_bundle(
        rows=rows,
        chunk_specs=[{"chunk_id": "BL-1:1", "object_type": "triggers", "target_count": 1}],
        review=review,
        approval_threshold=0.8,
        allowed_references={"groups": ["Claims & Incidents"], "ticket_forms": []},
    )

    assessment = gate["chunk_assessments"][0]
    assert assessment["raw_quality_score"] == 0.95
    assert assessment["effective_quality_score"] == 0.49
    assert assessment["effective_approved"] is False
    assert any("group routing action" in reason for reason in assessment["approval_gate_reasons"])
