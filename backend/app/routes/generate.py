from fastapi import APIRouter, HTTPException
from app.services.sheets_service import send_to_sheets

from app.api.grok.routing import resolve_model_route
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
    planner_route = resolve_model_route(settings, "planner")
    generator_route = resolve_model_route(settings, "generator")

    try:
        # Step 1: Planning
        plan = await run_planner(prompt)

        # Step 2: Generation
        generated_data = await run_generator(plan)

        # Step 3: Validation
        validation = validate_output(generated_data)

        # Step 4: Send to Google Sheets
        sheet_response = send_to_sheets({
            "prompt": prompt,
            "plan": plan,
            "generated_data": generated_data,
            "validation": validation
        })

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
            "routes": {
                "planner": planner_route.__dict__,
                "generator": generator_route.__dict__,
            },
            "sheet_response": sheet_response
        },
    )

@router.get("/test-apis")
async def test_apis() -> ApiTestResponse:
    result = await run_api_test()
    return ApiTestResponse(**result)

