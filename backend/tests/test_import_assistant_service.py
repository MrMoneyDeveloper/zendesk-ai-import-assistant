from types import SimpleNamespace

from app.models.schemas import ValidationSummary
from app.services.import_assistant_service import (
    _annotate_focus_object_constraints,
    _apply_generation_safety_to_preview,
    _apply_dependency_resolution,
    _annotate_duplicate_candidates,
    _build_deterministic_chunk_rows,
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
    _is_business_blueprint_prompt,
    _can_use_deterministic_chunk_fallback,
    _resolve_wave_api_key,
    _resolve_wave_generator_model,
    _reconcile_backlog_item,
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
