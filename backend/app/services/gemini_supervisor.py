import asyncio
import json
import logging
import re
import time
from copy import deepcopy
from typing import Any

import httpx

from app.core.settings import get_settings
from app.helpers.json_parser import extract_json_payload
from app.services.perf_capture import emit_perf_event
from app.services.usage_telemetry import record_model_call
from app.validation.payloads import normalize_generated_rows

GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1/interactions"
logger = logging.getLogger(__name__)

_CANONICAL_PATCH_OPERATIONS = (
    "add_action",
    "add_dependency_note",
    "add_field_options",
    "add_tag",
    "add_view_output_columns",
    "normalize_title",
    "set_article_body",
    "set_field_type",
    "set_group_action_by_name",
    "set_group_condition_by_name",
    "set_ticket_form_condition_by_name",
)

SUPERVISOR_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "quality_score": {"type": "number"},
        "context_gaps": {"type": "array", "items": {"type": "string"}},
        "dependency_issues": {"type": "array", "items": {"type": "string"}},
        "chunk_assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "string"},
                    "approved": {"type": "boolean"},
                    "quality_score": {"type": "number"},
                    "blocking_issues": {"type": "array", "items": {"type": "string"}},
                    "requires_regeneration": {"type": "boolean"},
                },
                "required": [
                    "chunk_id",
                    "approved",
                    "quality_score",
                    "blocking_issues",
                    "requires_regeneration",
                ],
            },
        },
        "patches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": list(_CANONICAL_PATCH_OPERATIONS),
                    },
                    "target_index": {"type": "integer"},
                    "target_title": {"type": "string"},
                    "record_key": {"type": "string"},
                    "field": {"type": "string"},
                    "value": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "number"},
                            {"type": "boolean"},
                            {"type": "array", "items": {"type": "string"}},
                            {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": True,
                                },
                            },
                            {"type": "object", "additionalProperties": True},
                            {"type": "null"},
                        ]
                    },
                    "reason": {"type": "string"},
                },
            },
        },
        "memory_delta": {"type": "array", "items": {"type": "string"}},
        "public_reasoning_summary": {"type": "string"},
        "requires_regeneration": {"type": "boolean"},
    },
    "required": [
        "approved",
        "quality_score",
        "context_gaps",
        "dependency_issues",
        "chunk_assessments",
        "patches",
        "memory_delta",
        "public_reasoning_summary",
        "requires_regeneration",
    ],
}

_SAFE_ACTION_FIELDS = {
    "comment_value",
    "comment_mode_is_public",
    "custom_field_options",
    "field_type",
    "output_columns",
    "sort_by",
    "sort_order",
    "priority",
    "status",
    "set_tags",
    "body",
}
MIN_ARTICLE_BODY_CHARS = 350

_OPERATION_ALIASES = {
    "add_dependency_note": "add_dependency_note",
    "dependency_note": "add_dependency_note",
    "add_note": "add_dependency_note",
    "add_warning": "add_dependency_note",
    "normalize_title": "normalize_title",
    "set_title": "normalize_title",
    "rename_title": "normalize_title",
    "add_tag": "add_tag",
    "append_tag": "add_tag",
    "add_action": "add_action",
    "append_action": "add_action",
    "set_article_body": "set_article_body",
    "improve_article_body": "set_article_body",
    "add_view_output_columns": "add_view_output_columns",
    "set_output_columns": "add_view_output_columns",
    "add_field_options": "add_field_options",
    "set_field_options": "add_field_options",
    "set_field_type": "set_field_type",
    "set_group_action_by_name": "set_group_action_by_name",
    "set_group_condition_by_name": "set_group_condition_by_name",
    "set_ticket_form_condition_by_name": "set_ticket_form_condition_by_name",
}


class GeminiSupervisorError(RuntimeError):
    def __init__(self, message: str, *, telemetry: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.telemetry = telemetry or {}


def _retry_delay_seconds(response: httpx.Response, *, fallback: float) -> float:
    candidates: list[object] = [response.headers.get("retry-after")]
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    details = error.get("details", []) if isinstance(error, dict) else []
    for detail in details if isinstance(details, list) else []:
        if isinstance(detail, dict):
            candidates.append(detail.get("retryDelay"))

    for candidate in candidates:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*s?\s*", str(candidate or ""))
        if match:
            return min(max(float(match.group(1)), 0.0), 300.0)
    return min(max(float(fallback or 1.0), 0.1), 300.0)


def _truncate_text(value: object, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _as_string_list(value: object, *, limit: int = 12, max_chars: int = 240) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        text = _truncate_text(item, max_chars)
        if text:
            output.append(text)
        if len(output) >= limit:
            break
    return output


def _normalize_title(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _normalize_review_payload(payload: object) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    patches = source.get("patches", [])
    if not isinstance(patches, list):
        patches = []
    raw_assessments = source.get("chunk_assessments", [])
    if not isinstance(raw_assessments, list):
        raw_assessments = []
    chunk_assessments: list[dict[str, Any]] = []
    for item in raw_assessments[:40]:
        if not isinstance(item, dict):
            continue
        chunk_id = _truncate_text(item.get("chunk_id", ""), 120)
        if not chunk_id:
            continue
        chunk_assessments.append(
            {
                "chunk_id": chunk_id,
                "approved": bool(item.get("approved", False)),
                "quality_score": min(
                    max(float(item.get("quality_score", 0.0) or 0.0), 0.0),
                    1.0,
                ),
                "blocking_issues": _as_string_list(item.get("blocking_issues"), limit=8),
                "requires_regeneration": bool(item.get("requires_regeneration", False)),
            }
        )
    return {
        "approved": bool(source.get("approved", False)),
        "quality_score": min(max(float(source.get("quality_score", 0.0) or 0.0), 0.0), 1.0),
        "context_gaps": _as_string_list(source.get("context_gaps"), limit=10),
        "dependency_issues": _as_string_list(source.get("dependency_issues"), limit=10),
        "chunk_assessments": chunk_assessments,
        "patches": [item for item in patches if isinstance(item, dict)][:20],
        "memory_delta": _as_string_list(source.get("memory_delta"), limit=12),
        "public_reasoning_summary": _truncate_text(
            source.get("public_reasoning_summary", ""),
            600,
        ),
        "requires_regeneration": bool(source.get("requires_regeneration", False)),
    }


def _extract_interaction_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    candidates: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                candidates.append(cleaned)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        step_type = str(value.get("type") or value.get("step_type") or "").lower()
        if step_type and "thought" in step_type:
            return
        for key in ("output_text", "text", "content"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                candidates.append(raw.strip())
        for key in ("model_output", "output", "parts", "items", "message", "content"):
            if key in value:
                visit(value.get(key))

    visit(payload.get("steps"))
    visit(payload.get("output"))
    if candidates:
        return candidates[-1]
    return json.dumps(payload)


def _extract_interaction_usage(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "thought_tokens": 0,
            "cached_tokens": 0,
            "tool_use_tokens": 0,
            "total_tokens": 0,
        }

    def value(key: str) -> int:
        try:
            return max(int(usage.get(key)), 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = value("total_input_tokens")
    output_tokens = value("total_output_tokens")
    thought_tokens = value("total_thought_tokens")
    cached_tokens = value("total_cached_tokens")
    tool_use_tokens = value("total_tool_use_tokens")
    total_tokens = value("total_tokens") or (
        input_tokens + output_tokens + thought_tokens + tool_use_tokens
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thought_tokens": thought_tokens,
        "cached_tokens": cached_tokens,
        "tool_use_tokens": tool_use_tokens,
        "total_tokens": total_tokens,
    }


def _record_gemini_call(metrics: dict[str, Any]) -> None:
    payload = {"provider": "Gemini", **metrics}
    record_model_call(payload)
    emit_perf_event("llm_call", payload)


def _resolve_patch_target(rows: list[dict], patch: dict[str, Any]) -> int | None:
    record_key = str(patch.get("record_key") or "").strip()
    if record_key:
        for index, row in enumerate(rows):
            if str(row.get("_supervisor_record_key") or row.get("record_key") or "").strip() == record_key:
                return index
        return None
    raw_index = patch.get("target_index")
    if isinstance(raw_index, int) and 0 <= raw_index < len(rows):
        return raw_index
    if isinstance(raw_index, int) and 1 <= raw_index <= len(rows):
        return raw_index - 1
    target_title = _normalize_title(patch.get("target_title") or patch.get("record_title"))
    if not target_title:
        return 0 if len(rows) == 1 else None
    for index, row in enumerate(rows):
        if _normalize_title(row.get("title")) == target_title:
            return index
    return None


def _ensure_notes(row: dict) -> list[str]:
    notes = row.get("dependency_notes")
    if isinstance(notes, list):
        cleaned = [str(item).strip() for item in notes if str(item).strip()]
    elif isinstance(notes, str) and notes.strip():
        cleaned = [notes.strip()]
    else:
        cleaned = []
    row["dependency_notes"] = cleaned
    return cleaned


def _ensure_actions(row: dict) -> list[dict]:
    actions = row.get("actions")
    if isinstance(actions, list):
        cleaned = [dict(item) for item in actions if isinstance(item, dict)]
    elif isinstance(actions, dict):
        cleaned = [dict(actions)]
    else:
        cleaned = []
    row["actions"] = cleaned
    return cleaned


def _ensure_conditions(row: dict) -> list[dict]:
    conditions = row.get("conditions")
    if isinstance(conditions, list):
        cleaned = [dict(item) for item in conditions if isinstance(item, dict)]
    elif isinstance(conditions, dict):
        cleaned = [dict(conditions)]
    else:
        cleaned = []
    row["conditions"] = cleaned
    return cleaned


def _replace_action(row: dict, field: str, value: object) -> None:
    normalized_field = str(field or "").strip().lower()
    actions = _ensure_actions(row)
    retained = [
        action
        for action in actions
        if str(action.get("field", "")).strip().lower() != normalized_field
    ]
    retained.append({"field": normalized_field, "value": value})
    row["actions"] = retained


def _action_text(row: dict, fields: set[str]) -> str:
    normalized_fields = {str(field).strip().lower() for field in fields}
    values = [
        str(action.get("value") or "").strip()
        for action in _ensure_actions(row)
        if str(action.get("field", "")).strip().lower() in normalized_fields
    ]
    return max(values, key=len, default="")


def _replace_condition(
    row: dict,
    field: str,
    value: object,
    *,
    operator: str = "is",
) -> None:
    normalized_field = str(field or "").strip().lower()
    conditions = _ensure_conditions(row)
    retained = [
        condition
        for condition in conditions
        if str(condition.get("field", "")).strip().lower() != normalized_field
    ]
    retained.append(
        {
            "field": normalized_field,
            "operator": str(operator or "is").strip() or "is",
            "value": value,
        }
    )
    row["conditions"] = retained


def _reference_names(
    allowed_references: dict[str, list[str]] | None,
    object_type: str,
) -> set[str]:
    values = (allowed_references or {}).get(object_type, [])
    return {_normalize_title(item) for item in values if str(item).strip()}


def _validated_named_reference(
    value: object,
    *,
    allowed_references: dict[str, list[str]] | None,
    object_type: str,
) -> str | None:
    name = str(value or "").strip()
    if not name or name.isdigit():
        return None
    if _normalize_title(name) not in _reference_names(allowed_references, object_type):
        return None
    return name


def _normalize_option(value: object) -> dict[str, str] | None:
    if isinstance(value, dict):
        name = str(value.get("name") or value.get("title") or value.get("label") or "").strip()
        option_value = str(value.get("value") or name).strip()
    else:
        name = str(value or "").strip()
        option_value = name
    if not name:
        return None
    normalized_value = "_".join(option_value.lower().split())[:255] or "_".join(name.lower().split())
    return {"name": name[:255], "value": normalized_value[:255]}


def _coerce_action(value: object, field: object = None) -> dict[str, Any] | None:
    if isinstance(value, dict):
        action = dict(value)
    else:
        action = {"field": str(field or "").strip(), "value": value}
    action_field = str(action.get("field") or field or "").strip().lower()
    if not action_field or action_field.endswith("_id") or action_field not in _SAFE_ACTION_FIELDS:
        return None
    action["field"] = action_field
    if action.get("value") in (None, ""):
        return None
    return action


def _validate_candidate_rows(rows: list[dict]) -> bool:
    try:
        normalize_generated_rows(deepcopy(rows))
    except Exception:
        return False
    return True


def _apply_patch(
    candidate: list[dict],
    patch: dict[str, Any],
    *,
    allowed_references: dict[str, list[str]] | None = None,
) -> tuple[bool, str]:
    operation = _OPERATION_ALIASES.get(str(patch.get("operation", "")).strip().lower())
    if not operation:
        return False, "unsupported_operation"
    target_index = _resolve_patch_target(candidate, patch)
    if target_index is None:
        return False, "target_not_found"
    row = candidate[target_index]
    value = patch.get("value")

    if operation == "add_dependency_note":
        note = _truncate_text(value or patch.get("reason", ""), 500)
        if not note:
            return False, "empty_note"
        notes = _ensure_notes(row)
        prefixed = f"Gemini supervisor: {note}"
        if prefixed not in notes:
            notes.append(prefixed)
        return True, "applied"

    if operation == "normalize_title":
        title = _truncate_text(value, 180)
        if not title:
            return False, "empty_title"
        previous_title = str(row.get("title", "")).strip()
        aliases = [
            str(item).strip()
            for item in list(row.get("_supervisor_title_aliases", []) or [])
            if str(item).strip()
        ]
        if previous_title and previous_title != title and previous_title not in aliases:
            aliases.append(previous_title)
        if aliases:
            row["_supervisor_title_aliases"] = aliases[-8:]
        row["title"] = title
        return True, "applied"

    if operation == "add_tag":
        raw_tags = value if isinstance(value, list) else [value]
        requested_tags = []
        for raw_tag in raw_tags:
            tag = str(raw_tag or "").strip().lower().replace(" ", "_")
            if tag and tag not in requested_tags:
                requested_tags.append(tag)
        if not requested_tags:
            return False, "empty_tag"
        actions = _ensure_actions(row)
        tokens: list[str] = []
        for action in actions:
            if str(action.get("field", "")).strip().lower() == "set_tags":
                existing = action.get("value", "")
                existing_values = existing if isinstance(existing, list) else str(existing).split()
                for item in existing_values:
                    cleaned = str(item or "").strip().lower().replace(" ", "_")
                    if cleaned and cleaned not in tokens:
                        tokens.append(cleaned)
        for tag in requested_tags:
            if tag not in tokens:
                tokens.append(tag)
        _replace_action(row, "set_tags", " ".join(tokens))
        return True, "applied"

    if operation == "add_action":
        action = _coerce_action(value, patch.get("field"))
        if not action:
            return False, "unsafe_action"
        _replace_action(row, str(action.get("field", "")), action.get("value"))
        return True, "applied"

    if operation == "set_article_body":
        if str(row.get("object_type", "")).strip().lower() != "articles":
            return False, "wrong_object_type"
        body = _truncate_text(value, 12000)
        if not body:
            return False, "empty_body"
        if len(body) < 120:
            return False, "body_not_substantive"
        existing_body = _action_text(row, {"body", "article_body"})
        if existing_body and len(body) < max(120, int(len(existing_body) * 0.6)):
            return False, "body_regression"
        _replace_action(row, "body", body)
        return True, "applied"

    if operation == "add_view_output_columns":
        if str(row.get("object_type", "")).strip().lower() != "views":
            return False, "wrong_object_type"
        columns = value if isinstance(value, list) else [value]
        normalized = [str(item).strip() for item in columns if str(item).strip()]
        if not normalized:
            return False, "empty_columns"
        existing_columns: list[str] = []
        for action in _ensure_actions(row):
            if str(action.get("field", "")).strip().lower() != "output_columns":
                continue
            values = action.get("value", [])
            values = values if isinstance(values, list) else [values]
            for item in values:
                column = str(item or "").strip()
                if column and column not in existing_columns:
                    existing_columns.append(column)
        for column in normalized:
            if column not in existing_columns:
                existing_columns.append(column)
        _replace_action(row, "output_columns", existing_columns[:12])
        return True, "applied"

    if operation == "add_field_options":
        if str(row.get("object_type", "")).strip().lower() != "ticket_fields":
            return False, "wrong_object_type"
        raw_options = value if isinstance(value, list) else [value]
        options: list[dict[str, str]] = []
        seen: set[str] = set()
        for action in _ensure_actions(row):
            if str(action.get("field", "")).strip().lower() != "custom_field_options":
                continue
            existing = action.get("value", [])
            existing = existing if isinstance(existing, list) else [existing]
            for raw in existing:
                option = _normalize_option(raw)
                if not option or option["value"] in seen:
                    continue
                seen.add(option["value"])
                options.append(option)
        for raw in raw_options:
            option = _normalize_option(raw)
            if not option or option["value"] in seen:
                continue
            seen.add(option["value"])
            options.append(option)
        if not options:
            return False, "empty_options"
        _replace_action(row, "custom_field_options", options[:50])
        return True, "applied"

    if operation == "set_field_type":
        if str(row.get("object_type", "")).strip().lower() != "ticket_fields":
            return False, "wrong_object_type"
        field_type = str(value or "").strip().lower()
        if field_type not in {"tagger", "multiselect", "text", "textarea", "integer", "decimal", "checkbox", "date"}:
            return False, "unsupported_field_type"
        _replace_action(row, "field_type", field_type)
        return True, "applied"

    if operation == "set_group_action_by_name":
        name = _validated_named_reference(
            value,
            allowed_references=allowed_references,
            object_type="groups",
        )
        if not name:
            return False, "unknown_group_name"
        _replace_action(row, "group_id", name)
        return True, "applied"

    if operation == "set_group_condition_by_name":
        name = _validated_named_reference(
            value,
            allowed_references=allowed_references,
            object_type="groups",
        )
        if not name:
            return False, "unknown_group_name"
        _replace_condition(row, "group_id", name)
        return True, "applied"

    if operation == "set_ticket_form_condition_by_name":
        name = _validated_named_reference(
            value,
            allowed_references=allowed_references,
            object_type="ticket_forms",
        )
        if not name:
            return False, "unknown_ticket_form_name"
        _replace_condition(row, "ticket_form_id", name)
        return True, "applied"

    return False, "unsupported_operation"


def apply_supervisor_patches(
    rows: list[dict],
    review: dict[str, Any],
    *,
    auto_apply: bool,
    allowed_references: dict[str, list[str]] | None = None,
    reserved_titles: dict[str, list[str]] | None = None,
) -> tuple[list[dict], dict[str, Any]]:
    patch_results: list[dict[str, Any]] = []
    working_rows = deepcopy(rows)
    applied = 0
    rejected = 0
    if auto_apply:
        for patch in review.get("patches", []):
            before = deepcopy(working_rows)
            ok, reason = _apply_patch(
                working_rows,
                patch,
                allowed_references=allowed_references,
            )
            operation = _OPERATION_ALIASES.get(
                str(patch.get("operation", "")).strip().lower()
            )
            if ok and operation == "normalize_title":
                seen_titles = {
                    object_type: {_normalize_title(title) for title in titles}
                    for object_type, titles in (reserved_titles or {}).items()
                }
                for candidate_row in working_rows:
                    object_type = str(candidate_row.get("object_type", "")).strip().lower()
                    title_key = _normalize_title(candidate_row.get("title"))
                    if not object_type or not title_key:
                        continue
                    bucket = seen_titles.setdefault(object_type, set())
                    if title_key in bucket:
                        working_rows = before
                        ok = False
                        reason = "duplicate_title"
                        break
                    bucket.add(title_key)
            if ok and not _validate_candidate_rows(working_rows):
                working_rows = before
                ok = False
                reason = "validation_failed_after_patch"
            if ok:
                applied += 1
            else:
                rejected += 1
            patch_results.append(
                {
                    "operation": str(patch.get("operation", "")),
                    "target_index": patch.get("target_index"),
                    "target_title": patch.get("target_title") or patch.get("record_title"),
                    "record_key": patch.get("record_key"),
                    "reason": _truncate_text(patch.get("reason", reason), 300),
                    "status": "applied" if ok else "rejected",
                    "reject_reason": "" if ok else reason,
                }
            )
    else:
        rejected = len(review.get("patches", []))
        for patch in review.get("patches", []):
            patch_results.append(
                {
                    "operation": str(patch.get("operation", "")),
                    "target_index": patch.get("target_index"),
                    "target_title": patch.get("target_title") or patch.get("record_title"),
                    "record_key": patch.get("record_key"),
                    "reason": _truncate_text(patch.get("reason", ""), 300),
                    "status": "skipped",
                    "reject_reason": "auto_apply_disabled",
                }
            )
    return working_rows, {
        "applied": applied,
        "rejected": rejected,
        "patch_results": patch_results,
    }


def _entry_fields(row: dict, bucket: str) -> list[str]:
    values = row.get(bucket, [])
    if isinstance(values, dict):
        values = [values]
    return [
        str(item.get("field", "")).strip().lower()
        for item in values
        if isinstance(item, dict) and str(item.get("field", "")).strip()
    ]


def _entry_values(row: dict, bucket: str, field: str) -> list[object]:
    values = row.get(bucket, [])
    if isinstance(values, dict):
        values = [values]
    output: list[object] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        if str(item.get("field", "")).strip().lower() == field:
            output.append(item.get("value"))
    return output


def _chunk_gate_reasons(
    rows: list[dict],
    spec: dict[str, Any],
    *,
    allowed_references: dict[str, list[str]] | None,
) -> list[str]:
    reasons: list[str] = []
    expected_count = max(int(spec.get("target_count", 1) or 1), 1)
    expected_type = str(spec.get("object_type", "")).strip().lower()
    if len(rows) < expected_count:
        reasons.append(f"Expected {expected_count} {expected_type} records; received {len(rows)}.")
    for row in rows:
        if str(row.get("object_type", "")).strip().lower() != expected_type:
            reasons.append("Chunk contains a mismatched object type.")
            break
        if not str(row.get("title", "")).strip():
            reasons.append("Chunk contains a record without a title.")
            break

    expected_titles = [str(item).strip() for item in spec.get("expected_titles", []) if str(item).strip()]
    actual_titles = {_normalize_title(row.get("title")) for row in rows}
    for title in expected_titles:
        if _normalize_title(title) not in actual_titles:
            reasons.append(f"Missing required record '{title}'.")

    if expected_type == "ticket_fields":
        expected_fields = [str(item).strip() for item in spec.get("fields", []) if str(item).strip()]
        for field_title in expected_fields:
            if _normalize_title(field_title) not in actual_titles:
                reasons.append(f"Missing required ticket field '{field_title}'.")

    for row in rows:
        condition_fields = _entry_fields(row, "conditions")
        action_fields = _entry_fields(row, "actions")
        if expected_type == "views":
            if not condition_fields:
                reasons.append(f"View '{row.get('title')}' has no conditions.")
            if len(_entry_values(row, "actions", "output_columns")) != 1:
                reasons.append(f"View '{row.get('title')}' must have one merged output_columns action.")
        elif expected_type == "triggers":
            if not condition_fields or not action_fields:
                reasons.append(f"Trigger '{row.get('title')}' requires conditions and actions.")
            if "group_id" not in action_fields:
                reasons.append(f"Trigger '{row.get('title')}' is missing a group routing action.")
            if "set_tags" not in action_fields:
                reasons.append(f"Trigger '{row.get('title')}' is missing a routing tag action.")
        elif expected_type == "automations":
            if not condition_fields or not action_fields:
                reasons.append(f"Automation '{row.get('title')}' requires conditions and actions.")
            if not any("hour" in field or "time" in field for field in condition_fields):
                reasons.append(f"Automation '{row.get('title')}' is missing time-based logic.")
        elif expected_type == "macros":
            if not any(field in {"comment_value", "comment_value_html"} for field in action_fields):
                reasons.append(f"Macro '{row.get('title')}' is missing response text.")
        elif expected_type == "ticket_forms":
            if "ticket_field_names" not in action_fields and "ticket_field_ids" not in action_fields:
                reasons.append(f"Ticket form '{row.get('title')}' is missing field references.")
        elif expected_type == "articles":
            bodies = _entry_values(row, "actions", "body")
            sections = [
                *_entry_values(row, "actions", "section_name"),
                *_entry_values(row, "actions", "section_id"),
            ]
            if not any(
                len(str(body or "").strip()) >= MIN_ARTICLE_BODY_CHARS
                for body in bodies
            ):
                reasons.append(
                    f"Article '{row.get('title')}' needs at least "
                    f"{MIN_ARTICLE_BODY_CHARS} characters of substantive body guidance."
                )
            if not any(str(section or "").strip() for section in sections):
                reasons.append(f"Article '{row.get('title')}' is missing its section dependency.")
        elif expected_type == "sections":
            if not any(
                field in {"category_name", "category_id"}
                for field in action_fields
            ):
                reasons.append(f"Section '{row.get('title')}' is missing its category dependency.")

        for bucket, reference_field, reference_type in (
            ("conditions", "group_id", "groups"),
            ("actions", "group_id", "groups"),
            ("conditions", "ticket_form_id", "ticket_forms"),
        ):
            allowed = _reference_names(allowed_references, reference_type)
            for raw_value in _entry_values(row, bucket, reference_field):
                value = str(raw_value or "").strip()
                if value and not value.isdigit() and _normalize_title(value) not in allowed:
                    reasons.append(
                        f"{row.get('title')}: unresolved {reference_type.rstrip('s')} reference '{value}'."
                    )

    return list(dict.fromkeys(reasons))


def evaluate_supervisor_bundle(
    *,
    rows: list[dict],
    chunk_specs: list[dict[str, Any]],
    review: dict[str, Any],
    approval_threshold: float,
    allowed_references: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    by_chunk: dict[str, list[dict]] = {}
    for row in rows:
        chunk_id = str(row.get("_supervisor_chunk_id") or row.get("chunk_id") or "").strip()
        if chunk_id:
            by_chunk.setdefault(chunk_id, []).append(row)
    model_assessments = {
        str(item.get("chunk_id", "")).strip(): item
        for item in review.get("chunk_assessments", []) or []
        if isinstance(item, dict) and str(item.get("chunk_id", "")).strip()
    }
    assessments: list[dict[str, Any]] = []
    for spec in chunk_specs:
        chunk_id = str(spec.get("chunk_id", "")).strip()
        chunk_rows = by_chunk.get(chunk_id, [])
        model = model_assessments.get(chunk_id, {})
        raw_approved = bool(model.get("approved", review.get("approved", False)))
        raw_score = min(
            max(float(model.get("quality_score", review.get("quality_score", 0.0)) or 0.0), 0.0),
            1.0,
        )
        model_regeneration = bool(
            model.get("requires_regeneration", review.get("requires_regeneration", False))
        )
        deterministic_reasons = _chunk_gate_reasons(
            chunk_rows,
            spec,
            allowed_references=allowed_references,
        )
        model_reported_issues = _as_string_list(model.get("blocking_issues", []), limit=8)
        # A model may describe the issue that its own patch just resolved. Preserve that
        # note for audit, but only enforce it when the model still rejects/regenerates.
        blocking_issues = (
            model_reported_issues
            if not raw_approved or model_regeneration
            else []
        )
        gate_reasons = list(dict.fromkeys([*deterministic_reasons, *blocking_issues]))
        effective_score = min(raw_score, 0.49) if deterministic_reasons else raw_score
        effective_approved = bool(
            raw_approved
            and not model_regeneration
            and effective_score >= approval_threshold
            and not deterministic_reasons
            and not blocking_issues
        )
        assessments.append(
            {
                "chunk_id": chunk_id,
                "raw_approved": raw_approved,
                "raw_quality_score": raw_score,
                "effective_quality_score": effective_score,
                "effective_approved": effective_approved,
                "requires_regeneration": bool(not effective_approved),
                "approval_gate_reasons": gate_reasons,
                "model_reported_issues": model_reported_issues,
                "record_keys": [
                    str(row.get("_supervisor_record_key") or "") for row in chunk_rows
                ],
            }
        )
    return {
        "effective_approved": bool(assessments and all(item["effective_approved"] for item in assessments)),
        "raw_quality_score": float(review.get("quality_score", 0.0) or 0.0),
        "effective_quality_score": min(
            [item["effective_quality_score"] for item in assessments],
            default=0.0,
        ),
        "chunk_assessments": assessments,
    }


class GeminiSupervisor:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._request_slot_lock = asyncio.Lock()
        self._next_request_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.settings.gemini_api_key)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.gemini_supervisor_enabled and self.configured)

    async def _wait_for_request_slot(self) -> None:
        interval = max(
            float(
                getattr(
                    self.settings,
                    "gemini_supervisor_min_request_interval_seconds",
                    0.0,
                )
                or 0.0
            ),
            0.0,
        )
        if interval <= 0:
            return
        loop = asyncio.get_running_loop()
        async with self._request_slot_lock:
            wait_seconds = max(self._next_request_at - loop.time(), 0.0)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            self._next_request_at = loop.time() + interval

    async def _defer_request_slot(self, delay_seconds: float) -> None:
        loop = asyncio.get_running_loop()
        async with self._request_slot_lock:
            self._next_request_at = max(
                self._next_request_at,
                loop.time() + max(float(delay_seconds or 0.0), 0.0),
            )

    async def review_and_patch_chunk(
        self,
        *,
        prompt: str,
        records: list[dict],
        object_type: str,
        wave: int | None,
        wave_position: int | None,
        chunk_index: int,
        chunk_total: int,
        blueprint: dict | None,
        supervisor_memory: list[str],
        reference_catalog: dict[str, list[dict]],
        review_scope: dict[str, Any] | None = None,
        cumulative_records: list[dict] | None = None,
        remaining_manifest_coverage: dict[str, Any] | None = None,
        allowed_references: dict[str, list[str]] | None = None,
        reserved_titles: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            reason = "missing_api_key" if self.settings.gemini_supervisor_enabled else "disabled"
            return {
                "status": "skipped",
                "reason": reason,
                "records": records,
                "review": {},
                "patch_summary": {"applied": 0, "rejected": 0, "patch_results": []},
                "latency_ms": 0,
            }

        started = time.perf_counter()
        review = await self._call_review(
            prompt=prompt,
            records=records,
            object_type=object_type,
            wave=wave,
            wave_position=wave_position,
            chunk_index=chunk_index,
            chunk_total=chunk_total,
            blueprint=blueprint,
            supervisor_memory=supervisor_memory,
            reference_catalog=reference_catalog,
            review_scope=review_scope,
            cumulative_records=cumulative_records,
            remaining_manifest_coverage=remaining_manifest_coverage,
        )
        telemetry = review.pop("_telemetry", {})
        patched_rows, patch_summary = apply_supervisor_patches(
            records,
            review,
            auto_apply=self.settings.gemini_supervisor_auto_apply_patches,
            allowed_references=allowed_references,
            reserved_titles=reserved_titles,
        )
        return {
            "status": "reviewed",
            "reason": "",
            "records": patched_rows,
            "review": review,
            "patch_summary": patch_summary,
            "telemetry": telemetry,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
        }

    async def _call_review(
        self,
        *,
        prompt: str,
        records: list[dict],
        object_type: str,
        wave: int | None,
        wave_position: int | None,
        chunk_index: int,
        chunk_total: int,
        blueprint: dict | None,
        supervisor_memory: list[str],
        reference_catalog: dict[str, list[dict]],
        review_scope: dict[str, Any] | None,
        cumulative_records: list[dict] | None,
        remaining_manifest_coverage: dict[str, Any] | None,
    ) -> dict[str, Any]:
        payload_context = {
            "task": "review_and_patch_zendesk_generation_chunk",
            "instructions": [
                "Review the generated Zendesk records for coverage, dependencies, and context carry-forward.",
                "Return JSON only. Do not rewrite the full output.",
                "Use patches only for safe incremental improvements.",
                "Do not delete records, change object_type, replace IDs, or contradict explicit user constraints.",
                "Use target_index as zero-based index into records whenever possible.",
                "For bundled reviews, assess every chunk_id independently in chunk_assessments.",
                "Judge each chunk only against its matching chunk_requirements target_count and expected_titles; records assigned to sibling chunks are not missing from this chunk.",
                "If proposed safe patches fully resolve an issue, approve that chunk and do not request regeneration for the resolved issue.",
                "Set approved=false and requires_regeneration=true for a chunk when required current-wave coverage is missing or cannot be fixed safely.",
                "Do not claim that an object exists unless it appears in cumulative_records or the current records.",
                "For macros, replace generic response copy with concise department-specific next steps and evidence requests.",
                f"For articles, provide at least {MIN_ARTICLE_BODY_CHARS} characters of concrete preparation, process, escalation, and outcome guidance.",
            ],
            "allowed_patch_operations": list(_CANONICAL_PATCH_OPERATIONS),
            "prompt": prompt,
            "object_type": object_type,
            "wave": wave,
            "wave_position": wave_position,
            "chunk_index": chunk_index,
            "chunk_total": chunk_total,
            "blueprint": blueprint or {},
            "supervisor_memory": supervisor_memory[-20:],
            "reference_catalog_keys": sorted((reference_catalog or {}).keys()),
            "selected_reference_records": {
                key: [
                    {
                        "id": item.get("id"),
                        "name": item.get("name") or item.get("title"),
                    }
                    for item in values[:100]
                    if isinstance(item, dict)
                ]
                for key, values in (reference_catalog or {}).items()
                if isinstance(values, list)
            },
            "review_scope": review_scope or {},
            "cumulative_records": cumulative_records or [],
            "remaining_manifest_coverage": remaining_manifest_coverage or {},
            "records": records,
        }
        chunk_requirements = list((review_scope or {}).get("chunk_requirements", []) or [])
        request_body = {
            "model": self.settings.gemini_supervisor_model,
            "input": json.dumps(payload_context, ensure_ascii=False),
            "store": False,
            "generation_config": {
                "max_output_tokens": min(
                    max(
                        1800,
                        1200
                        + (350 * max(len(chunk_requirements), 1))
                        + (100 * len(records)),
                    ),
                    6000,
                ),
                "temperature": 0.1,
                "thinking_level": "low",
            },
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": SUPERVISOR_REVIEW_SCHEMA,
            },
        }
        call_started = time.perf_counter()
        estimated_input_tokens = max(
            len(str(request_body.get("input") or "")) // 4,
            1,
        )
        usage_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "thought_tokens": 0,
            "cached_tokens": 0,
            "tool_use_tokens": 0,
            "total_tokens": 0,
        }
        status_codes: list[int] = []
        retry_wait_seconds = 0.0

        def add_usage(response_payload: dict[str, Any]) -> None:
            usage = _extract_interaction_usage(response_payload)
            for key in usage_totals:
                usage_totals[key] += int(usage.get(key, 0) or 0)

        def build_telemetry(
            *,
            final_status: str,
            attempt_count: int,
            http_status: int | None,
            error_class: str,
        ) -> dict[str, Any]:
            return {
                "task": "supervisor",
                "model": self.settings.gemini_supervisor_model,
                "api_key_profile": "gemini_supervisor",
                "final_status": final_status,
                "http_status": http_status,
                "attempt_count": max(int(attempt_count), 1),
                "retry_count": max(int(attempt_count) - 1, 0),
                "estimated_tokens": estimated_input_tokens,
                **usage_totals,
                "elapsed_ms": round((time.perf_counter() - call_started) * 1000.0, 2),
                "pre_request_wait_ms": round(retry_wait_seconds * 1000.0, 2),
                "error_class": error_class,
                "http_statuses": status_codes[-8:],
            }

        def telemetry_error(
            message: str,
            *,
            attempt_count: int,
            http_status: int | None,
            error_class: str,
        ) -> GeminiSupervisorError:
            telemetry = build_telemetry(
                final_status="error",
                attempt_count=attempt_count,
                http_status=http_status,
                error_class=error_class,
            )
            _record_gemini_call(telemetry)
            return GeminiSupervisorError(message, telemetry=telemetry)

        max_retries = max(
            int(getattr(self.settings, "gemini_supervisor_rate_limit_retries", 0) or 0),
            0,
        )
        min_interval = max(
            float(
                getattr(
                    self.settings,
                    "gemini_supervisor_min_request_interval_seconds",
                    0.0,
                )
                or 0.0
            ),
            0.0,
        )
        response: httpx.Response | None = None
        last_output_error: GeminiSupervisorError | None = None
        for attempt in range(max_retries + 1):
            await self._wait_for_request_slot()
            try:
                async with httpx.AsyncClient(timeout=self.settings.gemini_supervisor_timeout_seconds) as client:
                    response = await client.post(
                        GEMINI_INTERACTIONS_URL,
                        headers={
                            "x-goog-api-key": self.settings.gemini_api_key,
                            "Content-Type": "application/json",
                        },
                        json=request_body,
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= max_retries:
                    raise telemetry_error(
                        "Gemini supervisor transport failed after "
                        f"{attempt + 1} attempt(s): {type(exc).__name__}.",
                        attempt_count=attempt + 1,
                        http_status=None,
                        error_class="transport_error",
                    ) from exc
                retry_delay = max(min_interval, float(2 ** attempt))
                retry_wait_seconds += retry_delay
                logger.warning(
                    "Gemini supervisor transport failed (%s); retrying attempt %s/%s after %.2fs.",
                    type(exc).__name__,
                    attempt + 2,
                    max_retries + 1,
                    retry_delay,
                )
                await self._defer_request_slot(retry_delay)
                continue
            status_codes.append(int(response.status_code))
            if response.status_code != 429:
                if not response.is_success:
                    detail = _truncate_text(response.text, 500)
                    raise telemetry_error(
                        f"Gemini supervisor request failed ({response.status_code}): {detail}",
                        attempt_count=attempt + 1,
                        http_status=response.status_code,
                        error_class="http_error",
                    )
                try:
                    response_payload = response.json()
                except ValueError:
                    last_output_error = GeminiSupervisorError(
                        "Gemini supervisor returned non-JSON response."
                    )
                else:
                    add_usage(response_payload)
                    raw_text = _extract_interaction_text(response_payload)
                    try:
                        parsed = extract_json_payload(raw_text)
                    except Exception:
                        last_output_error = GeminiSupervisorError(
                            "Gemini supervisor output was not parseable JSON."
                        )
                    else:
                        normalized_review = _normalize_review_payload(parsed)
                        telemetry = build_telemetry(
                            final_status="reviewed",
                            attempt_count=attempt + 1,
                            http_status=response.status_code,
                            error_class="none",
                        )
                        _record_gemini_call(telemetry)
                        normalized_review["_telemetry"] = telemetry
                        return normalized_review
                if attempt >= max_retries:
                    raise telemetry_error(
                        str(last_output_error or "Gemini supervisor returned malformed output."),
                        attempt_count=attempt + 1,
                        http_status=response.status_code,
                        error_class="malformed_output",
                    )
                logger.warning(
                    "Gemini supervisor returned malformed structured output; retrying attempt %s/%s.",
                    attempt + 2,
                    max_retries + 1,
                )
                continue
            if attempt >= max_retries:
                raise telemetry_error(
                    f"Gemini supervisor rate limited after {attempt + 1} attempt(s).",
                    attempt_count=attempt + 1,
                    http_status=response.status_code,
                    error_class="rate_limited",
                )
            retry_delay = _retry_delay_seconds(
                response,
                fallback=max(min_interval, float(15 * (2 ** attempt))),
            )
            retry_wait_seconds += retry_delay
            logger.warning(
                "Gemini supervisor rate limited; retrying attempt %s/%s after %.2fs.",
                attempt + 2,
                max_retries + 1,
                retry_delay,
            )
            await self._defer_request_slot(retry_delay)

        if response is None:
            raise telemetry_error(
                "Gemini supervisor request did not return a response.",
                attempt_count=max_retries + 1,
                http_status=None,
                error_class="no_response",
            )
        if last_output_error is not None:
            raise telemetry_error(
                str(last_output_error),
                attempt_count=max_retries + 1,
                http_status=response.status_code,
                error_class="malformed_output",
            )
        raise telemetry_error(
            "Gemini supervisor request exhausted retries.",
            attempt_count=max_retries + 1,
            http_status=response.status_code,
            error_class="retry_exhausted",
        )
