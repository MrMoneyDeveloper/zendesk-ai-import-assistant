from fastapi import APIRouter
from pydantic import BaseModel

from app.services.planner import run_planner
from app.services.generator import run_generator
from app.services.validator import validate_output

router = APIRouter()

# Request schema
class GenerateRequest(BaseModel):
    prompt: str


@router.post("/generate")
async def generate(request: GenerateRequest):
    prompt = request.prompt

    # Step 1: Planning
    plan = run_planner(prompt)

    # Step 2: Generation
    generated_data = run_generator(plan)

    # Step 3: Validation
    validation = validate_output(generated_data)

    return {
        "plan": plan,
        "generated_data": generated_data,
        "validation": validation
    }