from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

import app.models.schemas as schema_models
from app.models.schemas import (
    AppScriptActionRequest,
    AppScriptActionResponse,
    ApprovalRequest,
    ApprovalResponse,
    ImportAssistantGenerateRequest,
    ImportAssistantGenerateResponse,
    JobStatusResponse,
    PreviewResponse,
)
from app.services.import_assistant_service import (
    apply_approval,
    generate_import_assistant_batch,
    get_job_status,
    get_preview,
)
from app.services.appscript_bridge import AppScriptBridgeService

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
    return await generate_import_assistant_batch(request)


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
        return apply_approval(
            batch_id=request.batch_id,
            approved_by=request.approved_by,
            decisions=[item.model_dump() for item in request.records],
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc


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
