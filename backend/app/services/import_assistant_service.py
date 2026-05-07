from collections import Counter
from datetime import UTC, datetime
from uuid import uuid4

from app.models.schemas import (
    ApprovalResponse,
    ApprovalSummary,
    ImportAssistantGenerateRequest,
    ImportAssistantGenerateResponse,
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

TAB_OBJECT_TYPES = {
    "trigger": "triggers",
    "triggers": "triggers",
    "macro": "macros",
    "macros": "macros",
    "view": "views",
    "views": "views",
    "ticket_field": "ticket_fields",
    "ticket_fields": "ticket_fields",
    "ticket_form": "ticket_forms",
    "ticket_forms": "ticket_forms",
    "tag_dictionary": "tag_dictionary",
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _new_batch_id() -> str:
    return f"BATCH-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6].upper()}"


def _normalize_object_type(raw: str) -> str:
    normalized = raw.strip().lower()
    return TAB_OBJECT_TYPES.get(normalized, "triggers")


def _build_preview_records(
    plan: dict,
    generated_data: list[dict],
) -> tuple[list[dict], ValidationSummary]:
    object_type = _normalize_object_type(str(plan.get("object_type", "triggers")))
    records: list[dict] = []
    passed = 0
    warnings = 0
    blocked = 0

    for idx, item in enumerate(generated_data, start=1):
        title = str(item.get("title", "")).strip()
        conditions = item.get("conditions", []) or []
        actions = item.get("actions", []) or []
        row_warnings: list[str] = []
        blocked_reason = None
        validation_status: str = "passed"

        if not title:
            blocked_reason = "Missing title."
            validation_status = "failed"
        if not actions:
            row_warnings.append("No actions defined for this record.")
        if object_type == "triggers" and not conditions:
            row_warnings.append("Trigger has no conditions; verify routing logic.")

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
            }
        )

    return records, ValidationSummary(passed=passed, warnings=warnings, blocked=blocked)


def _count_generated(records: list[dict]) -> dict[str, int]:
    counts = Counter(row.get("object_type", "recommendations") for row in records)
    return dict(counts)


async def generate_import_assistant_batch(
    request: ImportAssistantGenerateRequest,
) -> ImportAssistantGenerateResponse:
    store = get_batch_store()
    sheets = SheetsService()
    appscript = AppScriptBridgeService()

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
    plan = await run_planner(request.prompt)
    store.append_status(batch_id, "planned", "Planner output received.")

    store.append_status(batch_id, "schemas_selected", "Schemas selected from registry.")
    store.append_status(batch_id, "generating", "Generator call in progress.")
    generated_data = await run_generator(plan)
    store.append_status(batch_id, "generated", "Structured records generated.")

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
                "planning_summary": {
                    "object_type": plan.get("object_type", "triggers"),
                    "intent": plan.get("intent", request.prompt),
                    "confidence": plan.get("confidence", 0.7),
                },
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
            "planning_summary": {
                "object_type": plan.get("object_type", "triggers"),
                "intent": plan.get("intent", request.prompt),
                "confidence": plan.get("confidence", 0.7),
            },
            "metadata": {
                "validation_phase_status": final_status,
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
