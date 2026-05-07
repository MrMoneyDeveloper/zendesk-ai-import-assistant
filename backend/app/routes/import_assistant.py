from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

import app.models.schemas as schema_models
from app.core.settings import get_settings
from app.models.schemas import (
    AppScriptActionRequest,
    AppScriptActionResponse,
    ApprovalRequest,
    ApprovalResponse,
    ImportAssistantGenerateRequest,
    ImportAssistantGenerateResponse,
    IntegrationStatusResponse,
    JobStatusResponse,
    PreviewResponse,
    ZendeskCredentialValidationRequest,
    ZendeskCredentialValidationResponse,
)
from app.services.import_assistant_service import (
    apply_approval,
    generate_import_assistant_batch,
    get_job_status,
    get_preview,
)
from app.services.appscript_bridge import AppScriptBridgeService
from app.services.sheets_service import SheetsService
from app.services.zendesk import validate_zendesk_credentials

router = APIRouter(prefix="/import-assistant", tags=["import-assistant"])

SCHEMA_SYNC_MODELS = [
    "GenerateRequest",
    "GenerateResponse",
    "ApiTestResponse",
    "ImportAssistantGenerateRequest",
    "ImportAssistantGenerateResponse",
    "JobStatusResponse",
    "PreviewRecord",
    "PreviewResponse",
    "ApprovalRequest",
    "ApprovalResponse",
    "AppScriptActionRequest",
    "AppScriptActionResponse",
    "IntegrationStatusResponse",
    "ZendeskCredentialValidationRequest",
    "ZendeskCredentialValidationResponse",
]


def _build_schema_bundle() -> dict:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": "backend/app/models/schemas.py",
        "models": {
            model_name: getattr(schema_models, model_name).model_json_schema()
            for model_name in SCHEMA_SYNC_MODELS
        },
    }


@router.post("/generate", response_model=ImportAssistantGenerateResponse)
async def generate(request: ImportAssistantGenerateRequest) -> ImportAssistantGenerateResponse:
    try:
        return await generate_import_assistant_batch(request)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/jobs/{batch_id}", response_model=JobStatusResponse)
async def get_job(batch_id: str) -> JobStatusResponse:
    try:
        return get_job_status(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc


@router.get("/preview/{batch_id}", response_model=PreviewResponse)
async def preview(batch_id: str) -> PreviewResponse:
    try:
        return get_preview(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc


@router.post("/approve", response_model=ApprovalResponse)
async def approve(request: ApprovalRequest) -> ApprovalResponse:
    try:
        return await apply_approval(
            batch_id=request.batch_id,
            approved_by=request.approved_by,
            decisions=[item.model_dump() for item in request.records],
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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


@router.get("/integrations/status", response_model=IntegrationStatusResponse)
async def integrations_status() -> IntegrationStatusResponse:
    settings = get_settings()
    appscript = AppScriptBridgeService()
    sheets = SheetsService()

    appscript_health = await appscript.invoke(action="health", method="GET")
    appscript_status = {
        "configured": appscript.enabled,
        "web_app_url_configured": bool(settings.appscript_web_app_url),
        "api_key_configured": bool(settings.appscript_api_key),
        "health": appscript_health.get("status"),
        "health_detail": appscript_health.get("detail"),
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
        "note": (
            "Zendesk deploy endpoint is not enabled in this milestone yet. "
            "Credentials are collected now for the next deployment phase."
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
