import re
from dataclasses import dataclass
from typing import Any

from app.api.grok.client import GrokClient, LLMRequestError
from app.api.grok.routing import resolve_model_route
from app.api.grok.schemas import GENERATOR_JSON_SCHEMA
from app.core.settings import get_settings
from app.helpers.json_parser import extract_json_payload
from app.helpers.prompts import build_generator_messages
from app.loggers.logger import get_logger
from app.validation.payloads import normalize_generated_rows

logger = get_logger(__name__)


@dataclass(frozen=True)
class _GeneratorMode:
    name: str
    use_schema: bool
    strict_schema: bool | None
    response_format_override: str | None
    requires_hardened_prompt: bool = False


class GeneratorStructuredOutputError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        attempts: list[dict[str, Any]],
        provider_error_code: str | None = None,
        failed_generation_excerpt: str | None = None,
        validator_reason: str | None = None,
        corrective_question: str | None = None,
        corrective_example: str | None = None,
        error_class: str | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.provider_error_code = provider_error_code
        self.failed_generation_excerpt = failed_generation_excerpt
        self.validator_reason = validator_reason
        self.corrective_question = corrective_question
        self.corrective_example = corrective_example
        self.error_class = error_class

    def as_metadata(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "provider_error_code": self.provider_error_code,
            "failed_generation_excerpt": self.failed_generation_excerpt,
            "validator_reason": self.validator_reason,
            "corrective_question": self.corrective_question,
            "corrective_example": self.corrective_example,
            "error_class": self.error_class,
        }


_GENERATOR_RETRY_MODES: tuple[_GeneratorMode, ...] = (
    _GeneratorMode(
        name="json_schema_strict",
        use_schema=True,
        strict_schema=True,
        response_format_override=None,
    ),
    _GeneratorMode(
        name="json_schema_best_effort",
        use_schema=True,
        strict_schema=False,
        response_format_override=None,
    ),
    _GeneratorMode(
        name="json_object",
        use_schema=False,
        strict_schema=False,
        response_format_override="json_object",
        requires_hardened_prompt=True,
    ),
    _GeneratorMode(
        name="no_response_format",
        use_schema=False,
        strict_schema=False,
        response_format_override="none",
        requires_hardened_prompt=True,
    ),
)


def _fallback_generated_rows() -> list[dict]:
    return [
        {
            "object_type": "triggers",
            "title": "Default Intake Triage",
            "conditions": [{"field": "status", "operator": "is", "value": "new"}],
            "actions": [
                {"field": "status", "value": "open"},
                {"field": "set_tags", "value": "ai_import_generated"},
            ],
            "dependency_notes": ["Generator fallback output used."],
        }
    ]


def _strip_code_fences(value: str) -> str:
    trimmed = value.strip()
    if not trimmed.startswith("```"):
        return trimmed
    lines = trimmed.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_balanced_json_block(text: str, open_char: str, close_char: str) -> str | None:
    start = text.find(open_char)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _collect_json_candidates(raw_text: str) -> list[str]:
    cleaned = _strip_code_fences(str(raw_text or ""))
    candidates: list[str] = []
    if cleaned:
        candidates.append(cleaned)

    wrapper_patterns = (
        r"<function[^>]*>([\s\S]*?)</function>",
        r"<tool[^>]*>([\s\S]*?)</tool>",
    )
    for pattern in wrapper_patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if match:
            segment = match.group(1).strip()
            if segment and segment not in candidates:
                candidates.append(segment)

    object_block = _extract_balanced_json_block(cleaned, "{", "}")
    if object_block and object_block not in candidates:
        candidates.append(object_block)
    array_block = _extract_balanced_json_block(cleaned, "[", "]")
    if array_block and array_block not in candidates:
        candidates.append(array_block)

    return candidates


def _extract_records_from_payload(payload: object) -> list[dict] | None:
    if isinstance(payload, dict):
        payload_records = payload.get("records")
        if isinstance(payload_records, list):
            return payload_records
    if isinstance(payload, list):
        return payload
    return None


def _parse_records_from_text(raw_text: str) -> tuple[list[dict], str | None]:
    parse_errors: list[str] = []
    for candidate in _collect_json_candidates(raw_text):
        try:
            payload = extract_json_payload(candidate)
        except Exception as exc:  # noqa: BLE001
            parse_errors.append(str(exc))
            continue
        records = _extract_records_from_payload(payload)
        if not isinstance(records, list):
            parse_errors.append('JSON payload missing top-level "records" array.')
            continue
        try:
            return normalize_generated_rows(records), None
        except Exception as exc:  # noqa: BLE001
            parse_errors.append(str(exc))
            continue
    reason = parse_errors[-1] if parse_errors else "Model output was not parseable JSON."
    raise ValueError(reason)


def _build_hardened_instruction(mode_name: str) -> str:
    return (
        f"Formatting mode: {mode_name}. "
        "Return exactly one JSON object on a single line. "
        "No markdown fences, no prose, no comments, no function wrappers. "
        'Top-level keys must be {"records":[...],"generation_notes":[...]}. '
        "Each record must contain object_type, title, conditions, actions, dependency_notes."
    )


def _with_hardened_instruction(messages: list[dict[str, Any]], mode_name: str) -> list[dict[str, Any]]:
    output = list(messages)
    output.insert(
        1,
        {
            "role": "system",
            "content": _build_hardened_instruction(mode_name),
        },
    )
    return output


def _build_corrective_hint(
    *,
    plan_object_type: str,
) -> tuple[str, str]:
    question = (
        f"I can generate the {plan_object_type} record, but I need a stricter output shape request. "
        "Should I return only one record with minimal conditions/actions and no extra notes?"
    )
    example = (
        "Example answer: Yes, generate one trigger record only with JSON fields "
        "object_type/title/conditions/actions/dependency_notes and no prose."
    )
    return question, example


def _build_mode_attempt_trace(mode: _GeneratorMode, metrics: dict[str, Any], error: Exception) -> dict[str, Any]:
    return {
        "mode": mode.name,
        "response_format_mode": metrics.get("response_format_mode"),
        "model": metrics.get("model"),
        "http_status": metrics.get("http_status"),
        "error_class": metrics.get("error_class"),
        "provider_error_code": metrics.get("provider_error_code"),
        "retry_count": metrics.get("retry_count"),
        "pre_request_wait_ms": metrics.get("pre_request_wait_ms"),
        "error": str(error),
    }


async def run_generator(
    plan: dict,
    *,
    dependency_mode: str = "match_existing_or_create_new",
    focus_object_types: list[str] | None = None,
    related_objects: list[dict] | None = None,
    reference_catalog: dict | None = None,
    recent_batch_context: list[str] | None = None,
    context_notes: str | None = None,
    chunk_instruction: str | None = None,
    chunk_target_count: int | None = None,
    chunk_index: int | None = None,
    chunk_total: int | None = None,
    existing_titles: list[str] | None = None,
    max_output_tokens: int | None = None,
    allow_fallback: bool = True,
) -> list[dict]:
    settings = get_settings()
    route = resolve_model_route(settings, "generator")
    client = GrokClient()
    base_messages = build_generator_messages(
        plan,
        dependency_mode=dependency_mode,
        focus_object_types=focus_object_types,
        related_objects=related_objects,
        reference_catalog=reference_catalog,
        recent_batch_context=recent_batch_context,
        context_notes=context_notes,
        chunk_instruction=chunk_instruction,
        chunk_target_count=chunk_target_count,
        chunk_index=chunk_index,
        chunk_total=chunk_total,
        existing_titles=existing_titles,
    )

    attempts: list[dict[str, Any]] = []
    last_failed_generation_excerpt: str | None = None
    last_provider_error_code: str | None = None
    last_error_class: str | None = None
    last_validator_reason: str | None = None

    for mode in _GENERATOR_RETRY_MODES:
        messages = (
            _with_hardened_instruction(base_messages, mode.name)
            if mode.requires_hardened_prompt
            else base_messages
        )
        mode_max_tokens = max_output_tokens or route.max_output_tokens
        if mode.name == "no_response_format":
            mode_max_tokens = min(route.max_output_tokens, max(settings.llm_generator_max_output_tokens, 350))
        try:
            raw = await client.chat(
                messages,
                temperature=0.1,
                model=route.model,
                max_output_tokens=mode_max_tokens,
                response_schema=GENERATOR_JSON_SCHEMA if mode.use_schema else None,
                response_schema_name="generator_records",
                strict_schema=mode.strict_schema,
                task=route.task,
                response_format_override=mode.response_format_override,
            )
            records, _ = _parse_records_from_text(raw)
            return records
        except Exception as exc:  # noqa: BLE001
            metrics = GrokClient.get_last_call_metrics(route.task)
            attempts.append(_build_mode_attempt_trace(mode, metrics, exc))
            last_provider_error_code = str(metrics.get("provider_error_code") or "").strip() or last_provider_error_code
            last_error_class = str(metrics.get("error_class") or "").strip() or last_error_class
            excerpt = str(metrics.get("provider_failed_generation_excerpt") or "").strip()
            if excerpt:
                last_failed_generation_excerpt = excerpt[:1800]
            if isinstance(exc, LLMRequestError):
                if exc.error_code:
                    last_provider_error_code = exc.error_code
                if exc.error_class:
                    last_error_class = exc.error_class
                if exc.failed_generation:
                    last_failed_generation_excerpt = exc.failed_generation[:1800]
                    if mode.name == "no_response_format":
                        try:
                            repaired, validator_reason = _parse_records_from_text(exc.failed_generation)
                            logger.warning(
                                "Generator recovered records from failed_generation repair path (mode=%s).",
                                mode.name,
                            )
                            return repaired
                        except Exception as repair_exc:  # noqa: BLE001
                            last_validator_reason = str(repair_exc)
            elif isinstance(exc, ValueError):
                last_validator_reason = str(exc)

    logger.warning("Generator retry ladder exhausted after %s attempts.", len(attempts))
    question, example = _build_corrective_hint(
        plan_object_type=str(plan.get("object_type", "records")),
    )
    error_message = "Generator JSON validation failed after retry ladder."
    structured_error = GeneratorStructuredOutputError(
        error_message,
        attempts=attempts,
        provider_error_code=last_provider_error_code,
        failed_generation_excerpt=last_failed_generation_excerpt,
        validator_reason=last_validator_reason,
        corrective_question=question,
        corrective_example=example,
        error_class=last_error_class,
    )
    if allow_fallback:
        logger.warning("Generator fallback in use: %s", structured_error)
        return _fallback_generated_rows()
    raise structured_error
