from dataclasses import dataclass
from typing import Literal

from app.core.settings import Settings

LLMTask = Literal["planner", "generator", "clarifier", "healthcheck"]


@dataclass(frozen=True)
class LLMRoute:
    task: LLMTask
    model: str
    max_output_tokens: int
    strict_schema: bool


def resolve_model_route(settings: Settings, task: LLMTask) -> LLMRoute:
    if task == "planner":
        return LLMRoute(
            task=task,
            model=settings.llm_model_planner,
            max_output_tokens=settings.llm_planner_max_output_tokens,
            strict_schema=(
                settings.llm_strict_schema_planner
                if settings.llm_strict_schema_planner is not None
                else settings.llm_strict_schema_mode
            ),
        )
    if task == "clarifier":
        return LLMRoute(
            task=task,
            model=settings.llm_model_clarifier,
            max_output_tokens=settings.llm_clarifier_max_output_tokens,
            strict_schema=(
                settings.llm_strict_schema_clarifier
                if settings.llm_strict_schema_clarifier is not None
                else settings.llm_strict_schema_mode
            ),
        )
    if task == "healthcheck":
        return LLMRoute(
            task=task,
            model=settings.xai_model,
            max_output_tokens=min(settings.xai_max_output_tokens, 256),
            strict_schema=False,
        )
    return LLMRoute(
        task="generator",
        model=settings.llm_model_generator,
        max_output_tokens=settings.llm_generator_max_output_tokens,
        strict_schema=(
            settings.llm_strict_schema_generator
            if settings.llm_strict_schema_generator is not None
            else settings.llm_strict_schema_mode
        ),
    )
