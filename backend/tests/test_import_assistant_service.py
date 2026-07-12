import asyncio
from types import SimpleNamespace

from app.models.schemas import ValidationSummary
from app.services.import_assistant_service import (
    _annotate_focus_object_constraints,
    _apply_generation_safety_to_preview,
    _apply_dependency_resolution,
    _annotate_duplicate_candidates,
    _build_deterministic_chunk_rows,
    _build_department_coverage_manifest,
    _build_department_supervisor_review_manifest,
    _build_catalog_lookup,
    _build_chunk_plan,
    _build_llm_routes_metadata,
    _canonicalize_generated_rows,
    _canonicalize_ticket_field_record,
    _build_orchestration_backlog,
    _build_clarification_questions,
    _build_existing_object_index,
    _build_preview_records,
    _dedupe_generated_rows,
    _estimate_requested_record_count,
    _extract_object_type_targets,
    _extract_explicit_constraints,
    _evaluate_generation_safety,
    _evaluate_department_coverage,
    _apply_coverage_gate_to_preview,
    _is_business_blueprint_prompt,
    _can_use_deterministic_chunk_fallback,
    _should_use_department_template_first,
    _resolve_wave_api_key,
    _resolve_wave_generator_model,
    _run_business_blueprint_compiler,
    _reconcile_backlog_item,
)
from app.services.planner import _resolve_fallback_object_type
from app.services.gemini_supervisor import evaluate_supervisor_bundle


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


def test_resolve_wave_generator_model_uses_wave_overrides():
    settings = SimpleNamespace(
        llm_model_generator="openai/gpt-oss-20b",
        llm_model_generator_wave3="qwen/qwen3-32b",
        llm_model_generator_wave4="meta-llama/llama-4-scout-17b-16e-instruct",
    )

    assert _resolve_wave_generator_model(settings=settings, wave=1) == "openai/gpt-oss-20b"
    assert _resolve_wave_generator_model(settings=settings, wave=2) == "openai/gpt-oss-20b"
    assert _resolve_wave_generator_model(settings=settings, wave=3) == "qwen/qwen3-32b"
    assert (
        _resolve_wave_generator_model(settings=settings, wave=4)
        == "meta-llama/llama-4-scout-17b-16e-instruct"
    )


def test_resolve_wave_api_key_uses_wave_overrides():
    settings = SimpleNamespace(
        xai_api_key="gsk_primary",
        xai_api_key_wave3="gsk_wave3",
        xai_api_key_wave4="gsk_wave4",
    )

    assert _resolve_wave_api_key(settings=settings, wave=1) == "gsk_primary"
    assert _resolve_wave_api_key(settings=settings, wave=2) == "gsk_primary"
    assert _resolve_wave_api_key(settings=settings, wave=3) == "gsk_wave3"
    assert _resolve_wave_api_key(settings=settings, wave=4) == "gsk_wave4"


def test_build_llm_routes_metadata_includes_wave_generator_overrides():
    planner_route = SimpleNamespace(task="planner", model="qwen/qwen3-32b", max_output_tokens=220, strict_schema=False)
    clarifier_route = SimpleNamespace(
        task="clarifier",
        model="qwen/qwen3-32b",
        max_output_tokens=180,
        strict_schema=False,
    )
    generator_route = SimpleNamespace(
        task="generator",
        model="openai/gpt-oss-20b",
        max_output_tokens=420,
        strict_schema=False,
    )
    settings = SimpleNamespace(
        llm_model_generator_wave3="qwen/qwen3-32b",
        llm_model_generator_wave4="meta-llama/llama-4-scout-17b-16e-instruct",
        xai_api_key_wave3="gsk_wave3",
        xai_api_key_wave4="gsk_wave4",
    )

    metadata = _build_llm_routes_metadata(
        planner_route=planner_route,
        clarifier_route=clarifier_route,
        generator_route=generator_route,
        settings=settings,
    )

    assert metadata["generator_wave3"] == "qwen/qwen3-32b"
    assert metadata["generator_wave4"] == "meta-llama/llama-4-scout-17b-16e-instruct"
    assert metadata["generator_wave3_api_key_configured"] is True
    assert metadata["generator_wave4_api_key_configured"] is True
    assert metadata["gemini_supervisor"]["min_request_interval_seconds"] == 0.0
    assert metadata["gemini_supervisor"]["rate_limit_retries"] == 0


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


def test_extract_explicit_constraints_trims_field_and_form_title_suffixes():
    field_constraints = _extract_explicit_constraints(
        "Make me a field called Claim Status as dropdown and values are Open, Closed",
    )
    form_constraints = _extract_explicit_constraints(
        "Create ticket form called Claims Intake Form including fields Claim Status and Claim Number",
    )

    assert field_constraints["title"] == "claim status"
    assert form_constraints["title"] == "claims intake form"


def test_generation_safety_allows_canonicalized_ticket_field_prompt_match():
    plan = {"confidence": 0.94}
    rows = [
        {
            "object_type": "ticket_fields",
            "title": "Claim Status",
            "conditions": [],
            "actions": [
                {"field": "field_type", "value": "tagger"},
                {
                    "field": "custom_field_options",
                    "value": [
                        {"name": "Open", "value": "open"},
                        {"name": "Closed", "value": "closed"},
                    ],
                },
            ],
            "dependency_notes": [],
        }
    ]
    safety = _evaluate_generation_safety(
        prompt="Make me a field called Claim Status as dropdown and values are Open, Closed",
        plan=plan,
        generated_rows=rows,
        focus_object_types={"ticket_fields"},
        min_confidence=0.65,
    )

    assert safety["blocked"] is False
    assert safety["explicit_constraint_violations"] == []


def test_generation_safety_allows_ticket_form_title_match_with_including_clause():
    plan = {"confidence": 0.93}
    rows = [
        {
            "object_type": "ticket_forms",
            "title": "Claims Intake Form",
            "conditions": [],
            "actions": [{"field": "ticket_field_names", "value": ["Claim Status", "Claim Number"]}],
            "dependency_notes": [],
        }
    ]
    safety = _evaluate_generation_safety(
        prompt="Create ticket form called Claims Intake Form including fields Claim Status and Claim Number",
        plan=plan,
        generated_rows=rows,
        focus_object_types={"ticket_forms"},
        min_confidence=0.65,
    )

    assert safety["blocked"] is False
    assert safety["explicit_constraint_violations"] == []


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
    normalized_rows, field_inference, form_resolution, canonicalization = _canonicalize_generated_rows(
        rows=rows,
        prompt="Create a ticket form called Claims Form with field Claim Number",
        reference_catalog={"ticket_fields": []},
        existing_index={},
        related_lookup={},
        catalog_lookup={},
        settings=settings,
    )

    assert len(normalized_rows) == 2
    assert any(row.get("object_type") == "ticket_fields" and row.get("title") == "Claim Number" for row in normalized_rows)
    form_row = next(row for row in normalized_rows if row.get("object_type") == "ticket_forms")
    form_action_map = {item["field"]: item["value"] for item in form_row["actions"]}
    assert form_action_map["ticket_field_names"] == ["Claim Number"]
    assert form_resolution["auto_created_fields"] == 1
    assert field_inference["total_records"] >= 1
    assert canonicalization["trigger_article"]["rules_processed"] == 0


def test_canonicalize_generated_rows_infers_ticket_form_fields_from_prompt_when_actions_sparse():
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
            "title": "Claims Intake Form",
            "conditions": [],
            "actions": [],
        }
    ]
    reference_catalog = {
        "ticket_fields": [
            {"id": "101", "name": "Claim Status"},
            {"id": "102", "name": "Claim Number"},
        ]
    }

    normalized_rows, _, form_resolution, _ = _canonicalize_generated_rows(
        rows=rows,
        prompt="Create ticket form called Claims Intake Form including fields Claim Status and Claim Number",
        reference_catalog=reference_catalog,
        existing_index={},
        related_lookup={},
        catalog_lookup={},
        settings=settings,
    )

    form_row = next(row for row in normalized_rows if row.get("object_type") == "ticket_forms")
    action_map = {item["field"]: item["value"] for item in form_row["actions"]}
    assert "ticket_field_ids" in action_map
    assert sorted(action_map["ticket_field_ids"]) == [101, 102]
    assert form_resolution["inference_hints_count"] >= 2
    assert "prompt+catalog" in form_resolution["inference_sources"]


def test_canonicalize_generated_rows_blocks_ticket_form_when_no_field_hints_available():
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
            "title": "Claims Intake Form",
            "conditions": [],
            "actions": [],
        }
    ]

    normalized_rows, _, _, _ = _canonicalize_generated_rows(
        rows=rows,
        prompt="Create a ticket form for claims",
        reference_catalog={"ticket_fields": []},
        existing_index={},
        related_lookup={},
        catalog_lookup={},
        settings=settings,
    )
    form_row = next(row for row in normalized_rows if row.get("object_type") == "ticket_forms")
    assert "validation_overrides" in form_row
    assert "no ticket form field references could be inferred" in form_row["validation_overrides"]["blocked_reason"].lower()


def test_build_preview_records_blocks_empty_ticket_form_actions():
    plan = {"object_type": "ticket_forms"}
    generated_data = [
        {"object_type": "ticket_forms", "title": "Claims Intake Form", "conditions": [], "actions": []}
    ]

    records, summary = _build_preview_records(plan, generated_data)

    assert summary.blocked == 1
    assert records[0]["validation_status"] == "failed"
    assert "field linkage actions" in str(records[0]["blocked_reason"]).lower()


def test_canonicalize_generated_rows_normalizes_trigger_aliases_and_blocks_empty_actions():
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
            "object_type": "triggers",
            "title": "Route Billing",
            "conditions": [{"field": "brand", "operator": "is", "value": "EPPF"}],
            "actions": [
                {"field": "assign", "value": "Finance"},
                {"field": "tag", "value": ["billing", "urgent"]},
                {"value": "missing_field"},
            ],
        }
    ]
    normalized_rows, _, _, canonicalization = _canonicalize_generated_rows(
        rows=rows,
        prompt="Create billing trigger",
        reference_catalog={},
        existing_index={},
        related_lookup={},
        catalog_lookup={},
        settings=settings,
    )
    trigger = normalized_rows[0]
    action_map = {item["field"]: item["value"] for item in trigger["actions"]}
    condition_map = {item["field"]: item["value"] for item in trigger["conditions"]}

    assert action_map["group_id"] == "Finance"
    assert action_map["set_tags"] == "billing urgent"
    assert condition_map["brand_id"] == "EPPF"
    assert any("Dropped" in note for note in trigger.get("dependency_notes", []))
    assert canonicalization["trigger_article"]["rules_processed"] == 1
    assert canonicalization["trigger_article"]["blocked_records"] == 0


def test_canonicalize_generated_rows_resolves_article_section_and_blocks_when_missing():
    settings = SimpleNamespace(
        inference_policy="infer_warn",
        form_missing_field_mode="auto_create",
        ticket_field_default_agent_can_edit=True,
        ticket_field_default_visible_in_portal=True,
        ticket_field_default_editable_in_portal=False,
        ticket_field_default_required=False,
        ticket_field_default_required_in_portal=False,
    )
    reference_catalog = {"sections": [{"id": "456", "name": "Claim Process"}]}
    catalog_lookup = _build_catalog_lookup(reference_catalog)

    rows = [
        {
            "object_type": "articles",
            "title": "How to claim",
            "conditions": [{"field": "section", "value": "Claim Process"}],
            "actions": [{"field": "content", "value": "<p>Step by step</p>"}],
        }
    ]
    normalized_rows, _, _, canonicalization = _canonicalize_generated_rows(
        rows=rows,
        prompt="Create claim article",
        reference_catalog=reference_catalog,
        existing_index={},
        related_lookup={},
        catalog_lookup=catalog_lookup,
        settings=settings,
    )
    article = normalized_rows[0]
    action_map = {item["field"]: item["value"] for item in article["actions"]}
    assert action_map["section_id"] == "456"
    assert action_map["body"] == "<p>Step by step</p>"
    assert canonicalization["trigger_article"]["articles_processed"] == 1

    rows_missing = [
        {
            "object_type": "articles",
            "title": "Missing section article",
            "conditions": [],
            "actions": [],
        }
    ]
    normalized_missing, _, _, _ = _canonicalize_generated_rows(
        rows=rows_missing,
        prompt="Create article",
        reference_catalog={},
        existing_index={},
        related_lookup={},
        catalog_lookup={},
        settings=settings,
    )
    blocked_row = normalized_missing[0]
    assert "validation_overrides" in blocked_row
    assert "section_id" in blocked_row["validation_overrides"]["blocked_reason"].lower()


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


def test_chunk_plan_uses_balanced_fast_profile_for_form_field_view_objects():
    settings = SimpleNamespace(
        llm_auto_chunk_enabled=True,
        llm_auto_chunk_size=6,
        llm_auto_chunk_max_chunks=12,
        llm_auto_chunk_trigger_min_records=7,
    )
    fields = _build_chunk_plan(settings=settings, estimated_count=12, object_type="ticket_fields")
    assert fields["activated"] is True
    assert fields["chunk_size"] == 3
    assert fields["chunk_targets"] == [3, 3, 3, 3]

    forms = _build_chunk_plan(settings=settings, estimated_count=4, object_type="ticket_forms")
    assert forms["activated"] is True
    assert forms["chunk_size"] == 2
    assert forms["chunk_targets"] == [2, 2]

    views = _build_chunk_plan(settings=settings, estimated_count=5, object_type="views")
    assert views["activated"] is True
    assert views["chunk_size"] == 2
    assert views["chunk_targets"] == [2, 2, 1]


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


def test_extract_object_type_targets_detects_multi_object_numeric_intent():
    targets = _extract_object_type_targets(
        prompt="Create 10 triggers, 5 macros, 3 views and 2 ticket forms for this business.",
        focus_object_types=[],
        estimated_count=20,
        chunk_estimate=None,
    )
    assert targets["triggers"] >= 10
    assert targets["macros"] >= 5
    assert targets["views"] >= 3
    assert targets["ticket_forms"] >= 2


def test_deterministic_chunk_fallback_support_includes_wave3_rule_objects():
    assert _can_use_deterministic_chunk_fallback("ticket_fields") is True
    assert _can_use_deterministic_chunk_fallback("ticket_forms") is True
    assert _can_use_deterministic_chunk_fallback("views") is True
    assert _can_use_deterministic_chunk_fallback("triggers") is True
    assert _can_use_deterministic_chunk_fallback("macros") is True
    assert _can_use_deterministic_chunk_fallback("automations") is True


def test_department_objects_are_template_first_for_gemini_supervision():
    backlog_item = {"source": "department_coverage"}
    department_types = {
        "categories",
        "sections",
        "groups",
        "ticket_fields",
        "ticket_forms",
        "views",
        "triggers",
        "macros",
        "automations",
        "articles",
    }

    assert all(
        _should_use_department_template_first(backlog_item, object_type)
        for object_type in department_types
    )
    assert _should_use_department_template_first({"source": "fallback"}, "categories") is False


def test_build_deterministic_chunk_rows_generates_valid_trigger_rule_payload():
    rows = _build_deterministic_chunk_rows(
        object_type="triggers",
        target_count=1,
        prompt="Create trigger named Billing Escalation for open tickets and add tag escalated_now",
        reference_catalog={},
        existing_titles=[],
        generated_rows=[],
        reason="synthetic schema failure",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["object_type"] == "triggers"
    assert row["conditions"]
    assert row["actions"]
    assert any(str(action.get("field")) == "set_tags" for action in row["actions"])
    assert any(str(action.get("value", "")).find("escalated_now") >= 0 for action in row["actions"])


def test_build_deterministic_chunk_rows_generates_valid_macro_payload():
    rows = _build_deterministic_chunk_rows(
        object_type="macros",
        target_count=1,
        prompt="Create macro named Follow Up and add tag follow_up_25h",
        reference_catalog={},
        existing_titles=[],
        generated_rows=[],
        reason="synthetic parse failure",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["object_type"] == "macros"
    assert row["actions"]
    assert any(str(action.get("field")) == "set_tags" for action in row["actions"])


def test_business_blueprint_prompt_classifier_routes_multi_object_intent():
    targets = {"triggers": 10, "macros": 5}
    activate, reason = _is_business_blueprint_prompt(
        prompt="For our finance business, create 10 triggers and 5 macros end-to-end.",
        focus_object_types=[],
        estimated_count=15,
        prompt_explicit=False,
        object_targets=targets,
    )
    assert activate is True
    assert reason in {"multi_object_intent", "bulk_multi_object_intent", "business_brief_signal"}


def test_build_orchestration_backlog_orders_by_wave_dependency():
    backlog = _build_orchestration_backlog(
        blueprint={
            "target_objects": [
                {"object_type": "triggers", "target_count": 6, "priority": 3},
                {"object_type": "groups", "target_count": 2, "priority": 1},
                {"object_type": "ticket_fields", "target_count": 4, "priority": 1},
                {"object_type": "ticket_forms", "target_count": 2, "priority": 2},
            ]
        },
        prompt="Build full setup",
        dependency_mode="match_existing_or_create_new",
        focus_object_types=[],
    )
    waves = [item["wave"] for item in backlog]
    assert waves == sorted(waves)
    assert backlog[0]["object_type"] == "groups"


APEX_OPERATING_MODEL_PROMPT = """
Build a Zendesk support operating model for Apex Mobility Finance.

Business context:
Apex has these departments:
- Customer Support: first-line support for account questions, payment issues, login problems, and general requests.
- Claims & Incidents: handles damaged vehicles, theft reports, accident claims, insurance evidence, and urgent safety incidents.
- Finance Operations: handles failed payments, settlement disputes, refunds, payoff quotes, and billing corrections.
- Fleet Onboarding: helps business customers onboard multiple riders, verify documents, activate vehicles, and schedule handover.
- Technical Support: handles app bugs, GPS/device issues, charger problems, battery diagnostics, and telematics troubleshooting.
- Compliance: handles KYC document review, suspicious activity, regulatory complaints, and data/privacy requests.
- VIP / Enterprise Success: handles high-value fleet accounts, partner escalations, and white-glove support.

Create a complete Zendesk configuration in dependency order:
- Groups for the departments above, reusing existing matching groups if selected.
- Ticket fields for Department, Customer Segment, Vehicle Type, Issue Category, Incident Severity, Payment Status, KYC Status, Fleet Size, and Requested Outcome.
- Ticket forms for General Support, Claims & Incidents, Finance Operations, Fleet Onboarding, Technical Support, Compliance Review, and VIP Enterprise Support.
- Triggers to route tickets to the correct department based on form, issue category, customer segment, severity, and payment/KYC signals.
- Automations for stale urgent incidents, unresolved failed payments, pending KYC reviews, and enterprise escalations.
- Macros for first response, missing information request, payment dispute acknowledgement, incident claim acknowledgement, KYC document request, technical troubleshooting steps, and VIP escalation acknowledgement.
- Views for each department showing useful columns like requester, priority, status, vehicle type, issue category, severity, payment status, KYC status, assignee, and updated date.
- Help center categories/sections/articles for payments, claims, onboarding, technical troubleshooting, compliance/KYC, and enterprise fleet support.

Operational rules:
- Use clear tags such as apex_payment_issue, apex_claims_incident, apex_fleet_onboarding, apex_technical_support, apex_kyc_review, apex_enterprise_vip, urgent_safety_incident.
"""


def test_department_coverage_manifest_extracts_apex_operating_model():
    manifest = _build_department_coverage_manifest(
        prompt=APEX_OPERATING_MODEL_PROMPT,
        focus_object_types=[],
    )

    assert manifest["enabled"] is True
    assert manifest["profile"] == "heavy"
    assert [item["name"] for item in manifest["departments"]] == [
        "Customer Support",
        "Claims & Incidents",
        "Finance Operations",
        "Fleet Onboarding",
        "Technical Support",
        "Compliance",
        "VIP / Enterprise Success",
    ]
    assert manifest["target_counts"]["groups"] == 7
    assert manifest["target_counts"]["views"] == 14
    assert manifest["target_counts"]["triggers"] == 21
    assert manifest["target_counts"]["automations"] == 14
    assert manifest["target_counts"]["macros"] == 21
    assert manifest["target_counts"]["articles"] == 14
    assert manifest["target_total"] >= 100


def test_department_coverage_backlog_expands_heavy_minimums_by_wave():
    manifest = _build_department_coverage_manifest(
        prompt=APEX_OPERATING_MODEL_PROMPT,
        focus_object_types=[],
    )
    backlog = _build_orchestration_backlog(
        blueprint={"coverage_manifest": manifest, "target_objects": []},
        prompt=APEX_OPERATING_MODEL_PROMPT,
        dependency_mode="match_existing_or_create_new",
        focus_object_types=[],
    )

    assert [item["wave"] for item in backlog] == sorted(item["wave"] for item in backlog)
    assert sum(1 for item in backlog if item["object_type"] == "groups") == 7
    assert sum(1 for item in backlog if item["object_type"] == "ticket_forms") == 7
    assert sum(item["target_count"] for item in backlog if item["object_type"] == "views") == 14
    assert sum(item["target_count"] for item in backlog if item["object_type"] == "triggers") == 21
    assert sum(item["target_count"] for item in backlog if item["object_type"] == "automations") == 14
    assert sum(item["target_count"] for item in backlog if item["object_type"] == "macros") == 21
    assert sum(item["target_count"] for item in backlog if item["object_type"] == "articles") == 14
    assert all(item["source"] == "department_coverage" for item in backlog)


def test_apex_department_supervisor_manifest_consolidates_to_35_initial_calls():
    manifest = _build_department_coverage_manifest(
        prompt=APEX_OPERATING_MODEL_PROMPT,
        focus_object_types=[],
    )
    backlog = _build_orchestration_backlog(
        blueprint={"coverage_manifest": manifest, "target_objects": []},
        prompt=APEX_OPERATING_MODEL_PROMPT,
        dependency_mode="match_existing_or_create_new",
        focus_object_types=[],
    )

    review_units = _build_department_supervisor_review_manifest(backlog)

    assert len(review_units) == 35
    assert len([item for item in review_units if item["wave"] == 1]) == 6
    assert len([item for item in review_units if item["wave"] == 2]) == 8
    assert len([item for item in review_units if item["wave"] in {3, 4, 5}]) == 21


def test_apex_business_blueprint_compiler_bypasses_generator_model():
    blueprint, telemetry = asyncio.run(
        _run_business_blueprint_compiler(
            prompt=APEX_OPERATING_MODEL_PROMPT,
            dependency_mode="match_existing_or_create_new",
            focus_object_types=[],
            object_targets={},
            planner_route=SimpleNamespace(model="unused", max_output_tokens=300),
        )
    )

    assert blueprint["mode"] == "deterministic_fallback"
    assert blueprint["coverage_manifest"]["enabled"] is True
    assert telemetry == {"bypassed": True, "reason": "department_coverage_manifest"}


def test_apex_deterministic_department_chunks_pass_enforced_supervisor_gate():
    manifest = _build_department_coverage_manifest(
        prompt=APEX_OPERATING_MODEL_PROMPT,
        focus_object_types=[],
    )
    backlog = _build_orchestration_backlog(
        blueprint={"coverage_manifest": manifest, "target_objects": []},
        prompt=APEX_OPERATING_MODEL_PROMPT,
        dependency_mode="match_existing_or_create_new",
        focus_object_types=[],
    )
    allowed_references = {
        "groups": [item["group_title"] for item in manifest["departments"]],
        "ticket_forms": [item["form_title"] for item in manifest["departments"]],
        "ticket_fields": list(manifest["ticket_fields"]),
    }
    all_rows = []

    for item in backlog:
        object_type = item["object_type"]
        rows = _build_deterministic_chunk_rows(
            object_type=object_type,
            target_count=item["target_count"],
            prompt=APEX_OPERATING_MODEL_PROMPT,
            reference_catalog={},
            existing_titles=[],
            generated_rows=[],
            reason="gate-regression",
            backlog_item=item,
        )
        all_rows.extend(rows)
        chunk_id = f"{item['backlog_id']}:1"
        for index, row in enumerate(rows):
            row["_supervisor_chunk_id"] = chunk_id
            row["_supervisor_record_key"] = f"{chunk_id}:{index}"
        expected_titles = []
        if object_type == "groups":
            expected_titles = [item["department_name"]]
        elif object_type == "ticket_forms":
            expected_titles = [item["form_title"]]
        spec = {
            "chunk_id": chunk_id,
            "object_type": object_type,
            "target_count": item["target_count"],
            "fields": item.get("fields", []),
            "expected_titles": expected_titles,
        }
        review = {
            "approved": True,
            "quality_score": 0.95,
            "requires_regeneration": False,
            "chunk_assessments": [
                {
                    "chunk_id": chunk_id,
                    "approved": True,
                    "quality_score": 0.95,
                    "blocking_issues": [],
                    "requires_regeneration": False,
                }
            ],
        }

        gate = evaluate_supervisor_bundle(
            rows=rows,
            chunk_specs=[spec],
            review=review,
            approval_threshold=0.8,
            allowed_references=allowed_references,
        )

        assert gate["effective_approved"] is True, (
            object_type,
            gate["chunk_assessments"][0]["approval_gate_reasons"],
        )

    coverage = _evaluate_department_coverage(manifest=manifest, records=all_rows)
    assert len(all_rows) >= 100
    assert coverage["status"] == "passed"
    assert all(item["status"] == "passed" for item in coverage["departments"])


def test_apex_coverage_gate_detects_global_article_count_loss():
    manifest = _build_department_coverage_manifest(
        prompt=APEX_OPERATING_MODEL_PROMPT,
        focus_object_types=[],
    )
    backlog = _build_orchestration_backlog(
        blueprint={"coverage_manifest": manifest, "target_objects": []},
        prompt=APEX_OPERATING_MODEL_PROMPT,
        dependency_mode="match_existing_or_create_new",
        focus_object_types=[],
    )
    rows = []
    for item in backlog:
        rows.extend(
            _build_deterministic_chunk_rows(
                object_type=item["object_type"],
                target_count=item["target_count"],
                prompt=APEX_OPERATING_MODEL_PROMPT,
                reference_catalog={},
                existing_titles=[row["title"] for row in rows],
                generated_rows=rows,
                reason="coverage-regression",
                backlog_item=item,
            )
        )
    article_indexes = [
        index for index, row in enumerate(rows) if row["object_type"] == "articles"
    ]
    rows = [
        row
        for index, row in enumerate(rows)
        if index not in set(article_indexes[-2:])
    ]

    coverage = _evaluate_department_coverage(manifest=manifest, records=rows)

    assert coverage["status"] == "warning"
    assert coverage["global"]["missing"]["articles"] == 2
    assert any(
        item["department"] == "global"
        and item["object_type"] == "articles"
        and item["missing"] == 2
        for item in coverage["missing"]
    )


def test_department_wave4_templates_build_deployable_trigger_rows():
    manifest = _build_department_coverage_manifest(
        prompt=APEX_OPERATING_MODEL_PROMPT,
        focus_object_types=[],
    )
    backlog = _build_orchestration_backlog(
        blueprint={"coverage_manifest": manifest, "target_objects": []},
        prompt=APEX_OPERATING_MODEL_PROMPT,
        dependency_mode="match_existing_or_create_new",
        focus_object_types=[],
    )
    trigger_item = next(
        item
        for item in backlog
        if item["object_type"] == "triggers" and item["department_name"] == "Claims & Incidents"
    )

    rows = _build_deterministic_chunk_rows(
        object_type="triggers",
        target_count=3,
        prompt=APEX_OPERATING_MODEL_PROMPT,
        reference_catalog={},
        existing_titles=[],
        generated_rows=[],
        reason="template-first",
        backlog_item=trigger_item,
    )

    assert len(rows) == 3
    assert all(row["object_type"] == "triggers" for row in rows)
    assert all(row["conditions"] for row in rows)
    assert all(row["actions"] for row in rows)
    assert any(
        action["field"] == "group_id" and action["value"] == "Claims & Incidents"
        for row in rows
        for action in row["actions"]
    )
    assert any("apex_claims_incident" in str(action["value"]) for row in rows for action in row["actions"])


def test_department_coverage_gate_warns_without_blocking_preview():
    manifest = _build_department_coverage_manifest(
        prompt=APEX_OPERATING_MODEL_PROMPT,
        focus_object_types=[],
    )
    preview_records = [
        {
            "record_id": "REC-0001",
            "object_type": "groups",
            "title": "Customer Support",
            "validation_status": "passed",
            "warnings": [],
            "conditions": [],
            "actions": [{"field": "description", "value": "Customer Support"}],
        }
    ]

    coverage = _evaluate_department_coverage(manifest=manifest, records=preview_records)
    patched = _apply_coverage_gate_to_preview(preview_records, coverage)

    assert coverage["status"] == "warning"
    assert coverage["missing_count"] > 0
    assert patched[0]["validation_status"] == "warning"
    assert any("Coverage gate warning" in warning for warning in patched[0]["warnings"])


def test_canonicalize_articles_allows_same_batch_section_dependency():
    rows = [
        {
            "object_type": "sections",
            "title": "Claims",
            "conditions": [],
            "actions": [{"field": "locale", "value": "en-us"}],
        },
        {
            "object_type": "articles",
            "title": "Claims Required Information",
            "conditions": [],
            "actions": [
                {"field": "section_name", "value": "Claims"},
                {"field": "body", "value": "<p>Claims information.</p>"},
            ],
        },
    ]

    normalized_rows, _field_meta, _form_meta, canonicalization = _canonicalize_generated_rows(
        rows=rows,
        prompt="Create claims article and section",
        reference_catalog={"ticket_fields": [], "sections": []},
        existing_index={},
        related_lookup={},
        catalog_lookup={},
        settings=SimpleNamespace(
            ticket_field_default_type="text",
            inference_policy="infer_with_warnings",
            form_missing_field_mode="warn",
        ),
    )

    article = next(row for row in normalized_rows if row["object_type"] == "articles")
    assert "validation_overrides" not in article
    assert canonicalization["trigger_article"]["blocked_records"] == 0
    assert any(action["field"] == "section_name" and action["value"] == "Claims" for action in article["actions"])


def test_canonicalize_article_resolves_topic_alias_after_section_title_patch():
    rows = [
        {
            "object_type": "sections",
            "title": "Damaged Vehicles & Insurance Claims",
            "conditions": [],
            "actions": [
                {"field": "locale", "value": "en-us"},
                {"field": "category_name", "value": "claims Support"},
            ],
            "_supervisor_title_aliases": ["claims"],
        },
        {
            "object_type": "articles",
            "title": "Claims Required Information",
            "conditions": [],
            "actions": [
                {"field": "section_name", "value": "claims"},
                {"field": "body", "value": "<p>Claims information with enough operational detail.</p>"},
            ],
        },
    ]

    normalized_rows, _field_meta, _form_meta, canonicalization = _canonicalize_generated_rows(
        rows=rows,
        prompt="Create claims article and section",
        reference_catalog={"ticket_fields": [], "sections": []},
        existing_index={},
        related_lookup={},
        catalog_lookup={},
        settings=SimpleNamespace(
            ticket_field_default_type="text",
            inference_policy="infer_with_warnings",
            form_missing_field_mode="warn",
        ),
    )

    article = next(row for row in normalized_rows if row["object_type"] == "articles")
    assert "validation_overrides" not in article
    assert canonicalization["trigger_article"]["blocked_records"] == 0
    assert any(
        action["field"] == "section_name"
        and action["value"] == "Damaged Vehicles & Insurance Claims"
        for action in article["actions"]
    )


def test_reconcile_backlog_item_respects_force_existing_only():
    index = _build_existing_object_index({"triggers": [{"id": "101", "name": "Route Claims"}]})
    item = {"object_type": "triggers", "target_count": 1}
    blocked = _reconcile_backlog_item(
        item=item,
        prompt="Create trigger called Billing Route",
        dependency_mode="force_existing_only",
        existing_index=index,
    )
    assert blocked["mode"] == "blocked"
    assert "existing-only mode" in blocked["blocked_reason"].lower()
