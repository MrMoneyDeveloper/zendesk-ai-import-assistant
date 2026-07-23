import asyncio
import json
import time
from collections import Counter
from datetime import UTC, datetime

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from pydantic import ValidationError

import app.models.schemas as schema_models
from app.core.settings import get_settings
from app.models.schemas import (
    AppScriptActionRequest,
    AppScriptActionResponse,
    AttachmentExtractResponse,
    ApprovalRequest,
    ApprovalResponse,
    CheckpointDecisionRequest,
    CheckpointDecisionResponse,
    CheckpointListResponse,
    ConversationCreateRequest,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationMessageCreateRequest,
    ConversationPatchRequest,
    ContextQuestionRequest,
    ContextQuestionResponse,
    ImportAssistantGenerateRequest,
    ImportAssistantGenerateResponse,
    IntegrationStatusResponse,
    JobListResponse,
    JobStatusResponse,
    PreviewResponse,
    RunControlRequest,
    RunControlResponse,
    ZendeskContextRequest,
    ZendeskContextResponse,
    ZendeskDeployRequest,
    ZendeskDeployResponse,
    ZendeskCredentialValidationRequest,
    ZendeskCredentialValidationResponse,
    ZendeskHelpCenterReadinessRequest,
    ZendeskHelpCenterReadinessResponse,
)
from app.services.import_assistant_service import (
    GenerateFailureError,
    apply_approval,
    create_request_validation_failed_batch,
    decide_job_checkpoint,
    deploy_batch_to_zendesk,
    generate_import_assistant_batch,
    get_job_checkpoints,
    list_recent_batches,
    get_job_status,
    get_preview,
    mark_import_assistant_batch_failed,
    reserve_import_assistant_batch,
    set_batch_run_control,
)
from app.services.perf_capture import emit_perf_event
from app.services.appscript_bridge import AppScriptBridgeService
from app.services.attachment_extractor import (
    AttachmentExtractionError,
    extract_attachment_payload,
)
from app.services.context_qa import answer_context_question
from app.services.conversation_service import (
    append_conversation_message,
    assign_ai_conversation_title,
    attach_batch_to_conversation,
    create_conversation,
    get_conversation,
    list_conversations,
    patch_conversation,
)
from app.services.sheets_service import SheetsService
from app.services.zendesk import (
    check_zendesk_help_center_readiness,
    fetch_zendesk_reference_catalog,
    validate_zendesk_credentials,
)

router = APIRouter(prefix="/import-assistant", tags=["import-assistant"])

_BACKGROUND_GENERATION_TASKS: set[asyncio.Task[None]] = set()
_BACKGROUND_CONVERSATION_TITLE_TASKS: set[asyncio.Task[None]] = set()

SCHEMA_SYNC_MODELS = [
    "GenerateRequest",
    "GenerateResponse",
    "ApiTestResponse",
    "ImportAssistantGenerateRequest",
    "ImportAssistantGenerateResponse",
    "ClarificationQuestion",
    "JobStatusResponse",
    "PreviewRecord",
    "PreviewResponse",
    "JobListResponse",
    "JobListItem",
    "RunControlRequest",
    "RunControlResponse",
    "CheckpointItem",
    "CheckpointListResponse",
    "CheckpointDecisionRequest",
    "CheckpointDecisionResponse",
    "ContextReference",
    "ContextQuestionTurn",
    "ContextQuestionRequest",
    "ContextQuestionCitation",
    "ContextQuestionResponse",
    "ConversationMessage",
    "ConversationBatchItem",
    "ConversationListItem",
    "ConversationListResponse",
    "ConversationDetailResponse",
    "ConversationCreateRequest",
    "ConversationMessageCreateRequest",
    "ConversationPatchRequest",
    "ApprovalRequest",
    "ApprovalResponse",
    "AppScriptActionRequest",
    "AppScriptActionResponse",
    "AttachmentExtractResponse",
    "IntegrationStatusResponse",
    "ZendeskCredentialValidationRequest",
    "ZendeskCredentialValidationResponse",
    "ZendeskContextRequest",
    "ZendeskContextResponse",
    "ZendeskHelpCenterReadinessRequest",
    "ZendeskHelpCenterReadinessResponse",
    "ZendeskDeployRequest",
    "ZendeskDeployRecordResult",
    "ZendeskDeploySummary",
    "ZendeskDeployResponse",
]

_INTEGRATIONS_HEALTH_CACHE_LOCK = asyncio.Lock()
_INTEGRATIONS_HEALTH_CACHE: dict[str, object] = {
    "at_monotonic": 0.0,
    "at_iso": None,
    "payload": None,
}


def _build_schema_bundle() -> dict:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "backend/app/models/schemas.py",
        "models": {
            model_name: getattr(schema_models, model_name).model_json_schema()
            for model_name in SCHEMA_SYNC_MODELS
        },
    }


def _build_failure_detail(
    *,
    stage: str,
    code: str,
    reason: str,
    next_step: str,
) -> dict:
    return {
        "failure_stage": stage,
        "failure_code": code,
        "failure_reason": reason,
        "next_step": next_step,
    }


def _safe_json_size(payload: object) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        return len(str(payload))


def _trim_text(
    value: object,
    *,
    max_len: int | None = None,
) -> tuple[str, bool]:
    text = str(value or "").strip()
    if max_len is None or max_len <= 0:
        return text, False
    if len(text) <= max_len:
        return text, False
    return text[:max_len], True


def _normalize_context_object_type(value: object) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "brand": "brand",
        "brands": "brand",
        "group": "group",
        "groups": "group",
        "ticket_form": "ticket_form",
        "ticket_forms": "ticket_form",
        "help_center": "help_center",
        "help_centers": "help_center",
        "category": "category",
        "categories": "category",
        "section": "section",
        "sections": "section",
        "trigger": "trigger",
        "triggers": "trigger",
        "automation": "automation",
        "automations": "automation",
        "macro": "macro",
        "macros": "macro",
        "view": "view",
        "views": "view",
        "ticket_field": "ticket_field",
        "ticket_fields": "ticket_field",
        "article": "article",
        "articles": "article",
        "sla_policy": "sla_policy",
        "sla_policies": "sla_policy",
        "schedule": "schedule",
        "schedules": "schedule",
        "user_field": "user_field",
        "user_fields": "user_field",
        "organization_field": "organization_field",
        "organization_fields": "organization_field",
        "custom_object": "custom_object",
        "custom_objects": "custom_object",
    }
    return mapping.get(text, text)


def _format_model_validation_errors(exc: ValidationError) -> tuple[list[dict[str, object]], str]:
    raw_errors = exc.errors()
    formatted: list[dict[str, object]] = []
    snippets: list[str] = []
    for item in raw_errors:
        loc = item.get("loc") if isinstance(item, dict) else ()
        path_parts = [str(part) for part in loc if str(part) not in {"body"}]
        path = ".".join(path_parts) if path_parts else "request"
        message = str(item.get("msg") or "Invalid value.") if isinstance(item, dict) else "Invalid value."
        formatted.append(
            {
                "path": path,
                "message": message,
                "type": str(item.get("type") or "") if isinstance(item, dict) else "",
                "input": item.get("input") if isinstance(item, dict) else None,
            }
        )
        if len(snippets) < 3:
            snippets.append(f"{path}: {message}")
    summary = "; ".join(snippets) if snippets else "Invalid request payload."
    return formatted, summary


def _sanitize_generate_payload(
    raw_payload: object,
    *,
    settings,
) -> tuple[dict, dict]:
    source = raw_payload if isinstance(raw_payload, dict) else {}
    sanitized = dict(source)
    update_target_source = (
        source.get("update_target")
        if isinstance(source.get("update_target"), dict)
        else {}
    )
    update_target_id = str(update_target_source.get("id") or "").strip()
    update_target_type = _normalize_context_object_type(
        update_target_source.get("object_type")
    )
    if update_target_source:
        sanitized["update_target"] = {
            **update_target_source,
            "object_type": update_target_type,
            "id": update_target_id,
        }
    related_limit = max(int(settings.llm_context_max_related_objects), 1)
    per_catalog_limit = max(int(settings.llm_context_max_entries_per_catalog), 1)
    total_catalog_limit = max(int(settings.llm_context_max_catalog_entries), 1)
    recent_items_limit = max(int(settings.llm_context_max_recent_items), 1)
    recent_chars_limit = max(int(settings.llm_context_max_recent_chars), 20)
    notes_chars_limit = max(int(settings.llm_context_max_notes_chars), 100)

    compaction = {
        "applied": False,
        "raw_payload_bytes": _safe_json_size(raw_payload),
        "compacted_payload_bytes": 0,
        "limits": {
            "prompt_max_chars": 12000,
            "context_notes_max_chars": notes_chars_limit,
            "related_objects_max_items": related_limit,
            "catalog_max_entries_per_key": per_catalog_limit,
            "catalog_max_total_entries": total_catalog_limit,
            "recent_context_max_items": recent_items_limit,
            "recent_context_max_chars_per_item": recent_chars_limit,
            "reference_id_max_chars": 120,
            "reference_name_max_chars": 300,
            "reference_description_max_chars": 1200,
        },
        "trimmed_fields": [],
        "dropped_counts": {
            "related_objects": 0,
            "reference_catalog": 0,
            "recent_batch_context": 0,
            "invalid_related_objects": 0,
            "invalid_catalog_entries": 0,
        },
    }

    trimmed_fields: set[str] = set()

    def _mark_trim(path: str) -> None:
        compaction["applied"] = True
        trimmed_fields.add(path)

    prompt_value, prompt_trimmed = _trim_text(sanitized.get("prompt"), max_len=12000)
    if "prompt" in sanitized:
        sanitized["prompt"] = prompt_value
        if prompt_trimmed:
            _mark_trim("prompt")

    if "context_notes" in sanitized:
        notes_value, notes_trimmed = _trim_text(sanitized.get("context_notes"), max_len=notes_chars_limit)
        sanitized["context_notes"] = notes_value or None
        if notes_trimmed:
            _mark_trim("context_notes")

    if "requester" in sanitized:
        sanitized["requester"] = str(sanitized.get("requester") or "").strip() or "local-user"
    if "mode" in sanitized:
        sanitized["mode"] = str(sanitized.get("mode") or "").strip() or "generate_validate_preview"
    if "target_environment" in sanitized:
        sanitized["target_environment"] = str(sanitized.get("target_environment") or "").strip() or "sandbox"

    recent_context = sanitized.get("recent_batch_context")
    if isinstance(recent_context, list):
        compact_recent: list[str] = []
        for index, value in enumerate(recent_context):
            if len(compact_recent) >= recent_items_limit:
                compaction["dropped_counts"]["recent_batch_context"] += 1
                compaction["applied"] = True
                continue
            text, was_trimmed = _trim_text(value, max_len=recent_chars_limit)
            if not text:
                compaction["dropped_counts"]["recent_batch_context"] += 1
                compaction["applied"] = True
                continue
            compact_recent.append(text)
            if was_trimmed:
                _mark_trim(f"recent_batch_context[{index}]")
        sanitized["recent_batch_context"] = compact_recent

    source_related = sanitized.get("related_objects")
    if isinstance(source_related, list):
        compact_related: list[dict] = []
        for index, item in enumerate(source_related):
            if len(compact_related) >= related_limit:
                compaction["dropped_counts"]["related_objects"] += 1
                compaction["applied"] = True
                continue
            if not isinstance(item, dict):
                compaction["dropped_counts"]["invalid_related_objects"] += 1
                compaction["applied"] = True
                continue
            entry = dict(item)
            entry["object_type"] = _normalize_context_object_type(entry.get("object_type"))
            entry_id, id_trimmed = _trim_text(entry.get("id"), max_len=120)
            entry_name, name_trimmed = _trim_text(entry.get("name"), max_len=300)
            description_value = entry.get("description")
            if description_value is None:
                entry_description = None
                desc_trimmed = False
            else:
                entry_description, desc_trimmed = _trim_text(description_value, max_len=1200)
                entry_description = entry_description or None
            entry["id"] = entry_id
            entry["name"] = entry_name
            entry["description"] = entry_description
            is_update_target = bool(
                str(sanitized.get("operation_mode") or "").strip().lower() == "update"
                and entry_id == update_target_id
                and entry.get("object_type") == update_target_type
            )
            if not is_update_target:
                entry.pop("snapshot", None)
            if not entry_id or not entry_name:
                compaction["dropped_counts"]["invalid_related_objects"] += 1
                compaction["applied"] = True
                continue
            compact_related.append(entry)
            if id_trimmed:
                _mark_trim(f"related_objects[{index}].id")
            if name_trimmed:
                _mark_trim(f"related_objects[{index}].name")
            if desc_trimmed:
                _mark_trim(f"related_objects[{index}].description")
        sanitized["related_objects"] = compact_related

    source_catalog = sanitized.get("reference_catalog")
    if isinstance(source_catalog, dict):
        compact_catalog: dict[str, list[dict]] = {}
        total_kept = 0
        for key, value in source_catalog.items():
            if total_kept >= total_catalog_limit:
                if isinstance(value, list):
                    compaction["dropped_counts"]["reference_catalog"] += len(value)
                else:
                    compaction["dropped_counts"]["reference_catalog"] += 1
                compaction["applied"] = True
                continue
            if not isinstance(value, list):
                compaction["dropped_counts"]["invalid_catalog_entries"] += 1
                compaction["applied"] = True
                continue
            compact_entries: list[dict] = []
            for index, item in enumerate(value):
                if len(compact_entries) >= per_catalog_limit or total_kept >= total_catalog_limit:
                    compaction["dropped_counts"]["reference_catalog"] += 1
                    compaction["applied"] = True
                    continue
                if not isinstance(item, dict):
                    compaction["dropped_counts"]["invalid_catalog_entries"] += 1
                    compaction["applied"] = True
                    continue
                entry = dict(item)
                entry["object_type"] = _normalize_context_object_type(entry.get("object_type"))
                entry_id, id_trimmed = _trim_text(entry.get("id"), max_len=120)
                entry_name, name_trimmed = _trim_text(entry.get("name"), max_len=300)
                description_value = entry.get("description")
                if description_value is None:
                    entry_description = None
                    desc_trimmed = False
                else:
                    entry_description, desc_trimmed = _trim_text(description_value, max_len=1200)
                    entry_description = entry_description or None
                entry["id"] = entry_id
                entry["name"] = entry_name
                entry["description"] = entry_description
                entry.pop("snapshot", None)
                if not entry_id or not entry_name:
                    compaction["dropped_counts"]["invalid_catalog_entries"] += 1
                    compaction["applied"] = True
                    continue
                compact_entries.append(entry)
                total_kept += 1
                if id_trimmed:
                    _mark_trim(f"reference_catalog.{key}[{index}].id")
                if name_trimmed:
                    _mark_trim(f"reference_catalog.{key}[{index}].name")
                if desc_trimmed:
                    _mark_trim(f"reference_catalog.{key}[{index}].description")
            compact_catalog[str(key)] = compact_entries
        sanitized["reference_catalog"] = compact_catalog

    compaction["trimmed_fields"] = sorted(trimmed_fields)
    compaction["compacted_payload_bytes"] = _safe_json_size(sanitized)
    if (
        compaction["compacted_payload_bytes"] != compaction["raw_payload_bytes"]
        or compaction["trimmed_fields"]
        or any(int(value) > 0 for value in compaction["dropped_counts"].values())
    ):
        compaction["applied"] = True
    return sanitized, compaction


async def _sync_schema_preflight_if_enabled() -> dict:
    service = AppScriptBridgeService()
    if not service.enabled:
        return {
            "status": "skipped",
            "detail": "Apps Script bridge not configured.",
        }
    payload = {"schema_bundle": _build_schema_bundle()}
    return await service.invoke(action="sync_schema", payload=payload, method="POST")


async def _get_appscript_health_with_cache(
    *,
    appscript: AppScriptBridgeService,
    settings,
) -> tuple[dict, str, str | None, float]:
    ttl_seconds = max(float(settings.integrations_health_cache_seconds), 0.0)
    now_mono = time.monotonic()
    cached_payload = _INTEGRATIONS_HEALTH_CACHE.get("payload")
    cached_at_mono = float(_INTEGRATIONS_HEALTH_CACHE.get("at_monotonic") or 0.0)
    cached_at_iso = _INTEGRATIONS_HEALTH_CACHE.get("at_iso")
    if (
        ttl_seconds > 0
        and isinstance(cached_payload, dict)
        and cached_at_mono > 0
        and (now_mono - cached_at_mono) <= ttl_seconds
    ):
        age_ms = max((now_mono - cached_at_mono) * 1000.0, 0.0)
        return cached_payload, "cache", str(cached_at_iso or ""), round(age_ms, 2)

    async with _INTEGRATIONS_HEALTH_CACHE_LOCK:
        now_mono = time.monotonic()
        cached_payload = _INTEGRATIONS_HEALTH_CACHE.get("payload")
        cached_at_mono = float(_INTEGRATIONS_HEALTH_CACHE.get("at_monotonic") or 0.0)
        cached_at_iso = _INTEGRATIONS_HEALTH_CACHE.get("at_iso")
        if (
            ttl_seconds > 0
            and isinstance(cached_payload, dict)
            and cached_at_mono > 0
            and (now_mono - cached_at_mono) <= ttl_seconds
        ):
            age_ms = max((now_mono - cached_at_mono) * 1000.0, 0.0)
            return cached_payload, "cache", str(cached_at_iso or ""), round(age_ms, 2)

        live_payload = await appscript.invoke(
            action="health",
            method="GET",
            timeout_seconds=settings.appscript_health_timeout_seconds,
        )
        now_iso = datetime.now(UTC).isoformat()
        _INTEGRATIONS_HEALTH_CACHE["payload"] = live_payload
        _INTEGRATIONS_HEALTH_CACHE["at_monotonic"] = now_mono
        _INTEGRATIONS_HEALTH_CACHE["at_iso"] = now_iso
        return live_payload, "live", now_iso, 0.0


@router.post("/generate", response_model=ImportAssistantGenerateResponse)
async def generate(request_payload: object = Body(...)) -> ImportAssistantGenerateResponse:
    settings = get_settings()
    sanitized_payload, compaction = _sanitize_generate_payload(request_payload, settings=settings)
    try:
        request = ImportAssistantGenerateRequest.model_validate(sanitized_payload)
    except ValidationError as exc:
        validation_errors, summary = _format_model_validation_errors(exc)
        batch_id = create_request_validation_failed_batch(
            request_payload=sanitized_payload,
            validation_errors=validation_errors,
            compaction=compaction,
        )
        top_paths = Counter(
            str(item.get("path") or "request").strip() or "request"
            for item in validation_errors
            if isinstance(item, dict)
        ).most_common(5)
        emit_perf_event(
            "import_assistant.request_validation_failed",
            {
                "batch_id": batch_id,
                "raw_payload_bytes": int(compaction.get("raw_payload_bytes") or 0),
                "compacted_payload_bytes": int(compaction.get("compacted_payload_bytes") or 0),
                "compaction_applied": bool(compaction.get("applied", False)),
                "dominant_validation_paths": [
                    {"path": path, "count": count} for path, count in top_paths
                ],
            },
        )
        detail = _build_failure_detail(
            stage="request",
            code="request_validation_failed",
            reason=f"Request validation failed. {summary}",
            next_step="Adjust the invalid fields shown in validation_errors and retry.",
        )
        detail["validation_errors"] = validation_errors
        detail["batch_id"] = batch_id
        detail["compaction"] = compaction
        raise HTTPException(status_code=422, detail=detail) from exc

    request = _ensure_request_conversation(request)
    try:
        preflight = await _sync_schema_preflight_if_enabled()
        if preflight.get("status") == "error":
            raise RuntimeError(preflight.get("detail") or "Apps Script schema sync preflight failed.")
        response = await generate_import_assistant_batch(request)
        if request.conversation_id:
            attach_batch_to_conversation(request.conversation_id, response.batch_id)
        return response
    except GenerateFailureError as exc:
        detail = _build_failure_detail(
            stage=exc.stage,
            code=exc.code,
            reason=exc.reason,
            next_step=exc.next_step,
        )
        if exc.code == "rate_limited":
            status_code = 429
        elif exc.code in {"run_cancelled", "checkpoint_rejected"}:
            status_code = 409
        else:
            status_code = 502
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except RuntimeError as exc:
        detail = _build_failure_detail(
            stage="generate",
            code="generate_runtime_error",
            reason=str(exc),
            next_step="Retry generation after fixing the reported prerequisite or runtime issue.",
        )
        raise HTTPException(status_code=502, detail=detail) from exc
    except Exception as exc:  # noqa: BLE001
        detail = _build_failure_detail(
            stage="generate",
            code="generate_runtime_error",
            reason=f"Unhandled generation error: {exc}",
            next_step="Retry generation after resolving the runtime error shown above.",
        )
        raise HTTPException(status_code=502, detail=detail) from exc


async def _run_reserved_generation(
    request: ImportAssistantGenerateRequest,
    batch_id: str,
) -> None:
    try:
        preflight = await _sync_schema_preflight_if_enabled()
        if preflight.get("status") == "error":
            raise RuntimeError(preflight.get("detail") or "Apps Script schema sync preflight failed.")
        await generate_import_assistant_batch(request, reserved_batch_id=batch_id)
    except asyncio.CancelledError:
        mark_import_assistant_batch_failed(
            batch_id,
            stage="generate",
            code="generation_interrupted",
            reason="Background generation was interrupted before the batch reached preview.",
            next_step="Retry the prompt after the backend service is available.",
        )
        raise
    except GenerateFailureError as exc:
        mark_import_assistant_batch_failed(
            batch_id,
            stage=exc.stage,
            code=exc.code,
            reason=exc.reason,
            next_step=exc.next_step,
        )
    except Exception as exc:  # noqa: BLE001
        mark_import_assistant_batch_failed(
            batch_id,
            stage="generate",
            code="generate_runtime_error",
            reason=f"Background generation failed: {exc}",
            next_step="Retry generation after resolving the runtime error shown above.",
        )


def _schedule_reserved_generation(
    request: ImportAssistantGenerateRequest,
    batch_id: str,
) -> None:
    task = asyncio.create_task(_run_reserved_generation(request, batch_id))
    _BACKGROUND_GENERATION_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_GENERATION_TASKS.discard)


def _schedule_conversation_title(conversation_id: str, first_message: str) -> None:
    task = asyncio.create_task(assign_ai_conversation_title(conversation_id, first_message))
    _BACKGROUND_CONVERSATION_TITLE_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_CONVERSATION_TITLE_TASKS.discard)


def _ensure_request_conversation(
    request: ImportAssistantGenerateRequest,
) -> ImportAssistantGenerateRequest:
    if not request.conversation_id:
        return request

    try:
        get_conversation(request.conversation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found.") from exc
    return request


@router.get("/conversations", response_model=ConversationListResponse)
async def conversation_history(limit: int = 60) -> ConversationListResponse:
    safe_limit = max(1, min(limit, 100))
    return ConversationListResponse(conversations=list_conversations(limit=safe_limit))


@router.post("/conversations", response_model=ConversationDetailResponse, status_code=201)
async def start_conversation(request: ConversationCreateRequest) -> ConversationDetailResponse:
    conversation = create_conversation(
        first_message=request.first_message,
        operation_mode=request.operation_mode,
        requester=request.requester,
        state=request.state,
    )
    conversation_id = str(conversation["conversation_id"])
    _schedule_conversation_title(conversation_id, request.first_message)
    return ConversationDetailResponse(**get_conversation(conversation_id))


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def conversation_detail(conversation_id: str) -> ConversationDetailResponse:
    try:
        return ConversationDetailResponse(**get_conversation(conversation_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found.") from exc


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=ConversationDetailResponse,
)
async def add_conversation_message(
    conversation_id: str,
    request: ConversationMessageCreateRequest,
) -> ConversationDetailResponse:
    try:
        append_conversation_message(
            conversation_id,
            role=request.role,
            kind=request.kind,
            content=request.content,
            batch_id=request.batch_id,
            metadata=request.metadata,
            state=request.state,
        )
        return ConversationDetailResponse(**get_conversation(conversation_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found.") from exc


@router.patch("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def update_conversation(
    conversation_id: str,
    request: ConversationPatchRequest,
) -> ConversationDetailResponse:
    try:
        patch_conversation(
            conversation_id,
            active_batch_id=request.active_batch_id,
            state=request.state,
        )
        return ConversationDetailResponse(**get_conversation(conversation_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/generate-async", response_model=JobStatusResponse, status_code=202)
async def generate_async(request_payload: object = Body(...)) -> JobStatusResponse:
    """Reserve a pollable job and run the existing pipeline in the background."""
    settings = get_settings()
    sanitized_payload, compaction = _sanitize_generate_payload(request_payload, settings=settings)
    try:
        request = ImportAssistantGenerateRequest.model_validate(sanitized_payload)
    except ValidationError as exc:
        validation_errors, summary = _format_model_validation_errors(exc)
        batch_id = create_request_validation_failed_batch(
            request_payload=sanitized_payload,
            validation_errors=validation_errors,
            compaction=compaction,
        )
        detail = _build_failure_detail(
            stage="request",
            code="request_validation_failed",
            reason=f"Request validation failed. {summary}",
            next_step="Adjust the invalid fields shown in validation_errors and retry.",
        )
        detail["validation_errors"] = validation_errors
        detail["batch_id"] = batch_id
        detail["compaction"] = compaction
        raise HTTPException(status_code=422, detail=detail) from exc

    request = _ensure_request_conversation(request)
    reserved = reserve_import_assistant_batch(request)
    if request.conversation_id:
        attach_batch_to_conversation(request.conversation_id, reserved.batch_id)
    _schedule_reserved_generation(request, reserved.batch_id)
    return reserved


@router.get("/jobs/{batch_id}", response_model=JobStatusResponse)
async def get_job(batch_id: str) -> JobStatusResponse:
    try:
        return get_job_status(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(limit: int = 20) -> JobListResponse:
    safe_limit = max(1, min(limit, 100))
    return list_recent_batches(limit=safe_limit)


@router.post("/jobs/{batch_id}/control", response_model=RunControlResponse)
async def control_job(batch_id: str, request: RunControlRequest) -> RunControlResponse:
    try:
        result = set_batch_run_control(
            batch_id=batch_id,
            action=request.action,
            requested_by=request.requested_by,
        )
        return RunControlResponse(**result)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/jobs/{batch_id}/checkpoints", response_model=CheckpointListResponse)
async def list_job_checkpoints(batch_id: str) -> CheckpointListResponse:
    try:
        return get_job_checkpoints(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc


@router.post(
    "/jobs/{batch_id}/checkpoints/{checkpoint_id}/decision",
    response_model=CheckpointDecisionResponse,
)
async def checkpoint_decision(
    batch_id: str,
    checkpoint_id: str,
    request: CheckpointDecisionRequest,
) -> CheckpointDecisionResponse:
    try:
        return await decide_job_checkpoint(
            batch_id=batch_id,
            checkpoint_id=checkpoint_id,
            decision=request.decision,
            requested_by=request.requested_by,
            note=request.note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch or checkpoint not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        detail = _build_failure_detail(
            stage="generate",
            code="rollback_failed",
            reason=str(exc),
            next_step="Retry checkpoint decision or rerun generate if rollback remains incomplete.",
        )
        raise HTTPException(status_code=502, detail=detail) from exc


@router.get("/preview/{batch_id}", response_model=PreviewResponse)
async def preview(batch_id: str) -> PreviewResponse:
    try:
        return get_preview(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc


@router.post("/approve", response_model=ApprovalResponse)
async def approve(request: ApprovalRequest) -> ApprovalResponse:
    try:
        preflight = await _sync_schema_preflight_if_enabled()
        if preflight.get("status") == "error":
            raise RuntimeError(preflight.get("detail") or "Apps Script schema sync preflight failed.")
        return await apply_approval(
            batch_id=request.batch_id,
            approved_by=request.approved_by,
            decisions=[item.model_dump() for item in request.records],
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc
    except RuntimeError as exc:
        detail = _build_failure_detail(
            stage="approve",
            code="approval_sync_error",
            reason=str(exc),
            next_step="Retry approval save after resolving Apps Script/Sheets connectivity issues.",
        )
        raise HTTPException(status_code=502, detail=detail) from exc


@router.post("/deploy", response_model=ZendeskDeployResponse)
async def deploy_to_zendesk(request: ZendeskDeployRequest) -> ZendeskDeployResponse:
    try:
        preflight = await _sync_schema_preflight_if_enabled()
        if preflight.get("status") == "error":
            raise RuntimeError(preflight.get("detail") or "Apps Script schema sync preflight failed.")

        result = await deploy_batch_to_zendesk(
            batch_id=request.batch_id,
            subdomain=request.subdomain,
            email=request.email,
            api_token=request.api_token,
            dry_run=request.dry_run,
            on_existing=request.on_existing,
            deployment_scope=request.deployment_scope,
            help_center_url=request.help_center_url,
            brand_id=request.brand_id,
            locale=request.locale,
            article_mode=request.article_mode,
            confirm_help_center_deploy=request.confirm_help_center_deploy,
            confirm_article_publish=request.confirm_article_publish,
        )
        return ZendeskDeployResponse(**result)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc
    except ValueError as exc:
        detail = _build_failure_detail(
            stage="deploy",
            code="deploy_validation_error",
            reason=str(exc),
            next_step="Resolve the listed dependency/validation issue, then deploy again.",
        )
        raise HTTPException(status_code=400, detail=detail) from exc
    except RuntimeError as exc:
        detail = _build_failure_detail(
            stage="deploy",
            code="deploy_runtime_error",
            reason=str(exc),
            next_step="Retry deployment after resolving Zendesk/API runtime errors.",
        )
        raise HTTPException(status_code=502, detail=detail) from exc
    except Exception as exc:  # noqa: BLE001
        detail = _build_failure_detail(
            stage="deploy",
            code="deploy_runtime_error",
            reason=f"Unhandled deploy error: {exc}",
            next_step="Retry deployment after resolving the runtime error shown above.",
        )
        raise HTTPException(status_code=502, detail=detail) from exc


@router.get("/appscript/health", response_model=AppScriptActionResponse)
async def appscript_health() -> AppScriptActionResponse:
    service = AppScriptBridgeService()
    result = await service.invoke(action="health", method="GET")
    return AppScriptActionResponse(**result)


@router.post("/appscript/action", response_model=AppScriptActionResponse)
async def appscript_action(request: AppScriptActionRequest) -> AppScriptActionResponse:
    service = AppScriptBridgeService()
    result = await service.invoke(action=request.action, payload=request.payload, method="POST")
    return AppScriptActionResponse(**result)


@router.post("/appscript/sync-schemas", response_model=AppScriptActionResponse)
async def appscript_sync_schemas() -> AppScriptActionResponse:
    service = AppScriptBridgeService()
    payload = {"schema_bundle": _build_schema_bundle()}
    result = await service.invoke(action="sync_schema", payload=payload, method="POST")
    return AppScriptActionResponse(**result)


@router.post("/attachments/extract", response_model=AttachmentExtractResponse)
async def extract_attachment(
    file: UploadFile = File(...),
) -> AttachmentExtractResponse:
    settings = get_settings()
    try:
        payload = await extract_attachment_payload(file, settings)
        return AttachmentExtractResponse(**payload)
    except AttachmentExtractionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/integrations/status", response_model=IntegrationStatusResponse)
async def integrations_status() -> IntegrationStatusResponse:
    settings = get_settings()
    appscript = AppScriptBridgeService()
    sheets = SheetsService()

    appscript_health, health_source, health_cached_at, health_cache_age_ms = await _get_appscript_health_with_cache(
        appscript=appscript,
        settings=settings,
    )
    appscript_status = {
        "configured": appscript.enabled,
        "web_app_url_configured": bool(settings.appscript_web_app_url),
        "api_key_configured": bool(settings.appscript_api_key),
        "health": appscript_health.get("status"),
        "health_detail": appscript_health.get("detail"),
        "health_cached_at": health_cached_at,
        "health_cache_age_ms": health_cache_age_ms,
        "health_source": health_source,
    }

    sheets_status = {
        "service_account_mode_enabled": sheets.enabled,
        "google_sheet_id_configured": bool(settings.google_sheet_id),
        "google_service_account_file_configured": bool(settings.google_service_account_file),
        "note": (
            "Main /api/import-assistant/generate will use Apps Script actions when configured, "
            "otherwise it falls back to backend Google Sheets service-account mode."
        ),
    }

    zendesk_configured = bool(
        settings.zendesk_subdomain and settings.zendesk_email and settings.zendesk_api_token
    )
    zendesk_status = {
        "configured": zendesk_configured,
        "subdomain_configured": bool(settings.zendesk_subdomain),
        "email_configured": bool(settings.zendesk_email),
        "api_token_configured": bool(settings.zendesk_api_token),
        "target_environment": settings.zendesk_target_environment,
        "deploy_endpoint_enabled": True,
        "note": (
            "Zendesk deploy endpoint is available. Session-validated credentials from the UI "
            "can be used to deploy approved records to the target instance."
        ),
    }

    return IntegrationStatusResponse(
        appscript=appscript_status,
        sheets_backend_mode=sheets_status,
        zendesk=zendesk_status,
    )


@router.post("/zendesk/validate", response_model=ZendeskCredentialValidationResponse)
async def zendesk_validate_credentials(
    request: ZendeskCredentialValidationRequest,
) -> ZendeskCredentialValidationResponse:
    result = await validate_zendesk_credentials(
        subdomain=request.subdomain,
        email=request.email,
        api_token=request.api_token,
    )
    return ZendeskCredentialValidationResponse(**result)


@router.post("/zendesk/context", response_model=ZendeskContextResponse)
async def zendesk_context_catalog(
    request: ZendeskContextRequest,
) -> ZendeskContextResponse:
    result = await fetch_zendesk_reference_catalog(
        subdomain=request.subdomain,
        email=request.email,
        api_token=request.api_token,
    )
    return ZendeskContextResponse(**result)


@router.post("/context-question", response_model=ContextQuestionResponse)
async def context_question(
    request: ContextQuestionRequest,
) -> ContextQuestionResponse:
    started = time.perf_counter()
    try:
        catalog_result = await fetch_zendesk_reference_catalog(
            subdomain=request.subdomain,
            email=request.email,
            api_token=request.api_token,
        )
        result = await answer_context_question(
            question=request.question,
            question_mode=request.question_mode,
            catalog_result=catalog_result,
            selected_objects=[item.model_dump() for item in request.selected_objects],
            conversation=[item.model_dump() for item in request.conversation],
            batch_id=request.batch_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found for change review.") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=(
                "Read-only Zendesk question failed before an answer could be grounded. "
                f"Error class: {type(exc).__name__}."
            ),
        ) from exc

    emit_perf_event(
        "import_assistant.context_question",
        {
            "question_mode": request.question_mode,
            "batch_id": request.batch_id,
            "selected_object_count": len(request.selected_objects),
            "catalog_total": int(result.get("scope", {}).get("catalog_total", 0) or 0),
            "catalog_included": int(result.get("scope", {}).get("catalog_included", 0) or 0),
            "provider": result.get("provider"),
            "fallback_used": bool(result.get("fallback_used")),
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 2),
            "read_only": True,
        },
    )
    return ContextQuestionResponse(**result)


@router.post(
    "/zendesk/help-center/readiness",
    response_model=ZendeskHelpCenterReadinessResponse,
)
async def zendesk_help_center_readiness(
    request: ZendeskHelpCenterReadinessRequest,
) -> ZendeskHelpCenterReadinessResponse:
    result = await check_zendesk_help_center_readiness(
        subdomain=request.subdomain,
        email=request.email,
        api_token=request.api_token,
        help_center_url=request.help_center_url,
        brand_id=request.brand_id,
        locale=request.locale,
    )
    return ZendeskHelpCenterReadinessResponse(**result)
