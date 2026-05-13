import asyncio
from collections import Counter
from datetime import UTC, datetime
import math
import random
import re
from uuid import uuid4

from app.api.grok.client import GrokClient
from app.api.grok.routing import resolve_model_route
from app.core.settings import get_settings
from app.models.schemas import (
    ApprovalResponse,
    ApprovalSummary,
    ImportAssistantGenerateRequest,
    ImportAssistantGenerateResponse,
    JobListItem,
    JobListResponse,
    JobStatusResponse,
    PreviewRecord,
    PreviewResponse,
    ValidationSummary,
)
from app.services.batch_store import get_batch_store
from app.services.generator import GeneratorStructuredOutputError, run_generator
from app.services.planner import run_planner
from app.services.appscript_bridge import AppScriptBridgeService
from app.services.sheets_service import SheetsService
from app.services.zendesk import deploy_records_to_zendesk

TAB_OBJECT_TYPES = {
    "trigger": "triggers",
    "triggers": "triggers",
    "automation": "automations",
    "automations": "automations",
    "macro": "macros",
    "macros": "macros",
    "view": "views",
    "views": "views",
    "group": "groups",
    "groups": "groups",
    "ticket_field": "ticket_fields",
    "ticket_fields": "ticket_fields",
    "ticket_form": "ticket_forms",
    "ticket_forms": "ticket_forms",
    "article": "articles",
    "articles": "articles",
    "tag_dictionary": "tag_dictionary",
}

REFERENCE_OBJECT_BY_FIELD = {
    "group_id": "group",
    "ticket_form_id": "ticket_form",
    "form_id": "ticket_form",
    "brand_id": "brand",
    "section_id": "section",
    "category_id": "category",
    "help_center_id": "help_center",
}

FOCUS_TO_CATALOG_KEYS = {
    "triggers": {"triggers", "groups", "ticket_forms", "brands"},
    "automations": {"automations", "groups", "ticket_forms", "brands"},
    "macros": {"macros", "groups", "ticket_forms", "ticket_fields"},
    "views": {"views", "groups", "ticket_forms", "ticket_fields"},
    "groups": {"groups", "brands"},
    "ticket_fields": {"ticket_fields", "ticket_forms"},
    "ticket_forms": {"ticket_forms", "ticket_fields", "groups", "brands"},
    "articles": {"articles", "help_centers", "categories", "sections"},
}

TICKET_FIELD_TYPE_ALIASES = {
    "dropdown": "tagger",
    "drop-down": "tagger",
    "drop_down": "tagger",
    "single-select": "tagger",
    "single_select": "tagger",
    "single select": "tagger",
    "select": "tagger",
    "tagger": "tagger",
    "multiselect": "multiselect",
    "multi-select": "multiselect",
    "multi_select": "multiselect",
    "multi select": "multiselect",
    "text": "text",
    "string": "text",
    "textarea": "textarea",
    "multi-line": "textarea",
    "multiline": "textarea",
    "integer": "integer",
    "number": "integer",
    "decimal": "decimal",
    "regexp": "regexp",
    "checkbox": "checkbox",
    "date": "date",
    "lookup": "lookup",
}

TICKET_FIELD_TYPE_FIELDS = {"field_type", "fieldtype", "field_type_name", "type"}
TICKET_FIELD_OPTIONS_FIELDS = {"custom_field_options", "options", "values", "field_values", "choices"}
TICKET_FIELD_PERMISSION_FIELD_ALIASES = {
    "agent_can_edit": {"agent_can_edit", "agents_can_edit", "agent_editable"},
    "visible_in_portal": {"visible_in_portal", "customers_can_view", "customer_can_view"},
    "editable_in_portal": {"editable_in_portal", "customers_can_edit", "customer_can_edit"},
    "required": {"required", "required_to_solve", "required_for_agents"},
    "required_in_portal": {"required_in_portal", "required_to_submit", "required_for_customers"},
}
TICKET_FORM_REFERENCE_FIELDS = {
    "ticket_field_ids",
    "ticket_fields",
    "field_ids",
    "field_names",
    "fields",
    "ticket_field_names",
}
DETERMINISTIC_LLM_ERROR_MARKERS = {
    "unsupported_response_format",
    "schema_validation_failure",
    "model_permission_blocked",
    "other_invalid_request",
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _new_batch_id() -> str:
    return f"BATCH-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6].upper()}"


def _is_deterministic_llm_error(exc: Exception) -> bool:
    text = str(exc).strip().lower()
    if not text:
        return False
    for marker in DETERMINISTIC_LLM_ERROR_MARKERS:
        if f"[{marker}]" in text:
            return True
    return False


def _is_schema_validation_failure(exc: Exception) -> bool:
    if isinstance(exc, GeneratorStructuredOutputError):
        error_class = str(exc.error_class or "").strip().lower()
        error_code = str(exc.provider_error_code or "").strip().lower()
        if error_class == "schema_validation_failure":
            return True
        if error_code in {"json_validate_failed", "failed_generation"}:
            return True
    text = str(exc).strip().lower()
    if not text:
        return False
    return (
        "schema_validation_failure" in text
        or "failed_generation" in text
        or "json_validate_failed" in text
        or "failed to validate json" in text
    )


def _normalize_object_type(raw: str) -> str:
    normalized = raw.strip().lower()
    return TAB_OBJECT_TYPES.get(normalized, "triggers")


def _normalize_focus_object_types(raw: list[str] | None) -> list[str]:
    if not raw:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        canonical = _normalize_object_type(text)
        if canonical in seen:
            continue
        seen.add(canonical)
        normalized.append(canonical)
    return normalized


def _is_numeric_string(value: object) -> bool:
    return str(value).strip().isdigit()


def _normalize_title_for_dedupe(title: object) -> str:
    normalized = str(title or "").strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def _count_enumerated_items(prompt: str) -> int:
    if not prompt:
        return 0
    lines = str(prompt).splitlines()
    enumerated_pattern = re.compile(r"^\s*(?:\d+[\).\]]|[-*•])\s+\S+")
    matches = [line for line in lines if enumerated_pattern.match(line or "")]
    return len(matches)


def _estimate_requested_record_count(prompt: str) -> dict:
    text = str(prompt or "").strip()
    lowered = text.lower()
    if not lowered:
        return {
            "estimated_count": 1,
            "sources": ["default"],
            "numeric_matches": [],
            "enumerated_items": 0,
        }

    numeric_pattern = re.compile(
        r"\b(\d{1,4})\s+"
        r"(?:(?:[a-z][\w/-]{0,30})\s+){0,3}?"
        r"("
        r"triggers?|automations?|macros?|views?|groups?|"
        r"ticket\s*forms?|forms?|ticket\s*fields?|fields?|articles?"
        r")\b",
        flags=re.IGNORECASE,
    )
    numeric_matches: list[dict] = []
    numeric_values: list[int] = []
    for match in numeric_pattern.finditer(text):
        value = int(match.group(1))
        if value <= 0:
            continue
        object_hint = _normalize_object_type(match.group(2))
        numeric_matches.append(
            {
                "value": value,
                "object_hint": object_hint,
                "raw": match.group(0),
            }
        )
        numeric_values.append(value)

    enumerated_items = _count_enumerated_items(text)
    candidates = [*numeric_values]
    if enumerated_items > 0:
        candidates.append(enumerated_items)

    if not candidates:
        estimated_count = 1
        sources = ["default"]
    else:
        estimated_count = max(candidates)
        sources = []
        if numeric_values:
            sources.append("numeric_intent")
        if enumerated_items > 0:
            sources.append("enumerated_items")

    return {
        "estimated_count": max(estimated_count, 1),
        "sources": sources,
        "numeric_matches": numeric_matches,
        "enumerated_items": enumerated_items,
    }


def _build_chunk_plan(
    *,
    settings,
    estimated_count: int,
) -> dict:
    chunk_size = max(int(settings.llm_auto_chunk_size), 1)
    max_chunks = max(int(settings.llm_auto_chunk_max_chunks), 1)
    trigger_min_records = max(int(settings.llm_auto_chunk_trigger_min_records), 2)
    activated = bool(settings.llm_auto_chunk_enabled and estimated_count >= trigger_min_records)
    if not activated:
        return {
            "activated": False,
            "estimated_count": max(estimated_count, 1),
            "chunk_size": chunk_size,
            "max_chunks": max_chunks,
            "trigger_min_records": trigger_min_records,
            "total_chunks": 1,
            "chunk_targets": [1],
            "exceeds_cap": False,
        }

    total_chunks = max(int(math.ceil(estimated_count / float(chunk_size))), 1)
    exceeds_cap = total_chunks > max_chunks
    bounded_chunks = min(total_chunks, max_chunks)
    chunk_targets: list[int] = []
    remaining = estimated_count
    for _ in range(bounded_chunks):
        target = min(chunk_size, max(remaining, 0))
        if target <= 0:
            break
        chunk_targets.append(int(target))
        remaining -= target
    if not chunk_targets:
        chunk_targets = [chunk_size]

    return {
        "activated": True,
        "estimated_count": max(estimated_count, 1),
        "chunk_size": chunk_size,
        "max_chunks": max_chunks,
        "trigger_min_records": trigger_min_records,
        "total_chunks": total_chunks,
        "chunk_targets": chunk_targets,
        "exceeds_cap": exceeds_cap,
    }


def _build_chunk_split_guidance(
    *,
    total_chunks: int,
    max_chunks: int,
    chunk_size: int,
    estimated_count: int,
) -> list[dict]:
    return [
        {
            "id": "chunk_limit_split_request",
            "question": (
                f"Your request is estimated at {estimated_count} records and requires about {total_chunks} chunks "
                f"(limit is {max_chunks}). Please split this into multiple requests of about {chunk_size * max_chunks} "
                f"records each or fewer."
            ),
            "reason": "Auto-chunk cap prevents partial generation runs and keeps reliability stable on free-tier limits.",
            "examples": [
                "Batch 1/2: create first half of triggers",
                "Batch 2/2: create remaining triggers",
            ],
        }
    ]


def _build_chunk_instruction(
    *,
    chunk_index: int,
    chunk_total: int,
    target_count: int,
) -> str:
    return (
        f"This is chunk {chunk_index}/{chunk_total}. "
        f"Generate about {target_count} records for this chunk only. "
        "Avoid generic fallback titles and avoid duplicate titles already provided in existing_titles."
    )


def _dedupe_generated_rows(rows: list[dict]) -> tuple[list[dict], int]:
    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    dropped = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        object_type = _normalize_object_type(str(row.get("object_type", "triggers")))
        title_key = _normalize_title_for_dedupe(row.get("title", ""))
        dedupe_key = (object_type, title_key)
        if title_key and dedupe_key in seen:
            dropped += 1
            continue
        if title_key:
            seen.add(dedupe_key)
        row["object_type"] = object_type
        deduped.append(row)
    return deduped, dropped


def _truncate_text(value: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _compact_related_objects(related_objects: list[dict], *, max_items: int) -> list[dict]:
    output: list[dict] = []
    for item in related_objects:
        if len(output) >= max_items:
            break
        if not isinstance(item, dict):
            continue
        output.append(
            {
                "object_type": str(item.get("object_type", "")).strip(),
                "id": str(item.get("id", "")).strip(),
                "name": _truncate_text(str(item.get("name", "")), 180),
                "description": _truncate_text(str(item.get("description", "")), 300),
            }
        )
    return output


def _compact_recent_context(
    recent_batch_context: list[str],
    *,
    max_items: int,
    max_item_chars: int,
) -> list[str]:
    compacted: list[str] = []
    for item in recent_batch_context[:max_items]:
        text = _truncate_text(str(item), max_item_chars)
        if text:
            compacted.append(text)
    return compacted


def _compact_reference_catalog(
    reference_catalog: dict[str, list[dict]],
    *,
    focus_object_types: set[str],
    max_entries_per_catalog: int,
    max_total_entries: int,
) -> dict[str, list[dict]]:
    if not reference_catalog:
        return {}

    focus_keys: set[str] = set()
    for object_type in focus_object_types:
        focus_keys.update(FOCUS_TO_CATALOG_KEYS.get(object_type, set()))
    catalog_keys = list(reference_catalog.keys())
    ordered_keys = [key for key in catalog_keys if key in focus_keys] + [
        key for key in catalog_keys if key not in focus_keys
    ]

    compacted: dict[str, list[dict]] = {}
    total_entries = 0
    for key in ordered_keys:
        rows = reference_catalog.get(key, [])
        if not isinstance(rows, list):
            continue
        bucket: list[dict] = []
        for row in rows:
            if total_entries >= max_total_entries or len(bucket) >= max_entries_per_catalog:
                break
            if not isinstance(row, dict):
                continue
            bucket.append(
                {
                    "object_type": str(row.get("object_type", "")).strip(),
                    "id": str(row.get("id", "")).strip(),
                    "name": _truncate_text(str(row.get("name", "")), 180),
                    "description": _truncate_text(str(row.get("description", "")), 220),
                }
            )
            total_entries += 1
        if bucket:
            compacted[key] = bucket
        if total_entries >= max_total_entries:
            break
    return compacted


def _build_llm_context_bundle(
    *,
    settings,
    focus_object_types: set[str],
    related_objects: list[dict],
    reference_catalog: dict[str, list[dict]],
    recent_batch_context: list[str],
    context_notes: str | None,
    aggressive: bool = False,
) -> dict:
    scale = 0.5 if aggressive else 1.0
    max_related = max(5, int(settings.llm_context_max_related_objects * scale))
    max_per_catalog = max(2, int(settings.llm_context_max_entries_per_catalog * scale))
    max_total_catalog = max(10, int(settings.llm_context_max_catalog_entries * scale))
    max_recent_items = max(1, int(settings.llm_context_max_recent_items * scale))
    max_recent_chars = max(60, int(settings.llm_context_max_recent_chars * scale))
    max_notes_chars = max(200, int(settings.llm_context_max_notes_chars * scale))

    compact_related = _compact_related_objects(related_objects, max_items=max_related)
    compact_catalog = _compact_reference_catalog(
        reference_catalog,
        focus_object_types=focus_object_types,
        max_entries_per_catalog=max_per_catalog,
        max_total_entries=max_total_catalog,
    )
    compact_recent = _compact_recent_context(
        recent_batch_context,
        max_items=max_recent_items,
        max_item_chars=max_recent_chars,
    )
    compact_notes = _truncate_text(context_notes or "", max_notes_chars)
    profile = "aggressive" if aggressive else "standard"

    return {
        "profile": profile,
        "related_objects": compact_related,
        "reference_catalog": compact_catalog,
        "recent_batch_context": compact_recent,
        "context_notes": compact_notes,
        "limits": {
            "max_related_objects": max_related,
            "max_entries_per_catalog": max_per_catalog,
            "max_total_catalog_entries": max_total_catalog,
            "max_recent_items": max_recent_items,
            "max_recent_chars": max_recent_chars,
            "max_context_notes_chars": max_notes_chars,
        },
        "counts": {
            "related_objects": len(compact_related),
            "catalog_entries": sum(len(values) for values in compact_catalog.values()),
            "recent_batch_context": len(compact_recent),
        },
    }


def _build_related_lookup(related_objects: list[dict]) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for item in related_objects:
        if not isinstance(item, dict):
            continue
        obj_type = str(item.get("object_type", "")).strip().lower()
        obj_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip().lower()
        if not obj_type or not obj_id or not name:
            continue
        lookup.setdefault(obj_type, {})[name] = obj_id
    return lookup


def _slugify_option_value(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    if text:
        return text[:255]
    return "option"


def _normalize_ticket_field_type(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    direct = TICKET_FIELD_TYPE_ALIASES.get(text)
    if direct:
        return direct
    compact = text.replace(" ", "_")
    return TICKET_FIELD_TYPE_ALIASES.get(compact)


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return None


def _extract_values_by_field_aliases(row: dict, aliases: set[str]) -> list[object]:
    values: list[object] = []
    for bucket_name in ("actions", "conditions"):
        bucket = row.get(bucket_name, [])
        if not isinstance(bucket, list):
            continue
        for entry in bucket:
            if not isinstance(entry, dict):
                continue
            field = str(entry.get("field", "")).strip().lower()
            if field in aliases:
                values.append(entry.get("value"))
    return values


def _drop_row_entries_by_aliases(row: dict, aliases: set[str]) -> None:
    for bucket_name in ("actions", "conditions"):
        bucket = row.get(bucket_name, [])
        if not isinstance(bucket, list):
            continue
        row[bucket_name] = [
            entry
            for entry in bucket
            if isinstance(entry, dict)
            and str(entry.get("field", "")).strip().lower() not in aliases
        ]


def _set_action_value(row: dict, field: str, value: object) -> None:
    actions = row.get("actions", [])
    if not isinstance(actions, list):
        actions = []
    normalized_field = field.strip().lower()
    for action in actions:
        if not isinstance(action, dict):
            continue
        if str(action.get("field", "")).strip().lower() == normalized_field:
            action["value"] = value
            row["actions"] = actions
            return
    actions.append({"field": normalized_field, "value": value})
    row["actions"] = actions


def _parse_custom_field_options(raw: object) -> tuple[list[dict[str, str]], list[str]]:
    warnings: list[str] = []
    options: list[dict[str, str]] = []

    def _add_option(name: str, value: str | None = None) -> None:
        cleaned_name = str(name).strip()
        if not cleaned_name:
            return
        normalized_value = _slugify_option_value(value or cleaned_name)
        if any(existing["value"] == normalized_value for existing in options):
            return
        options.append({"name": cleaned_name[:255], "value": normalized_value})

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                raw_name = str(item.get("name") or item.get("label") or item.get("value") or "").strip()
                raw_value = str(item.get("value") or "").strip() or None
                if not raw_name:
                    continue
                _add_option(raw_name, raw_value)
            else:
                _add_option(str(item))
    elif isinstance(raw, str):
        chunks = [part.strip() for part in re.split(r"[\n,|;]", raw) if part.strip()]
        for chunk in chunks:
            _add_option(chunk)
    elif raw is not None:
        _add_option(str(raw))

    if not options and raw not in (None, "", []):
        warnings.append("Could not parse custom field options from generated output.")
    return options, warnings


def _infer_requested_ticket_field_type(prompt: str) -> str | None:
    text = prompt.strip().lower()
    if not text:
        return None
    if any(token in text for token in ["multi-select", "multiselect", "multi select"]):
        return "multiselect"
    if any(token in text for token in ["dropdown", "drop-down", "single-select", "single select"]):
        return "tagger"
    return None


def _extract_ticket_field_option_hints(prompt: str) -> list[str]:
    text = prompt.strip()
    if not text:
        return []
    matches = re.findall(r"(?:values?|options?)\s*(?:are|is|:)?\s*([a-z0-9 ,|;/_-]{4,})", text, flags=re.IGNORECASE)
    for match in matches:
        chunks = [part.strip() for part in re.split(r"[,|;/]", match) if part.strip()]
        if len(chunks) >= 2:
            return chunks[:12]
    quoted = re.findall(r"[\"']([^\"']{2,80})[\"']", text)
    if len(quoted) >= 2:
        return [item.strip() for item in quoted[:12]]
    return []


def _annotate_focus_object_constraints(
    rows: list[dict],
    *,
    focus_object_types: set[str],
) -> tuple[list[dict], dict]:
    if not focus_object_types:
        generated_types = sorted(
            {
                _normalize_object_type(str(row.get("object_type", "triggers")))
                for row in rows
                if isinstance(row, dict)
            }
        )
        return rows, {
            "requested_focus": [],
            "generated_types": generated_types,
            "mismatch_count": 0,
            "mismatches": [],
            "mode": "soft_prefer",
        }

    mismatches: list[str] = []
    patched: list[dict] = []
    generated_types: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        object_type = _normalize_object_type(str(row.get("object_type", "triggers")))
        row["object_type"] = object_type
        generated_types.add(object_type)
        if object_type not in focus_object_types:
            mismatches.append(
                f"{row.get('title', 'Untitled')}: generated {object_type}, expected one of {sorted(focus_object_types)}"
            )
            notes = list(row.get("dependency_notes", []) or [])
            notes.append(
                f"Focus preference mismatch: generated '{object_type}' while selected focus is {', '.join(sorted(focus_object_types))}."
            )
            row["dependency_notes"] = notes
        patched.append(row)
    return patched, {
        "requested_focus": sorted(focus_object_types),
        "generated_types": sorted(generated_types),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "mode": "soft_prefer",
    }


def _extract_explicit_constraints(prompt: str) -> dict:
    def _normalize_title_constraint(raw_title: str) -> str:
        cleaned = str(raw_title).strip(" .,!?:;\"'")
        if not cleaned:
            return ""
        trailing_markers = [
            r"\bmake it\b",
            r"\bwith\b",
            r"\band\b",
            r"\bthat\b",
            r"\bfor\b",
            r"\bwhere\b",
            r"\bwhich\b",
        ]
        for marker in trailing_markers:
            parts = re.split(marker, cleaned, maxsplit=1, flags=re.IGNORECASE)
            if parts:
                cleaned = parts[0].strip(" .,!?:;\"'")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    text = prompt.strip().lower()
    explicit = {
        "title": None,
        "tag": None,
        "status": None,
        "field_type": None,
        "field_options_requested": False,
    }

    title_patterns = [
        r"\b(?:called|named)\s+([a-z0-9][a-z0-9 _-]{0,120})",
        r"\bname(?:\s+it)?\s+(?:as|to)?\s*([a-z0-9][a-z0-9 _-]{0,120})",
    ]
    for pattern in title_patterns:
        match = re.search(pattern, text)
        if match:
            normalized_title = _normalize_title_constraint(match.group(1))
            if normalized_title:
                explicit["title"] = normalized_title
            break

    tag_match = re.search(
        r"\btag(?:\s+name)?\s*(?:is|=|to|as|called)?\s*[\"']?([a-z0-9_-]{2,})",
        text,
    )
    if tag_match:
        explicit["tag"] = tag_match.group(1).strip()

    status_match = re.search(
        r"\b(?:set|change|update)\s+status\s*(?:to|=|as)?\s*([a-z_]+)",
        text,
    )
    if status_match:
        explicit["status"] = status_match.group(1).strip()

    requested_field_type = _infer_requested_ticket_field_type(prompt)
    if requested_field_type:
        explicit["field_type"] = requested_field_type

    if any(token in text for token in ["options", "values are", "values:", "with values", "with options"]):
        explicit["field_options_requested"] = True

    return explicit


def _evaluate_generation_safety(
    *,
    prompt: str,
    plan: dict,
    generated_rows: list[dict],
    focus_object_types: set[str],
    min_confidence: float,
) -> dict:
    reasons: list[str] = []
    focus_violations: list[str] = []
    explicit_constraint_violations: list[str] = []
    blocked_record_ids: list[str] = []
    fallback_detected = False
    confidence = float(plan.get("confidence", 0.0) or 0.0)

    for row in generated_rows:
        notes = list(row.get("dependency_notes", []) or [])
        if any("fallback output used" in str(note).strip().lower() for note in notes):
            fallback_detected = True
            break

    if fallback_detected:
        reasons.append("Generator fallback output detected. Regenerate before deployment.")

    if confidence < min_confidence:
        reasons.append(
            f"Planner confidence {confidence:.2f} is below minimum deploy threshold {min_confidence:.2f}."
        )

    if focus_object_types:
        for row in generated_rows:
            row_type = _normalize_object_type(str(row.get("object_type", "triggers")))
            if row_type not in focus_object_types:
                focus_violations.append(
                    f"{row.get('title', 'Untitled')}: {row_type} is outside selected focus."
                )

    constraints = _extract_explicit_constraints(prompt)
    requested_title = str(constraints.get("title") or "").strip().lower()
    requested_tag = str(constraints.get("tag") or "").strip().lower()
    requested_status = str(constraints.get("status") or "").strip().lower()
    requested_field_type = str(constraints.get("field_type") or "").strip().lower()
    requested_field_options = bool(constraints.get("field_options_requested"))

    if requested_title:
        title_match = any(
            requested_title in str(row.get("title", "")).strip().lower()
            for row in generated_rows
        )
        if not title_match:
            explicit_constraint_violations.append(
                f"Requested title contains '{requested_title}', but generated title does not match."
            )

    if requested_tag:
        tag_match = False
        for row in generated_rows:
            for action in row.get("actions", []) or []:
                field = str(action.get("field", "")).strip().lower()
                value = str(action.get("value", "")).strip().lower()
                if field in {"set_tags", "tags"} and requested_tag in value:
                    tag_match = True
                    break
            if tag_match:
                break
        if not tag_match:
            explicit_constraint_violations.append(
                f"Requested tag '{requested_tag}' was not found in generated actions."
            )

    if requested_status:
        status_match = False
        for row in generated_rows:
            for action in row.get("actions", []) or []:
                field = str(action.get("field", "")).strip().lower()
                value = str(action.get("value", "")).strip().lower()
                if field == "status" and value == requested_status:
                    status_match = True
                    break
            if status_match:
                break
        if not status_match:
            explicit_constraint_violations.append(
                f"Requested status '{requested_status}' was not found in generated actions."
            )

    if explicit_constraint_violations:
        reasons.append("Generated output does not satisfy explicit prompt constraints.")

    ticket_field_rows = [
        row for row in generated_rows
        if _normalize_object_type(str(row.get("object_type", "triggers"))) == "ticket_fields"
    ]
    if ticket_field_rows and requested_field_type in {"tagger", "multiselect"}:
        has_matching_type = False
        for row in ticket_field_rows:
            raw_values = _extract_values_by_field_aliases(row, TICKET_FIELD_TYPE_FIELDS)
            normalized = _normalize_ticket_field_type(raw_values[0]) if raw_values else None
            if normalized in {"tagger", "multiselect"}:
                has_matching_type = True
                break
        if not has_matching_type:
            explicit_constraint_violations.append(
                "Prompt requested a dropdown/select field, but generated ticket field type is not dropdown-compatible."
            )

    if ticket_field_rows and requested_field_options:
        has_options = False
        for row in ticket_field_rows:
            raw_values = _extract_values_by_field_aliases(row, TICKET_FIELD_OPTIONS_FIELDS)
            raw_options = raw_values[0] if raw_values else None
            parsed_options, _ = _parse_custom_field_options(raw_options)
            if parsed_options:
                has_options = True
                break
        if not has_options:
            explicit_constraint_violations.append(
                "Prompt requested field options, but no valid custom_field_options were generated."
            )

    if any(
        "dropdown/select field" in violation or "field options" in violation
        for violation in explicit_constraint_violations
    ) and "Generated output does not satisfy explicit prompt constraints." not in reasons:
        reasons.append("Generated output does not satisfy explicit prompt constraints.")

    blocked = bool(reasons)
    if blocked:
        blocked_record_ids = [
            f"REC-{idx:04d}"
            for idx, _ in enumerate(generated_rows, start=1)
        ]

    return {
        "blocked": blocked,
        "reasons": reasons,
        "confidence": confidence,
        "min_confidence": min_confidence,
        "fallback_detected": fallback_detected,
        "focus_violations": focus_violations,
        "explicit_constraint_violations": explicit_constraint_violations,
        "blocked_record_ids": blocked_record_ids,
    }


def _recompute_validation_summary(records: list[dict]) -> ValidationSummary:
    passed = 0
    warnings = 0
    blocked = 0
    for row in records:
        status = str(row.get("validation_status", "passed")).strip().lower()
        if status == "failed":
            blocked += 1
        elif status == "warning":
            warnings += 1
        else:
            passed += 1
    return ValidationSummary(passed=passed, warnings=warnings, blocked=blocked)


def _apply_generation_safety_to_preview(records: list[dict], safety: dict) -> list[dict]:
    if not safety.get("blocked"):
        return records

    reason_text = " | ".join(safety.get("reasons", []) or ["Generation safety gate blocked deployment."])
    for row in records:
        row["deployable"] = False
        row["validation_status"] = "failed"
        row["blocked_reason"] = reason_text
        row["import_decision"] = "blocked"
        existing_warnings = list(row.get("warnings", []) or [])
        existing_warnings.append("Generation safety gate blocked deployment. Regenerate and retry.")
        row["warnings"] = existing_warnings
    return records


def _apply_dependency_resolution(
    rows: list[dict],
    *,
    related_lookup: dict[str, dict[str, str]],
    dependency_mode: str,
) -> tuple[list[dict], dict]:
    if not related_lookup:
        return rows, {"resolved_links": 0, "unresolved_links": 0, "unresolved_samples": []}

    resolved_links = 0
    unresolved_links = 0
    unresolved_samples: list[str] = []
    patched_rows: list[dict] = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        patched_row = {
            **row,
            "conditions": list(row.get("conditions", []) or []),
            "actions": list(row.get("actions", []) or []),
        }
        row_notes: list[str] = []

        for bucket_name in ("conditions", "actions"):
            bucket = patched_row.get(bucket_name, [])
            for entry in bucket:
                if not isinstance(entry, dict):
                    continue
                field = str(entry.get("field", "")).strip().lower()
                expected_object = REFERENCE_OBJECT_BY_FIELD.get(field)
                if not expected_object:
                    continue
                raw_value = entry.get("value")
                if raw_value is None:
                    continue
                value = str(raw_value).strip()
                if not value:
                    continue
                if _is_numeric_string(value):
                    continue

                resolved = related_lookup.get(expected_object, {}).get(value.lower())
                if resolved:
                    entry["value"] = resolved
                    resolved_links += 1
                    row_notes.append(
                        f"{field}: mapped '{value}' to ID {resolved} from selected context."
                    )
                    continue

                unresolved_links += 1
                if len(unresolved_samples) < 10:
                    unresolved_samples.append(f"{field}:{value}")
                row_notes.append(
                    f"{field}: '{value}' not found in selected context."
                )

        if row_notes:
            existing = list(patched_row.get("dependency_notes", []) or [])
            patched_row["dependency_notes"] = [*existing, *row_notes]
        patched_rows.append(patched_row)

    if dependency_mode == "force_existing_only" and unresolved_links > 0:
        for row in patched_rows:
            notes = list(row.get("dependency_notes", []) or [])
            if any("not found in selected context" in note for note in notes):
                row.setdefault("validation_overrides", {})
                row["validation_overrides"]["blocked_reason"] = (
                    "Dependency resolution required existing object IDs, but one or more references were not found."
                )

    return patched_rows, {
        "resolved_links": resolved_links,
        "unresolved_links": unresolved_links,
        "unresolved_samples": unresolved_samples,
    }


def _build_preview_records(
    plan: dict,
    generated_data: list[dict],
) -> tuple[list[dict], ValidationSummary]:
    plan_object_type = _normalize_object_type(str(plan.get("object_type", "triggers")))
    records: list[dict] = []
    passed = 0
    warnings = 0
    blocked = 0

    for idx, item in enumerate(generated_data, start=1):
        object_type = _normalize_object_type(str(item.get("object_type", plan_object_type)))
        title = str(item.get("title", "")).strip()
        conditions = item.get("conditions", []) or []
        actions = item.get("actions", []) or []
        row_warnings: list[str] = []
        blocked_reason = None
        validation_status: str = "passed"
        dependency_notes = list(item.get("dependency_notes", []) or [])
        validation_overrides = item.get("validation_overrides", {}) or {}
        override_blocked_reason = str(validation_overrides.get("blocked_reason", "")).strip()
        if override_blocked_reason:
            blocked_reason = override_blocked_reason
            validation_status = "failed"

        if not title:
            blocked_reason = "Missing title."
            validation_status = "failed"
        if not actions:
            row_warnings.append("No actions defined for this record.")
        if object_type in {"triggers", "automations", "views"} and not conditions:
            row_warnings.append("Record has no conditions; verify routing/filter logic.")
        if object_type in {"groups"} and not title:
            row_warnings.append("Group record requires a name/title.")
        if dependency_notes:
            row_warnings.extend(dependency_notes)

        if blocked_reason:
            blocked += 1
        elif row_warnings:
            warnings += 1
            validation_status = "warning"
        else:
            passed += 1

        records.append(
            {
                "record_id": f"REC-{idx:04d}",
                "object_type": object_type,
                "title": title or f"Untitled {idx}",
                "preview_summary": f"{object_type.replace('_', ' ').title()} configuration record.",
                "validation_status": validation_status,
                "warnings": row_warnings,
                "blocked_reason": blocked_reason,
                "import_decision": "blocked" if blocked_reason else "pending_review",
                "deployable": blocked_reason is None,
                "conditions": conditions,
                "actions": actions,
                "deployment_status": "pending",
                "zendesk_object_id": None,
                "execution_message": "",
            }
        )

    return records, ValidationSummary(passed=passed, warnings=warnings, blocked=blocked)


def _count_generated(records: list[dict]) -> dict[str, int]:
    counts = Counter(row.get("object_type", "recommendations") for row in records)
    return dict(counts)


CATALOG_OBJECT_NORMALIZATION = {
    "brands": "brands",
    "brand": "brands",
    "groups": "groups",
    "group": "groups",
    "ticket_forms": "ticket_forms",
    "ticket_form": "ticket_forms",
    "triggers": "triggers",
    "trigger": "triggers",
    "automations": "automations",
    "automation": "automations",
    "macros": "macros",
    "macro": "macros",
    "views": "views",
    "view": "views",
    "ticket_fields": "ticket_fields",
    "ticket_field": "ticket_fields",
    "articles": "articles",
    "article": "articles",
    "help_centers": "help_centers",
    "help_center": "help_centers",
    "categories": "categories",
    "category": "categories",
    "sections": "sections",
    "section": "sections",
}


def _build_existing_object_index(reference_catalog: dict[str, list[dict]]) -> dict[str, dict[str, dict]]:
    index: dict[str, dict[str, dict]] = {}
    for raw_key, rows in reference_catalog.items():
        canonical_key = CATALOG_OBJECT_NORMALIZATION.get(str(raw_key).strip().lower(), "")
        if not canonical_key:
            continue
        bucket = index.setdefault(canonical_key, {})
        for item in rows:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            bucket[name.lower()] = item
    return index


def _annotate_duplicate_candidates(
    rows: list[dict],
    *,
    existing_index: dict[str, dict[str, dict]],
    dependency_mode: str,
) -> tuple[list[dict], list[dict]]:
    if not existing_index:
        return rows, []

    duplicate_candidates: list[dict] = []
    patched_rows: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        object_type = _normalize_object_type(str(row.get("object_type", "triggers")))
        title = str(row.get("title", "")).strip()
        if not title:
            patched_rows.append(row)
            continue

        existing_match = existing_index.get(object_type, {}).get(title.lower())
        if not existing_match:
            if dependency_mode == "force_existing_only":
                row.setdefault("validation_overrides", {})
                row["validation_overrides"]["blocked_reason"] = (
                    "Existing-only mode is active and no existing object with this exact title was found."
                )
            patched_rows.append(row)
            continue

        existing_id = str(existing_match.get("id", "")).strip()
        existing_description = str(existing_match.get("description", "")).strip()
        note = (
            f"Duplicate candidate: existing {object_type.rstrip('s')} '{title}'"
            + (f" (id={existing_id})" if existing_id else "")
            + ". Confirm whether to reuse/update or keep creating new."
        )

        existing_notes = list(row.get("dependency_notes", []) or [])
        existing_notes.append(note)
        row["dependency_notes"] = existing_notes

        duplicate_candidates.append(
            {
                "object_type": object_type,
                "title": title,
                "existing_id": existing_id or None,
                "existing_description": existing_description or None,
            }
        )
        patched_rows.append(row)

    return patched_rows, duplicate_candidates


def _canonicalize_ticket_field_record(
    row: dict,
    *,
    prompt: str,
    settings,
) -> tuple[dict, dict]:
    info = {
        "title": str(row.get("title", "Untitled field")).strip() or "Untitled field",
        "object_type": "ticket_fields",
        "alias_mappings": [],
        "defaults_applied": [],
        "warnings": [],
    }
    row.setdefault("actions", [])
    row.setdefault("conditions", [])

    requested_type = _infer_requested_ticket_field_type(prompt)
    raw_type_values = _extract_values_by_field_aliases(row, TICKET_FIELD_TYPE_FIELDS)
    normalized_type = None
    if raw_type_values:
        normalized_type = _normalize_ticket_field_type(raw_type_values[0])
    if normalized_type is None and requested_type is not None:
        normalized_type = requested_type
        info["alias_mappings"].append("inferred field_type from prompt")
    if normalized_type is None:
        normalized_type = "text"
        info["defaults_applied"].append("field_type=text")
        info["warnings"].append("Field type not explicit; defaulted to text.")
    _drop_row_entries_by_aliases(row, TICKET_FIELD_TYPE_FIELDS)
    _set_action_value(row, "field_type", normalized_type)

    raw_option_values = _extract_values_by_field_aliases(row, TICKET_FIELD_OPTIONS_FIELDS)
    raw_options = raw_option_values[0] if raw_option_values else None
    if raw_options in (None, "", []):
        prompt_option_hints = _extract_ticket_field_option_hints(prompt)
        if prompt_option_hints:
            raw_options = prompt_option_hints
            info["alias_mappings"].append("inferred custom_field_options from prompt")
    parsed_options, option_warnings = _parse_custom_field_options(raw_options)
    info["warnings"].extend(option_warnings)
    _drop_row_entries_by_aliases(row, TICKET_FIELD_OPTIONS_FIELDS)
    if parsed_options:
        _set_action_value(row, "custom_field_options", parsed_options)
    elif normalized_type in {"tagger", "multiselect"}:
        row.setdefault("validation_overrides", {})
        row["validation_overrides"]["blocked_reason"] = (
            "Dropdown/multi-select fields require custom_field_options, but no valid options were generated."
        )
        info["warnings"].append("Missing custom_field_options for dropdown/multi-select field.")

    defaults = {
        "agent_can_edit": settings.ticket_field_default_agent_can_edit,
        "visible_in_portal": settings.ticket_field_default_visible_in_portal,
        "editable_in_portal": settings.ticket_field_default_editable_in_portal,
        "required": settings.ticket_field_default_required,
        "required_in_portal": settings.ticket_field_default_required_in_portal,
    }
    for canonical_field, aliases in TICKET_FIELD_PERMISSION_FIELD_ALIASES.items():
        values = _extract_values_by_field_aliases(row, aliases)
        parsed_value = None
        for raw_value in values:
            parsed = _coerce_bool(raw_value)
            if parsed is not None:
                parsed_value = parsed
                break
        if parsed_value is None:
            parsed_value = defaults[canonical_field]
            info["defaults_applied"].append(f"{canonical_field}={str(parsed_value).lower()}")
        _drop_row_entries_by_aliases(row, aliases)
        _set_action_value(row, canonical_field, parsed_value)

    if info["defaults_applied"]:
        info["warnings"].append(
            "Applied infer+warn defaults: " + ", ".join(info["defaults_applied"]) + "."
        )

    if info["warnings"]:
        dependency_notes = list(row.get("dependency_notes", []) or [])
        dependency_notes.extend(info["warnings"])
        row["dependency_notes"] = dependency_notes

    return row, info


def _extract_ticket_form_field_references(row: dict) -> list[str]:
    refs: list[str] = []
    raw_values = _extract_values_by_field_aliases(row, TICKET_FORM_REFERENCE_FIELDS)
    for raw in raw_values:
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    candidate = (
                        item.get("id")
                        or item.get("name")
                        or item.get("title")
                        or item.get("value")
                    )
                    if candidate is not None:
                        refs.append(str(candidate).strip())
                else:
                    refs.append(str(item).strip())
        elif isinstance(raw, dict):
            candidate = raw.get("id") or raw.get("name") or raw.get("title") or raw.get("value")
            if candidate is not None:
                refs.append(str(candidate).strip())
        elif raw is not None:
            text = str(raw).strip()
            if not text:
                continue
            if "," in text or "|" in text or ";" in text:
                refs.extend([part.strip() for part in re.split(r"[,|;]", text) if part.strip()])
            else:
                refs.append(text)
    deduped: list[str] = []
    seen: set[str] = set()
    for value in refs:
        key = value.lower()
        if not value or key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


def _canonicalize_generated_rows(
    *,
    rows: list[dict],
    prompt: str,
    reference_catalog: dict[str, list[dict]],
    existing_index: dict[str, dict[str, dict]],
    settings,
) -> tuple[list[dict], dict, dict]:
    normalized_rows: list[dict] = []
    field_inference_records: list[dict] = []
    form_resolution = {
        "resolved_ids": 0,
        "auto_created_fields": 0,
        "unresolved": [],
        "forms_processed": 0,
    }

    existing_field_map: dict[str, str] = {}
    for item in reference_catalog.get("ticket_fields", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().lower()
        item_id = str(item.get("id", "")).strip()
        if name and item_id:
            existing_field_map[name] = item_id

    generated_field_titles: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        row["object_type"] = _normalize_object_type(str(row.get("object_type", "triggers")))
        if row["object_type"] == "ticket_fields":
            row, info = _canonicalize_ticket_field_record(
                row,
                prompt=prompt,
                settings=settings,
            )
            generated_field_titles.add(str(row.get("title", "")).strip().lower())
            field_inference_records.append(info)
        normalized_rows.append(row)

    for row in normalized_rows:
        if str(row.get("object_type", "")).strip().lower() != "ticket_forms":
            continue
        form_resolution["forms_processed"] += 1
        references = _extract_ticket_form_field_references(row)
        if not references:
            continue

        resolved_ids: list[int] = []
        referenced_names: list[str] = []
        for ref in references:
            ref_text = str(ref).strip()
            if not ref_text:
                continue
            if ref_text.isdigit():
                resolved_ids.append(int(ref_text))
                form_resolution["resolved_ids"] += 1
                continue

            lookup_id = existing_field_map.get(ref_text.lower())
            if lookup_id and lookup_id.isdigit():
                resolved_ids.append(int(lookup_id))
                form_resolution["resolved_ids"] += 1
                continue

            if ref_text.lower() in generated_field_titles:
                referenced_names.append(ref_text)
                continue

            if settings.form_missing_field_mode == "auto_create":
                generated_field_titles.add(ref_text.lower())
                referenced_names.append(ref_text)
                form_resolution["auto_created_fields"] += 1
                companion_field = {
                    "object_type": "ticket_fields",
                    "title": ref_text,
                    "conditions": [],
                    "actions": [{"field": "field_type", "value": "text"}],
                    "dependency_notes": [
                        "Auto-created from ticket form reference because field did not exist in Zendesk context."
                    ],
                }
                companion_field, info = _canonicalize_ticket_field_record(
                    companion_field,
                    prompt=prompt,
                    settings=settings,
                )
                info["warnings"].append(
                    "Companion field was auto-created from ticket form references."
                )
                field_inference_records.append(info)
                normalized_rows.append(companion_field)
            elif settings.form_missing_field_mode == "existing_only":
                row.setdefault("validation_overrides", {})
                row["validation_overrides"]["blocked_reason"] = (
                    f"Form references missing field '{ref_text}', and existing-only form mode is active."
                )
                form_resolution["unresolved"].append(ref_text)
            else:
                form_resolution["unresolved"].append(ref_text)
                dependency_notes = list(row.get("dependency_notes", []) or [])
                dependency_notes.append(
                    f"Suggested follow-up: create or map field '{ref_text}' before form deployment."
                )
                row["dependency_notes"] = dependency_notes

        _drop_row_entries_by_aliases(row, TICKET_FORM_REFERENCE_FIELDS)
        if resolved_ids:
            _set_action_value(row, "ticket_field_ids", resolved_ids)
        if referenced_names:
            _set_action_value(row, "ticket_field_names", referenced_names)
            dependency_notes = list(row.get("dependency_notes", []) or [])
            dependency_notes.append(
                "ticket_field_names will be resolved to IDs at deployment time."
            )
            row["dependency_notes"] = dependency_notes

    field_inference = {
        "policy": settings.inference_policy,
        "records": field_inference_records,
        "total_records": len(field_inference_records),
    }
    return normalized_rows, field_inference, form_resolution


def _is_explicit_enough_for_generation(prompt: str, object_type: str) -> bool:
    text = prompt.strip().lower()
    if not text:
        return False

    if object_type in {"triggers", "automations", "views"}:
        mentions_action = any(
            token in text for token in ["add tag", "set tag", "tag", "status", "priority", "group", "notify", "comment"]
        )
        mentions_scope = any(
            token in text
            for token in [
                "all groups",
                "all forms",
                "all statuses",
                "any open ticket",
                "open ticket",
                "status is open",
                "status=open",
            ]
        )
        mentions_concrete_condition = any(
            token in text
            for token in [
                "where",
                "apply to",
                "status is",
                "status=",
                "open ticket",
                "new ticket",
                "group",
                "form",
                "brand",
            ]
        )
        tag_value = re.search(
            r"\btag(?:\s+name)?\s*(?:is|=|to|as)\s*[\"']?([a-z0-9_-]{2,})",
            text,
        )
        add_tag_value = re.search(
            r"\badd(?:s)?\s+(?:the\s+)?tag\s+[\"']?([a-z0-9_-]{2,})",
            text,
        )
        status_action_value = re.search(
            r"\b(?:set|change|update)\s+status\s*(?:to|=|as)?\s*([a-z_]+)",
            text,
        )
        mentions_action_value = bool(tag_value or add_tag_value or status_action_value)
        return mentions_action and mentions_action_value and (mentions_scope or mentions_concrete_condition)

    if object_type == "macros":
        explicit_message = bool(
            re.search(r"comment\s*:\s*.+", text)
            or re.search(r"reply\s*:\s*.+", text)
            or re.search(r"[\"'].{4,}[\"']", text)
        )
        explicit_side_effect = bool(
            re.search(r"\btag(?:\s+name)?\s*(?:is|=|to|as)\s*[\"']?([a-z0-9_-]{2,})", text)
            or re.search(r"\b(?:set|change|update)\s+status\s*(?:to|=|as)?\s*([a-z_]+)", text)
            or re.search(r"\b(?:set|assign)\s+group\s*(?:to|=|as)?\s*[\"']?([a-z0-9 _-]{2,})", text)
        )
        return explicit_message or explicit_side_effect

    if object_type == "articles":
        mentions_location = any(token in text for token in ["section", "category", "help center", "knowledge base"])
        mentions_content = any(token in text for token in ["article", "title", "body", "publish"])
        return mentions_location and mentions_content

    if object_type == "ticket_fields":
        has_field_intent = any(
            token in text
            for token in ["field", "ticket field", "custom field", "dropdown", "drop-down", "multi-select", "text field"]
        )
        has_name_signal = bool(re.search(r"\b(?:called|named|name)\b", text))
        has_type_signal = bool(_infer_requested_ticket_field_type(prompt)) or any(
            token in text for token in ["text", "textarea", "integer", "decimal", "checkbox", "date", "lookup"]
        )
        has_option_signal = bool(_extract_ticket_field_option_hints(prompt))
        if has_field_intent and (has_name_signal or has_type_signal or has_option_signal):
            return True

    if object_type == "ticket_forms":
        has_form_intent = any(token in text for token in ["form", "ticket form", "request form"])
        has_content_signal = any(
            token in text
            for token in ["field", "fields", "include", "with", "add", "remove"]
        )
        if has_form_intent and has_content_signal:
            return True

    return len(text) >= 20


def _build_clarification_questions(
    prompt: str,
    plan: dict,
    *,
    ambiguity_score: float,
    ambiguity_threshold: float,
) -> list[dict]:
    def _finalize_question(
        *,
        qid: str,
        question: str,
        missing_detail: str,
        why_required: str,
        example_answer: str,
        understood_hint: str,
    ) -> list[dict]:
        return [
            {
                "id": qid,
                "question": question,
                "reason": (
                    f"Understood so far: {understood_hint}. "
                    f"Missing detail: {missing_detail}. "
                    f"Why this is required: {why_required}."
                ),
                "examples": [example_answer],
            }
        ]

    text = prompt.strip().lower()
    object_type = _normalize_object_type(str(plan.get("object_type", "triggers")))
    questions: list[dict] = []
    prompt_explicit = _is_explicit_enough_for_generation(prompt, object_type)
    confidence = float(plan.get("confidence", 0.0) or 0.0)
    planner_questions = plan.get("clarification_questions", [])
    include_planner_questions = (
        not prompt_explicit
        and isinstance(planner_questions, list)
        and (
            ambiguity_score >= max(ambiguity_threshold + 0.08, 0.68)
            or confidence < 0.62
        )
    )
    if include_planner_questions:
        for index, item in enumerate(planner_questions, start=1):
            question_text = str(item).strip()
            if not question_text:
                continue
            questions.append(
                {
                    "id": f"planner_question_{index}",
                    "question": question_text,
                    "missing_detail": "Planner-detected missing scope",
                    "why_required": "The plan confidence is low enough that generation would likely be unreliable.",
                    "example_answer": "Apply this only to group=Finance & Investments and form=Claim & Payouts.",
                    "understood_hint": str(plan.get("intent", prompt)).strip()[:220],
                }
            )

    has_time_phrase = bool(re.search(r"\b\d+\s*(hour|hours|hr|hrs|day|days)\b", text))
    mentions_message_content = any(
        key in text for key in ["reply", "message", "comment", "body", "template", "say"]
    )
    mentions_action_target = any(
        key in text for key in ["assign", "group", "tag", "status", "priority", "form", "field"]
    )
    mentions_condition = any(
        key in text
        for key in [
            "when",
            "if",
            "for tickets",
            "condition",
            "where",
            "only if",
            "route",
            "routing",
            "triage",
            "all groups",
            "all forms",
            "all statuses",
            "open ticket",
            "status is",
            "apply to",
        ]
    )
    ticket_field_intent = any(
        token in text
        for token in ["ticket field", "custom field", "dropdown field", "drop-down field", "field called"]
    )

    if object_type == "macros" and not prompt_explicit:
        if has_time_phrase:
            questions.append(
                {
                    "id": "macro_vs_automation",
                    "question": "Do you want a manual macro or a timed automation?",
                    "missing_detail": "Execution mode",
                    "why_required": "Macros run manually and cannot execute by elapsed time on their own.",
                    "example_answer": "Timed automation after 25 hours.",
                    "understood_hint": "You want a 25-hour follow-up behavior.",
                }
            )
        if not mentions_message_content:
            questions.append(
                {
                    "id": "macro_content",
                    "question": "What exact reply/comment text should the macro add?",
                    "missing_detail": "Message content",
                    "why_required": "Macro outputs must include the exact text to apply to tickets.",
                    "example_answer": "Please share your policy number and claim reference.",
                    "understood_hint": "You want a macro for ticket replies/updates.",
                }
            )
        if not mentions_action_target:
            questions.append(
                {
                    "id": "macro_side_effects",
                    "question": "Should this macro also set tags, status, assignee group, or priority?",
                    "missing_detail": "Ticket side-effects",
                    "why_required": "Without side-effects, the macro may not perform the operational update you expect.",
                    "example_answer": "Set tag follow_up_25h and status open.",
                    "understood_hint": "You want a macro response flow.",
                }
            )

    if (
        object_type in {"triggers", "automations", "views"}
        and not mentions_condition
        and not prompt_explicit
        and not ticket_field_intent
    ):
        questions.append(
            {
                "id": "conditions_needed",
                "question": "What conditions should this apply to (status, group, form, brand, tags)?",
                "missing_detail": "Rule filter criteria",
                "why_required": "Trigger/automation/view objects need conditions to avoid applying to unintended tickets.",
                "example_answer": "Only for status=new and form=Claim & Payouts.",
                "understood_hint": f"You want a {object_type.rstrip('s')} configuration.",
            }
        )

    if (
        object_type == "articles"
        and not prompt_explicit
        and not any(key in text for key in ["section", "category", "help center"])
    ):
        questions.append(
            {
                "id": "article_target_location",
                "question": "Which Help Center section/category should the article be created in?",
                "missing_detail": "Article destination",
                "why_required": "Zendesk article creation requires a section/category target.",
                "example_answer": "Category: Pensioners, Section: FAQ.",
                "understood_hint": "You want a Help Center article created.",
            }
        )

    # de-duplicate by normalized text and keep concise.
    deduped: list[dict] = []
    seen: set[str] = set()
    for item in questions:
        key = str(item.get("question", "")).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    if not deduped:
        return []
    highest = deduped[0]
    return _finalize_question(
        qid=str(highest.get("id", "clarification_needed")),
        question=str(highest.get("question", "Please clarify this request.")),
        missing_detail=str(highest.get("missing_detail", "Required generation detail")),
        why_required=str(highest.get("why_required", "Needed for reliable generation and deployment safety.")),
        example_answer=str(highest.get("example_answer", "Provide exact scope and expected action.")),
        understood_hint=str(highest.get("understood_hint", str(plan.get("intent", prompt)).strip()[:220])),
    )


def _build_planning_summary(
    *,
    plan: dict,
    request: ImportAssistantGenerateRequest,
    normalized_focus_object_types: list[str],
    related_objects: list[dict],
    reference_catalog: dict[str, list[dict]],
    prompt_explicit: bool,
    llm_context_bundle: dict,
) -> dict:
    return {
        "object_type": plan.get("object_type", "triggers"),
        "intent": plan.get("intent", request.prompt),
        "confidence": plan.get("confidence", 0.7),
        "ambiguity_score": plan.get("ambiguity_score", 0.0),
        "prompt_explicit": prompt_explicit,
        "ambiguity_reasons": plan.get("ambiguity_reasons", []),
        "dependency_mode": request.dependency_mode,
        "focus_object_types": normalized_focus_object_types,
        "dependency_notes": plan.get("dependency_notes", ""),
        "related_objects_selected": len(related_objects),
        "reference_catalog_counts": {key: len(values) for key, values in reference_catalog.items()},
        "llm_context_profile": llm_context_bundle.get("profile", "standard"),
        "llm_context_counts": llm_context_bundle.get("counts", {}),
        "llm_context_limits": llm_context_bundle.get("limits", {}),
        "llm": plan.get("llm", {}),
    }


async def _run_generator_with_context_fallback(
    *,
    plan: dict,
    request: ImportAssistantGenerateRequest,
    focus_object_types: list[str],
    standard_context_bundle: dict,
    aggressive_context_bundle: dict,
    chunk_instruction: str | None = None,
    chunk_target_count: int | None = None,
    chunk_index: int | None = None,
    chunk_total: int | None = None,
    existing_titles: list[str] | None = None,
) -> tuple[list[dict], dict, str]:
    runtime_metrics: dict = {}
    context_profile = standard_context_bundle.get("profile", "standard")
    reduced_token_budget = max(300, int(get_settings().llm_generator_max_output_tokens * 0.6))
    try:
        generated_data = await run_generator(
            plan,
            dependency_mode=request.dependency_mode,
            focus_object_types=focus_object_types,
            related_objects=standard_context_bundle["related_objects"],
            reference_catalog=standard_context_bundle["reference_catalog"],
            recent_batch_context=standard_context_bundle["recent_batch_context"],
            context_notes=standard_context_bundle["context_notes"],
            chunk_instruction=chunk_instruction,
            chunk_target_count=chunk_target_count,
            chunk_index=chunk_index,
            chunk_total=chunk_total,
            existing_titles=existing_titles,
            max_output_tokens=None,
            allow_fallback=False,
        )
    except GeneratorStructuredOutputError as exc:
        if _is_schema_validation_failure(exc):
            context_profile = aggressive_context_bundle.get("profile", "aggressive")
            try:
                generated_data = await run_generator(
                    plan,
                    dependency_mode=request.dependency_mode,
                    focus_object_types=focus_object_types,
                    related_objects=aggressive_context_bundle["related_objects"],
                    reference_catalog=aggressive_context_bundle["reference_catalog"],
                    recent_batch_context=aggressive_context_bundle["recent_batch_context"],
                    context_notes=aggressive_context_bundle["context_notes"],
                    chunk_instruction=chunk_instruction,
                    chunk_target_count=chunk_target_count,
                    chunk_index=chunk_index,
                    chunk_total=chunk_total,
                    existing_titles=existing_titles,
                    max_output_tokens=reduced_token_budget,
                    allow_fallback=False,
                )
            except GeneratorStructuredOutputError as compact_exc:
                raise compact_exc from compact_exc
        else:
            raise
    except RuntimeError as exc:
        if _is_deterministic_llm_error(exc):
            raise
        context_profile = aggressive_context_bundle.get("profile", "aggressive")
        try:
            generated_data = await run_generator(
                plan,
                dependency_mode=request.dependency_mode,
                focus_object_types=focus_object_types,
                related_objects=aggressive_context_bundle["related_objects"],
                reference_catalog=aggressive_context_bundle["reference_catalog"],
                recent_batch_context=aggressive_context_bundle["recent_batch_context"],
                context_notes=aggressive_context_bundle["context_notes"],
                chunk_instruction=chunk_instruction,
                chunk_target_count=chunk_target_count,
                chunk_index=chunk_index,
                chunk_total=chunk_total,
                existing_titles=existing_titles,
                max_output_tokens=reduced_token_budget,
                allow_fallback=False,
            )
        except RuntimeError as compact_exc:
            if _is_deterministic_llm_error(compact_exc):
                raise
            generated_data = await run_generator(
                plan,
                dependency_mode=request.dependency_mode,
                focus_object_types=focus_object_types,
                related_objects=aggressive_context_bundle["related_objects"],
                reference_catalog=aggressive_context_bundle["reference_catalog"],
                recent_batch_context=aggressive_context_bundle["recent_batch_context"],
                context_notes=aggressive_context_bundle["context_notes"],
                chunk_instruction=chunk_instruction,
                chunk_target_count=chunk_target_count,
                chunk_index=chunk_index,
                chunk_total=chunk_total,
                existing_titles=existing_titles,
                max_output_tokens=reduced_token_budget,
                allow_fallback=True,
            )
    runtime_metrics = GrokClient.get_last_call_metrics("generator")
    return generated_data, runtime_metrics, context_profile


async def generate_import_assistant_batch(
    request: ImportAssistantGenerateRequest,
) -> ImportAssistantGenerateResponse:
    store = get_batch_store()
    sheets = SheetsService()
    appscript = AppScriptBridgeService()
    settings = get_settings()
    benchmark_mode = bool(
        settings.benchmark_mode_enabled
        and str(request.mode or "").strip().lower() == "benchmark"
    )
    planner_route = resolve_model_route(settings, "planner")
    clarifier_route = resolve_model_route(settings, "clarifier")
    generator_route = resolve_model_route(settings, "generator")
    related_objects = [item.model_dump() for item in request.related_objects]
    focus_object_types = _normalize_focus_object_types(request.focus_object_types)
    focus_object_type_set = set(focus_object_types)
    reference_catalog = {
        key: [item.model_dump() for item in values]
        for key, values in (request.reference_catalog or {}).items()
    }
    related_lookup = _build_related_lookup(related_objects)
    existing_object_index = _build_existing_object_index(reference_catalog)

    batch_id = _new_batch_id()
    created_at = _utc_now()
    batch = {
        "batch_id": batch_id,
        "status": "received",
        "prompt": request.prompt,
        "requester": request.requester,
        "target_environment": request.target_environment,
        "mode": request.mode,
        "created_at": created_at,
        "updated_at": created_at,
        "status_history": [{"status": "received", "message": "Batch accepted.", "at": created_at}],
        "records": [],
        "generated_counts": {},
        "validation_summary": {"passed": 0, "warnings": 0, "blocked": 0},
        "planning_summary": {},
        "metadata": {},
    }
    store.save_batch(batch)

    llm_context_standard = _build_llm_context_bundle(
        settings=settings,
        focus_object_types=focus_object_type_set,
        related_objects=related_objects,
        reference_catalog=reference_catalog,
        recent_batch_context=request.recent_batch_context,
        context_notes=request.context_notes,
        aggressive=False,
    )
    llm_context_aggressive = _build_llm_context_bundle(
        settings=settings,
        focus_object_types=focus_object_type_set,
        related_objects=related_objects,
        reference_catalog=reference_catalog,
        recent_batch_context=request.recent_batch_context,
        context_notes=request.context_notes,
        aggressive=True,
    )
    planner_context_bundle = llm_context_standard
    generator_context_bundle = llm_context_standard
    planner_telemetry: dict = {}
    generator_telemetry: dict = {}

    store.append_status(batch_id, "request_validated", "Incoming request validated.")
    store.append_status(batch_id, "planning", "Planner call in progress.")
    try:
        plan = await run_planner(
            request.prompt,
            dependency_mode=request.dependency_mode,
            focus_object_types=focus_object_types,
            related_objects=planner_context_bundle["related_objects"],
            reference_catalog=planner_context_bundle["reference_catalog"],
            recent_batch_context=planner_context_bundle["recent_batch_context"],
            context_notes=planner_context_bundle["context_notes"],
            allow_fallback=False,
        )
    except RuntimeError as exc:
        if _is_deterministic_llm_error(exc):
            store.append_status(
                batch_id,
                "planning",
                "Planner returned deterministic JSON failure; switching to heuristic planner fallback.",
            )
            plan = await run_planner(
                request.prompt,
                dependency_mode=request.dependency_mode,
                focus_object_types=focus_object_types,
                related_objects=planner_context_bundle["related_objects"],
                reference_catalog=planner_context_bundle["reference_catalog"],
                recent_batch_context=planner_context_bundle["recent_batch_context"],
                context_notes=planner_context_bundle["context_notes"],
                allow_fallback=True,
            )
        else:
            store.append_status(
                batch_id,
                "planning",
                "Planner retrying with compact context to stay within model limits.",
            )
            planner_context_bundle = llm_context_aggressive
            try:
                plan = await run_planner(
                    request.prompt,
                    dependency_mode=request.dependency_mode,
                    focus_object_types=focus_object_types,
                    related_objects=planner_context_bundle["related_objects"],
                    reference_catalog=planner_context_bundle["reference_catalog"],
                    recent_batch_context=planner_context_bundle["recent_batch_context"],
                    context_notes=planner_context_bundle["context_notes"],
                    allow_fallback=False,
                )
            except RuntimeError as compact_exc:
                if _is_deterministic_llm_error(compact_exc):
                    store.append_status(
                        batch_id,
                        "planning",
                        "Compact planner attempt hit deterministic JSON failure; using heuristic planner fallback.",
                    )
                    plan = await run_planner(
                        request.prompt,
                        dependency_mode=request.dependency_mode,
                        focus_object_types=focus_object_types,
                        related_objects=planner_context_bundle["related_objects"],
                        reference_catalog=planner_context_bundle["reference_catalog"],
                        recent_batch_context=planner_context_bundle["recent_batch_context"],
                        context_notes=planner_context_bundle["context_notes"],
                        allow_fallback=True,
                    )
                else:
                    plan = await run_planner(
                        request.prompt,
                        dependency_mode=request.dependency_mode,
                        focus_object_types=focus_object_types,
                        related_objects=planner_context_bundle["related_objects"],
                        reference_catalog=planner_context_bundle["reference_catalog"],
                        recent_batch_context=planner_context_bundle["recent_batch_context"],
                        context_notes=planner_context_bundle["context_notes"],
                        allow_fallback=True,
                    )
    planner_telemetry = (
        plan.get("llm", {}).get("telemetry", {})
        if isinstance(plan, dict)
        else {}
    )
    if not planner_telemetry:
        planner_telemetry = GrokClient.get_last_call_metrics("planner")
    store.append_status(batch_id, "planned", "Planner output received.")

    ambiguity_score = float(plan.get("ambiguity_score", 0.0) or 0.0)
    ambiguity_threshold = settings.llm_ambiguity_threshold
    planned_object_type = _normalize_object_type(str(plan.get("object_type", "triggers")))
    prompt_explicit = _is_explicit_enough_for_generation(request.prompt, planned_object_type)
    clarification_questions = _build_clarification_questions(
        request.prompt,
        plan,
        ambiguity_score=ambiguity_score,
        ambiguity_threshold=ambiguity_threshold,
    )
    if (
        not benchmark_mode
        and ambiguity_score >= ambiguity_threshold
        and not clarification_questions
        and not prompt_explicit
    ):
        ambiguity_reasons = plan.get("ambiguity_reasons", [])
        reason_text = (
            str(ambiguity_reasons[0]).strip()
            if isinstance(ambiguity_reasons, list) and ambiguity_reasons
            else "The request appears ambiguous for a reliable one-shot deployment."
        )
        clarification_questions = [
            {
                "id": "ambiguity_scope",
                "question": "What exact scope should this apply to (object target + conditions + expected actions)?",
                "reason": (
                    f"Understood so far: {str(plan.get('intent', request.prompt)).strip()[:220]}. "
                    "Missing detail: explicit scope and action mapping. "
                    f"Why this is required: {reason_text}."
                ),
                "examples": [
                    "For trigger X: status=new, group=Claims; action=set tag test_ticket and assign group Billing.",
                ],
            }
        ]
    if prompt_explicit and clarification_questions:
        # Deterministic override: explicit, actionable prompts should not be trapped in clarification loops.
        clarification_questions = []
    needs_clarification = bool(clarification_questions)
    if benchmark_mode and needs_clarification:
        clarification_questions = []
        needs_clarification = False
    planning_summary = _build_planning_summary(
        plan=plan,
        request=request,
        normalized_focus_object_types=focus_object_types,
        related_objects=related_objects,
        reference_catalog=reference_catalog,
        prompt_explicit=prompt_explicit,
        llm_context_bundle=planner_context_bundle,
    )
    chunk_estimate = _estimate_requested_record_count(request.prompt)
    chunk_plan = _build_chunk_plan(
        settings=settings,
        estimated_count=int(chunk_estimate.get("estimated_count", 1) or 1),
    )
    chunking_metadata = {
        "activated": bool(chunk_plan.get("activated", False)),
        "estimated_requested_records": int(chunk_plan.get("estimated_count", 1) or 1),
        "chunk_size": int(chunk_plan.get("chunk_size", 1) or 1),
        "max_chunks": int(chunk_plan.get("max_chunks", 1) or 1),
        "trigger_min_records": int(chunk_plan.get("trigger_min_records", 2) or 2),
        "total_chunks": int(chunk_plan.get("total_chunks", 1) or 1),
        "chunk_targets": list(chunk_plan.get("chunk_targets", [1]) or [1]),
        "exceeds_cap": bool(chunk_plan.get("exceeds_cap", False)),
        "estimation_sources": list(chunk_estimate.get("sources", []) or []),
        "numeric_matches": list(chunk_estimate.get("numeric_matches", []) or []),
        "enumerated_items": int(chunk_estimate.get("enumerated_items", 0) or 0),
        "chunks": [],
        "total_generated_before_dedupe": 0,
        "total_generated_after_dedupe": 0,
        "total_duplicates_dropped": 0,
        "total_pacing_wait_ms": 0.0,
        "final_status": "pending",
        "abort_reason": None,
    }
    if chunking_metadata["activated"] and chunking_metadata["exceeds_cap"]:
        clarification_questions = _build_chunk_split_guidance(
            total_chunks=chunking_metadata["total_chunks"],
            max_chunks=chunking_metadata["max_chunks"],
            chunk_size=chunking_metadata["chunk_size"],
            estimated_count=chunking_metadata["estimated_requested_records"],
        )
        needs_clarification = True
        chunking_metadata["final_status"] = "clarification_required"
        chunking_metadata["abort_reason"] = (
            f"Estimated chunk count {chunking_metadata['total_chunks']} exceeds cap "
            f"{chunking_metadata['max_chunks']}."
        )
    if needs_clarification:
        store.append_status(
            batch_id,
            "clarification_required",
            "Additional details required before generation.",
        )
        batch = store.update_batch(
            batch_id,
            {
                "status": "clarification_required",
                "planning_summary": planning_summary,
                "metadata": {
                    "benchmark": {
                        "enabled": benchmark_mode,
                    },
                    "clarification": {
                        "required": True,
                        "questions": clarification_questions,
                        "ambiguity_score": ambiguity_score,
                        "ambiguity_threshold": ambiguity_threshold,
                    },
                    "llm_routes": {
                        "planner": planner_route.__dict__,
                        "clarifier": clarifier_route.__dict__,
                        "generator": generator_route.__dict__,
                    },
                    "llm_runtime": {
                        "planner": planner_telemetry,
                    },
                    "llm_context": {
                        "planner_profile": planner_context_bundle.get("profile"),
                        "planner_counts": planner_context_bundle.get("counts", {}),
                        "planner_limits": planner_context_bundle.get("limits", {}),
                    },
                    "chunking": chunking_metadata,
                    "context_notes": request.context_notes or "",
                    "recent_batch_context": request.recent_batch_context,
                    "failure": {
                        "failure_stage": "generate",
                        "failure_code": "clarification_required",
                        "failure_reason": "Additional details are required before generation can continue.",
                        "next_step": "Answer the clarification question and submit again.",
                    },
                },
            },
        )
        return ImportAssistantGenerateResponse(
            batch_id=batch_id,
            status="clarification_required",
            planning_summary=batch.get("planning_summary", {}),
            generated_counts={},
            validation_summary=ValidationSummary(),
            preview_url=f"/api/import-assistant/preview/{batch_id}",
            needs_clarification=True,
            clarification_questions=clarification_questions,
            metadata=batch.get("metadata", {}),
        )

    store.append_status(batch_id, "schemas_selected", "Schemas selected from registry.")
    store.append_status(batch_id, "generating", "Generator call in progress.")
    generated_data: list[dict] = []
    generator_chunk_telemetry: list[dict] = []
    chunked_titles: list[str] = []
    chunked_title_set: set[str] = set()
    total_duplicates_dropped = 0
    total_generated_before_dedupe = 0
    total_pacing_wait_ms = 0.0
    try:
        if chunking_metadata["activated"]:
            chunk_targets = list(chunking_metadata.get("chunk_targets", []) or [])
            for index, target_count in enumerate(chunk_targets, start=1):
                if index > 1:
                    wait_seconds = max(settings.llm_auto_chunk_pacing_seconds, 0.0)
                    if settings.llm_auto_chunk_pacing_jitter_seconds > 0:
                        wait_seconds += random.uniform(0.0, settings.llm_auto_chunk_pacing_jitter_seconds)
                    if wait_seconds > 0:
                        store.append_status(
                            batch_id,
                            "generating",
                            f"Pacing before chunk {index}/{len(chunk_targets)} ({wait_seconds:.2f}s).",
                        )
                        await asyncio.sleep(wait_seconds)
                        total_pacing_wait_ms += wait_seconds * 1000.0

                store.append_status(
                    batch_id,
                    "generating",
                    f"Generating chunk {index}/{len(chunk_targets)} (target={target_count}).",
                )
                chunk_instruction = _build_chunk_instruction(
                    chunk_index=index,
                    chunk_total=len(chunk_targets),
                    target_count=int(target_count),
                )
                chunk_rows, chunk_runtime_metrics, context_profile = await _run_generator_with_context_fallback(
                    plan=plan,
                    request=request,
                    focus_object_types=focus_object_types,
                    standard_context_bundle=generator_context_bundle,
                    aggressive_context_bundle=llm_context_aggressive,
                    chunk_instruction=chunk_instruction,
                    chunk_target_count=int(target_count),
                    chunk_index=index,
                    chunk_total=len(chunk_targets),
                    existing_titles=chunked_titles[-200:],
                )
                if context_profile == "aggressive":
                    store.append_status(
                        batch_id,
                        "generating",
                        f"Chunk {index}/{len(chunk_targets)} used compact context profile.",
                    )

                before_chunk_dedupe_count = len(generated_data)
                raw_chunk_count = len(chunk_rows)
                total_generated_before_dedupe += raw_chunk_count
                generated_data.extend(chunk_rows)
                generated_data, dropped = _dedupe_generated_rows(generated_data)
                total_duplicates_dropped += dropped
                after_chunk_dedupe_count = len(generated_data)
                added_after_dedupe = max(after_chunk_dedupe_count - before_chunk_dedupe_count, 0)

                chunked_titles = []
                chunked_title_set = set()
                for row in generated_data:
                    title = str(row.get("title", "")).strip()
                    title_key = _normalize_title_for_dedupe(title)
                    if not title or not title_key or title_key in chunked_title_set:
                        continue
                    chunked_title_set.add(title_key)
                    chunked_titles.append(title)

                chunk_entry = {
                    "chunk_index": index,
                    "chunk_total": len(chunk_targets),
                    "target_records": int(target_count),
                    "generated_records": raw_chunk_count,
                    "deduped_records_added": added_after_dedupe,
                    "duplicates_dropped": dropped,
                    "context_profile": context_profile,
                    "pre_request_wait_ms": chunk_runtime_metrics.get("pre_request_wait_ms"),
                    "retry_count": chunk_runtime_metrics.get("retry_count"),
                    "final_status": chunk_runtime_metrics.get("final_status"),
                    "http_status": chunk_runtime_metrics.get("http_status"),
                    "model": chunk_runtime_metrics.get("model"),
                }
                generator_chunk_telemetry.append(chunk_entry)
        else:
            generated_data, generator_telemetry, context_profile = await _run_generator_with_context_fallback(
                plan=plan,
                request=request,
                focus_object_types=focus_object_types,
                standard_context_bundle=generator_context_bundle,
                aggressive_context_bundle=llm_context_aggressive,
                chunk_instruction=None,
                chunk_target_count=None,
                chunk_index=None,
                chunk_total=None,
                existing_titles=None,
            )
            if context_profile == "aggressive":
                store.append_status(
                    batch_id,
                    "generating",
                    "Generator used compact context profile to stay within limits.",
                )
            generator_chunk_telemetry.append(
                {
                    "chunk_index": 1,
                    "chunk_total": 1,
                    "target_records": chunking_metadata["estimated_requested_records"],
                    "generated_records": len(generated_data),
                    "deduped_records_added": len(generated_data),
                    "duplicates_dropped": 0,
                    "context_profile": context_profile,
                    "pre_request_wait_ms": generator_telemetry.get("pre_request_wait_ms"),
                    "retry_count": generator_telemetry.get("retry_count"),
                    "final_status": generator_telemetry.get("final_status"),
                    "http_status": generator_telemetry.get("http_status"),
                    "model": generator_telemetry.get("model"),
                }
            )
            total_generated_before_dedupe = len(generated_data)
    except RuntimeError as exc:
        chunking_metadata["chunks"] = generator_chunk_telemetry
        chunking_metadata["total_generated_before_dedupe"] = total_generated_before_dedupe
        chunking_metadata["total_generated_after_dedupe"] = len(generated_data)
        chunking_metadata["total_duplicates_dropped"] = total_duplicates_dropped
        chunking_metadata["total_pacing_wait_ms"] = round(total_pacing_wait_ms, 2)
        chunking_metadata["final_status"] = "failed"
        chunking_metadata["abort_reason"] = str(exc)
        runtime_metrics = GrokClient.get_last_call_metrics("generator")
        generator_error_metadata: dict = {}
        failure_code = "chunk_generation_failed"
        failure_message = str(exc)
        next_step_message = "Retry generation or reduce request scope/chunk size."
        status_message_prefix = "Generation failed during chunking"
        if not chunking_metadata.get("activated"):
            status_message_prefix = "Generator JSON validation failed"
        if isinstance(exc, GeneratorStructuredOutputError):
            error_meta = exc.as_metadata()
            generator_error_metadata = error_meta
            failure_code = "generator_json_validation_failed"
            failure_message = (
                "Generator could not produce schema-valid JSON after multiple recovery modes."
            )
            next_step_message = (
                "Use a narrower prompt with explicit Zendesk action fields (status/group_id/set_tags), "
                "or answer the clarification question to continue."
            )
            if not benchmark_mode and exc.corrective_question:
                clarification_questions = [
                    {
                        "id": "generator_output_shape",
                        "question": exc.corrective_question,
                        "reason": (
                            "Generator output failed JSON/schema validation across retry modes. "
                            "Please confirm a stricter, single-record output shape."
                        ),
                        "examples": [exc.corrective_example] if exc.corrective_example else [],
                    }
                ]
                store.append_status(
                    batch_id,
                    "clarification_required",
                    "Generator JSON validation failed. Clarification requested before retry.",
                )
                clarification_batch = store.update_batch(
                    batch_id,
                    {
                        "status": "clarification_required",
                        "planning_summary": planning_summary,
                        "records": [],
                        "generated_counts": {},
                        "validation_summary": ValidationSummary().model_dump(),
                        "metadata": {
                            "benchmark": {"enabled": benchmark_mode},
                            "chunking": chunking_metadata,
                            "llm_routes": {
                                "planner": planner_route.__dict__,
                                "clarifier": clarifier_route.__dict__,
                                "generator": generator_route.__dict__,
                            },
                            "llm_runtime": {
                                "planner": planner_telemetry,
                                "generator": runtime_metrics,
                                "generator_chunks": generator_chunk_telemetry,
                                "generator_error": generator_error_metadata,
                            },
                            "failure": {
                                "failure_stage": "generate",
                                "failure_code": failure_code,
                                "failure_reason": failure_message,
                                "next_step": next_step_message,
                            },
                        },
                    },
                )
                return ImportAssistantGenerateResponse(
                    batch_id=batch_id,
                    status="clarification_required",
                    planning_summary=clarification_batch.get("planning_summary", {}),
                    generated_counts={},
                    validation_summary=ValidationSummary(),
                    preview_url=f"/api/import-assistant/preview/{batch_id}",
                    needs_clarification=True,
                    clarification_questions=clarification_questions,
                    metadata=clarification_batch.get("metadata", {}),
                )
        store.append_status(batch_id, "failed", f"{status_message_prefix}: {exc}")
        store.update_batch(
            batch_id,
            {
                "planning_summary": planning_summary,
                "metadata": {
                    "benchmark": {
                        "enabled": benchmark_mode,
                    },
                    "chunking": chunking_metadata,
                    "llm_routes": {
                        "planner": planner_route.__dict__,
                        "clarifier": clarifier_route.__dict__,
                        "generator": generator_route.__dict__,
                    },
                    "llm_runtime": {
                        "planner": planner_telemetry,
                        "generator": runtime_metrics,
                        "generator_chunks": generator_chunk_telemetry,
                        "generator_error": generator_error_metadata,
                    },
                    "failure": {
                        "failure_stage": "generate",
                        "failure_code": failure_code,
                        "failure_reason": failure_message,
                        "next_step": next_step_message,
                    },
                },
            },
        )
        if chunking_metadata.get("activated"):
            raise RuntimeError(f"Chunked generation aborted: {exc}") from exc
        raise RuntimeError(f"Generator JSON validation failed: {exc}") from exc

    if chunking_metadata["activated"]:
        chunking_metadata["total_generated_before_dedupe"] = total_generated_before_dedupe
        chunking_metadata["total_generated_after_dedupe"] = len(generated_data)
        chunking_metadata["total_duplicates_dropped"] = total_duplicates_dropped
        chunking_metadata["total_pacing_wait_ms"] = round(total_pacing_wait_ms, 2)
        chunking_metadata["chunks"] = generator_chunk_telemetry
        chunking_metadata["final_status"] = "ok"
    else:
        chunking_metadata["total_generated_before_dedupe"] = len(generated_data)
        chunking_metadata["total_generated_after_dedupe"] = len(generated_data)
        chunking_metadata["total_duplicates_dropped"] = 0
        chunking_metadata["total_pacing_wait_ms"] = round(total_pacing_wait_ms, 2)
        chunking_metadata["chunks"] = generator_chunk_telemetry
        chunking_metadata["final_status"] = "single_pass"
    generator_telemetry = GrokClient.get_last_call_metrics("generator")
    store.append_status(batch_id, "generated", "Structured records generated.")

    generated_data, field_inference_metadata, form_field_resolution_metadata = _canonicalize_generated_rows(
        rows=generated_data,
        prompt=request.prompt,
        reference_catalog=reference_catalog,
        existing_index=existing_object_index,
        settings=settings,
    )

    generated_data, dependency_resolution = _apply_dependency_resolution(
        generated_data,
        related_lookup=related_lookup,
        dependency_mode=request.dependency_mode,
    )
    generated_data, focus_diagnostics = _annotate_focus_object_constraints(
        generated_data,
        focus_object_types=focus_object_type_set,
    )
    generated_data, duplicate_candidates = _annotate_duplicate_candidates(
        generated_data,
        existing_index=existing_object_index,
        dependency_mode=request.dependency_mode,
    )
    generation_safety = _evaluate_generation_safety(
        prompt=request.prompt,
        plan=plan,
        generated_rows=generated_data,
        focus_object_types=focus_object_type_set,
        min_confidence=settings.llm_min_deploy_confidence,
    )
    if benchmark_mode:
        generation_safety = {
            "blocked": False,
            "reasons": ["Generation safety checks bypassed in benchmark mode."],
            "confidence": float(plan.get("confidence", 0.0) or 0.0),
            "min_confidence": settings.llm_min_deploy_confidence,
            "fallback_detected": bool(generation_safety.get("fallback_detected")),
            "focus_violations": list(generation_safety.get("focus_violations", [])),
            "explicit_constraint_violations": list(
                generation_safety.get("explicit_constraint_violations", [])
            ),
            "blocked_record_ids": [],
            "bypassed": True,
        }

    preview_records, validation_summary = _build_preview_records(plan, generated_data)
    preview_records = _apply_generation_safety_to_preview(preview_records, generation_safety)
    validation_summary = _recompute_validation_summary(preview_records)
    generated_counts = _count_generated(preview_records)

    store.append_status(batch_id, "staging", "Staging batch to Google Sheets.")
    staging_metadata: dict = {}
    validation_metadata: dict = {}

    if appscript.enabled:
        appscript_stage = await appscript.invoke(
            action="write_batch_to_sheets",
            payload={
                "batch_id": batch_id,
                "prompt": request.prompt,
                "requester": request.requester,
                "status": "staging",
                "target_environment": request.target_environment,
                "created_at": created_at,
                "planning_summary": planning_summary,
                "records": preview_records,
            },
        )
        staging_metadata = {
            "mode": "appscript",
            "status": appscript_stage.get("status"),
            "detail": appscript_stage.get("detail"),
            "http_status": appscript_stage.get("http_status"),
            "result": appscript_stage.get("data", {}),
        }
        if appscript_stage.get("status") != "ok" and sheets.enabled:
            fallback_result = sheets.stage_batch(batch, plan, preview_records)
            staging_metadata["fallback"] = {
                "mode": "google_sheets_service_account",
                "result": fallback_result,
            }
        elif appscript_stage.get("status") != "ok":
            store.append_status(batch_id, "failed", "Apps Script staging failed.")
            raise RuntimeError(
                appscript_stage.get("detail")
                or "Apps Script staging failed and no backend fallback is configured."
            )
    else:
        staging_metadata = {
            "mode": "google_sheets_service_account",
            "result": sheets.stage_batch(batch, plan, preview_records),
        }

    store.append_status(batch_id, "staged", "Batch staged.")

    store.append_status(batch_id, "validating", "Running row-level validation.")
    if appscript.enabled:
        appscript_validation = await appscript.invoke(
            action="validate_batch",
            payload={"batch_id": batch_id},
        )
        validation_metadata = {
            "mode": "appscript",
            "status": appscript_validation.get("status"),
            "detail": appscript_validation.get("detail"),
            "http_status": appscript_validation.get("http_status"),
            "result": appscript_validation.get("data", {}),
        }
        validation_data = appscript_validation.get("data", {})
        if isinstance(validation_data, dict):
            summary_data = validation_data.get("summary", {})
            if isinstance(summary_data, dict):
                try:
                    validation_summary = ValidationSummary(**summary_data)
                except Exception:
                    pass

        if appscript_validation.get("status") != "ok" and sheets.enabled:
            sheets.write_validation_log(batch_id, preview_records)
            validation_metadata["fallback"] = {
                "mode": "google_sheets_service_account",
                "result": "validation log written from backend fallback",
            }
        elif appscript_validation.get("status") != "ok":
            store.append_status(batch_id, "failed", "Apps Script validation failed.")
            raise RuntimeError(
                appscript_validation.get("detail")
                or "Apps Script validation failed and no backend fallback is configured."
            )
    else:
        sheets.write_validation_log(batch_id, preview_records)
        validation_metadata = {
            "mode": "google_sheets_service_account",
            "result": "validation log written",
        }

    appscript_preview_metadata: dict = {}
    if appscript.enabled:
        appscript_preview = await appscript.invoke(
            action="get_batch_preview",
            payload={"batch_id": batch_id},
        )
        appscript_preview_metadata = {
            "mode": "appscript",
            "status": appscript_preview.get("status"),
            "detail": appscript_preview.get("detail"),
            "http_status": appscript_preview.get("http_status"),
        }

    if validation_summary.blocked > 0:
        store.append_status(batch_id, "validated_failed", "Validation produced blocked records.")
        final_status = "validated_failed"
    elif validation_summary.warnings > 0:
        store.append_status(batch_id, "validated_warning", "Validation passed with warnings.")
        final_status = "validated_warning"
    else:
        store.append_status(batch_id, "validated_passed", "Validation passed.")
        final_status = "validated_passed"

    store.append_status(batch_id, "preview_ready", "Preview payload ready.")
    batch = store.update_batch(
        batch_id,
        {
            "status": "preview_ready",
            "records": preview_records,
            "generated_counts": generated_counts,
            "validation_summary": validation_summary.model_dump(),
            "planning_summary": planning_summary,
            "metadata": {
                "benchmark": {
                    "enabled": benchmark_mode,
                },
                "validation_phase_status": final_status,
                "dependency_resolution": dependency_resolution,
                "focus_violations": focus_diagnostics.get("mismatches", []),
                "focus_diagnostics": focus_diagnostics,
                "duplicate_candidates": duplicate_candidates,
                "generation_safety": generation_safety,
                "field_inference": field_inference_metadata,
                "form_field_resolution": form_field_resolution_metadata,
                "ambiguity_score": ambiguity_score,
                "ambiguity_threshold": ambiguity_threshold,
                "llm_routes": {
                    "planner": planner_route.__dict__,
                    "clarifier": clarifier_route.__dict__,
                    "generator": generator_route.__dict__,
                },
                "llm_runtime": {
                    "planner": planner_telemetry,
                    "generator": generator_telemetry,
                    "generator_chunks": generator_chunk_telemetry,
                },
                "llm_context": {
                    "planner_profile": planner_context_bundle.get("profile"),
                    "planner_counts": planner_context_bundle.get("counts", {}),
                    "planner_limits": planner_context_bundle.get("limits", {}),
                    "generator_profile": generator_context_bundle.get("profile"),
                    "generator_counts": generator_context_bundle.get("counts", {}),
                    "generator_limits": generator_context_bundle.get("limits", {}),
                },
                "chunking": chunking_metadata,
                "context_notes": request.context_notes or "",
                "recent_batch_context": request.recent_batch_context,
                "staging": staging_metadata,
                "validation": validation_metadata,
                "preview_roundtrip": appscript_preview_metadata,
                "failure": None,
            },
        },
    )

    return ImportAssistantGenerateResponse(
        batch_id=batch_id,
        status="preview_ready",
        planning_summary=batch["planning_summary"],
        generated_counts=generated_counts,
        validation_summary=validation_summary,
        preview_url=f"/api/import-assistant/preview/{batch_id}",
        metadata=batch.get("metadata", {}),
    )


def get_job_status(batch_id: str) -> JobStatusResponse:
    store = get_batch_store()
    batch = store.get_batch(batch_id)
    if not batch:
        raise KeyError(batch_id)
    return JobStatusResponse(**batch)


def list_recent_batches(limit: int = 20) -> JobListResponse:
    store = get_batch_store()
    batches = list(store.list_batches())
    batches.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)

    items: list[JobListItem] = []
    for batch in batches[:limit]:
        prompt = str(batch.get("prompt", "")).strip()
        items.append(
            JobListItem(
                batch_id=str(batch.get("batch_id", "")),
                status=str(batch.get("status", "received")),
                created_at=str(batch.get("created_at", "")),
                updated_at=str(batch.get("updated_at", "")),
                requester=str(batch.get("requester", "")),
                prompt_preview=(prompt[:90] + "...") if len(prompt) > 90 else prompt,
            )
        )
    return JobListResponse(jobs=items)


def get_preview(batch_id: str) -> PreviewResponse:
    store = get_batch_store()
    batch = store.get_batch(batch_id)
    if not batch:
        raise KeyError(batch_id)
    return PreviewResponse(
        batch_id=batch_id,
        status=batch["status"],
        records=[PreviewRecord(**row) for row in batch.get("records", [])],
        planning_summary=batch.get("planning_summary", {}),
        generated_counts=batch.get("generated_counts", {}),
        validation_summary=ValidationSummary(**batch.get("validation_summary", {})),
    )


async def apply_approval(batch_id: str, approved_by: str, decisions: list[dict]) -> ApprovalResponse:
    store = get_batch_store()
    sheets = SheetsService()
    appscript = AppScriptBridgeService()
    batch = store.get_batch(batch_id)
    if not batch:
        raise KeyError(batch_id)

    decision_map = {item["record_id"]: item["import_decision"] for item in decisions}
    updated_records = []
    counts = {"approved": 0, "skipped": 0, "edit_later": 0}
    for record in batch.get("records", []):
        decision = decision_map.get(record["record_id"], record.get("import_decision", "pending_review"))
        if record.get("validation_status") == "failed":
            decision = "blocked"
        record["import_decision"] = decision
        if decision in counts:
            counts[decision] += 1
        updated_records.append(record)

    status = "approved" if counts["approved"] > 0 else "partially_approved"
    store.update_batch(
        batch_id,
        {
            "records": updated_records,
            "approved_by": approved_by,
        },
    )
    store.append_status(batch_id, status, "Record-level approval decisions saved.")
    approval_sync_metadata: dict = {}
    if appscript.enabled:
        appscript_result = await appscript.invoke(
            action="update_approval_status",
            payload={
                "batch_id": batch_id,
                "approved_by": approved_by,
                "records": decisions,
            },
        )
        approval_sync_metadata = {
            "mode": "appscript",
            "status": appscript_result.get("status"),
            "detail": appscript_result.get("detail"),
            "http_status": appscript_result.get("http_status"),
            "result": appscript_result.get("data", {}),
        }
        if appscript_result.get("status") != "ok" and sheets.enabled:
            fallback_rows = sheets.write_approval_log(batch_id, decisions)
            approval_sync_metadata["fallback"] = {
                "mode": "google_sheets_service_account",
                "rows_written": fallback_rows,
            }
        elif appscript_result.get("status") != "ok":
            raise RuntimeError(
                appscript_result.get("detail")
                or "Approval sync failed: Apps Script unavailable and no backend fallback configured."
            )
    else:
        rows_written = sheets.write_approval_log(batch_id, decisions)
        approval_sync_metadata = {
            "mode": "google_sheets_service_account",
            "rows_written": rows_written,
        }

    return ApprovalResponse(
        batch_id=batch_id,
        status=status,  # type: ignore[arg-type]
        summary=ApprovalSummary(**counts),
        message="Approval decisions saved.",
        metadata={
            "approval_sync": approval_sync_metadata,
            "failure": None,
        },
    )


async def deploy_batch_to_zendesk(
    *,
    batch_id: str,
    subdomain: str,
    email: str,
    api_token: str,
    dry_run: bool = False,
    on_existing: str = "create_new",
) -> dict:
    store = get_batch_store()
    appscript = AppScriptBridgeService()
    settings = get_settings()
    batch = store.get_batch(batch_id)
    if not batch:
        raise KeyError(batch_id)

    records = batch.get("records", [])
    approved_records = [
        row for row in records
        if str(row.get("import_decision", "")).strip().lower() == "approved"
    ]
    deployable_approved_records = [
        row for row in approved_records
        if bool(row.get("deployable", False))
    ]
    if approved_records and not deployable_approved_records:
        generation_safety = (
            batch.get("metadata", {}).get("generation_safety", {})
            if isinstance(batch.get("metadata", {}), dict)
            else {}
        )
        safety_reasons = generation_safety.get("reasons", []) if isinstance(generation_safety, dict) else []
        extra_reason = (
            f" Reasons: {' | '.join(safety_reasons)}"
            if isinstance(safety_reasons, list) and safety_reasons
            else ""
        )
        raise ValueError(
            "Deployment blocked: all approved records are non-deployable due to generation safety checks."
            " Regenerate batch output before deploying."
            + extra_reason
        )

    store.append_status(batch_id, "deploying", "Deploying approved records to Zendesk.")

    deployment = await deploy_records_to_zendesk(
        subdomain=subdomain,
        email=email,
        api_token=api_token,
        records=records,
        dry_run=dry_run,
        on_existing=on_existing,
    )
    dependency_auto_create = deployment.get("dependency_auto_create", {}) if isinstance(deployment, dict) else {}
    dependency_events = (
        dependency_auto_create.get("events", [])
        if isinstance(dependency_auto_create, dict)
        else []
    )
    if isinstance(dependency_events, list) and dependency_events:
        for item in dependency_events[:6]:
            if not isinstance(item, dict):
                continue
            event_status = str(item.get("status", "created")).strip()
            object_type = str(item.get("object_type", "dependency")).strip()
            title = str(item.get("title", "untitled")).strip()
            created_id = str(item.get("created_id", "")).strip()
            suffix = f" (id={created_id})" if created_id else ""
            store.append_status(
                batch_id,
                "deploying",
                f"Dependency {event_status}: {object_type.rstrip('s')} '{title}'{suffix}.",
            )
    summary = deployment.get("summary", {})
    results = deployment.get("results", [])

    result_by_record = {item.get("record_id"): item for item in results}
    updated_records = []
    for row in records:
        record_id = row.get("record_id")
        result = result_by_record.get(record_id, {})
        row["deployment_status"] = result.get("deployment_status", row.get("deployment_status", "pending"))
        row["zendesk_object_id"] = result.get("zendesk_object_id")
        row["execution_message"] = result.get("execution_message", "")
        updated_records.append(row)

    execution_log_result: dict = {}
    if appscript.enabled:
        execution_payload = {
            "batch_id": batch_id,
            "results": results,
        }
        bounded_timeout = max(float(settings.appscript_health_timeout_seconds), 0.5) + 1.0
        try:
            execution_log = await asyncio.wait_for(
                appscript.invoke(
                    action="write_execution_log",
                    payload=execution_payload,
                ),
                timeout=bounded_timeout,
            )
            execution_log_result = {
                "mode": "appscript",
                "status": execution_log.get("status"),
                "detail": execution_log.get("detail"),
                "http_status": execution_log.get("http_status"),
                "data": execution_log.get("data", {}),
            }
        except asyncio.TimeoutError:
            execution_log_result = {
                "mode": "appscript",
                "status": "deferred",
                "detail": "Execution log write exceeded bounded timeout; scheduled asynchronous follow-up.",
                "http_status": None,
                "data": {},
            }

            async def _flush_execution_log_later() -> None:
                try:
                    await appscript.invoke(
                        action="write_execution_log",
                        payload=execution_payload,
                        timeout_seconds=settings.appscript_timeout_seconds,
                    )
                except Exception:
                    return

            asyncio.create_task(_flush_execution_log_later())

    deployed = int(summary.get("deployed", 0))
    failed = int(summary.get("failed", 0))
    attempted = int(summary.get("attempted", 0))
    if attempted == 0:
        final_status = "deployed_partial"
        final_message = "No approved deployable records were found for deployment."
    elif deployed == 0 and failed == 0:
        final_status = "deployed_partial"
        final_message = "No objects were created or updated. All approved items were skipped."
    elif failed > 0 and deployed > 0:
        final_status = "deployed_partial"
        final_message = "Deployment completed with partial failures."
    elif failed > 0 and deployed == 0:
        final_status = "deploy_failed"
        final_message = "Deployment failed."
    else:
        final_status = "deployed"
        final_message = "Deployment completed successfully."

    store.update_batch(
        batch_id,
        {
            "records": updated_records,
            "metadata": {
                **batch.get("metadata", {}),
                "zendesk_deploy": {
                    "summary": summary,
                    "results": results,
                    "base_url": deployment.get("base_url"),
                    "execution_log": execution_log_result,
                    "dependency_auto_create": dependency_auto_create,
                },
            },
        },
    )
    store.append_status(batch_id, final_status, final_message)

    return {
        "batch_id": batch_id,
        "status": final_status,
        "summary": summary,
        "results": results,
        "message": final_message,
        "metadata": {
            "base_url": deployment.get("base_url"),
            "execution_log": execution_log_result,
            "dry_run": dry_run,
            "on_existing": on_existing,
            "dependency_auto_create": dependency_auto_create,
            "failure": (
                {
                    "failure_stage": "deploy",
                    "failure_code": "deploy_failed",
                    "failure_reason": final_message,
                    "next_step": "Review failed rows in execution details and resolve those issues before redeploying.",
                }
                if final_status in {"deploy_failed", "deployed_partial"} and failed > 0
                else None
            ),
        },
    }
