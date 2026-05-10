from collections import Counter
from datetime import UTC, datetime
import re
from uuid import uuid4

from app.api.grok.routing import resolve_model_route
from app.core.settings import get_settings
from app.models.schemas import (
    ApprovalResponse,
    ApprovalSummary,
    ImportAssistantGenerateRequest,
    ImportAssistantGenerateResponse,
    JobListItem,
    JobListResponse,
    JobStatusResponse,
    PreviewRecord,
    PreviewResponse,
    ValidationSummary,
)
from app.services.batch_store import get_batch_store
from app.services.generator import run_generator
from app.services.planner import run_planner
from app.services.appscript_bridge import AppScriptBridgeService
from app.services.sheets_service import SheetsService
from app.services.zendesk import deploy_records_to_zendesk

TAB_OBJECT_TYPES = {
    "trigger": "triggers",
    "triggers": "triggers",
    "automation": "automations",
    "automations": "automations",
    "macro": "macros",
    "macros": "macros",
    "view": "views",
    "views": "views",
    "group": "groups",
    "groups": "groups",
    "ticket_field": "ticket_fields",
    "ticket_fields": "ticket_fields",
    "ticket_form": "ticket_forms",
    "ticket_forms": "ticket_forms",
    "article": "articles",
    "articles": "articles",
    "tag_dictionary": "tag_dictionary",
}

REFERENCE_OBJECT_BY_FIELD = {
    "group_id": "group",
    "ticket_form_id": "ticket_form",
    "form_id": "ticket_form",
    "brand_id": "brand",
    "section_id": "section",
    "category_id": "category",
    "help_center_id": "help_center",
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _new_batch_id() -> str:
    return f"BATCH-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6].upper()}"


def _normalize_object_type(raw: str) -> str:
    normalized = raw.strip().lower()
    return TAB_OBJECT_TYPES.get(normalized, "triggers")


def _is_numeric_string(value: object) -> bool:
    return str(value).strip().isdigit()


def _build_related_lookup(related_objects: list[dict]) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for item in related_objects:
        if not isinstance(item, dict):
            continue
        obj_type = str(item.get("object_type", "")).strip().lower()
        obj_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip().lower()
        if not obj_type or not obj_id or not name:
            continue
        lookup.setdefault(obj_type, {})[name] = obj_id
    return lookup


def _apply_dependency_resolution(
    rows: list[dict],
    *,
    related_lookup: dict[str, dict[str, str]],
    dependency_mode: str,
) -> tuple[list[dict], dict]:
    if not related_lookup:
        return rows, {"resolved_links": 0, "unresolved_links": 0, "unresolved_samples": []}

    resolved_links = 0
    unresolved_links = 0
    unresolved_samples: list[str] = []
    patched_rows: list[dict] = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        patched_row = {
            **row,
            "conditions": list(row.get("conditions", []) or []),
            "actions": list(row.get("actions", []) or []),
        }
        row_notes: list[str] = []

        for bucket_name in ("conditions", "actions"):
            bucket = patched_row.get(bucket_name, [])
            for entry in bucket:
                if not isinstance(entry, dict):
                    continue
                field = str(entry.get("field", "")).strip().lower()
                expected_object = REFERENCE_OBJECT_BY_FIELD.get(field)
                if not expected_object:
                    continue
                raw_value = entry.get("value")
                if raw_value is None:
                    continue
                value = str(raw_value).strip()
                if not value:
                    continue
                if _is_numeric_string(value):
                    continue

                resolved = related_lookup.get(expected_object, {}).get(value.lower())
                if resolved:
                    entry["value"] = resolved
                    resolved_links += 1
                    row_notes.append(
                        f"{field}: mapped '{value}' to ID {resolved} from selected context."
                    )
                    continue

                unresolved_links += 1
                if len(unresolved_samples) < 10:
                    unresolved_samples.append(f"{field}:{value}")
                row_notes.append(
                    f"{field}: '{value}' not found in selected context."
                )

        if row_notes:
            existing = list(patched_row.get("dependency_notes", []) or [])
            patched_row["dependency_notes"] = [*existing, *row_notes]
        patched_rows.append(patched_row)

    if dependency_mode == "force_existing_only" and unresolved_links > 0:
        for row in patched_rows:
            notes = list(row.get("dependency_notes", []) or [])
            if any("not found in selected context" in note for note in notes):
                row.setdefault("validation_overrides", {})
                row["validation_overrides"]["blocked_reason"] = (
                    "Dependency resolution required existing object IDs, but one or more references were not found."
                )

    return patched_rows, {
        "resolved_links": resolved_links,
        "unresolved_links": unresolved_links,
        "unresolved_samples": unresolved_samples,
    }


def _build_preview_records(
    plan: dict,
    generated_data: list[dict],
) -> tuple[list[dict], ValidationSummary]:
    plan_object_type = _normalize_object_type(str(plan.get("object_type", "triggers")))
    records: list[dict] = []
    passed = 0
    warnings = 0
    blocked = 0

    for idx, item in enumerate(generated_data, start=1):
        object_type = _normalize_object_type(str(item.get("object_type", plan_object_type)))
        title = str(item.get("title", "")).strip()
        conditions = item.get("conditions", []) or []
        actions = item.get("actions", []) or []
        row_warnings: list[str] = []
        blocked_reason = None
        validation_status: str = "passed"
        dependency_notes = list(item.get("dependency_notes", []) or [])
        validation_overrides = item.get("validation_overrides", {}) or {}
        override_blocked_reason = str(validation_overrides.get("blocked_reason", "")).strip()
        if override_blocked_reason:
            blocked_reason = override_blocked_reason
            validation_status = "failed"

        if not title:
            blocked_reason = "Missing title."
            validation_status = "failed"
        if not actions:
            row_warnings.append("No actions defined for this record.")
        if object_type in {"triggers", "automations", "views"} and not conditions:
            row_warnings.append("Record has no conditions; verify routing/filter logic.")
        if object_type in {"groups"} and not title:
            row_warnings.append("Group record requires a name/title.")
        if dependency_notes:
            row_warnings.extend(dependency_notes)

        if blocked_reason:
            blocked += 1
        elif row_warnings:
            warnings += 1
            validation_status = "warning"
        else:
            passed += 1

        records.append(
            {
                "record_id": f"REC-{idx:04d}",
                "object_type": object_type,
                "title": title or f"Untitled {idx}",
                "preview_summary": f"{object_type.replace('_', ' ').title()} configuration record.",
                "validation_status": validation_status,
                "warnings": row_warnings,
                "blocked_reason": blocked_reason,
                "import_decision": "blocked" if blocked_reason else "pending_review",
                "deployable": blocked_reason is None,
                "conditions": conditions,
                "actions": actions,
                "deployment_status": "pending",
                "zendesk_object_id": None,
                "execution_message": "",
            }
        )

    return records, ValidationSummary(passed=passed, warnings=warnings, blocked=blocked)


def _count_generated(records: list[dict]) -> dict[str, int]:
    counts = Counter(row.get("object_type", "recommendations") for row in records)
    return dict(counts)


CATALOG_OBJECT_NORMALIZATION = {
    "brands": "brands",
    "brand": "brands",
    "groups": "groups",
    "group": "groups",
    "ticket_forms": "ticket_forms",
    "ticket_form": "ticket_forms",
    "triggers": "triggers",
    "trigger": "triggers",
    "automations": "automations",
    "automation": "automations",
    "macros": "macros",
    "macro": "macros",
    "views": "views",
    "view": "views",
    "ticket_fields": "ticket_fields",
    "ticket_field": "ticket_fields",
    "articles": "articles",
    "article": "articles",
    "help_centers": "help_centers",
    "help_center": "help_centers",
    "categories": "categories",
    "category": "categories",
    "sections": "sections",
    "section": "sections",
}


def _build_existing_object_index(reference_catalog: dict[str, list[dict]]) -> dict[str, dict[str, dict]]:
    index: dict[str, dict[str, dict]] = {}
    for raw_key, rows in reference_catalog.items():
        canonical_key = CATALOG_OBJECT_NORMALIZATION.get(str(raw_key).strip().lower(), "")
        if not canonical_key:
            continue
        bucket = index.setdefault(canonical_key, {})
        for item in rows:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            bucket[name.lower()] = item
    return index


def _annotate_duplicate_candidates(
    rows: list[dict],
    *,
    existing_index: dict[str, dict[str, dict]],
    dependency_mode: str,
) -> tuple[list[dict], list[dict]]:
    if not existing_index:
        return rows, []

    duplicate_candidates: list[dict] = []
    patched_rows: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        object_type = _normalize_object_type(str(row.get("object_type", "triggers")))
        title = str(row.get("title", "")).strip()
        if not title:
            patched_rows.append(row)
            continue

        existing_match = existing_index.get(object_type, {}).get(title.lower())
        if not existing_match:
            if dependency_mode == "force_existing_only":
                row.setdefault("validation_overrides", {})
                row["validation_overrides"]["blocked_reason"] = (
                    "Existing-only mode is active and no existing object with this exact title was found."
                )
            patched_rows.append(row)
            continue

        existing_id = str(existing_match.get("id", "")).strip()
        existing_description = str(existing_match.get("description", "")).strip()
        note = (
            f"Duplicate candidate: existing {object_type.rstrip('s')} '{title}'"
            + (f" (id={existing_id})" if existing_id else "")
            + ". Confirm whether to reuse/update or keep creating new."
        )

        existing_notes = list(row.get("dependency_notes", []) or [])
        existing_notes.append(note)
        row["dependency_notes"] = existing_notes

        duplicate_candidates.append(
            {
                "object_type": object_type,
                "title": title,
                "existing_id": existing_id or None,
                "existing_description": existing_description or None,
            }
        )
        patched_rows.append(row)

    return patched_rows, duplicate_candidates


def _is_explicit_enough_for_generation(prompt: str, object_type: str) -> bool:
    text = prompt.strip().lower()
    if not text:
        return False

    if object_type in {"triggers", "automations", "views"}:
        mentions_action = any(
            token in text for token in ["add tag", "set tag", "tag", "status", "priority", "group", "notify", "comment"]
        )
        mentions_scope = any(
            token in text
            for token in [
                "all groups",
                "all forms",
                "all statuses",
                "any open ticket",
                "open ticket",
                "status is open",
                "status=open",
            ]
        )
        mentions_concrete_condition = any(
            token in text
            for token in [
                "where",
                "apply to",
                "status is",
                "status=",
                "open ticket",
                "new ticket",
                "group",
                "form",
                "brand",
            ]
        )
        tag_value = re.search(
            r"\btag(?:\s+name)?\s*(?:is|=|to|as)\s*[\"']?([a-z0-9_-]{2,})",
            text,
        )
        add_tag_value = re.search(
            r"\badd(?:s)?\s+(?:the\s+)?tag\s+[\"']?([a-z0-9_-]{2,})",
            text,
        )
        status_action_value = re.search(
            r"\b(?:set|change|update)\s+status\s*(?:to|=|as)?\s*([a-z_]+)",
            text,
        )
        mentions_action_value = bool(tag_value or add_tag_value or status_action_value)
        return mentions_action and mentions_action_value and (mentions_scope or mentions_concrete_condition)

    if object_type == "macros":
        explicit_message = bool(
            re.search(r"comment\s*:\s*.+", text)
            or re.search(r"reply\s*:\s*.+", text)
            or re.search(r"[\"'].{4,}[\"']", text)
        )
        explicit_side_effect = bool(
            re.search(r"\btag(?:\s+name)?\s*(?:is|=|to|as)\s*[\"']?([a-z0-9_-]{2,})", text)
            or re.search(r"\b(?:set|change|update)\s+status\s*(?:to|=|as)?\s*([a-z_]+)", text)
            or re.search(r"\b(?:set|assign)\s+group\s*(?:to|=|as)?\s*[\"']?([a-z0-9 _-]{2,})", text)
        )
        return explicit_message or explicit_side_effect

    if object_type == "articles":
        mentions_location = any(token in text for token in ["section", "category", "help center", "knowledge base"])
        mentions_content = any(token in text for token in ["article", "title", "body", "publish"])
        return mentions_location and mentions_content

    return len(text) >= 20


def _build_clarification_questions(
    prompt: str,
    plan: dict,
    *,
    ambiguity_score: float,
    ambiguity_threshold: float,
) -> list[dict]:
    text = prompt.strip().lower()
    object_type = _normalize_object_type(str(plan.get("object_type", "triggers")))
    questions: list[dict] = []
    prompt_explicit = _is_explicit_enough_for_generation(prompt, object_type)
    confidence = float(plan.get("confidence", 0.0) or 0.0)
    planner_questions = plan.get("clarification_questions", [])
    include_planner_questions = (
        not prompt_explicit
        and isinstance(planner_questions, list)
        and (
            ambiguity_score >= max(ambiguity_threshold + 0.08, 0.68)
            or confidence < 0.62
        )
    )
    if include_planner_questions:
        for index, item in enumerate(planner_questions, start=1):
            question_text = str(item).strip()
            if not question_text:
                continue
            questions.append(
                {
                    "id": f"planner_question_{index}",
                    "question": question_text,
                    "reason": "Planner flagged this as missing detail for reliable generation.",
                    "examples": [],
                }
            )

    has_time_phrase = bool(re.search(r"\b\d+\s*(hour|hours|hr|hrs|day|days)\b", text))
    mentions_message_content = any(
        key in text for key in ["reply", "message", "comment", "body", "template", "say"]
    )
    mentions_action_target = any(
        key in text for key in ["assign", "group", "tag", "status", "priority", "form", "field"]
    )
    mentions_condition = any(
        key in text
        for key in [
            "when",
            "if",
            "for tickets",
            "condition",
            "where",
            "only if",
            "route",
            "routing",
            "triage",
            "all groups",
            "all forms",
            "all statuses",
            "open ticket",
            "status is",
            "apply to",
        ]
    )

    if object_type == "macros" and not prompt_explicit:
        if has_time_phrase:
            questions.append(
                {
                    "id": "macro_vs_automation",
                    "question": "Do you want a manual macro or a timed automation?",
                    "reason": "Macros run manually, while timed behavior requires an automation/trigger flow.",
                    "examples": [
                        "Manual macro only",
                        "Timed automation after 25 hours",
                    ],
                }
            )
        if not mentions_message_content:
            questions.append(
                {
                    "id": "macro_content",
                    "question": "What exact reply/comment text should the macro add?",
                    "reason": "Macro output is ambiguous without message content.",
                    "examples": [
                        "Please share your policy number and claim reference.",
                        "We are following up on your request and will respond within 1 business day.",
                    ],
                }
            )
        if not mentions_action_target:
            questions.append(
                {
                    "id": "macro_side_effects",
                    "question": "Should this macro also set tags, status, assignee group, or priority?",
                    "reason": "No ticket update actions were specified.",
                    "examples": [
                        "Set tag follow_up_25h and status open",
                        "Only add comment, no ticket field changes",
                    ],
                }
            )

    if object_type in {"triggers", "automations", "views"} and not mentions_condition and not prompt_explicit:
        questions.append(
            {
                "id": "conditions_needed",
                "question": "What conditions should this apply to (status, group, form, brand, tags)?",
                "reason": "Rule-like objects need clear filter criteria.",
                "examples": [
                    "Only for status=new and form=Claim & Payouts",
                    "Only for group=Finance & Investments and priority=high",
                ],
            }
        )

    if (
        object_type == "articles"
        and not prompt_explicit
        and not any(key in text for key in ["section", "category", "help center"])
    ):
        questions.append(
            {
                "id": "article_target_location",
                "question": "Which Help Center section/category should the article be created in?",
                "reason": "Article placement requires section/category context.",
                "examples": [
                    "Section: Claim Process",
                    "Category: Pensioners, Section: FAQ",
                ],
            }
        )

    # de-duplicate by normalized text and keep concise.
    deduped: list[dict] = []
    seen: set[str] = set()
    for item in questions:
        key = str(item.get("question", "")).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:1]


def _build_planning_summary(
    *,
    plan: dict,
    request: ImportAssistantGenerateRequest,
    related_objects: list[dict],
    reference_catalog: dict[str, list[dict]],
    prompt_explicit: bool,
) -> dict:
    return {
        "object_type": plan.get("object_type", "triggers"),
        "intent": plan.get("intent", request.prompt),
        "confidence": plan.get("confidence", 0.7),
        "ambiguity_score": plan.get("ambiguity_score", 0.0),
        "prompt_explicit": prompt_explicit,
        "ambiguity_reasons": plan.get("ambiguity_reasons", []),
        "dependency_mode": request.dependency_mode,
        "dependency_notes": plan.get("dependency_notes", ""),
        "related_objects_selected": len(related_objects),
        "reference_catalog_counts": {key: len(values) for key, values in reference_catalog.items()},
        "llm": plan.get("llm", {}),
    }


async def generate_import_assistant_batch(
    request: ImportAssistantGenerateRequest,
) -> ImportAssistantGenerateResponse:
    store = get_batch_store()
    sheets = SheetsService()
    appscript = AppScriptBridgeService()
    settings = get_settings()
    planner_route = resolve_model_route(settings, "planner")
    generator_route = resolve_model_route(settings, "generator")
    related_objects = [item.model_dump() for item in request.related_objects]
    reference_catalog = {
        key: [item.model_dump() for item in values]
        for key, values in (request.reference_catalog or {}).items()
    }
    related_lookup = _build_related_lookup(related_objects)
    existing_object_index = _build_existing_object_index(reference_catalog)

    batch_id = _new_batch_id()
    created_at = _utc_now()
    batch = {
        "batch_id": batch_id,
        "status": "received",
        "prompt": request.prompt,
        "requester": request.requester,
        "target_environment": request.target_environment,
        "mode": request.mode,
        "created_at": created_at,
        "updated_at": created_at,
        "status_history": [{"status": "received", "message": "Batch accepted.", "at": created_at}],
        "records": [],
        "generated_counts": {},
        "validation_summary": {"passed": 0, "warnings": 0, "blocked": 0},
        "planning_summary": {},
        "metadata": {},
    }
    store.save_batch(batch)

    store.append_status(batch_id, "request_validated", "Incoming request validated.")
    store.append_status(batch_id, "planning", "Planner call in progress.")
    plan = await run_planner(
        request.prompt,
        dependency_mode=request.dependency_mode,
        related_objects=related_objects,
        reference_catalog=reference_catalog,
        recent_batch_context=request.recent_batch_context,
        context_notes=request.context_notes,
    )
    store.append_status(batch_id, "planned", "Planner output received.")

    ambiguity_score = float(plan.get("ambiguity_score", 0.0) or 0.0)
    ambiguity_threshold = settings.llm_ambiguity_threshold
    planned_object_type = _normalize_object_type(str(plan.get("object_type", "triggers")))
    prompt_explicit = _is_explicit_enough_for_generation(request.prompt, planned_object_type)
    clarification_questions = _build_clarification_questions(
        request.prompt,
        plan,
        ambiguity_score=ambiguity_score,
        ambiguity_threshold=ambiguity_threshold,
    )
    if ambiguity_score >= ambiguity_threshold and not clarification_questions and not prompt_explicit:
        ambiguity_reasons = plan.get("ambiguity_reasons", [])
        reason_text = (
            str(ambiguity_reasons[0]).strip()
            if isinstance(ambiguity_reasons, list) and ambiguity_reasons
            else "The request appears ambiguous for a reliable one-shot deployment."
        )
        clarification_questions = [
            {
                "id": "ambiguity_scope",
                "question": "Please provide specific scope, conditions, and expected actions for this request.",
                "reason": reason_text,
                "examples": [
                    "Applies to which group/form/brand?",
                    "What exact action should be applied when conditions match?",
                ],
            }
        ]
    if prompt_explicit and clarification_questions:
        # Deterministic override: explicit, actionable prompts should not be trapped in clarification loops.
        clarification_questions = []
    needs_clarification = bool(clarification_questions)
    planning_summary = _build_planning_summary(
        plan=plan,
        request=request,
        related_objects=related_objects,
        reference_catalog=reference_catalog,
        prompt_explicit=prompt_explicit,
    )
    if needs_clarification:
        store.append_status(
            batch_id,
            "clarification_required",
            "Additional details required before generation.",
        )
        batch = store.update_batch(
            batch_id,
            {
                "status": "clarification_required",
                "planning_summary": planning_summary,
                "metadata": {
                    "clarification": {
                        "required": True,
                        "questions": clarification_questions,
                        "ambiguity_score": ambiguity_score,
                        "ambiguity_threshold": ambiguity_threshold,
                    },
                    "llm_routes": {
                        "planner": planner_route.__dict__,
                        "generator": generator_route.__dict__,
                    },
                    "context_notes": request.context_notes or "",
                    "recent_batch_context": request.recent_batch_context,
                },
            },
        )
        return ImportAssistantGenerateResponse(
            batch_id=batch_id,
            status="clarification_required",
            planning_summary=batch.get("planning_summary", {}),
            generated_counts={},
            validation_summary=ValidationSummary(),
            preview_url=f"/api/import-assistant/preview/{batch_id}",
            needs_clarification=True,
            clarification_questions=clarification_questions,
            metadata=batch.get("metadata", {}),
        )

    store.append_status(batch_id, "schemas_selected", "Schemas selected from registry.")
    store.append_status(batch_id, "generating", "Generator call in progress.")
    generated_data = await run_generator(
        plan,
        dependency_mode=request.dependency_mode,
        related_objects=related_objects,
        reference_catalog=reference_catalog,
        recent_batch_context=request.recent_batch_context,
        context_notes=request.context_notes,
    )
    store.append_status(batch_id, "generated", "Structured records generated.")

    generated_data, dependency_resolution = _apply_dependency_resolution(
        generated_data,
        related_lookup=related_lookup,
        dependency_mode=request.dependency_mode,
    )
    generated_data, duplicate_candidates = _annotate_duplicate_candidates(
        generated_data,
        existing_index=existing_object_index,
        dependency_mode=request.dependency_mode,
    )

    preview_records, validation_summary = _build_preview_records(plan, generated_data)
    generated_counts = _count_generated(preview_records)

    store.append_status(batch_id, "staging", "Staging batch to Google Sheets.")
    staging_metadata: dict = {}
    validation_metadata: dict = {}

    if appscript.enabled:
        appscript_stage = await appscript.invoke(
            action="write_batch_to_sheets",
            payload={
                "batch_id": batch_id,
                "prompt": request.prompt,
                "requester": request.requester,
                "status": "staging",
                "target_environment": request.target_environment,
                "created_at": created_at,
                "planning_summary": planning_summary,
                "records": preview_records,
            },
        )
        staging_metadata = {
            "mode": "appscript",
            "status": appscript_stage.get("status"),
            "detail": appscript_stage.get("detail"),
            "http_status": appscript_stage.get("http_status"),
            "result": appscript_stage.get("data", {}),
        }
        if appscript_stage.get("status") != "ok" and sheets.enabled:
            fallback_result = sheets.stage_batch(batch, plan, preview_records)
            staging_metadata["fallback"] = {
                "mode": "google_sheets_service_account",
                "result": fallback_result,
            }
        elif appscript_stage.get("status") != "ok":
            store.append_status(batch_id, "failed", "Apps Script staging failed.")
            raise RuntimeError(
                appscript_stage.get("detail")
                or "Apps Script staging failed and no backend fallback is configured."
            )
    else:
        staging_metadata = {
            "mode": "google_sheets_service_account",
            "result": sheets.stage_batch(batch, plan, preview_records),
        }

    store.append_status(batch_id, "staged", "Batch staged.")

    store.append_status(batch_id, "validating", "Running row-level validation.")
    if appscript.enabled:
        appscript_validation = await appscript.invoke(
            action="validate_batch",
            payload={"batch_id": batch_id},
        )
        validation_metadata = {
            "mode": "appscript",
            "status": appscript_validation.get("status"),
            "detail": appscript_validation.get("detail"),
            "http_status": appscript_validation.get("http_status"),
            "result": appscript_validation.get("data", {}),
        }
        validation_data = appscript_validation.get("data", {})
        if isinstance(validation_data, dict):
            summary_data = validation_data.get("summary", {})
            if isinstance(summary_data, dict):
                try:
                    validation_summary = ValidationSummary(**summary_data)
                except Exception:
                    pass

        if appscript_validation.get("status") != "ok" and sheets.enabled:
            sheets.write_validation_log(batch_id, preview_records)
            validation_metadata["fallback"] = {
                "mode": "google_sheets_service_account",
                "result": "validation log written from backend fallback",
            }
        elif appscript_validation.get("status") != "ok":
            store.append_status(batch_id, "failed", "Apps Script validation failed.")
            raise RuntimeError(
                appscript_validation.get("detail")
                or "Apps Script validation failed and no backend fallback is configured."
            )
    else:
        sheets.write_validation_log(batch_id, preview_records)
        validation_metadata = {
            "mode": "google_sheets_service_account",
            "result": "validation log written",
        }

    appscript_preview_metadata: dict = {}
    if appscript.enabled:
        appscript_preview = await appscript.invoke(
            action="get_batch_preview",
            payload={"batch_id": batch_id},
        )
        appscript_preview_metadata = {
            "mode": "appscript",
            "status": appscript_preview.get("status"),
            "detail": appscript_preview.get("detail"),
            "http_status": appscript_preview.get("http_status"),
        }

    if validation_summary.blocked > 0:
        store.append_status(batch_id, "validated_failed", "Validation produced blocked records.")
        final_status = "validated_failed"
    elif validation_summary.warnings > 0:
        store.append_status(batch_id, "validated_warning", "Validation passed with warnings.")
        final_status = "validated_warning"
    else:
        store.append_status(batch_id, "validated_passed", "Validation passed.")
        final_status = "validated_passed"

    store.append_status(batch_id, "preview_ready", "Preview payload ready.")
    batch = store.update_batch(
        batch_id,
        {
            "status": "preview_ready",
            "records": preview_records,
            "generated_counts": generated_counts,
            "validation_summary": validation_summary.model_dump(),
            "planning_summary": planning_summary,
            "metadata": {
                "validation_phase_status": final_status,
                "dependency_resolution": dependency_resolution,
                "duplicate_candidates": duplicate_candidates,
                "ambiguity_score": ambiguity_score,
                "ambiguity_threshold": ambiguity_threshold,
                "llm_routes": {
                    "planner": planner_route.__dict__,
                    "generator": generator_route.__dict__,
                },
                "context_notes": request.context_notes or "",
                "recent_batch_context": request.recent_batch_context,
                "staging": staging_metadata,
                "validation": validation_metadata,
                "preview_roundtrip": appscript_preview_metadata,
            },
        },
    )

    return ImportAssistantGenerateResponse(
        batch_id=batch_id,
        status="preview_ready",
        planning_summary=batch["planning_summary"],
        generated_counts=generated_counts,
        validation_summary=validation_summary,
        preview_url=f"/api/import-assistant/preview/{batch_id}",
        metadata=batch.get("metadata", {}),
    )


def get_job_status(batch_id: str) -> JobStatusResponse:
    store = get_batch_store()
    batch = store.get_batch(batch_id)
    if not batch:
        raise KeyError(batch_id)
    return JobStatusResponse(**batch)


def list_recent_batches(limit: int = 20) -> JobListResponse:
    store = get_batch_store()
    batches = list(store.list_batches())
    batches.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)

    items: list[JobListItem] = []
    for batch in batches[:limit]:
        prompt = str(batch.get("prompt", "")).strip()
        items.append(
            JobListItem(
                batch_id=str(batch.get("batch_id", "")),
                status=str(batch.get("status", "received")),
                created_at=str(batch.get("created_at", "")),
                updated_at=str(batch.get("updated_at", "")),
                requester=str(batch.get("requester", "")),
                prompt_preview=(prompt[:90] + "...") if len(prompt) > 90 else prompt,
            )
        )
    return JobListResponse(jobs=items)


def get_preview(batch_id: str) -> PreviewResponse:
    store = get_batch_store()
    batch = store.get_batch(batch_id)
    if not batch:
        raise KeyError(batch_id)
    return PreviewResponse(
        batch_id=batch_id,
        status=batch["status"],
        records=[PreviewRecord(**row) for row in batch.get("records", [])],
        planning_summary=batch.get("planning_summary", {}),
        generated_counts=batch.get("generated_counts", {}),
        validation_summary=ValidationSummary(**batch.get("validation_summary", {})),
    )


async def apply_approval(batch_id: str, approved_by: str, decisions: list[dict]) -> ApprovalResponse:
    store = get_batch_store()
    sheets = SheetsService()
    appscript = AppScriptBridgeService()
    batch = store.get_batch(batch_id)
    if not batch:
        raise KeyError(batch_id)

    decision_map = {item["record_id"]: item["import_decision"] for item in decisions}
    updated_records = []
    counts = {"approved": 0, "skipped": 0, "edit_later": 0}
    for record in batch.get("records", []):
        decision = decision_map.get(record["record_id"], record.get("import_decision", "pending_review"))
        if record.get("validation_status") == "failed":
            decision = "blocked"
        record["import_decision"] = decision
        if decision in counts:
            counts[decision] += 1
        updated_records.append(record)

    status = "approved" if counts["approved"] > 0 else "partially_approved"
    store.update_batch(
        batch_id,
        {
            "records": updated_records,
            "approved_by": approved_by,
        },
    )
    store.append_status(batch_id, status, "Record-level approval decisions saved.")
    approval_sync_metadata: dict = {}
    if appscript.enabled:
        appscript_result = await appscript.invoke(
            action="update_approval_status",
            payload={
                "batch_id": batch_id,
                "approved_by": approved_by,
                "records": decisions,
            },
        )
        approval_sync_metadata = {
            "mode": "appscript",
            "status": appscript_result.get("status"),
            "detail": appscript_result.get("detail"),
            "http_status": appscript_result.get("http_status"),
            "result": appscript_result.get("data", {}),
        }
        if appscript_result.get("status") != "ok" and sheets.enabled:
            fallback_rows = sheets.write_approval_log(batch_id, decisions)
            approval_sync_metadata["fallback"] = {
                "mode": "google_sheets_service_account",
                "rows_written": fallback_rows,
            }
        elif appscript_result.get("status") != "ok":
            raise RuntimeError(
                appscript_result.get("detail")
                or "Approval sync failed: Apps Script unavailable and no backend fallback configured."
            )
    else:
        rows_written = sheets.write_approval_log(batch_id, decisions)
        approval_sync_metadata = {
            "mode": "google_sheets_service_account",
            "rows_written": rows_written,
        }

    return ApprovalResponse(
        batch_id=batch_id,
        status=status,  # type: ignore[arg-type]
        summary=ApprovalSummary(**counts),
        message="Approval decisions saved.",
        metadata={"approval_sync": approval_sync_metadata},
    )


async def deploy_batch_to_zendesk(
    *,
    batch_id: str,
    subdomain: str,
    email: str,
    api_token: str,
    dry_run: bool = False,
    on_existing: str = "create_new",
) -> dict:
    store = get_batch_store()
    appscript = AppScriptBridgeService()
    batch = store.get_batch(batch_id)
    if not batch:
        raise KeyError(batch_id)

    records = batch.get("records", [])
    store.append_status(batch_id, "deploying", "Deploying approved records to Zendesk.")

    deployment = await deploy_records_to_zendesk(
        subdomain=subdomain,
        email=email,
        api_token=api_token,
        records=records,
        dry_run=dry_run,
        on_existing=on_existing,
    )
    summary = deployment.get("summary", {})
    results = deployment.get("results", [])

    result_by_record = {item.get("record_id"): item for item in results}
    updated_records = []
    for row in records:
        record_id = row.get("record_id")
        result = result_by_record.get(record_id, {})
        row["deployment_status"] = result.get("deployment_status", row.get("deployment_status", "pending"))
        row["zendesk_object_id"] = result.get("zendesk_object_id")
        row["execution_message"] = result.get("execution_message", "")
        updated_records.append(row)

    execution_log_result: dict = {}
    if appscript.enabled:
        execution_log = await appscript.invoke(
            action="write_execution_log",
            payload={
                "batch_id": batch_id,
                "results": results,
            },
        )
        execution_log_result = {
            "mode": "appscript",
            "status": execution_log.get("status"),
            "detail": execution_log.get("detail"),
            "http_status": execution_log.get("http_status"),
            "data": execution_log.get("data", {}),
        }

    deployed = int(summary.get("deployed", 0))
    failed = int(summary.get("failed", 0))
    attempted = int(summary.get("attempted", 0))
    if attempted == 0:
        final_status = "deployed_partial"
        final_message = "No approved deployable records were found for deployment."
    elif deployed == 0 and failed == 0:
        final_status = "deployed_partial"
        final_message = "No objects were created or updated. All approved items were skipped."
    elif failed > 0 and deployed > 0:
        final_status = "deployed_partial"
        final_message = "Deployment completed with partial failures."
    elif failed > 0 and deployed == 0:
        final_status = "deploy_failed"
        final_message = "Deployment failed."
    else:
        final_status = "deployed"
        final_message = "Deployment completed successfully."

    store.update_batch(
        batch_id,
        {
            "records": updated_records,
            "metadata": {
                **batch.get("metadata", {}),
                "zendesk_deploy": {
                    "summary": summary,
                    "results": results,
                    "base_url": deployment.get("base_url"),
                    "execution_log": execution_log_result,
                },
            },
        },
    )
    store.append_status(batch_id, final_status, final_message)

    return {
        "batch_id": batch_id,
        "status": final_status,
        "summary": summary,
        "results": results,
        "message": final_message,
        "metadata": {
            "base_url": deployment.get("base_url"),
            "execution_log": execution_log_result,
            "dry_run": dry_run,
            "on_existing": on_existing,
        },
    }
