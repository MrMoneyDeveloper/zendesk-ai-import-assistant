from types import SimpleNamespace

from app.models.schemas import ValidationSummary
from app.services.import_assistant_service import (
    _annotate_focus_object_constraints,
    _apply_generation_safety_to_preview,
    _apply_dependency_resolution,
    _annotate_duplicate_candidates,
    _build_catalog_lookup,
    _build_chunk_plan,
    _canonicalize_generated_rows,
    _canonicalize_ticket_field_record,
    _build_clarification_questions,
    _build_existing_object_index,
    _build_preview_records,
    _dedupe_generated_rows,
    _estimate_requested_record_count,
    _evaluate_generation_safety,
)
from app.services.planner import _resolve_fallback_object_type


def test_build_preview_records_sets_warnings_and_blocks():
    plan = {"object_type": "trigger"}
    generated_data = [
        {"title": "Route claims", "conditions": [], "actions": []},
        {"title": "", "conditions": [{"field": "group", "operator": "is", "value": "claims"}], "actions": []},
    ]

    records, summary = _build_preview_records(plan, generated_data)

    assert len(records) == 2
    assert isinstance(summary, ValidationSummary)
    assert summary.passed == 0
    assert summary.warnings == 1
    assert summary.blocked == 1
    assert records[0]["validation_status"] == "warning"
    assert records[1]["validation_status"] == "failed"


def test_duplicate_annotation_adds_dependency_note():
    catalog = {
        "triggers": [
            {"object_type": "trigger", "id": "123", "name": "Route claims", "description": "conditions=1; actions=2"}
        ]
    }
    index = _build_existing_object_index(catalog)
    rows = [
        {
            "object_type": "triggers",
            "title": "Route claims",
            "conditions": [{"field": "status", "operator": "is", "value": "new"}],
            "actions": [{"field": "set_tags", "value": "claims"}],
            "dependency_notes": [],
        }
    ]

    patched, duplicates = _annotate_duplicate_candidates(
        rows,
        existing_index=index,
        dependency_mode="match_existing_or_create_new",
    )

    assert len(patched) == 1
    assert len(duplicates) == 1
    notes = patched[0].get("dependency_notes", [])
    assert any("Duplicate candidate" in note for note in notes)


def test_dependency_resolution_uses_catalog_lookup_when_no_selected_context():
    rows = [
        {
            "object_type": "triggers",
            "title": "Finance Route",
            "conditions": [{"field": "group_id", "operator": "is", "value": "Finance & Investments"}],
            "actions": [{"field": "group_id", "value": "Finance & Investments"}],
            "dependency_notes": [],
        }
    ]
    catalog_lookup = _build_catalog_lookup(
        {
            "groups": [
                {"object_type": "group", "id": "12345", "name": "Finance & Investments"},
            ]
        }
    )

    patched_rows, meta = _apply_dependency_resolution(
        rows,
        related_lookup={},
        catalog_lookup=catalog_lookup,
        dependency_mode="match_existing_or_create_new",
    )

    assert meta["resolved_links"] == 2
    patched = patched_rows[0]
    assert patched["conditions"][0]["value"] == "12345"
    assert patched["actions"][0]["value"] == "12345"
    assert any("catalog context" in note for note in patched.get("dependency_notes", []))


def test_clarification_questions_capped_to_one():
    plan = {
        "object_type": "macros",
        "confidence": 0.2,
        "clarification_questions": [
            "Question one?",
            "Question two?",
        ],
    }

    questions = _build_clarification_questions(
        "create a macro",
        plan,
        ambiguity_score=0.9,
        ambiguity_threshold=0.55,
    )

    assert len(questions) == 1


def test_focus_object_constraint_marks_soft_preference_mismatch():
    rows = [
        {
            "object_type": "views",
            "title": "Claims queue",
            "conditions": [],
            "actions": [],
        }
    ]

    patched, diagnostics = _annotate_focus_object_constraints(
        rows,
        focus_object_types={"triggers"},
    )

    assert diagnostics["mismatch_count"] == 1
    assert diagnostics["mode"] == "soft_prefer"
    assert any("Focus preference mismatch" in note for note in patched[0]["dependency_notes"])


def test_generation_safety_blocks_fallback_and_constraint_mismatch():
    plan = {"confidence": 0.5}
    rows = [
        {
            "object_type": "triggers",
            "title": "Default Intake Triage",
            "conditions": [{"field": "status", "operator": "is", "value": "new"}],
            "actions": [{"field": "status", "value": "open"}],
            "dependency_notes": ["Generator fallback output used."],
        }
    ]
    safety = _evaluate_generation_safety(
        prompt="Create trigger called test_trigger and set tag test",
        plan=plan,
        generated_rows=rows,
        focus_object_types={"triggers"},
        min_confidence=0.65,
    )

    assert safety["blocked"] is True
    assert safety["fallback_detected"] is True
    assert any("confidence" in reason.lower() for reason in safety["reasons"])
    assert any("explicit prompt constraints" in reason.lower() for reason in safety["reasons"])

    preview_rows = [
        {
            "record_id": "REC-0001",
            "object_type": "triggers",
            "title": "Default Intake Triage",
            "validation_status": "warning",
            "warnings": [],
            "blocked_reason": None,
            "deployable": True,
            "import_decision": "pending_review",
        }
    ]
    patched_preview = _apply_generation_safety_to_preview(preview_rows, safety)
    assert patched_preview[0]["deployable"] is False
    assert patched_preview[0]["validation_status"] == "failed"
    assert patched_preview[0]["import_decision"] == "blocked"


def test_ticket_field_canonicalization_maps_dropdown_alias_and_parses_options():
    settings = SimpleNamespace(
        ticket_field_default_agent_can_edit=True,
        ticket_field_default_visible_in_portal=True,
        ticket_field_default_editable_in_portal=False,
        ticket_field_default_required=False,
        ticket_field_default_required_in_portal=False,
    )
    row = {
        "object_type": "ticket_fields",
        "title": "Test",
        "conditions": [],
        "actions": [
            {"field": "type", "value": "dropdown"},
            {"field": "options", "value": "tested, not tested"},
        ],
    }

    normalized_row, info = _canonicalize_ticket_field_record(
        row,
        prompt="Make me a field called Test make it a dropdown and the 2 values are tested and not tested",
        settings=settings,
    )
    action_map = {item["field"]: item["value"] for item in normalized_row["actions"]}

    assert action_map["field_type"] == "tagger"
    assert action_map["custom_field_options"] == [
        {"name": "tested", "value": "tested"},
        {"name": "not tested", "value": "not_tested"},
    ]
    assert action_map["agent_can_edit"] is True
    assert action_map["visible_in_portal"] is True
    assert action_map["editable_in_portal"] is False
    assert action_map["required"] is False
    assert action_map["required_in_portal"] is False
    assert isinstance(info["warnings"], list)


def test_generation_safety_blocks_ticket_field_type_mismatch():
    plan = {"confidence": 0.92}
    rows = [
        {
            "object_type": "ticket_fields",
            "title": "Test",
            "conditions": [],
            "actions": [{"field": "field_type", "value": "text"}],
            "dependency_notes": [],
        }
    ]
    safety = _evaluate_generation_safety(
        prompt="Make me a dropdown ticket field called Test with values tested and not tested",
        plan=plan,
        generated_rows=rows,
        focus_object_types={"ticket_fields"},
        min_confidence=0.65,
    )
    assert safety["blocked"] is True
    assert any("dropdown/select field" in item for item in safety["explicit_constraint_violations"])


def test_canonicalize_generated_rows_auto_creates_companion_field_for_form():
    settings = SimpleNamespace(
        inference_policy="infer_warn",
        form_missing_field_mode="auto_create",
        ticket_field_default_agent_can_edit=True,
        ticket_field_default_visible_in_portal=True,
        ticket_field_default_editable_in_portal=False,
        ticket_field_default_required=False,
        ticket_field_default_required_in_portal=False,
    )
    rows = [
        {
            "object_type": "ticket_forms",
            "title": "Claims Form",
            "conditions": [],
            "actions": [{"field": "ticket_field_names", "value": ["Claim Number"]}],
        }
    ]
    normalized_rows, field_inference, form_resolution = _canonicalize_generated_rows(
        rows=rows,
        prompt="Create a ticket form called Claims Form with field Claim Number",
        reference_catalog={"ticket_fields": []},
        existing_index={},
        settings=settings,
    )

    assert len(normalized_rows) == 2
    assert any(row.get("object_type") == "ticket_fields" and row.get("title") == "Claim Number" for row in normalized_rows)
    form_row = next(row for row in normalized_rows if row.get("object_type") == "ticket_forms")
    form_action_map = {item["field"]: item["value"] for item in form_row["actions"]}
    assert form_action_map["ticket_field_names"] == ["Claim Number"]
    assert form_resolution["auto_created_fields"] == 1
    assert field_inference["total_records"] >= 1


def test_planner_fallback_object_type_respects_focus():
    assert _resolve_fallback_object_type(["ticket_fields"]) == "ticket_fields"
    assert _resolve_fallback_object_type(["views", "triggers"]) == "views"
    assert _resolve_fallback_object_type([]) == "triggers"


def test_estimate_requested_record_count_uses_numeric_and_enumerated_hints():
    estimate_numeric = _estimate_requested_record_count("Create 20 triggers for billing workflows")
    assert estimate_numeric["estimated_count"] == 20
    assert "numeric_intent" in estimate_numeric["sources"]

    estimate_list = _estimate_requested_record_count(
        "Create these macros:\n1. Greeting macro\n2. Escalation macro\n3. Closure macro"
    )
    assert estimate_list["estimated_count"] == 3
    assert "enumerated_items" in estimate_list["sources"]


def test_chunk_plan_math_and_cap_detection():
    settings = SimpleNamespace(
        llm_auto_chunk_enabled=True,
        llm_auto_chunk_size=6,
        llm_auto_chunk_max_chunks=12,
        llm_auto_chunk_trigger_min_records=7,
    )
    plan = _build_chunk_plan(settings=settings, estimated_count=20)
    assert plan["activated"] is True
    assert plan["total_chunks"] == 4
    assert plan["chunk_targets"] == [6, 6, 6, 2]
    assert plan["exceeds_cap"] is False

    capped = _build_chunk_plan(settings=settings, estimated_count=100)
    assert capped["activated"] is True
    assert capped["total_chunks"] == 17
    assert capped["exceeds_cap"] is True


def test_dedupe_generated_rows_drops_duplicate_titles_by_object_type():
    rows = [
        {"object_type": "trigger", "title": "Route Claims", "conditions": [], "actions": []},
        {"object_type": "triggers", "title": "route claims", "conditions": [], "actions": []},
        {"object_type": "macros", "title": "Route Claims", "conditions": [], "actions": []},
    ]
    deduped, dropped = _dedupe_generated_rows(rows)
    assert dropped == 1
    assert len(deduped) == 2
    assert deduped[0]["object_type"] == "triggers"
    assert deduped[1]["object_type"] == "macros"
