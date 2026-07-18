import asyncio
import json
from types import SimpleNamespace

import httpx

from app.services.gemini_supervisor import (
    GeminiSupervisor,
    _extract_interaction_usage,
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
        httpx.Response(
            200,
            json={
                "output_text": json.dumps(reviewed_payload),
                "usage": {
                    "total_input_tokens": 210,
                    "total_output_tokens": 35,
                    "total_thought_tokens": 5,
                    "total_tokens": 250,
                },
            },
        ),
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
    assert review["_telemetry"]["attempt_count"] == 2
    assert review["_telemetry"]["retry_count"] == 1
    assert review["_telemetry"]["input_tokens"] == 210
    assert review["_telemetry"]["output_tokens"] == 35
    assert review["_telemetry"]["thought_tokens"] == 5
    assert review["_telemetry"]["total_tokens"] == 250
    assert responses == []


def test_interaction_usage_supports_official_usage_fields():
    assert _extract_interaction_usage(
        {
            "usage": {
                "total_input_tokens": 100,
                "total_output_tokens": 20,
                "total_thought_tokens": 10,
                "total_cached_tokens": 5,
                "total_tool_use_tokens": 3,
                "total_tokens": 133,
            }
        }
    ) == {
        "input_tokens": 100,
        "output_tokens": 20,
        "thought_tokens": 10,
        "cached_tokens": 5,
        "tool_use_tokens": 3,
        "total_tokens": 133,
    }


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
            "actions": [{"field": "current_tags", "value": "claims"}],
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
    assert patched[0]["actions"][0] == {
        "field": "current_tags",
        "value": "claims vip_claims",
    }
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
    replacement_body = (
        "Gather the payment reference, statement date, transaction amount, and supporting receipt. "
        "Submit the evidence through the secure request form, then wait for Finance Operations to "
        "confirm reconciliation and provide the next action. Escalate suspected fraud immediately."
    )
    rows = [
        {
            "object_type": "articles",
            "title": "Payment Help",
            "conditions": [],
            "actions": [
                {"field": "body", "value": "old"},
                {"field": "current_tags", "value": "payments"},
            ],
        }
    ]
    review = {
        "patches": [
            {"operation": "add_tag", "target_index": 0, "value": ["billing support", "payments"]},
            {"operation": "set_article_body", "target_index": 0, "value": replacement_body},
        ]
    }

    patched, summary = apply_supervisor_patches(rows, review, auto_apply=True)

    assert summary["applied"] == 2
    assert [item for item in patched[0]["actions"] if item["field"] == "body"] == [
        {"field": "body", "value": replacement_body}
    ]
    assert [item for item in patched[0]["actions"] if item["field"] == "current_tags"] == [
        {"field": "current_tags", "value": "payments billing_support"}
    ]


def test_supervisor_rejects_article_body_regression():
    existing_body = "Existing deployment-safe guidance. " * 20
    rows = [
        {
            "object_type": "articles",
            "title": "Payment Help",
            "conditions": [],
            "actions": [{"field": "body", "value": existing_body}],
        }
    ]
    review = {
        "patches": [
            {
                "operation": "set_article_body",
                "target_index": 0,
                "value": "Approved for ingestion.",
            }
        ]
    }

    patched, summary = apply_supervisor_patches(rows, review, auto_apply=True)

    assert summary["applied"] == 0
    assert summary["rejected"] == 1
    assert summary["patch_results"][0]["reject_reason"] == "body_not_substantive"
    assert patched[0]["actions"] == [{"field": "body", "value": existing_body}]


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


def test_supervisor_gate_does_not_impose_department_routing_on_exact_update():
    rows = [
        {
            "object_type": "triggers",
            "title": "Unassign Out Of Office Agent",
            "conditions": [{"field": "current_tags", "operator": "includes", "value": "agent_ooo"}],
            "actions": [
                {"field": "assignee_id", "value": ""},
                {"field": "remove_tags", "value": "agent_ooo"},
                {"field": "current_tags", "value": "quality_checked"},
            ],
            "_supervisor_chunk_id": "UPDATE:1",
            "_supervisor_record_key": "UPDATE:1:0",
        }
    ]
    review = {
        "approved": True,
        "quality_score": 0.96,
        "requires_regeneration": False,
        "chunk_assessments": [
            {
                "chunk_id": "UPDATE:1",
                "approved": True,
                "quality_score": 0.96,
                "blocking_issues": [],
                "requires_regeneration": False,
            }
        ],
    }

    gate = evaluate_supervisor_bundle(
        rows=rows,
        chunk_specs=[
            {
                "chunk_id": "UPDATE:1",
                "object_type": "triggers",
                "target_count": 1,
                "operation_mode": "update",
                "require_group_routing": False,
                "require_routing_tag": False,
            }
        ],
        review=review,
        approval_threshold=0.8,
    )

    assessment = gate["chunk_assessments"][0]
    assert assessment["effective_approved"] is True
    assert assessment["effective_quality_score"] == 0.96
    assert assessment["approval_gate_reasons"] == []


def test_supervisor_gate_treats_approved_pre_patch_issue_as_resolved():
    rows = [
        {
            "object_type": "groups",
            "title": "Claims",
            "conditions": [],
            "actions": [],
            "_supervisor_chunk_id": "BL-3:1",
            "_supervisor_record_key": "BL-3:1:0",
        }
    ]
    review = {
        "approved": True,
        "quality_score": 1.0,
        "requires_regeneration": False,
        "chunk_assessments": [
            {
                "chunk_id": "BL-3:1",
                "approved": True,
                "quality_score": 1.0,
                "blocking_issues": ["Incorrect group title 'Claims 3' generated instead of 'Claims'."],
                "requires_regeneration": False,
            }
        ],
    }

    gate = evaluate_supervisor_bundle(
        rows=rows,
        chunk_specs=[
            {
                "chunk_id": "BL-3:1",
                "object_type": "groups",
                "target_count": 1,
                "expected_titles": ["Claims"],
            }
        ],
        review=review,
        approval_threshold=0.8,
        allowed_references={"groups": ["Claims"], "ticket_forms": []},
    )

    assessment = gate["chunk_assessments"][0]
    assert assessment["effective_approved"] is True
    assert assessment["approval_gate_reasons"] == []
    assert assessment["model_reported_issues"] == review["chunk_assessments"][0]["blocking_issues"]


def test_supervisor_gate_rejects_shallow_article_body_despite_high_model_score():
    rows = [
        {
            "object_type": "articles",
            "title": "How to Submit a Claim",
            "conditions": [],
            "actions": [
                {"field": "body", "value": "A short summary that does not explain the actual process."},
                {"field": "section_name", "value": "Claims"},
            ],
            "_supervisor_chunk_id": "BL-9:1",
            "_supervisor_record_key": "BL-9:1:0",
        }
    ]
    review = {
        "approved": True,
        "quality_score": 0.98,
        "requires_regeneration": False,
        "chunk_assessments": [
            {
                "chunk_id": "BL-9:1",
                "approved": True,
                "quality_score": 0.98,
                "blocking_issues": [],
                "requires_regeneration": False,
            }
        ],
    }

    gate = evaluate_supervisor_bundle(
        rows=rows,
        chunk_specs=[{"chunk_id": "BL-9:1", "object_type": "articles", "target_count": 1}],
        review=review,
        approval_threshold=0.8,
        allowed_references={"groups": [], "ticket_forms": []},
    )

    assessment = gate["chunk_assessments"][0]
    assert assessment["effective_quality_score"] == 0.49
    assert assessment["effective_approved"] is False
    assert any("350 characters" in reason for reason in assessment["approval_gate_reasons"])
