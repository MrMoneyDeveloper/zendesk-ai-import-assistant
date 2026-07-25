import asyncio
import json
from types import SimpleNamespace

from app.api.grok.client import GrokClient, LLMRequestError
from app.models.schemas import ImportAssistantGenerateRequest, ValidationSummary
from app.services.import_assistant_service import (
    _annotate_focus_object_constraints,
    _apply_explicit_article_dependencies,
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
    _bind_update_target_to_generated_rows,
    _compact_related_objects,
    _dedupe_generated_rows,
    _draft_department_content_rows,
    _estimate_requested_record_count,
    _extract_article_category_names,
    _extract_article_section_names,
    _extract_article_specs_from_prompt,
    _extract_automation_specs_from_prompt,
    _extract_inline_support_team_names,
    _extract_macro_specs_from_prompt,
    _extract_object_type_targets,
    _extract_ticket_field_specs_from_prompt,
    _extract_ticket_form_specs_from_prompt,
    _extract_trigger_specs_from_prompt,
    _extract_view_specs_from_prompt,
    _extract_explicit_constraints,
    _evaluate_generation_safety,
    _evaluate_department_coverage,
    _apply_coverage_gate_to_preview,
    _is_business_blueprint_prompt,
    _can_use_deterministic_chunk_fallback,
    _should_use_department_template_first,
    _should_use_explicit_template_first,
    _should_bypass_planner_for_explicit_manifest,
    _resolve_wave_api_key,
    _resolve_wave_generator_model,
    _select_wave_generator_route,
    _supervisor_review_bundle_key,
    _run_business_blueprint_compiler,
    _reconcile_backlog_item,
    _prepare_update_request,
    _planner_used_heuristic_fallback,
    apply_approval,
)
from app.services.batch_store import get_batch_store, reset_batch_store
from app.services.planner import _resolve_fallback_object_type
from app.services.gemini_supervisor import evaluate_supervisor_bundle


CLEARSKY_REGRESSION_PROMPT = """We are setting up Zendesk for ClearSky Insurance Group.
They have five support teams: Personal Lines Support, Commercial Lines Support,
Claims, Underwriting, and Client Retention.

1. Five ticket fields:
   - A dropdown called "Query Type" with options: New Quote Request,
     Claim Submission, Policy Cancellation, Complaint, General Inquiry
   - A dropdown called "Policy Type" with options: Vehicle Insurance, Business Insurance
   - A dropdown called "Client Segment" with options: Individual, Corporate, Broker Referred
   - A dropdown called "Claim Status" with options: Submitted, Approved, Rejected
   - A text field called "Policy Number"

2. Six triggers:
   - When Query Type is "Underwriting", assign to Underwriting.

3. Five views:
   - All open Claim Submission tickets assigned to Claims.

4. Four macros:
   - A macro called "Acknowledge Claim Submission" that sends a reply and adds a tag.

5. Three ticket forms:
   - A form called "ClearSky Personal Lines Form" that includes fields in order:
     Subject, Description, Query Type, Policy Type, Client Segment, Policy Number, Priority
   - A form called "ClearSky Claims Form" that includes fields in order:
     Subject, Description, Query Type, Policy Type, Client Segment, Claim Status, Policy Number, Priority
   - A form called "ClearSky Commercial Lines Form" that includes fields in order:
     Subject, Description, Query Type, Policy Type, Client Segment, Policy Number, Priority

6. Five knowledge base articles:
   - An article called "How to Submit a Claim with ClearSky Insurance"
     in the category Claims that explains required documents and timeframes
   - An article called "Understanding Your ClearSky Policy Schedule"
     in the category Policy Management that explains cover limits and amendments
   - An article called "How to Get a Quote for Business Insurance"
     in the category New Business that explains required quote information
   - An article called "What to Do After a Vehicle Accident"
     in the category Claims that explains emergency steps
   - An article called "ClearSky Cancellation Policy and Your Options"
     in the category Policy Management that explains notice and retention options
"""

AQUASHIELD_COMPACT_PROMPT = """Create a compact Zendesk demo configuration for AquaShield Home Services.

Business context:
AquaShield sells and installs residential water filtration systems in South Africa.

AquaShield has one support team:
AquaShield Customer Support

Create exactly the following objects. Do not add additional departments or expand the requested quantities.

1. Brand
- AquaShield Home Services

2. Help Center structure
- Category: AquaShield Support
- Section: Product Help and Installation

3. Group
- AquaShield Customer Support

4. Ticket fields
- Issue Type: dropdown with Installation, Filter Replacement, Water Quality, Warranty, Billing
- Product Type: dropdown with Filter System, Water Softener
- Product Serial Number: text field
- Appointment Date: date field

5. Ticket form
- AquaShield Support Request
- Include Subject, Description, Issue Type, Product Type, Product Serial Number, Appointment Date and Priority

6. Views
- New AquaShield Requests: new and open tickets assigned to AquaShield Customer Support
- Urgent AquaShield Issues: high and urgent tickets assigned to AquaShield Customer Support
- Include useful columns such as status, priority, requester, assignee and updated date

7. Triggers
- Route AquaShield support requests to AquaShield Customer Support
- Mark Water Quality requests as high priority and add tag aquashield_water_quality
- Add tag aquashield_warranty when Issue Type is Warranty

8. Macros
- Request Product Serial Number: ask the customer for the serial number and add tag awaiting_serial_number
- Installation Preparation Instructions: provide installation preparation steps and add tag installation_preparation_sent

9. Automation
- When a ticket has remained pending for 48 hours, add tag pending_followup_required and notify the assigned support group

10. Help Center articles
- How to Replace Your AquaShield Filter
- How to Prepare for an AquaShield Installation
- Place both articles in Product Help and Installation
- Each article must contain clear steps, safety notes and escalation guidance

Requirements:
- Use exact object names and name-based dependencies, not invented Zendesk IDs
- Keep conditions and actions deploy-safe
- Generate substantive macro text and article bodies
- Produce a preview only
- Do not deploy anything to Zendesk
- Do not exceed 20 generated records
"""


def _update_request(prompt: str = "Add the vip tag and keep the current routing logic."):
    return ImportAssistantGenerateRequest(
        prompt=prompt,
        operation_mode="update",
        instance_sync_id="SYNC-TEST",
        update_target={
            "object_type": "trigger",
            "id": "321",
            "name": "Route Enterprise Tickets",
            "updated_at": "2026-07-18T10:00:00Z",
            "snapshot_hash": "abc123",
            "editable": True,
            "snapshot": {
                "id": 321,
                "title": "Route Enterprise Tickets",
                "active": True,
                "updated_at": "2026-07-18T10:00:00Z",
                "conditions": {
                    "all": [{"field": "status", "operator": "is", "value": "new"}],
                    "any": [{"field": "priority", "operator": "is", "value": "high"}],
                },
                "actions": [{"field": "group_id", "value": "777"}],
            },
        },
    )


def test_prepare_update_request_forces_one_existing_target_with_snapshot():
    prepared, metadata = _prepare_update_request(_update_request())

    assert prepared.dependency_mode == "force_existing_only"
    assert prepared.focus_object_types == ["triggers"]
    assert len(prepared.related_objects) == 1
    assert prepared.related_objects[0].snapshot["id"] == 321
    assert "EXACT UPDATE MODE" in prepared.context_notes
    assert metadata["target_object_id"] == "321"

    compact = _compact_related_objects(
        [prepared.related_objects[0].model_dump()],
        max_items=5,
    )
    assert compact[0]["update_target"] is True
    assert compact[0]["snapshot"]["conditions"]["any"][0]["field"] == "priority"


def test_update_binding_preserves_id_title_and_any_condition_scope():
    request = _update_request()
    rows, metadata = _bind_update_target_to_generated_rows(
        [
            {
                "object_type": "triggers",
                "title": "Accidental Rename",
                "conditions": [
                    {"field": "status", "operator": "is", "value": "new", "scope": "all"},
                    {"field": "priority", "operator": "is", "value": "high", "scope": "any"},
                ],
                "actions": [
                    {"field": "group_id", "value": "777"},
                    {"field": "set_tags", "value": "vip"},
                ],
            }
        ],
        request=request,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Route Enterprise Tickets"
    assert row["target_object_id"] == "321"
    assert row["zendesk_object_id"] == "321"
    assert row["before_configuration"]["conditions"][1]["scope"] == "any"
    assert row["after_configuration"]["actions"][-1]["value"] == "vip"
    assert row["after_configuration"]["actions"][-1]["field"] == "current_tags"
    assert metadata["changed_fields"] == ["actions"]
    assert any("cannot erase existing ticket tags" in warning for warning in metadata["warnings"])

    preview, summary = _build_preview_records({"object_type": "triggers"}, rows)
    assert preview[0]["operation_mode"] == "update"
    assert preview[0]["target_object_id"] == "321"
    assert preview[0]["before_configuration"]["title"] == "Route Enterprise Tickets"
    assert summary.blocked == 0


def test_update_binding_blocks_noop_replacement():
    request = _update_request()
    rows, _ = _bind_update_target_to_generated_rows(
        [
            {
                "object_type": "triggers",
                "title": "Route Enterprise Tickets",
                "active": True,
                "conditions": [
                    {"field": "status", "operator": "is", "value": "new", "scope": "all"},
                    {"field": "priority", "operator": "is", "value": "high", "scope": "any"},
                ],
                "actions": [{"field": "group_id", "value": "777"}],
            }
        ],
        request=request,
    )

    preview, summary = _build_preview_records({"object_type": "triggers"}, rows)
    assert summary.blocked == 1
    assert "does not change" in preview[0]["blocked_reason"].lower()


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


def test_approval_sync_uses_backend_effective_blocked_decision(monkeypatch, tmp_path):
    captured = []

    class CaptureAppScriptBridge:
        @property
        def enabled(self):
            return True

        async def invoke(self, action, payload=None, method="POST", timeout_seconds=None):
            captured.append({"action": action, "payload": payload})
            return {
                "action": action,
                "status": "ok",
                "detail": None,
                "http_status": 200,
                "data": {"ok": True},
            }

    monkeypatch.setenv("BATCH_STORE_FILE", str(tmp_path / "approval-batches.json"))
    reset_batch_store()
    monkeypatch.setattr(
        "app.services.import_assistant_service.AppScriptBridgeService",
        CaptureAppScriptBridge,
    )
    store = get_batch_store()
    store.save_batch(
        {
            "batch_id": "BATCH-APPROVAL-GATE",
            "status": "preview_ready",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "status_history": [],
            "records": [
                {
                    "record_id": "REC-BLOCKED",
                    "object_type": "triggers",
                    "title": "Unsafe trigger",
                    "validation_status": "failed",
                    "blocked_reason": "Missing routing action.",
                    "import_decision": "pending_review",
                }
            ],
        }
    )

    asyncio.run(
        apply_approval(
            "BATCH-APPROVAL-GATE",
            "reviewer@example.com",
            [{"record_id": "REC-BLOCKED", "import_decision": "approved"}],
        )
    )

    assert store.get_batch("BATCH-APPROVAL-GATE")["records"][0]["import_decision"] == "blocked"
    assert captured[0]["action"] == "update_approval_status"
    assert captured[0]["payload"]["records"] == [
        {"record_id": "REC-BLOCKED", "import_decision": "blocked"}
    ]


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


def test_ticket_field_canonicalization_removes_options_from_text_fields():
    settings = SimpleNamespace(
        ticket_field_default_agent_can_edit=True,
        ticket_field_default_visible_in_portal=True,
        ticket_field_default_editable_in_portal=False,
        ticket_field_default_required=False,
        ticket_field_default_required_in_portal=False,
    )
    row = {
        "object_type": "ticket_fields",
        "title": "Policy Number",
        "conditions": [],
        "actions": [
            {"field": "field_type", "value": "text"},
            {
                "field": "custom_field_options",
                "value": ["Client", "Broker Referred"],
            },
        ],
    }

    normalized_row, info = _canonicalize_ticket_field_record(
        row,
        prompt=CLEARSKY_REGRESSION_PROMPT,
        settings=settings,
    )
    action_map = {item["field"]: item["value"] for item in normalized_row["actions"]}

    assert action_map["field_type"] == "text"
    assert "custom_field_options" not in action_map
    assert any("does not support options" in warning for warning in info["warnings"])


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
    assert action_map["current_tags"] == "billing urgent"
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

    small = _build_chunk_plan(settings=settings, estimated_count=4)
    assert small["activated"] is False
    assert small["chunk_targets"] == [4]


def test_chunk_plan_uses_balanced_fast_profile_for_form_field_view_objects():
    settings = SimpleNamespace(
        llm_auto_chunk_enabled=True,
        llm_auto_chunk_size=6,
        llm_auto_chunk_max_chunks=12,
        llm_auto_chunk_trigger_min_records=7,
    )
    fields = _build_chunk_plan(settings=settings, estimated_count=12, object_type="ticket_fields")
    assert fields["activated"] is True
    assert fields["chunk_size"] == 5
    assert fields["chunk_targets"] == [5, 5, 2]

    forms = _build_chunk_plan(settings=settings, estimated_count=4, object_type="ticket_forms")
    assert forms["activated"] is True
    assert forms["chunk_size"] == 3
    assert forms["chunk_targets"] == [3, 1]

    views = _build_chunk_plan(settings=settings, estimated_count=5, object_type="views")
    assert views["activated"] is True
    assert views["chunk_size"] == 3
    assert views["chunk_targets"] == [3, 2]


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


def test_aquashield_numbered_manifest_preserves_exact_counts_and_specs():
    estimate = _estimate_requested_record_count(AQUASHIELD_COMPACT_PROMPT)
    targets = _extract_object_type_targets(
        prompt=AQUASHIELD_COMPACT_PROMPT,
        focus_object_types=[],
        estimated_count=estimate["estimated_count"],
        chunk_estimate=estimate,
    )

    assert targets == {
        "groups": 1,
        "brands": 1,
        "ticket_fields": 4,
        "ticket_forms": 1,
        "views": 2,
        "triggers": 3,
        "macros": 2,
        "automations": 1,
        "articles": 2,
        "categories": 1,
        "sections": 1,
    }
    assert sum(targets.values()) == 19
    assert _extract_article_category_names(AQUASHIELD_COMPACT_PROMPT) == [
        "AquaShield Support"
    ]
    assert _extract_article_section_names(AQUASHIELD_COMPACT_PROMPT) == [
        "Product Help and Installation"
    ]

    fields = _extract_ticket_field_specs_from_prompt(AQUASHIELD_COMPACT_PROMPT)
    assert [(item["title"], item["field_type"]) for item in fields] == [
        ("Issue Type", "tagger"),
        ("Product Type", "tagger"),
        ("Product Serial Number", "text"),
        ("Appointment Date", "date"),
    ]
    assert fields[1]["options"] == ["Filter System", "Water Softener"]

    forms = _extract_ticket_form_specs_from_prompt(AQUASHIELD_COMPACT_PROMPT)
    assert forms == [
        {
            "title": "AquaShield Support Request",
            "fields": [
                "Subject",
                "Description",
                "Issue Type",
                "Product Type",
                "Product Serial Number",
                "Appointment Date",
                "Priority",
            ],
        }
    ]
    assert len(_extract_view_specs_from_prompt(AQUASHIELD_COMPACT_PROMPT)) == 2
    assert len(_extract_trigger_specs_from_prompt(AQUASHIELD_COMPACT_PROMPT)) == 3
    assert len(_extract_macro_specs_from_prompt(AQUASHIELD_COMPACT_PROMPT)) == 2
    assert len(_extract_automation_specs_from_prompt(AQUASHIELD_COMPACT_PROMPT)) == 1
    assert len(_extract_article_specs_from_prompt(AQUASHIELD_COMPACT_PROMPT)) == 2

    for object_type, target_count in targets.items():
        assert _should_use_explicit_template_first(
            prompt=AQUASHIELD_COMPACT_PROMPT,
            object_type=object_type,
            target_count=target_count,
        )


def test_aquashield_templates_generate_complete_safe_preview_manifest():
    targets = _extract_object_type_targets(
        prompt=AQUASHIELD_COMPACT_PROMPT,
        focus_object_types=[],
        estimated_count=19,
        chunk_estimate=None,
    )
    rows: list[dict] = []
    for object_type in (
        "brands",
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
    ):
        rows.extend(
            _build_deterministic_chunk_rows(
                object_type=object_type,
                target_count=targets[object_type],
                prompt=AQUASHIELD_COMPACT_PROMPT,
                reference_catalog={},
                existing_titles=[],
                generated_rows=rows,
                reason="Explicit prompt template-first generation.",
                backlog_item={"_chunk_offset": 0},
            )
        )

    assert len(rows) == 19
    assert sum(row["object_type"] == "ticket_fields" for row in rows) == 4
    assert sum(row["object_type"] == "views" for row in rows) == 2
    assert sum(row["object_type"] == "triggers" for row in rows) == 3
    assert sum(row["object_type"] == "macros" for row in rows) == 2
    assert sum(row["object_type"] == "articles" for row in rows) == 2

    section = next(row for row in rows if row["object_type"] == "sections")
    assert {"field": "category_name", "value": "AquaShield Support"} in section["actions"]
    for trigger in (row for row in rows if row["object_type"] == "triggers"):
        action_fields = {item["field"] for item in trigger["actions"]}
        assert {"group_id", "current_tags"} <= action_fields
        assert trigger["conditions"]
    for article in (row for row in rows if row["object_type"] == "articles"):
        action_map = {item["field"]: item["value"] for item in article["actions"]}
        assert action_map["section_name"] == "Product Help and Installation"
        assert len(action_map["body"]) >= 350

    safety = _evaluate_generation_safety(
        prompt=AQUASHIELD_COMPACT_PROMPT,
        plan={"confidence": 0.7},
        generated_rows=rows,
        focus_object_types=set(),
        min_confidence=0.65,
    )
    assert safety["blocked"] is False
    assert safety["explicit_constraint_violations"] == []


def test_generation_safety_blocks_only_records_related_to_missing_tag():
    generated_rows = [
        {
            "record_id": "REC-0001",
            "object_type": "brands",
            "title": "AquaShield Home Services",
            "conditions": [],
            "actions": [],
            "dependency_notes": [],
        },
        {
            "record_id": "REC-0002",
            "object_type": "triggers",
            "title": "Route AquaShield",
            "conditions": [{"field": "status", "operator": "is", "value": "new"}],
            "actions": [{"field": "group_id", "value": "AquaShield Customer Support"}],
            "dependency_notes": [],
        },
    ]
    safety = _evaluate_generation_safety(
        prompt="Create the brand and trigger, and add tag required_tag to the trigger.",
        plan={"confidence": 0.9},
        generated_rows=generated_rows,
        focus_object_types=set(),
        min_confidence=0.65,
    )

    assert safety["blocked"] is True
    assert safety["global_blocked"] is False
    assert safety["blocked_record_ids"] == ["REC-0002"]

    preview_rows = [
        {
            "record_id": "REC-0001",
            "validation_status": "passed",
            "deployable": True,
            "import_decision": "pending_review",
            "warnings": [],
        },
        {
            "record_id": "REC-0002",
            "validation_status": "warning",
            "deployable": True,
            "import_decision": "pending_review",
            "warnings": [],
        },
    ]
    patched = _apply_generation_safety_to_preview(preview_rows, safety)
    assert patched[0]["validation_status"] == "passed"
    assert patched[0]["deployable"] is True
    assert patched[1]["validation_status"] == "failed"
    assert patched[1]["import_decision"] == "blocked"


def test_markdown_groups_section_preserves_named_groups_and_target_count():
    prompt = """# Build a Multi-Brand Zendesk AI Copilot

# Groups
- HomeSphere Tier 1 Support
- AquaShield Technical Support
- Billing and Refunds - handles payment escalations

# User Roles
- Administrator
"""

    assert _extract_inline_support_team_names(prompt) == [
        "HomeSphere Tier 1 Support",
        "AquaShield Technical Support",
        "Billing and Refunds",
    ]

    targets = _extract_object_type_targets(
        prompt=prompt,
        focus_object_types=[],
        estimated_count=1,
        chunk_estimate=None,
    )
    assert targets["groups"] == 3

    rows = _build_deterministic_chunk_rows(
        object_type="groups",
        target_count=3,
        prompt=prompt,
        reference_catalog={},
        existing_titles=[],
        generated_rows=[],
        reason="planner fallback",
    )
    assert [row["title"] for row in rows] == [
        "HomeSphere Tier 1 Support",
        "AquaShield Technical Support",
        "Billing and Refunds",
    ]


def test_clearsky_prompt_extracts_exact_teams_categories_forms_and_targets():
    assert _extract_inline_support_team_names(CLEARSKY_REGRESSION_PROMPT) == [
        "Personal Lines Support",
        "Commercial Lines Support",
        "Claims",
        "Underwriting",
        "Client Retention",
    ]
    assert _extract_article_category_names(CLEARSKY_REGRESSION_PROMPT) == [
        "Claims",
        "Policy Management",
        "New Business",
    ]
    form_specs = _extract_ticket_form_specs_from_prompt(CLEARSKY_REGRESSION_PROMPT)
    assert [item["title"] for item in form_specs] == [
        "ClearSky Personal Lines Form",
        "ClearSky Claims Form",
        "ClearSky Commercial Lines Form",
    ]
    assert form_specs[1]["fields"] == [
        "Subject",
        "Description",
        "Query Type",
        "Policy Type",
        "Client Segment",
        "Claim Status",
        "Policy Number",
        "Priority",
    ]

    targets = _extract_object_type_targets(
        prompt=CLEARSKY_REGRESSION_PROMPT,
        focus_object_types=[],
        estimated_count=34,
        chunk_estimate=None,
    )
    assert targets == {
        "categories": 3,
        "sections": 3,
        "groups": 5,
        "ticket_fields": 5,
        "ticket_forms": 3,
        "views": 5,
        "triggers": 6,
        "macros": 4,
        "articles": 5,
    }
    assert _should_bypass_planner_for_explicit_manifest(
        prompt=CLEARSKY_REGRESSION_PROMPT,
        object_targets=targets,
        force_wave_chunk_reason="create_following_numbered_prompt",
    ) is True


def test_planner_bypass_keeps_less_explicit_multi_object_prompts_on_model_route():
    assert _should_bypass_planner_for_explicit_manifest(
        prompt="Create the following: some triggers, views, macros, and forms for our support team.",
        object_targets={"triggers": 3, "views": 2, "macros": 2, "ticket_forms": 1},
        force_wave_chunk_reason="create_following_numbered_prompt",
    ) is False


def test_clearsky_field_parser_adds_later_referenced_underwriting_option():
    field_specs = _extract_ticket_field_specs_from_prompt(CLEARSKY_REGRESSION_PROMPT)
    query_type = next(item for item in field_specs if item["title"] == "Query Type")
    assert "Underwriting" in query_type["options"]
    assert query_type["implied_options_added"] == ["Underwriting"]


def test_clearsky_explicit_trigger_specs_compile_exact_routing_actions():
    prompt = """1. Two ticket fields:
   - A dropdown called "Query Type" with options: Claim Submission, Complaint
   - A dropdown called "Client Segment" with options: High Value Client
2. Two triggers:
   - When a ticket is created and Query Type is "Claim Submission"
     and Client Segment is "High Value Client", assign to Claims,
     add tag "hv_claim_submission", and set priority to Urgent
   - When a ticket is created and Query Type is "Complaint",
     assign to Personal Lines Support, add tag "client_complaint",
     and set priority to High
3. End.
"""
    specs = _extract_trigger_specs_from_prompt(prompt)

    assert len(specs) == 2
    assert specs[0]["conditions"] == [
        {"field": "status", "operator": "is", "value": "new"},
        {"field": "custom_field_query_type", "operator": "is", "value": "claim_submission"},
        {"field": "custom_field_client_segment", "operator": "is", "value": "high_value_client"},
    ]
    assert specs[0]["actions"] == [
        {"field": "group_id", "value": "Claims"},
        {"field": "current_tags", "value": "hv_claim_submission"},
        {"field": "priority", "value": "urgent"},
    ]
    assert _should_use_explicit_template_first(
        prompt=prompt,
        object_type="triggers",
        target_count=2,
    ) is True


def test_clearsky_explicit_views_compile_filters_and_sort_output():
    prompt = """1. Two ticket fields:
   - A dropdown called "Query Type" with options: Claim Submission, New Quote Request
   - A dropdown called "Client Segment" with options: Individual, Corporate
2. Two triggers:
   - When a ticket is created and Query Type is "Claim Submission", assign to Claims,
     add tag "claim_submission", and set priority to High
   - When a ticket is created and Query Type is "New Quote Request"
     and Client Segment is "Corporate", assign to Commercial Lines Support,
     add tag "corporate_quote", and set priority to Normal
3. Three views:
   - All open Claim Submission tickets assigned to Claims sorted by priority descending
   - All open Corporate Quote tickets assigned to Commercial Lines Support sorted by creation date
   - All unassigned tickets across all teams sorted by oldest first
4. End.
"""
    specs = _extract_view_specs_from_prompt(prompt)

    assert len(specs) == 3
    assert specs[0]["conditions"][-2:] == [
        {"field": "current_tags", "operator": "includes", "value": "claim_submission"},
        {"field": "group_id", "operator": "is", "value": "Claims"},
    ]
    assert specs[0]["actions"][-2:] == [
        {"field": "sort_by", "value": "priority"},
        {"field": "sort_order", "value": "desc"},
    ]
    assert specs[1]["conditions"][-2:] == [
        {"field": "current_tags", "operator": "includes", "value": "corporate_quote"},
        {"field": "group_id", "operator": "is", "value": "Commercial Lines Support"},
    ]
    assert specs[2]["conditions"][-1] == {
        "field": "assignee_id",
        "operator": "is",
        "value": "",
    }
    assert _should_use_explicit_template_first(
        prompt=prompt,
        object_type="views",
        target_count=3,
    ) is True


def test_clearsky_explicit_macro_specs_preserve_reply_and_private_note_modes():
    prompt = """4. Two macros:
   - A macro called "Acknowledge Claim" that sends a reply:
     "We received your claim and will respond within two business days."
     and adds tag "claim_acknowledged"
   - A macro called "Escalate High Value Claim" that assigns the ticket
     to Claims, sets priority to Urgent, adds tag "hv_claim_escalated",
     and adds an internal note: "Assign a senior assessor within four hours."
5. End.
"""
    specs = _extract_macro_specs_from_prompt(prompt)

    assert len(specs) == 2
    assert specs[0]["comment_is_public"] is True
    assert specs[0]["tags"] == ["claim_acknowledged"]
    assert specs[1] == {
        "title": "Escalate High Value Claim",
        "comment": "Assign a senior assessor within four hours.",
        "comment_is_public": False,
        "group": "Claims",
        "priority": "urgent",
        "tags": ["hv_claim_escalated"],
    }

    rows = _build_deterministic_chunk_rows(
        object_type="macros",
        target_count=2,
        prompt=prompt,
        reference_catalog={},
        existing_titles=[],
        generated_rows=[],
        reason="template-first",
        backlog_item={"_chunk_offset": 0},
    )
    private_note = rows[1]
    assert any(
        action["field"] == "comment_mode_is_public" and action["value"] is False
        for action in private_note["actions"]
    )
    assert any(
        action["field"] == "group_id" and action["value"] == "Claims"
        for action in private_note["actions"]
    )


def test_clearsky_group_titles_do_not_collide_with_category_or_section_titles():
    generated_rows = [
        {"object_type": "categories", "title": "Claims"},
        {"object_type": "sections", "title": "Claims"},
    ]

    rows = _build_deterministic_chunk_rows(
        object_type="groups",
        target_count=5,
        prompt=CLEARSKY_REGRESSION_PROMPT,
        reference_catalog={},
        existing_titles=["Claims"],
        generated_rows=generated_rows,
        reason="template-first",
    )

    assert [row["title"] for row in rows] == [
        "Personal Lines Support",
        "Commercial Lines Support",
        "Claims",
        "Underwriting",
        "Client Retention",
    ]


def test_targeted_group_rebuild_excludes_its_own_source_chunk():
    source_chunk_id = "BL-003:1"
    generated_rows = [
        {
            "object_type": "groups",
            "title": title,
            "_supervisor_chunk_id": source_chunk_id,
        }
        for title in (
            "Personal Lines Support",
            "Commercial Lines Support",
            "Claims",
            "Underwriting",
            "Client Retention",
        )
    ]

    rows = _build_deterministic_chunk_rows(
        object_type="groups",
        target_count=5,
        prompt=CLEARSKY_REGRESSION_PROMPT,
        reference_catalog={},
        existing_titles=[row["title"] for row in generated_rows],
        generated_rows=generated_rows,
        reason="Supervisor repair required for this chunk only.",
        excluded_chunk_id=source_chunk_id,
    )

    assert [row["title"] for row in rows] == [row["title"] for row in generated_rows]


def test_clearsky_article_fallback_preserves_titles_and_same_batch_sections():
    specs = _extract_article_specs_from_prompt(CLEARSKY_REGRESSION_PROMPT)
    assert len(specs) == 5
    assert specs[1]["category"] == "Policy Management"

    rows = _build_deterministic_chunk_rows(
        object_type="articles",
        target_count=5,
        prompt=CLEARSKY_REGRESSION_PROMPT,
        reference_catalog={},
        existing_titles=[],
        generated_rows=[],
        reason="model output malformed",
    )
    assert [row["title"] for row in rows] == [item["title"] for item in specs]
    section_names = [
        next(action["value"] for action in row["actions"] if action["field"] == "section_name")
        for row in rows
    ]
    assert section_names == [item["category"] for item in specs]
    assert all(
        len(next(action["value"] for action in row["actions"] if action["field"] == "body")) > 180
        for row in rows
    )


def test_clearsky_runtime_chunks_consolidate_to_nine_initial_review_bundles():
    chunks = [
        {"wave": 1, "object_type": "categories"},
        {"wave": 1, "object_type": "sections"},
        {"wave": 2, "object_type": "groups"},
        {"wave": 2, "object_type": "ticket_fields"},
        {"wave": 3, "object_type": "ticket_forms"},
        {"wave": 3, "object_type": "views"},
        {"wave": 3, "object_type": "views"},
        {"wave": 4, "object_type": "triggers"},
        {"wave": 4, "object_type": "triggers"},
        {"wave": 4, "object_type": "macros"},
        {"wave": 5, "object_type": "articles"},
        {"wave": 5, "object_type": "articles"},
    ]
    review_units = {
        (int(chunk["wave"]), _supervisor_review_bundle_key(chunk))
        for chunk in chunks
    }
    assert len(chunks) == 12
    assert len(review_units) == 9


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


def test_department_hybrid_strategy_routes_macros_and_articles_to_models():
    backlog_item = {"source": "department_coverage"}

    assert _should_use_department_template_first(
        backlog_item,
        "triggers",
        strategy="hybrid",
    ) is True
    assert _should_use_department_template_first(
        backlog_item,
        "automations",
        strategy="hybrid",
    ) is True
    assert _should_use_department_template_first(
        backlog_item,
        "macros",
        strategy="hybrid",
    ) is False
    assert _should_use_department_template_first(
        backlog_item,
        "articles",
        strategy="hybrid",
    ) is False


def test_wave_generator_route_cursor_rotates_across_department_items():
    routes = [
        {"profile": "tertiary", "model": "model-c", "api_key": "key-c"},
        {"profile": "secondary", "model": "model-b", "api_key": "key-b"},
        {"profile": "primary", "model": "model-a", "api_key": "key-a"},
    ]
    cursor = 0
    selected = []
    for _ in range(7):
        route, cursor = _select_wave_generator_route(routes, cursor)
        selected.append(route["profile"])

    assert selected == [
        "tertiary",
        "secondary",
        "primary",
        "tertiary",
        "secondary",
        "primary",
        "tertiary",
    ]


def test_department_hybrid_content_draft_overlays_body_without_replacing_structure(monkeypatch):
    class FakeGrokClient:
        async def chat(self, messages, **_kwargs):
            payload = json.loads(messages[1]["content"])
            return json.dumps(
                {
                    "drafts": [
                        {
                            "title": item["title"],
                            "body": (
                                "Please provide the account reference, relevant dates, screenshots, and "
                                "a clear description of the impact. Our specialist will verify the details, "
                                "confirm the next action, and provide an expected resolution time. Escalate "
                                "immediately when there is a safety, fraud, or service continuity risk."
                            ),
                        }
                        for item in payload["draft_targets"]
                    ]
                }
            )

    monkeypatch.setattr(
        "app.services.import_assistant_service.GrokClient",
        FakeGrokClient,
    )
    rows, applied = asyncio.run(
        _draft_department_content_rows(
            object_type="macros",
            target_count=3,
            prompt=APEX_OPERATING_MODEL_PROMPT,
            reference_catalog={},
            existing_titles=[],
            generated_rows=[],
            backlog_item={
                "source": "department_coverage",
                "department_name": "Finance Operations",
                "topic": "Payments",
                "tag": "apex_payment_issue",
            },
            model="test-model",
            api_key="test-key",
        )
    )

    assert applied == 3
    assert len(rows) == 3
    assert all(row["object_type"] == "macros" for row in rows)
    assert all(any(action["field"] == "current_tags" for action in row["actions"]) for row in rows)
    assert all(
        any(
            action["field"] == "comment_value" and "account reference" in action["value"]
            for action in row["actions"]
        )
        for row in rows
    )
    assert all(
        not any("fallback used because model output could not be parsed" in warning for warning in row["warnings"])
        for row in rows
    )
    assert all(
        any("Primary model content draft applied" in note for note in row["dependency_notes"])
        for row in rows
    )
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
    assert any(str(action.get("field")) == "current_tags" for action in row["actions"])
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
    assert any(str(action.get("field")) == "current_tags" for action in row["actions"])


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


def test_explicit_numbered_blueprint_compiler_uses_deterministic_targets():
    targets = {
        "categories": 3,
        "sections": 3,
        "groups": 5,
        "ticket_fields": 5,
        "ticket_forms": 3,
        "views": 5,
        "triggers": 6,
        "macros": 4,
        "articles": 5,
    }
    blueprint, telemetry = asyncio.run(
        _run_business_blueprint_compiler(
            prompt=CLEARSKY_REGRESSION_PROMPT,
            dependency_mode="match_existing_or_create_new",
            focus_object_types=[],
            object_targets=targets,
            planner_route=SimpleNamespace(model="unused", max_output_tokens=300),
            deterministic_only=True,
        )
    )

    assert blueprint["mode"] == "deterministic_fallback"
    assert {
        item["object_type"]: item["target_count"]
        for item in blueprint["target_objects"]
    } == targets
    assert telemetry == {"bypassed": True, "reason": "explicit_numbered_operating_model"}


def test_planner_fallback_detection_skips_redundant_blueprint_model_call():
    assert _planner_used_heuristic_fallback(
        {
            "ambiguity_reasons": [
                "Planner fallback used due to model output parse failure."
            ]
        }
    )
    assert not _planner_used_heuristic_fallback(
        {"ambiguity_reasons": ["Prompt contains inferred routing details."]}
    )


def test_business_blueprint_rate_limit_rotates_to_secondary_groq_key(monkeypatch):
    route_settings = SimpleNamespace(
        llm_model_generator="openai/gpt-oss-20b",
        llm_model_generator_secondary="openai/gpt-oss-20b",
        llm_model_generator_tertiary="openai/gpt-oss-20b",
        llm_model_generator_wave3="",
        llm_model_generator_wave4="",
        xai_api_key="primary-key",
        xai_api_key_secondary="secondary-key",
        xai_api_key_tertiary="tertiary-key",
        xai_api_key_wave3="",
        xai_api_key_wave4="",
    )
    calls = []

    def fake_init(self):
        self.settings = route_settings

    async def fake_chat(self, messages, **kwargs):
        calls.append(kwargs)
        if kwargs.get("api_key_override") == "secondary-key":
            return json.dumps(
                {
                    "capabilities": ["routing"],
                    "target_objects": [
                        {
                            "object_type": "groups",
                            "target_count": 2,
                            "priority": "high",
                            "wave": 2,
                        }
                    ],
                    "assumptions": [],
                    "priorities": [],
                    "dependency_hints": [],
                }
            )
        raise LLMRequestError(
            "rate limited",
            error_class="rate_limited",
            http_status=429,
        )

    monkeypatch.setattr(GrokClient, "__init__", fake_init)
    monkeypatch.setattr(GrokClient, "chat", fake_chat)
    monkeypatch.setattr(
        GrokClient,
        "get_last_call_metrics",
        classmethod(lambda cls, task: {"task": task}),
    )

    blueprint, telemetry = asyncio.run(
        _run_business_blueprint_compiler(
            prompt="Create two groups and two triggers for a multi-brand support operation.",
            dependency_mode="match_existing_or_create_new",
            focus_object_types=[],
            object_targets={"groups": 2, "triggers": 2},
            planner_route=SimpleNamespace(
                model="openai/gpt-oss-20b",
                max_output_tokens=300,
            ),
        )
    )

    assert blueprint["mode"] == "compiler"
    assert telemetry["blueprint_failover_used"] is True
    assert telemetry["blueprint_failover_profile"] == "secondary"
    assert calls[0].get("api_key_override") is None
    assert calls[1]["api_key_override"] == "secondary-key"
    assert calls[1]["prefer_provider"] == "groq"


def test_business_blueprint_uses_deterministic_fallback_when_all_keys_are_limited(
    monkeypatch,
):
    route_settings = SimpleNamespace(
        llm_model_generator="openai/gpt-oss-20b",
        llm_model_generator_secondary="openai/gpt-oss-20b",
        llm_model_generator_tertiary="openai/gpt-oss-20b",
        llm_model_generator_wave3="",
        llm_model_generator_wave4="",
        xai_api_key="primary-key",
        xai_api_key_secondary="secondary-key",
        xai_api_key_tertiary="tertiary-key",
        xai_api_key_wave3="",
        xai_api_key_wave4="",
    )
    calls = []

    def fake_init(self):
        self.settings = route_settings

    async def fake_chat(self, messages, **kwargs):
        calls.append(kwargs)
        raise LLMRequestError(
            "rate limited",
            error_class="rate_limited",
            http_status=429,
        )

    monkeypatch.setattr(GrokClient, "__init__", fake_init)
    monkeypatch.setattr(GrokClient, "chat", fake_chat)
    monkeypatch.setattr(
        GrokClient,
        "get_last_call_metrics",
        classmethod(lambda cls, task: {"task": task}),
    )

    blueprint, telemetry = asyncio.run(
        _run_business_blueprint_compiler(
            prompt="Create two groups and two triggers for a multi-brand support operation.",
            dependency_mode="match_existing_or_create_new",
            focus_object_types=[],
            object_targets={"groups": 2, "triggers": 2},
            planner_route=SimpleNamespace(
                model="openai/gpt-oss-20b",
                max_output_tokens=300,
            ),
        )
    )

    assert blueprint["mode"] == "deterministic_fallback"
    assert telemetry["blueprint_fallback_used"] is True
    assert telemetry["blueprint_fallback_reason"] == "rate_limited"
    assert [item["profile"] for item in telemetry["blueprint_provider_attempts"]] == [
        "gemini_then_primary",
        "secondary",
        "tertiary",
    ]
    assert len(calls) == 3


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


def test_explicit_article_dependency_is_restored_before_supervisor_review():
    prompt = """6. One knowledge base article:
   - An article called "How to Submit a Claim"
     in the category Claims that explains required documents and timeframes
"""
    rows = [
        {
            "object_type": "articles",
            "title": "How to Submit a Claim",
            "conditions": [],
            "actions": [{"field": "body", "value": "<p>Detailed claims guidance.</p>"}],
        }
    ]

    assert _apply_explicit_article_dependencies(rows, prompt=prompt) == 1
    assert rows[0]["actions"][-1] == {"field": "section_name", "value": "Claims"}
    assert _apply_explicit_article_dependencies(rows, prompt=prompt) == 0

    rows[0]["actions"][-1] = {"field": "section", "value": "Claims"}
    assert _apply_explicit_article_dependencies(rows, prompt=prompt) == 1
    assert not any(action["field"] == "section" for action in rows[0]["actions"])
    assert any(
        action["field"] == "section_name" and action["value"] == "Claims"
        for action in rows[0]["actions"]
    )


def test_form_canonicalization_does_not_create_custom_copies_of_system_fields():
    rows = [
        {
            "object_type": "ticket_fields",
            "title": "Policy Number",
            "conditions": [],
            "actions": [{"field": "field_type", "value": "text"}],
        },
        {
            "object_type": "ticket_forms",
            "title": "Claims Form",
            "conditions": [],
            "actions": [
                {
                    "field": "ticket_field_names",
                    "value": ["Subject", "Description", "Policy Number", "Priority"],
                }
            ],
        },
    ]

    normalized_rows, _field_meta, form_meta, _canonicalization = _canonicalize_generated_rows(
        rows=rows,
        prompt="Create a claims form with Subject, Description, Policy Number, and Priority.",
        reference_catalog={"ticket_fields": [], "sections": []},
        existing_index={},
        related_lookup={},
        catalog_lookup={},
        settings=SimpleNamespace(
            inference_policy="infer_with_warnings",
            form_missing_field_mode="auto_create",
            ticket_field_default_agent_can_edit=True,
            ticket_field_default_visible_in_portal=True,
            ticket_field_default_editable_in_portal=False,
            ticket_field_default_required=False,
            ticket_field_default_required_in_portal=False,
        ),
    )

    generated_field_titles = [
        row["title"] for row in normalized_rows if row["object_type"] == "ticket_fields"
    ]
    assert generated_field_titles == ["Policy Number"]
    assert form_meta["auto_created_fields"] == 0
    assert form_meta["system_field_references"] == 3
    form = next(row for row in normalized_rows if row["object_type"] == "ticket_forms")
    field_names = next(
        action["value"] for action in form["actions"] if action["field"] == "ticket_field_names"
    )
    assert field_names == ["Subject", "Description", "Policy Number", "Priority"]


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


def test_article_canonicalization_prefers_supervisor_body_over_short_content_alias():
    improved_body = (
        "<h3>Submit your claim</h3><p>Gather your policy number, incident date, photographs, "
        "police reference when applicable, and proof of loss. Submit the request within the "
        "required timeframe and keep all original evidence.</p><p>A claims assessor will confirm "
        "receipt, identify missing documents, explain the assessment stages, and provide the next "
        "update date.</p>"
    )
    rows = [
        {
            "object_type": "sections",
            "title": "Claims",
            "conditions": [],
            "actions": [{"field": "category_name", "value": "Claims"}],
        },
        {
            "object_type": "articles",
            "title": "How to Submit a Claim",
            "conditions": [],
            "actions": [
                {"field": "content", "value": "Short generator summary."},
                {"field": "body", "value": improved_body},
                {"field": "section_name", "value": "Claims"},
            ],
        },
    ]

    normalized_rows, *_ = _canonicalize_generated_rows(
        rows=rows,
        prompt="Create a detailed claims article.",
        reference_catalog={"ticket_fields": [], "sections": []},
        existing_index={},
        related_lookup={},
        catalog_lookup={},
        settings=SimpleNamespace(
            inference_policy="infer_with_warnings",
            form_missing_field_mode="warn",
            ticket_field_default_agent_can_edit=True,
            ticket_field_default_visible_in_portal=True,
            ticket_field_default_editable_in_portal=False,
            ticket_field_default_required=False,
            ticket_field_default_required_in_portal=False,
        ),
    )

    article = next(row for row in normalized_rows if row["object_type"] == "articles")
    assert not any(action["field"] == "content" for action in article["actions"])
    assert next(action["value"] for action in article["actions"] if action["field"] == "body") == improved_body


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
