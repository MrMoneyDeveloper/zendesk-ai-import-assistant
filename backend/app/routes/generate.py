from fastapi import APIRouter, HTTPException

from app.core.settings import get_settings
from app.loggers.logger import get_logger
from app.models.schemas import ApiTestResponse, GenerateRequest, GenerateResponse
from app.services.api_test import run_api_test
from app.services.planner import run_planner
from app.services.generator import run_generator
from app.services.validator import validate_output

router = APIRouter()
logger = get_logger(__name__)
settings = get_settings()


@router.post("/generate")
async def generate(request: GenerateRequest) -> GenerateResponse:
    prompt = request.prompt

    try:
        # Step 1: Planning
        plan = await run_planner(prompt)

        # Step 2: Generation
        generated_data = await run_generator(plan)

        # Step 3: Validation
        validation = validate_output(generated_data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unexpected /generate failure: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Generation failed due to an internal error.",
        ) from exc

    return GenerateResponse(
        plan=plan,
        generated_data=generated_data,
        validation=validation,
        metadata={
            "provider": settings.llm_provider,
            "model": settings.xai_model,
        },
    )


@router.get("/test-apis")
async def test_apis() -> ApiTestResponse:
    result = await run_api_test()
    return ApiTestResponse(**result)
