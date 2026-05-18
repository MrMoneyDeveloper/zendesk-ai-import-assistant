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
        mode_order: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.provider_error_code = provider_error_code
        self.failed_generation_excerpt = failed_generation_excerpt
        self.validator_reason = validator_reason
        self.corrective_question = corrective_question
        self.corrective_example = corrective_example
        self.error_class = error_class
        self.mode_order = list(mode_order or [])

    def as_metadata(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "provider_error_code": self.provider_error_code,
            "failed_generation_excerpt": self.failed_generation_excerpt,
            "validator_reason": self.validator_reason,
            "corrective_question": self.corrective_question,
            "corrective_example": self.corrective_example,
            "error_class": self.error_class,
            "mode_order": self.mode_order,
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
_GENERATOR_MODE_BY_NAME = {mode.name: mode for mode in _GENERATOR_RETRY_MODES}


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


def _build_hardened_instruction(mode_name: str, object_type_hint: str) -> str:
    object_type = str(object_type_hint or "triggers").strip().lower()
    example_record = {
        "object_type": object_type if object_type else "triggers",
        "title": "Example Title",
        "conditions": [{"field": "status", "operator": "is", "value": "new"}],
        "actions": [{"field": "set_tags", "value": "example_tag"}],
        "dependency_notes": [],
    }
    if object_type == "ticket_fields":
        example_record["conditions"] = []
        example_record["actions"] = [
            {"field": "field_type", "value": "tagger"},
            {
                "field": "custom_field_options",
                "value": [{"name": "Option A", "value": "option_a"}],
            },
        ]
    elif object_type == "ticket_forms":
        example_record["conditions"] = []
        example_record["actions"] = [
            {"field": "ticket_field_names", "value": ["Field A", "Field B"]},
        ]
    elif object_type == "articles":
        example_record["conditions"] = []
        example_record["actions"] = [
            {"field": "section_id", "value": "123"},
            {"field": "body", "value": "<p>Article body</p>"},
        ]
    elif object_type == "views":
        example_record["conditions"] = [{"field": "status", "operator": "less_than", "value": "solved"}]
        example_record["actions"] = [
            {"field": "output_columns", "value": ["status", "updated", "subject"]},
        ]

    example_json = (
        '{"records":[{"object_type":"'
        + str(example_record["object_type"])
        + '","title":"'
        + str(example_record["title"])
        + '","conditions":'
        + str(example_record["conditions"]).replace("'", '"')
        + ',"actions":'
        + str(example_record["actions"]).replace("'", '"')
        + ',"dependency_notes":[]}],"generation_notes":[]}'
    )
    extra_constraints = ""
    if object_type == "ticket_forms":
        extra_constraints = (
            " For ticket_forms records: keep conditions as an empty array, and include at least one action with "
            '"field":"ticket_field_names" (array of strings) or "field":"ticket_field_ids" (array of numeric IDs). '
            "Do not include any explanation text."
        )
    elif object_type == "ticket_fields":
        extra_constraints = (
            " For ticket_fields records: include field_type plus custom_field_options when the field is dropdown/tagger "
            "or multiselect."
        )
    elif object_type == "articles":
        extra_constraints = (
            " For articles records: include section_id in actions and provide body as plain HTML string."
        )
    elif object_type == "views":
        extra_constraints = (
            " For views records: include at least one condition object with field/operator/value, and include "
            "an action with field=output_columns and value as an array of column keys."
        )

    return (
        f"Formatting mode: {mode_name}. "
        "Return exactly one JSON object on a single line. "
        "No markdown fences, no prose, no comments, no function wrappers. "
        'Top-level keys must be {"records":[...],"generation_notes":[...]}. '
        "Each record must contain object_type, title, conditions, actions, dependency_notes. "
        f"Use this exact shape example: {example_json}.{extra_constraints}"
    )


def _with_hardened_instruction(
    messages: list[dict[str, Any]],
    mode_name: str,
    object_type_hint: str,
) -> list[dict[str, Any]]:
    output = list(messages)
    output.insert(
        1,
        {
            "role": "system",
            "content": _build_hardened_instruction(mode_name, object_type_hint),
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


async def _repair_records_with_json_object(
    *,
    client: GrokClient,
    route,
    model: str,
    api_key_override: str | None,
    raw_text: str,
    max_output_tokens: int,
) -> list[dict]:
    repair_messages = [
        {
            "role": "system",
            "content": (
                "You are a JSON repair assistant. "
                "Convert the user content into exactly one strict JSON object with top-level keys "
                '{"records":[...],"generation_notes":[...]}. '
                "Return JSON only. Do not add markdown, comments, or prose."
            ),
        },
        {
            "role": "user",
            "content": f"Repair this content into strict JSON:\n{raw_text}",
        },
    ]
    repaired_raw = await client.chat(
        repair_messages,
        temperature=0.0,
        model=model,
        max_output_tokens=max(220, min(max_output_tokens, 520)),
        response_schema=None,
        response_schema_name="generator_records_repair",
        strict_schema=False,
        task=route.task,
        response_format_override="json_object",
        api_key_override=api_key_override,
    )
    repaired_records, _ = _parse_records_from_text(repaired_raw)
    return repaired_records


def _resolve_retry_modes(
    *,
    compatibility_first: bool,
    compatibility_only: bool = False,
) -> tuple[_GeneratorMode, ...]:
    if compatibility_only:
        ordered_names = (
            "json_object",
            "no_response_format",
        )
    elif compatibility_first:
        ordered_names = (
            "json_object",
            "no_response_format",
            "json_schema_best_effort",
            "json_schema_strict",
        )
    else:
        return _GENERATOR_RETRY_MODES

    resolved: list[_GeneratorMode] = []
    for name in ordered_names:
        mode = _GENERATOR_MODE_BY_NAME.get(name)
        if mode is not None:
            resolved.append(mode)
    return tuple(resolved) if resolved else _GENERATOR_RETRY_MODES


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
    compatibility_first: bool = False,
    compatibility_only: bool = False,
    model_override: str | None = None,
    api_key_override: str | None = None,
) -> list[dict]:
    settings = get_settings()
    route = resolve_model_route(settings, "generator")
    selected_model = str(model_override or route.model).strip() or route.model
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
    retry_modes = _resolve_retry_modes(
        compatibility_first=compatibility_first,
        compatibility_only=compatibility_only,
    )
    mode_order = [mode.name for mode in retry_modes]
    last_failed_generation_excerpt: str | None = None
    last_provider_error_code: str | None = None
    last_error_class: str | None = None
    last_validator_reason: str | None = None

    object_type_hint = str(plan.get("object_type", "triggers"))
    for mode in retry_modes:
        messages = (
            _with_hardened_instruction(base_messages, mode.name, object_type_hint)
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
                model=selected_model,
                max_output_tokens=mode_max_tokens,
                response_schema=GENERATOR_JSON_SCHEMA if mode.use_schema else None,
                response_schema_name="generator_records",
                strict_schema=mode.strict_schema,
                task=route.task,
                response_format_override=mode.response_format_override,
                api_key_override=api_key_override,
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
                if exc.error_class == "rate_limited":
                    logger.warning(
                        "Generator fail-fast: rate limited in mode '%s'; skipping remaining retry modes.",
                        mode.name,
                    )
                    break
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
                if mode.name == "no_response_format":
                    last_failed_generation_excerpt = str(raw)[:1800]
                    try:
                        repaired_records = await _repair_records_with_json_object(
                            client=client,
                            route=route,
                            model=selected_model,
                            api_key_override=api_key_override,
                            raw_text=str(raw),
                            max_output_tokens=mode_max_tokens,
                        )
                        logger.warning(
                            "Generator recovered records via json_object repair pass after no_response_format parse failure.",
                        )
                        return repaired_records
                    except Exception as repair_exc:  # noqa: BLE001
                        repair_metrics = GrokClient.get_last_call_metrics(route.task)
                        attempts.append(
                            {
                                "mode": "repair_json_object",
                                "response_format_mode": repair_metrics.get("response_format_mode"),
                                "model": repair_metrics.get("model"),
                                "http_status": repair_metrics.get("http_status"),
                                "error_class": repair_metrics.get("error_class"),
                                "provider_error_code": repair_metrics.get("provider_error_code"),
                                "retry_count": repair_metrics.get("retry_count"),
                                "pre_request_wait_ms": repair_metrics.get("pre_request_wait_ms"),
                                "error": str(repair_exc),
                            }
                        )
                        repair_error_class = str(repair_metrics.get("error_class") or "").strip()
                        if repair_error_class:
                            last_error_class = repair_error_class
                        repair_provider_code = str(repair_metrics.get("provider_error_code") or "").strip()
                        if repair_provider_code:
                            last_provider_error_code = repair_provider_code

    logger.warning("Generator retry ladder exhausted after %s attempts.", len(attempts))
    question, example = _build_corrective_hint(
        plan_object_type=str(plan.get("object_type", "records")),
    )
    if last_error_class == "rate_limited":
        error_message = "Generator rate limited before producing valid output."
    else:
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
        mode_order=mode_order,
    )
    if allow_fallback:
        logger.warning("Generator fallback in use: %s", structured_error)
        return _fallback_generated_rows()
    raise structured_error
