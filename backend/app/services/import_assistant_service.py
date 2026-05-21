import asyncio
from collections import Counter
from datetime import UTC, datetime
import math
import random
import re
import time
from uuid import uuid4

from app.api.grok.client import GrokClient, LLMRequestError
from app.api.grok.routing import resolve_model_route
from app.core.settings import get_settings
from app.helpers.json_parser import extract_json_payload
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
RULE_FIELD_ALIASES = {
    "assign": "group_id",
    "group": "group_id",
    "group_name": "group_id",
    "team": "group_id",
    "team_id": "group_id",
    "form": "ticket_form_id",
    "form_id": "ticket_form_id",
    "ticket_form": "ticket_form_id",
    "ticket_form_name": "ticket_form_id",
    "brand": "brand_id",
    "brand_name": "brand_id",
    "tag": "set_tags",
    "tags": "set_tags",
    "add_tag": "set_tags",
    "set_tag": "set_tags",
    "add_note": "comment_value",
    "comment": "comment_value",
    "comment_body": "comment_value",
    "comment_text": "comment_value",
}
ARTICLE_FIELD_ALIASES = {
    "section_id": {"section_id", "section", "section_name"},
    "category_id": {"category_id", "category", "category_name"},
    "help_center_id": {"help_center_id", "help_center", "help_center_name"},
    "locale": {"locale", "language"},
    "body": {"body", "content", "article_body", "html_body"},
    "draft": {"draft", "is_draft"},
    "published": {"published", "is_published"},
}
DETERMINISTIC_LLM_ERROR_MARKERS = {
    "unsupported_response_format",
    "schema_validation_failure",
    "model_permission_blocked",
    "other_invalid_request",
}

BUSINESS_BLUEPRINT_OBJECT_HINTS = {
    "groups": [r"\bgroup\b", r"\bgroups\b", r"\bteam\b", r"\bteams\b"],
    "ticket_fields": [r"\bfield\b", r"\bfields\b", r"\bcustom field\b", r"\bcustom fields\b"],
    "ticket_forms": [r"\bform\b", r"\bforms\b", r"\bticket form\b", r"\bticket forms\b"],
    "views": [r"\bview\b", r"\bviews\b", r"\bqueue\b", r"\bqueues\b"],
    "triggers": [r"\btrigger\b", r"\btriggers\b", r"\brule\b", r"\brules\b"],
    "macros": [r"\bmacro\b", r"\bmacros\b"],
    "automations": [r"\bautomation\b", r"\bautomations\b"],
    "articles": [r"\barticle\b", r"\barticles\b", r"\bhelp center\b", r"\bknowledge base\b"],
}

ORCHESTRATION_WAVES = {
    1: ["groups", "ticket_fields"],
    2: ["ticket_forms", "views"],
    3: ["triggers", "macros", "automations"],
    4: ["articles"],
}

ORCHESTRATION_WAVE_BY_OBJECT = {
    object_type: wave
    for wave, object_types in ORCHESTRATION_WAVES.items()
    for object_type in object_types
}
WAVE3_RULE_OBJECT_TYPES = {"triggers", "macros", "automations"}
OBJECT_DETERMINISTIC_FAILOVER_THRESHOLD = 3

BUSINESS_BRIEF_SIGNAL_PATTERNS = (
    r"\bbusiness\b",
    r"\binstance\b",
    r"\bend[- ]to[- ]end\b",
    r"\bfull setup\b",
    r"\bcomplete setup\b",
    r"\bwhole (?:system|workspace|instance)\b",
    r"\bfor (?:a|an|the) (?:business|company|organization)\b",
)
NUMBER_WORD_VALUES = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}


class GenerateFailureError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        reason: str,
        next_step: str,
        stage: str = "generate",
    ) -> None:
        super().__init__(reason)
        self.stage = stage
        self.code = code
        self.reason = reason
        self.next_step = next_step


def _is_rate_limited_error(exc: Exception) -> bool:
    text = str(exc).strip().lower()
    if not text:
        return False
    return (
        "[rate_limited]" in text
        or "rate_limit" in text
        or "too many requests" in text
        or "429" in text
    )


def _extract_terminal_error_class(
    *,
    exc: Exception,
    runtime_metrics: dict | None = None,
    generator_error_metadata: dict | None = None,
) -> str:
    runtime_metrics = runtime_metrics if isinstance(runtime_metrics, dict) else {}
    generator_error_metadata = (
        generator_error_metadata if isinstance(generator_error_metadata, dict) else {}
    )
    error_class = str(generator_error_metadata.get("error_class", "")).strip().lower()
    if error_class:
        return error_class
    if isinstance(exc, GeneratorStructuredOutputError):
        error_class = str(runtime_metrics.get("error_class", "")).strip().lower()
        if error_class:
            return error_class
    text = str(exc).strip().lower()
    for marker in (
        "rate_limited",
        "schema_validation_failure",
        "unsupported_response_format",
        "model_permission_blocked",
        "other_invalid_request",
    ):
        if f"[{marker}]" in text or marker in text:
            return marker
    return "unknown"


def _infer_object_type_from_prompt(
    *,
    prompt: str,
    focus_object_types: list[str],
) -> str:
    if len(focus_object_types) == 1:
        return focus_object_types[0]
    lowered = str(prompt or "").strip().lower()
    if not lowered:
        return focus_object_types[0] if focus_object_types else "triggers"

    scored: dict[str, int] = {}

    def _score(object_type: str, patterns: list[str]) -> None:
        for pattern in patterns:
            if re.search(pattern, lowered):
                scored[object_type] = scored.get(object_type, 0) + 1

    _score("ticket_fields", [r"\bticket field\b", r"\bcustom field\b", r"\bdrop[\s-]?down\b", r"\bmulti[\s-]?select\b"])
    _score("ticket_forms", [r"\bticket form\b", r"\brequest form\b"])
    _score("automations", [r"\bautomation\b", r"\bautomate\b"])
    _score("triggers", [r"\btrigger\b"])
    _score("macros", [r"\bmacro\b"])
    _score("views", [r"\bview\b"])
    _score("groups", [r"\bgroup\b"])
    _score("articles", [r"\barticle\b", r"\bhelp center\b", r"\bknowledge base\b"])
    if "ticket_forms" not in scored and re.search(r"\bform\b", lowered):
        scored["ticket_forms"] = scored.get("ticket_forms", 0) + 1
    if "ticket_fields" not in scored and re.search(r"\bfield\b", lowered):
        scored["ticket_fields"] = scored.get("ticket_fields", 0) + 1

    candidate_types = focus_object_types or [
        "triggers",
        "automations",
        "macros",
        "views",
        "groups",
        "ticket_forms",
        "ticket_fields",
        "articles",
    ]
    best_type = candidate_types[0] if candidate_types else "triggers"
    best_score = -1
    for object_type in candidate_types:
        score = int(scored.get(object_type, 0))
        if score > best_score:
            best_score = score
            best_type = object_type
    return best_type


def _build_deterministic_planner_plan(
    *,
    prompt: str,
    dependency_mode: str,
    focus_object_types: list[str],
    estimated_count: int,
) -> dict:
    object_type = _infer_object_type_from_prompt(
        prompt=prompt,
        focus_object_types=focus_object_types,
    )
    return {
        "object_type": object_type,
        "intent": str(prompt).strip(),
        "confidence": 0.84,
        "ambiguity_score": 0.18,
        "ambiguity_reasons": [],
        "clarification_questions": [],
        "dependency_notes": "Deterministic planner bypass applied for explicit single-item request.",
        "llm": {
            "task": "planner",
            "model": "deterministic_inference",
            "strict_schema": False,
            "telemetry": {
                "bypassed": True,
                "bypass_reason": "single_item_explicit_prompt",
                "dependency_mode": dependency_mode,
                "estimated_requested_records": int(estimated_count),
            },
        },
    }


def _generator_mode_order(
    *,
    compatibility_first: bool,
    compatibility_only: bool = False,
) -> list[str]:
    if compatibility_only:
        return [
            "json_object",
            "no_response_format",
        ]
    if compatibility_first:
        return [
            "json_object",
            "no_response_format",
            "json_schema_best_effort",
            "json_schema_strict",
        ]
    return [
        "json_schema_strict",
        "json_schema_best_effort",
        "json_object",
        "no_response_format",
    ]


def _build_llm_routes_metadata(
    *,
    planner_route,
    clarifier_route,
    generator_route,
    settings,
) -> dict:
    return {
        "planner": planner_route.__dict__,
        "clarifier": clarifier_route.__dict__,
        "generator": generator_route.__dict__,
        "generator_wave3": str(settings.llm_model_generator_wave3 or "").strip() or None,
        "generator_wave4": str(settings.llm_model_generator_wave4 or "").strip() or None,
        "generator_secondary": str(getattr(settings, "llm_model_generator_secondary", "") or "").strip() or None,
        "generator_tertiary": str(getattr(settings, "llm_model_generator_tertiary", "") or "").strip() or None,
        "generator_wave3_api_key_configured": bool(
            str(getattr(settings, "xai_api_key_wave3", "") or "").strip()
        ),
        "generator_wave4_api_key_configured": bool(
            str(getattr(settings, "xai_api_key_wave4", "") or "").strip()
        ),
        "generator_secondary_api_key_configured": bool(
            str(getattr(settings, "xai_api_key_secondary", "") or "").strip()
        ),
        "generator_tertiary_api_key_configured": bool(
            str(getattr(settings, "xai_api_key_tertiary", "") or "").strip()
        ),
    }


def _resolve_wave_generator_model(
    *,
    settings,
    wave: int,
) -> str:
    primary_model = str(settings.llm_model_generator or "").strip()
    if not primary_model:
        return ""
    wave3_model = str(
        getattr(settings, "llm_model_generator_secondary", "")
        or settings.llm_model_generator_wave3
        or ""
    ).strip()
    wave4_model = str(
        getattr(settings, "llm_model_generator_tertiary", "")
        or settings.llm_model_generator_wave4
        or ""
    ).strip()
    if wave >= 4 and wave4_model:
        return wave4_model
    if wave >= 3 and wave3_model:
        return wave3_model
    return primary_model


def _resolve_wave_api_key(
    *,
    settings,
    wave: int,
) -> str:
    primary_key = str(getattr(settings, "xai_api_key", "") or "").strip()
    wave3_key = str(
        getattr(settings, "xai_api_key_secondary", "")
        or getattr(settings, "xai_api_key_wave3", "")
        or ""
    ).strip()
    wave4_key = str(
        getattr(settings, "xai_api_key_tertiary", "")
        or getattr(settings, "xai_api_key_wave4", "")
        or ""
    ).strip()
    if wave >= 4 and wave4_key:
        return wave4_key
    if wave >= 3 and wave3_key:
        return wave3_key
    return primary_key


def _build_wave_generator_routes(
    *,
    settings,
    wave: int,
) -> list[dict]:
    primary_model = str(settings.llm_model_generator or "").strip()
    primary_key = str(getattr(settings, "xai_api_key", "") or "").strip()
    secondary_model = str(
        getattr(settings, "llm_model_generator_secondary", "")
        or getattr(settings, "llm_model_generator_wave3", "")
        or primary_model
    ).strip()
    tertiary_model = str(
        getattr(settings, "llm_model_generator_tertiary", "")
        or getattr(settings, "llm_model_generator_wave4", "")
        or primary_model
    ).strip()
    secondary_key = str(
        getattr(settings, "xai_api_key_secondary", "")
        or getattr(settings, "xai_api_key_wave3", "")
        or ""
    ).strip()
    tertiary_key = str(
        getattr(settings, "xai_api_key_tertiary", "")
        or getattr(settings, "xai_api_key_wave4", "")
        or ""
    ).strip()

    if wave >= 4:
        ordered = [
            ("tertiary", tertiary_model, tertiary_key),
            ("secondary", secondary_model, secondary_key),
            ("primary", primary_model, primary_key),
        ]
    elif wave >= 3:
        ordered = [
            ("secondary", secondary_model, secondary_key),
            ("tertiary", tertiary_model, tertiary_key),
            ("primary", primary_model, primary_key),
        ]
    else:
        ordered = [
            ("primary", primary_model, primary_key),
            ("secondary", secondary_model, secondary_key),
            ("tertiary", tertiary_model, tertiary_key),
        ]

    routes: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for profile, model, api_key in ordered:
        model_clean = str(model or "").strip()
        api_key_clean = str(api_key or "").strip()
        if not model_clean or not api_key_clean:
            continue
        identity = (model_clean, api_key_clean)
        if identity in seen:
            continue
        seen.add(identity)
        routes.append(
            {
                "profile": profile,
                "model": model_clean,
                "api_key": api_key_clean,
            }
        )
    return routes


def _should_retry_chunk_on_primary_model(exc: Exception) -> bool:
    terminal_error = _extract_terminal_error_class(exc=exc)
    return terminal_error in {
        "rate_limited",
        "model_permission_blocked",
        "unsupported_response_format",
    }


def _force_compatibility_payload_shape(object_type: str) -> bool:
    normalized = _normalize_object_type(str(object_type or ""))
    return normalized in {"ticket_fields", "ticket_forms", "views"}


def _is_wave3_rule_object_type(object_type: str) -> bool:
    return _normalize_object_type(str(object_type or "")) in WAVE3_RULE_OBJECT_TYPES


def _resolve_object_chunk_profile(
    *,
    settings,
    object_type: str | None,
) -> tuple[int, int]:
    base_chunk_size = max(int(settings.llm_auto_chunk_size), 1)
    base_trigger_min_records = max(int(settings.llm_auto_chunk_trigger_min_records), 2)
    if not str(object_type or "").strip():
        return base_chunk_size, base_trigger_min_records
    normalized = _normalize_object_type(str(object_type or ""))

    # Quality-first micro-chunking profile for fragile object types.
    if normalized == "ticket_fields":
        return max(1, min(base_chunk_size, 2)), 2
    if normalized in {"ticket_forms", "views", "articles"}:
        return 1, 1
    if normalized in {"triggers", "macros", "automations"}:
        return max(1, min(base_chunk_size, 3)), min(base_trigger_min_records, 3)

    return base_chunk_size, base_trigger_min_records


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
    normalized = str(raw or "").strip().lower()
    if not normalized:
        return "triggers"
    direct_match = TAB_OBJECT_TYPES.get(normalized)
    if direct_match:
        return direct_match

    normalized_key = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    alias_map = {
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
        "field": "ticket_fields",
        "fields": "ticket_fields",
        "custom_field": "ticket_fields",
        "custom_fields": "ticket_fields",
        "ticket_form": "ticket_forms",
        "ticket_forms": "ticket_forms",
        "form": "ticket_forms",
        "forms": "ticket_forms",
        "article": "articles",
        "articles": "articles",
        "help_center_article": "articles",
        "help_center_articles": "articles",
    }
    return alias_map.get(normalized_key, "triggers")


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


def _normalize_title_for_constraint_match(title: object) -> str:
    normalized = str(title or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _count_enumerated_items(prompt: str) -> int:
    if not prompt:
        return 0
    text = str(prompt)
    lines = text.splitlines()
    line_enumerated_pattern = re.compile(r"^\s*(?:\d+[\).\]]|[-*\u2022])\s+\S+")
    line_matches = [line for line in lines if line_enumerated_pattern.match(line or "")]

    inline_numbered_pattern = re.compile(
        r"(?:(?<=^)|(?<=[\s;:,]))(\d{1,2})\s*[\).:]\s+(?=[a-z])",
        flags=re.IGNORECASE,
    )
    inline_matches = inline_numbered_pattern.findall(text)

    return max(len(line_matches), len(inline_matches))


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

    number_word_pattern = "|".join(
        sorted(NUMBER_WORD_VALUES.keys(), key=lambda item: len(item), reverse=True)
    )
    number_word_numeric_pattern = re.compile(
        rf"\b({number_word_pattern})\s+"
        r"(?:(?:[a-z][\w/-]{0,30})\s+){0,3}?"
        r"("
        r"triggers?|automations?|macros?|views?|groups?|"
        r"ticket\s*forms?|forms?|ticket\s*fields?|fields?|articles?"
        r")\b",
        flags=re.IGNORECASE,
    )
    for match in number_word_numeric_pattern.finditer(text):
        number_word = str(match.group(1) or "").strip().lower()
        value = int(NUMBER_WORD_VALUES.get(number_word, 0) or 0)
        if value <= 0:
            continue
        object_hint = _normalize_object_type(match.group(2))
        numeric_matches.append(
            {
                "value": value,
                "object_hint": object_hint,
                "raw": match.group(0),
                "source": "number_word_intent",
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
            if any(
                str(item.get("source", "")).strip().lower() == "number_word_intent"
                for item in numeric_matches
                if isinstance(item, dict)
            ):
                sources.append("number_word_intent")
        if enumerated_items > 0:
            sources.append("enumerated_items")

    return {
        "estimated_count": max(estimated_count, 1),
        "sources": sources,
        "numeric_matches": numeric_matches,
        "enumerated_items": enumerated_items,
    }


def _extract_object_type_targets(
    *,
    prompt: str,
    focus_object_types: list[str],
    estimated_count: int,
    chunk_estimate: dict | None = None,
) -> dict[str, int]:
    text = str(prompt or "").strip()
    lowered = text.lower()
    targets: dict[str, int] = {}

    estimate = chunk_estimate or _estimate_requested_record_count(text)
    numeric_matches = estimate.get("numeric_matches", [])
    if isinstance(numeric_matches, list):
        for item in numeric_matches:
            if not isinstance(item, dict):
                continue
            object_hint = _normalize_object_type(str(item.get("object_hint", "")))
            value = int(item.get("value", 0) or 0)
            if value <= 0:
                continue
            targets[object_hint] = targets.get(object_hint, 0) + value

    direct_numeric_patterns = {
        "triggers": [r"\b(\d{1,4})\s+triggers?\b"],
        "automations": [r"\b(\d{1,4})\s+automations?\b"],
        "macros": [r"\b(\d{1,4})\s+macros?\b"],
        "views": [r"\b(\d{1,4})\s+views?\b", r"\b(\d{1,4})\s+queues?\b"],
        "groups": [r"\b(\d{1,4})\s+groups?\b", r"\b(\d{1,4})\s+teams?\b"],
        "ticket_forms": [r"\b(\d{1,4})\s+ticket\s*forms?\b", r"\b(\d{1,4})\s+forms?\b"],
        "ticket_fields": [r"\b(\d{1,4})\s+ticket\s*fields?\b", r"\b(\d{1,4})\s+fields?\b"],
        "articles": [r"\b(\d{1,4})\s+articles?\b"],
    }
    for object_type, patterns in direct_numeric_patterns.items():
        if object_type in targets and targets[object_type] > 0:
            continue
        extracted_value = 0
        for pattern in patterns:
            for match in re.finditer(pattern, lowered):
                try:
                    extracted_value = max(extracted_value, int(match.group(1) or 0))
                except Exception:
                    continue
        if extracted_value > 0:
            targets[object_type] = extracted_value

    for object_type, patterns in BUSINESS_BLUEPRINT_OBJECT_HINTS.items():
        if object_type in targets:
            continue
        if any(re.search(pattern, lowered) for pattern in patterns):
            targets[object_type] = 1

    if not targets and focus_object_types:
        for item in focus_object_types:
            canonical = _normalize_object_type(item)
            targets[canonical] = max(targets.get(canonical, 0), 1)

    if not targets:
        inferred = _infer_object_type_from_prompt(
            prompt=text,
            focus_object_types=focus_object_types,
        )
        targets[inferred] = max(int(estimated_count or 1), 1)

    if len(targets) == 1:
        only_key = next(iter(targets))
        targets[only_key] = max(targets[only_key], int(estimated_count or 1), 1)

    return {key: max(int(value), 1) for key, value in targets.items()}


def _is_business_blueprint_prompt(
    *,
    prompt: str,
    focus_object_types: list[str],
    estimated_count: int,
    prompt_explicit: bool,
    object_targets: dict[str, int],
) -> tuple[bool, str]:
    if prompt_explicit and estimated_count <= 1 and len(object_targets) <= 1:
        return False, "explicit_single_item"

    lowered = str(prompt or "").strip().lower()
    has_business_signal = any(
        re.search(pattern, lowered)
        for pattern in BUSINESS_BRIEF_SIGNAL_PATTERNS
    )
    multi_object = len([key for key, value in object_targets.items() if value > 0]) >= 2
    focus_multi = len(focus_object_types) >= 2
    explicit_bulk = estimated_count >= 7 and multi_object

    if multi_object:
        return True, "multi_object_intent"
    if explicit_bulk:
        return True, "bulk_multi_object_intent"
    if has_business_signal and (focus_multi or estimated_count > 1):
        return True, "business_brief_signal"
    return False, "standard_record_prompt"


def _should_force_wave_chunk_path(
    *,
    prompt: str,
    estimated_count: int,
    object_targets: dict[str, int],
    chunk_estimate: dict | None = None,
) -> tuple[bool, str]:
    lowered = str(prompt or "").strip().lower()
    estimate = chunk_estimate if isinstance(chunk_estimate, dict) else {}
    enumerated_items = int(estimate.get("enumerated_items", 0) or 0)
    numeric_matches = list(estimate.get("numeric_matches", []) or [])
    number_word_matches = sum(
        1
        for item in numeric_matches
        if isinstance(item, dict)
        and str(item.get("source", "")).strip().lower() == "number_word_intent"
    )
    target_total = sum(max(int(value or 0), 0) for value in object_targets.values())
    object_type_count = len([key for key, value in object_targets.items() if int(value or 0) > 0])
    has_create_following_signal = bool(
        re.search(
            r"\b(?:create|build|configure|set\s*up|setup)\b[\s\S]{0,80}\bfollowing\b",
            lowered,
            flags=re.IGNORECASE,
        )
    )
    has_large_list_signal = bool(
        re.search(
            r"\b(?:create|build|configure|set\s*up|setup)\b[\s\S]{0,80}\b(?:the\s+)?(?:below|list)\b",
            lowered,
            flags=re.IGNORECASE,
        )
    )

    if has_create_following_signal and (enumerated_items >= 2 or object_type_count >= 2):
        return True, "create_following_numbered_prompt"
    if has_large_list_signal and (enumerated_items >= 2 or target_total >= 3):
        return True, "explicit_list_prompt"
    if object_type_count >= 2 and target_total >= 3:
        return True, "multi_object_target_prompt"
    if estimated_count >= 3 and (enumerated_items >= 2 or number_word_matches >= 2):
        return True, "numbered_multi_item_prompt"
    return False, "standard_prompt"


def _resolve_wave_for_object_type(object_type: str) -> int:
    return int(ORCHESTRATION_WAVE_BY_OBJECT.get(object_type, 3))


def _normalize_blueprint_target_objects(
    raw_targets: object,
    *,
    fallback_targets: dict[str, int],
) -> list[dict]:
    normalized: list[dict] = []
    seen: set[str] = set()

    if isinstance(raw_targets, dict):
        iterable = [
            {
                "object_type": key,
                "target_count": value,
            }
            for key, value in raw_targets.items()
        ]
    elif isinstance(raw_targets, list):
        iterable = raw_targets
    else:
        iterable = []

    for item in iterable:
        if not isinstance(item, dict):
            continue
        object_type = _normalize_object_type(str(item.get("object_type", "")))
        if object_type in seen:
            continue
        target_count = int(item.get("target_count", 0) or 0)
        if target_count <= 0:
            target_count = int(fallback_targets.get(object_type, 1) or 1)
        if target_count <= 0:
            continue
        wave = _resolve_wave_for_object_type(object_type)
        priority = int(item.get("priority", wave) or wave)
        normalized.append(
            {
                "object_type": object_type,
                "target_count": target_count,
                "priority": priority,
                "wave": wave,
                "source": "compiler",
            }
        )
        seen.add(object_type)

    if not normalized:
        for object_type, target_count in fallback_targets.items():
            normalized.append(
                {
                    "object_type": object_type,
                    "target_count": max(int(target_count or 1), 1),
                    "priority": _resolve_wave_for_object_type(object_type),
                    "wave": _resolve_wave_for_object_type(object_type),
                    "source": "fallback",
                }
            )
    return sorted(
        normalized,
        key=lambda item: (int(item.get("wave", 9)), int(item.get("priority", 9)), item.get("object_type", "")),
    )


async def _run_business_blueprint_compiler(
    *,
    prompt: str,
    dependency_mode: str,
    focus_object_types: list[str],
    object_targets: dict[str, int],
    planner_route,
) -> tuple[dict, dict]:
    fallback_blueprint = {
        "mode": "deterministic_fallback",
        "capabilities": ["inference_first", "match_existing_or_create"],
        "assumptions": [
            "No clarification loop is used; missing details are inferred from prompt and catalog context.",
        ],
        "priorities": ["deterministic_wave_order", "reuse_before_create", "fail_whole_run"],
        "dependency_hints": [],
        "target_objects": [
            {
                "object_type": key,
                "target_count": value,
                "priority": _resolve_wave_for_object_type(key),
                "wave": _resolve_wave_for_object_type(key),
            }
            for key, value in object_targets.items()
        ],
    }
    if not str(prompt or "").strip():
        return fallback_blueprint, {"bypassed": True, "reason": "empty_prompt"}

    client = GrokClient()
    user_payload = {
        "prompt": prompt,
        "dependency_mode": dependency_mode,
        "focus_object_types": focus_object_types,
        "target_object_hints": object_targets,
        "wave_order": ORCHESTRATION_WAVES,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You compile Zendesk business briefs into executable blueprint JSON. "
                "Return strict JSON object only with keys: capabilities, target_objects, assumptions, "
                "priorities, dependency_hints. "
                "target_objects must be array of {object_type,target_count,priority,wave}. "
                "object_type values must be one of: groups,ticket_fields,ticket_forms,views,triggers,macros,automations,articles. "
                "Respect this deterministic wave order: wave1 groups+ticket_fields, wave2 ticket_forms+views, "
                "wave3 triggers+macros+automations, wave4 articles."
            ),
        },
        {
            "role": "user",
            "content": str(user_payload),
        },
    ]

    try:
        raw = await client.chat(
            messages,
            temperature=0.0,
            model=planner_route.model,
            max_output_tokens=max(280, int(planner_route.max_output_tokens or 300)),
            response_schema=None,
            strict_schema=False,
            task="planner",
            response_format_override="json_object",
        )
        payload = extract_json_payload(raw)
        if not isinstance(payload, dict):
            raise ValueError("Business compiler response must be a JSON object.")
        target_objects = _normalize_blueprint_target_objects(
            payload.get("target_objects"),
            fallback_targets=object_targets,
        )
        blueprint = {
            "mode": "compiler",
            "capabilities": [
                str(item).strip()
                for item in list(payload.get("capabilities", []) or [])
                if str(item).strip()
            ][:10],
            "assumptions": [
                str(item).strip()
                for item in list(payload.get("assumptions", []) or [])
                if str(item).strip()
            ][:12],
            "priorities": [
                str(item).strip()
                for item in list(payload.get("priorities", []) or [])
                if str(item).strip()
            ][:12],
            "dependency_hints": [
                str(item).strip()
                for item in list(payload.get("dependency_hints", []) or [])
                if str(item).strip()
            ][:12],
            "target_objects": target_objects,
        }
        telemetry = GrokClient.get_last_call_metrics("planner")
        return blueprint, telemetry
    except LLMRequestError as exc:
        if str(exc.error_class or "").strip().lower() == "rate_limited":
            raise
    except Exception:
        pass

    return fallback_blueprint, GrokClient.get_last_call_metrics("planner")


def _build_orchestration_backlog(
    *,
    blueprint: dict,
    prompt: str,
    dependency_mode: str,
    focus_object_types: list[str],
) -> list[dict]:
    normalized_targets = _normalize_blueprint_target_objects(
        blueprint.get("target_objects"),
        fallback_targets=_extract_object_type_targets(
            prompt=prompt,
            focus_object_types=focus_object_types,
            estimated_count=max(
                1,
                sum(
                    int(item.get("target_count", 0) or 0)
                    for item in list(blueprint.get("target_objects", []) or [])
                    if isinstance(item, dict)
                ),
            ),
            chunk_estimate=None,
        ),
    )
    allowed_focus = set(focus_object_types)
    backlog: list[dict] = []
    for index, item in enumerate(normalized_targets, start=1):
        object_type = _normalize_object_type(str(item.get("object_type", "")))
        if allowed_focus and object_type not in allowed_focus:
            continue
        target_count = max(int(item.get("target_count", 1) or 1), 1)
        backlog.append(
            {
                "backlog_id": f"BL-{index:03d}",
                "object_type": object_type,
                "target_count": target_count,
                "wave": _resolve_wave_for_object_type(object_type),
                "priority": int(item.get("priority", _resolve_wave_for_object_type(object_type)) or _resolve_wave_for_object_type(object_type)),
                "source": str(item.get("source", "compiler")).strip() or "compiler",
                "dependency_mode": dependency_mode,
            }
        )

    return sorted(
        backlog,
        key=lambda row: (int(row.get("wave", 9)), int(row.get("priority", 9)), str(row.get("backlog_id", ""))),
    )


def _reconcile_backlog_item(
    *,
    item: dict,
    prompt: str,
    dependency_mode: str,
    existing_index: dict[str, dict[str, dict]],
) -> dict:
    object_type = _normalize_object_type(str(item.get("object_type", "triggers")))
    base_match = _match_existing_base_object(
        prompt=prompt,
        object_type=object_type,
        existing_index=existing_index,
    )
    update_intent = bool(re.search(r"\b(update|modify|change|edit|adjust|overwrite|replace)\b", str(prompt or "").lower()))
    if dependency_mode == "force_create_new":
        return {
            "mode": "create",
            "match_score": 0.0,
            "match_source": "forced",
            "base_object": None,
        }
    if dependency_mode == "force_existing_only":
        if not base_match:
            return {
                "mode": "blocked",
                "match_score": 0.0,
                "match_source": "none",
                "base_object": None,
                "blocked_reason": (
                    f"Existing-only mode is active, but no matching {object_type.rstrip('s')} "
                    "was found in the reference catalog."
                ),
            }
        return {
            "mode": "update" if update_intent else "reuse",
            "match_score": 1.0 if base_match.get("match_type") == "exact_title" else 0.9,
            "match_source": str(base_match.get("match_type", "existing_match")),
            "base_object": base_match,
        }
    if not base_match:
        return {
            "mode": "create",
            "match_score": 0.0,
            "match_source": "none",
            "base_object": None,
        }
    match_type = str(base_match.get("match_type", "")).strip().lower()
    score = 0.78
    if match_type == "exact_title":
        score = 1.0
    elif match_type == "normalized_title":
        score = 0.94
    return {
        "mode": "update" if update_intent else "reuse",
        "match_score": score,
        "match_source": match_type or "existing_match",
        "base_object": base_match,
    }


def _build_wave_prompt(
    *,
    prompt: str,
    item: dict,
    reconciliation: dict,
) -> str:
    object_type = _normalize_object_type(str(item.get("object_type", "triggers")))
    target_count = max(int(item.get("target_count", 1) or 1), 1)
    mode = str(reconciliation.get("mode", "create")).strip().lower()
    base = reconciliation.get("base_object") if isinstance(reconciliation, dict) else None
    base_suffix = ""
    if isinstance(base, dict):
        base_name = str(base.get("name", "")).strip()
        base_id = str(base.get("id", "")).strip()
        if base_name:
            base_suffix = f" Use existing {object_type.rstrip('s')} '{base_name}'"
            if base_id:
                base_suffix += f" (id={base_id})"
            base_suffix += " as baseline context."

    mode_instruction = {
        "reuse": "Prefer matching existing configuration and extend safely.",
        "update": "Update existing configuration in-place using best match.",
        "create": "Create new configuration when no safe match exists.",
    }.get(mode, "Create or match existing configuration as needed.")

    return (
        f"Business brief: {prompt}\n"
        f"Wave object type: {object_type}\n"
        f"Target records for this wave item: {target_count}\n"
        f"Execution mode: {mode}. {mode_instruction}{base_suffix}"
    )


def _build_chunk_plan(
    *,
    settings,
    estimated_count: int,
    object_type: str | None = None,
) -> dict:
    chunk_size, trigger_min_records = _resolve_object_chunk_profile(
        settings=settings,
        object_type=object_type,
    )
    max_chunks = max(int(settings.llm_auto_chunk_max_chunks), 1)
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


def _can_use_deterministic_chunk_fallback(object_type: str) -> bool:
    return _normalize_object_type(object_type) in {
        "groups",
        "ticket_fields",
        "ticket_forms",
        "views",
        "triggers",
        "macros",
        "automations",
        "articles",
    }


def _build_deterministic_chunk_rows(
    *,
    object_type: str,
    target_count: int,
    prompt: str,
    reference_catalog: dict[str, list[dict]],
    existing_titles: list[str] | None,
    generated_rows: list[dict] | None,
    reason: str,
) -> list[dict]:
    normalized_object_type = _normalize_object_type(object_type)
    requested_count = max(int(target_count or 1), 1)
    prompt_text = str(prompt or "").strip()
    existing_title_keys = {
        _normalize_title_for_dedupe(title)
        for title in (existing_titles or [])
        if str(title).strip()
    }

    def _next_unique_title(base: str, position: int) -> str:
        candidate = str(base or "").strip() or f"Generated {normalized_object_type.rstrip('s').title()}"
        key = _normalize_title_for_dedupe(candidate)
        if key and key not in existing_title_keys:
            existing_title_keys.add(key)
            return candidate
        suffix = max(2, int(position or 2))
        while True:
            deduped = f"{candidate} {suffix}"
            dedupe_key = _normalize_title_for_dedupe(deduped)
            if dedupe_key not in existing_title_keys:
                existing_title_keys.add(dedupe_key)
                return deduped
            suffix += 1

    rows: list[dict] = []
    fallback_note = (
        f"Deterministic {normalized_object_type} fallback used because model output could not be parsed. "
        f"Source error: {reason}"
    )

    if normalized_object_type == "ticket_fields":
        field_specs = _extract_ticket_field_specs_from_prompt(prompt_text)
        used_titles = {
            _normalize_title_for_dedupe(str(row.get("title", "")).strip())
            for row in (generated_rows or [])
            if isinstance(row, dict) and str(row.get("title", "")).strip()
        }
        available_specs = [
            spec
            for spec in field_specs
            if _normalize_title_for_dedupe(str(spec.get("title", "")).strip()) not in used_titles
        ]
        if not available_specs:
            requested_type = _infer_requested_ticket_field_type(prompt_text) or "text"
            option_hints = _extract_ticket_field_option_hints(prompt_text)
            if requested_type in {"tagger", "multiselect"} and len(option_hints) < 2:
                option_hints = ["Option A", "Option B"]
            available_specs = [
                {
                    "title": "Generated Field",
                    "field_type": requested_type,
                    "options": option_hints,
                }
            ]

        for index in range(1, requested_count + 1):
            spec = available_specs[(index - 1) % len(available_specs)]
            spec_title = str(spec.get("title", "")).strip() or "Generated Field"
            requested_type = _normalize_ticket_field_type(spec.get("field_type") or "text")
            option_hints = [
                str(item).strip()
                for item in list(spec.get("options", []) or [])
                if str(item).strip()
            ]
            if requested_type in {"tagger", "multiselect"} and len(option_hints) < 2:
                option_hints = ["Option A", "Option B"]
            option_values = [
                {"name": opt[:80], "value": _slugify_option_value(opt)}
                for opt in option_hints[:20]
            ]
            actions = [{"field": "field_type", "value": requested_type}]
            if option_values and requested_type in {"tagger", "multiselect"}:
                actions.append({"field": "custom_field_options", "value": option_values})
            rows.append(
                {
                    "object_type": "ticket_fields",
                    "title": _next_unique_title(spec_title, index),
                    "conditions": [],
                    "actions": actions,
                    "dependency_notes": [fallback_note],
                }
            )
        return rows

    if normalized_object_type == "groups":
        title_hints = _extract_title_hints(prompt_text)
        base_title = title_hints[0] if title_hints else "Generated Support Group"
        description_match = re.search(
            r"\b(?:for|supports?)\s+([a-z0-9][a-z0-9 &/_-]{2,120})",
            prompt_text,
            flags=re.IGNORECASE,
        )
        description_hint = str(description_match.group(1)).strip() if description_match else ""
        for index in range(1, requested_count + 1):
            actions = []
            if description_hint:
                actions.append({"field": "description", "value": f"Support group for {description_hint}."})
            rows.append(
                {
                    "object_type": "groups",
                    "title": _next_unique_title(base_title, index),
                    "conditions": [],
                    "actions": actions,
                    "dependency_notes": [fallback_note],
                }
            )
        return rows

    if normalized_object_type == "ticket_forms":
        referenced_fields = _extract_ticket_form_field_hints_from_prompt(
            prompt=prompt_text,
            reference_catalog=reference_catalog,
        )
        if not referenced_fields:
            for row in generated_rows or []:
                if not isinstance(row, dict):
                    continue
                if _normalize_object_type(str(row.get("object_type", ""))) != "ticket_fields":
                    continue
                field_title = str(row.get("title", "")).strip()
                if field_title:
                    referenced_fields.append(field_title)
        if not referenced_fields:
            for item in reference_catalog.get("ticket_fields", []) or []:
                if not isinstance(item, dict):
                    continue
                field_name = str(item.get("name", "")).strip()
                if field_name:
                    referenced_fields.append(field_name)
                if len(referenced_fields) >= 3:
                    break
        if not referenced_fields:
            referenced_fields = ["General Intake Field"]

        for index in range(1, requested_count + 1):
            rows.append(
                {
                    "object_type": "ticket_forms",
                    "title": _next_unique_title("Generated Intake Form", index),
                    "conditions": [],
                    "actions": [{"field": "ticket_field_names", "value": referenced_fields[:8]}],
                    "dependency_notes": [fallback_note],
                }
            )
        return rows

    if normalized_object_type == "views":
        default_conditions = [{"field": "status", "operator": "less_than", "value": "solved"}]
        if re.search(r"\bopen\b", prompt_text, flags=re.IGNORECASE):
            default_conditions = [{"field": "status", "operator": "is", "value": "open"}]
        for index in range(1, requested_count + 1):
            rows.append(
                {
                    "object_type": "views",
                    "title": _next_unique_title("Generated Work Queue", index),
                    "conditions": default_conditions,
                    "actions": [{"field": "output_columns", "value": ["status", "updated", "subject"]}],
                    "dependency_notes": [fallback_note],
                }
            )
        return rows

    if normalized_object_type == "articles":
        section_id: str | None = None
        for section in reference_catalog.get("sections", []) or []:
            if not isinstance(section, dict):
                continue
            candidate = str(section.get("id", "")).strip()
            if candidate.isdigit():
                section_id = candidate
                break
        title_hints = _extract_title_hints(prompt_text)
        base_title = title_hints[0] if title_hints else "Generated Help Article"
        for index in range(1, requested_count + 1):
            actions = [
                {"field": "locale", "value": "en-us"},
                {"field": "body", "value": "<p>Draft article generated from business brief fallback.</p>"},
            ]
            if section_id:
                actions.append({"field": "section_id", "value": section_id})
            rows.append(
                {
                    "object_type": "articles",
                    "title": _next_unique_title(base_title, index),
                    "conditions": [],
                    "actions": actions,
                    "dependency_notes": [fallback_note],
                }
            )
        return rows

    if normalized_object_type in WAVE3_RULE_OBJECT_TYPES:
        constraints = _extract_explicit_constraints(prompt_text)
        title_hints = _extract_title_hints(prompt_text)
        base_title = (
            title_hints[0]
            if title_hints
            else {
                "triggers": "Generated Trigger",
                "automations": "Generated Automation",
                "macros": "Generated Macro",
            }.get(normalized_object_type, "Generated Rule")
        )
        requested_tag = str(constraints.get("tag") or "").strip()
        if not requested_tag:
            add_tag_match = re.search(
                r"\badd(?:s)?\s+(?:the\s+)?tag\s+[\"']?([a-z0-9_-]{2,})",
                prompt_text,
                flags=re.IGNORECASE,
            )
            set_tag_match = re.search(
                r"\bset(?:s)?\s+tag\s*(?:to|as)?\s*[\"']?([a-z0-9_-]{2,})",
                prompt_text,
                flags=re.IGNORECASE,
            )
            if add_tag_match:
                requested_tag = str(add_tag_match.group(1) or "").strip()
            elif set_tag_match:
                requested_tag = str(set_tag_match.group(1) or "").strip()
        normalized_tag = _normalize_set_tags_value(requested_tag) or "ai_import_generated"

        requested_status = str(constraints.get("status") or "").strip().lower()
        open_only_scope = bool(
            re.search(
                r"\b(open[- ]only|only open|any open ticket|open tickets?|status\s*(?:is|=)\s*open)\b",
                prompt_text,
                flags=re.IGNORECASE,
            )
        )
        if open_only_scope:
            base_condition = {"field": "status", "operator": "is", "value": "open"}
        elif requested_status:
            base_condition = {"field": "status", "operator": "is", "value": requested_status}
        else:
            base_condition = {"field": "status", "operator": "less_than", "value": "solved"}

        for index in range(1, requested_count + 1):
            actions = [{"field": "set_tags", "value": normalized_tag}]
            if requested_status:
                actions.append({"field": "status", "value": requested_status})
            rows.append(
                {
                    "object_type": normalized_object_type,
                    "title": _next_unique_title(base_title, index),
                    "conditions": [] if normalized_object_type == "macros" else [dict(base_condition)],
                    "actions": [dict(action) for action in actions],
                    "dependency_notes": [fallback_note],
                }
            )
        return rows

    return rows


def _supplement_chunk_rows_to_target(
    *,
    object_type: str,
    target_count: int,
    chunk_rows: list[dict],
    prompt: str,
    reference_catalog: dict[str, list[dict]],
    existing_titles: list[str] | None,
    generated_rows: list[dict] | None,
    reason: str,
) -> tuple[list[dict], int]:
    normalized_target = max(int(target_count or 1), 1)
    current_rows = list(chunk_rows or [])
    if len(current_rows) >= normalized_target:
        return current_rows, 0

    missing = normalized_target - len(current_rows)
    title_seed = list(existing_titles or [])
    for row in current_rows:
        title = str(row.get("title", "")).strip()
        if title:
            title_seed.append(title)

    supplements = _build_deterministic_chunk_rows(
        object_type=object_type,
        target_count=missing,
        prompt=prompt,
        reference_catalog=reference_catalog,
        existing_titles=title_seed,
        generated_rows=list(generated_rows or []) + current_rows,
        reason=reason,
    )
    if not supplements:
        return current_rows, 0
    return current_rows + supplements, len(supplements)


def _enforce_expected_object_type(
    *,
    rows: list[dict],
    expected_object_type: str,
) -> tuple[list[dict], int]:
    expected = _normalize_object_type(expected_object_type)
    aligned: list[dict] = []
    mismatched = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        row_type = _normalize_object_type(str(row.get("object_type", "")))
        if row_type != expected:
            mismatched += 1
            continue
        row["object_type"] = expected
        aligned.append(row)
    return aligned, mismatched


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


def _normalize_lookup_name(value: object) -> str:
    normalized = str(value or "").strip().lower()
    normalized = re.sub(r"[_\-\s]+", " ", normalized)
    normalized = re.sub(r"[^a-z0-9 ]+", "", normalized)
    return normalized.strip()


CATALOG_LOOKUP_OBJECT_TYPE = {
    "groups": "group",
    "group": "group",
    "ticket_forms": "ticket_form",
    "ticket_form": "ticket_form",
    "brands": "brand",
    "brand": "brand",
    "sections": "section",
    "section": "section",
    "categories": "category",
    "category": "category",
    "help_centers": "help_center",
    "help_center": "help_center",
    "ticket_fields": "ticket_field",
    "ticket_field": "ticket_field",
}


def _build_catalog_lookup(reference_catalog: dict[str, list[dict]]) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for raw_key, rows in (reference_catalog or {}).items():
        canonical_key = CATALOG_LOOKUP_OBJECT_TYPE.get(str(raw_key).strip().lower(), "")
        if not canonical_key or not isinstance(rows, list):
            continue
        bucket = lookup.setdefault(canonical_key, {})
        for item in rows:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", "")).strip()
            name = str(item.get("name", "")).strip()
            if not item_id or not name:
                continue
            lower_name = name.lower()
            normalized_name = _normalize_lookup_name(name)
            if lower_name:
                bucket[lower_name] = item_id
            if normalized_name and normalized_name not in bucket:
                bucket[normalized_name] = item_id
    return lookup


def _extract_title_hints(prompt: str) -> list[str]:
    text = str(prompt or "").strip()
    if not text:
        return []
    hints: list[str] = []
    patterns = [
        r"\b(?:named|called|titled)\s+[\"'\u201c\u201d]([^\"'\u201c\u201d]{2,200})[\"'\u201c\u201d]",
        r"\b(?:named|called|titled)\s+([A-Za-z0-9][A-Za-z0-9 _&\-/]{2,120})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            hint = str(match.group(1) or "").strip(" .,:;")
            if hint and hint not in hints:
                hints.append(hint)
    return hints[:4]


def _match_existing_base_object(
    *,
    prompt: str,
    object_type: str,
    existing_index: dict[str, dict[str, dict]],
) -> dict | None:
    bucket = existing_index.get(object_type, {})
    if not bucket:
        return None

    normalized_bucket: dict[str, dict] = {}
    for row in bucket.values():
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        normalized_bucket.setdefault(_normalize_lookup_name(name), row)

    for hint in _extract_title_hints(prompt):
        exact = bucket.get(hint.lower())
        if isinstance(exact, dict):
            return {
                "object_type": object_type,
                "match_type": "exact_title",
                "id": str(exact.get("id", "")).strip(),
                "name": str(exact.get("name", "")).strip(),
                "description": str(exact.get("description", "")).strip(),
            }
        normalized = normalized_bucket.get(_normalize_lookup_name(hint))
        if isinstance(normalized, dict):
            return {
                "object_type": object_type,
                "match_type": "normalized_title",
                "id": str(normalized.get("id", "")).strip(),
                "name": str(normalized.get("name", "")).strip(),
                "description": str(normalized.get("description", "")).strip(),
            }

    normalized_prompt = _normalize_lookup_name(prompt)
    if normalized_prompt:
        fuzzy_matches: list[dict] = []
        for row in bucket.values():
            if not isinstance(row, dict):
                continue
            name = str(row.get("name", "")).strip()
            if not name:
                continue
            normalized_name = _normalize_lookup_name(name)
            if not normalized_name:
                continue
            if normalized_name in normalized_prompt or normalized_prompt in normalized_name:
                fuzzy_matches.append(row)
        if len(fuzzy_matches) == 1:
            row = fuzzy_matches[0]
            return {
                "object_type": object_type,
                "match_type": "unique_fuzzy",
                "id": str(row.get("id", "")).strip(),
                "name": str(row.get("name", "")).strip(),
                "description": str(row.get("description", "")).strip(),
            }

    return None


def _build_inference_assumptions(
    *,
    prompt: str,
    plan: dict,
    focus_object_types: list[str],
    reference_catalog: dict[str, list[dict]],
    existing_index: dict[str, dict[str, dict]],
) -> dict:
    assumptions: list[dict] = []
    context_note_parts: list[str] = []
    resolved_object_type = _normalize_object_type(str(plan.get("object_type", "triggers")))

    if focus_object_types and resolved_object_type not in set(focus_object_types):
        prior = resolved_object_type
        resolved_object_type = focus_object_types[0]
        assumptions.append(
            {
                "kind": "focus_override",
                "message": (
                    f"Planner selected '{prior}', but selected focus enforces '{resolved_object_type}'. "
                    "Generation continued with focused object type."
                ),
            }
        )

    base_match = _match_existing_base_object(
        prompt=prompt,
        object_type=resolved_object_type,
        existing_index=existing_index,
    )
    if base_match:
        assumptions.append(
            {
                "kind": "base_object_match",
                "message": (
                    f"Using existing {resolved_object_type.rstrip('s')} '{base_match.get('name')}' "
                    f"(id={base_match.get('id')}) as reference."
                ),
            }
        )
        context_note_parts.append(
            "Existing base reference selected: "
            f"type={resolved_object_type}; id={base_match.get('id')}; name={base_match.get('name')}."
        )

    prompt_text = str(prompt or "").lower()
    if (
        resolved_object_type == "articles"
        and not any(token in prompt_text for token in ["section", "category", "help center"])
    ):
        section_rows = reference_catalog.get("sections", [])
        category_rows = reference_catalog.get("categories", [])
        help_center_rows = reference_catalog.get("help_centers", [])
        destination = None
        if isinstance(section_rows, list) and section_rows:
            destination = ("section", section_rows[0])
        elif isinstance(category_rows, list) and category_rows:
            destination = ("category", category_rows[0])
        elif isinstance(help_center_rows, list) and help_center_rows:
            destination = ("help_center", help_center_rows[0])
        if destination and isinstance(destination[1], dict):
            destination_type, destination_row = destination
            destination_id = str(destination_row.get("id", "")).strip()
            destination_name = str(destination_row.get("name", "")).strip()
            assumptions.append(
                {
                    "kind": "article_destination_default",
                    "message": (
                        f"No article location was specified. Defaulting to {destination_type} "
                        f"'{destination_name}' (id={destination_id})."
                    ),
                }
            )
            context_note_parts.append(
                f"Default article destination inferred: {destination_type}_id={destination_id} ({destination_name})."
            )

    if assumptions:
        context_note_parts.insert(0, "Inference-first mode applied assumptions; do not ask clarification questions.")

    confidence = float(plan.get("confidence", 0.0) or 0.0)
    ambiguity = float(plan.get("ambiguity_score", 0.0) or 0.0)
    inference_confidence = min(max((confidence * 0.8) + ((1.0 - ambiguity) * 0.2), 0.0), 1.0)

    return {
        "resolved_object_type": resolved_object_type,
        "assumptions": assumptions,
        "context_notes": " ".join(part for part in context_note_parts if part).strip(),
        "base_object_match": base_match,
        "inference_confidence": inference_confidence,
    }


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


def _append_dependency_note(row: dict, note: str) -> None:
    text = str(note).strip()
    if not text:
        return
    notes = list(row.get("dependency_notes", []) or [])
    notes.append(text)
    row["dependency_notes"] = notes


def _normalize_set_tags_value(raw_value: object) -> str | None:
    values: list[str] = []
    if isinstance(raw_value, list):
        for item in raw_value:
            values.extend(str(item).replace(",", " ").replace("|", " ").split())
    elif raw_value is not None:
        text = str(raw_value)
        text = re.sub(r"[|,;\n\r\t]+", " ", text)
        values.extend(text.split())
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = re.sub(r"[^a-zA-Z0-9_-]+", "", str(value).strip())
        if not token:
            continue
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        cleaned.append(lowered)
    if not cleaned:
        return None
    return " ".join(cleaned)


def _is_placeholder_reference_token(raw_value: object) -> bool:
    text = str(raw_value or "").strip().lower()
    if not text:
        return False
    if re.search(r"^\{\{[^{}]+\}\}$", text):
        return True
    if re.search(r"^<[^<>]+>$", text):
        return True
    if re.search(r"^\[\[[^\[\]]+\]\]$", text):
        return True
    placeholder_tokens = {
        "tbd",
        "todo",
        "placeholder",
        "replace_me",
        "replace-this",
        "your_value",
        "your_id",
        "insert_here",
    }
    normalized = re.sub(r"[^a-z0-9_:-]+", "_", text).strip("_")
    if normalized in placeholder_tokens:
        return True
    return bool(re.search(r"\b(?:tbd|todo|placeholder|replace[_ -]?me)\b", text))


def _normalize_rule_entries(
    row: dict,
    *,
    bucket_name: str,
    treat_as_conditions: bool,
    alias_counter: dict[str, int],
) -> list[str]:
    warnings: list[str] = []
    bucket = row.get(bucket_name, [])
    if isinstance(bucket, dict):
        bucket = [bucket]
    if not isinstance(bucket, list):
        bucket = []

    normalized_entries: list[dict] = []
    dropped_invalid = 0
    for entry in bucket:
        if not isinstance(entry, dict):
            dropped_invalid += 1
            continue
        raw_field = str(entry.get("field", "")).strip().lower()
        if not raw_field:
            dropped_invalid += 1
            continue
        normalized_field = RULE_FIELD_ALIASES.get(raw_field, raw_field)
        if normalized_field != raw_field:
            alias_counter[f"{raw_field}->{normalized_field}"] = alias_counter.get(
                f"{raw_field}->{normalized_field}",
                0,
            ) + 1

        value = entry.get("value")
        if normalized_field == "set_tags":
            value = _normalize_set_tags_value(value)
            if not value:
                dropped_invalid += 1
                warnings.append(
                    f"Dropped empty tag action from {bucket_name}; set_tags requires at least one tag."
                )
                continue
        if (
            normalized_field == "comment_value"
            and not treat_as_conditions
            and not str(value or "").strip()
        ):
            dropped_invalid += 1
            warnings.append(
                f"Dropped empty comment action from {bucket_name}; comment_value requires text."
            )
            continue

        normalized_entry: dict[str, object] = {
            "field": normalized_field,
            "value": value,
        }
        if treat_as_conditions:
            operator = str(entry.get("operator", "is")).strip() or "is"
            normalized_entry["operator"] = operator
        normalized_entries.append(normalized_entry)

    if dropped_invalid > 0:
        warnings.append(
            f"Dropped {dropped_invalid} invalid {bucket_name} entries without usable field/value."
        )
    row[bucket_name] = normalized_entries
    return warnings


def _canonicalize_rule_record(
    row: dict,
    *,
    object_type: str,
    related_lookup: dict[str, dict[str, str]] | None = None,
    catalog_lookup: dict[str, dict[str, str]] | None = None,
) -> dict:
    info = {
        "title": str(row.get("title", "Untitled Rule")).strip() or "Untitled Rule",
        "object_type": object_type,
        "alias_mappings": [],
        "warnings": [],
        "blocked": False,
    }
    alias_counter: dict[str, int] = {}

    warnings: list[str] = []
    warnings.extend(
        _normalize_rule_entries(
            row,
            bucket_name="conditions",
            treat_as_conditions=True,
            alias_counter=alias_counter,
        )
    )
    warnings.extend(
        _normalize_rule_entries(
            row,
            bucket_name="actions",
            treat_as_conditions=False,
            alias_counter=alias_counter,
        )
    )

    if object_type in {"triggers", "automations", "macros", "views"}:
        actions = list(row.get("actions", []) or [])
        conditions = list(row.get("conditions", []) or [])
        known_ids_by_type: dict[str, set[str]] = {}
        for reference_type in {"group", "ticket_form", "brand"}:
            values: set[str] = set()
            for lookup in (
                (related_lookup or {}).get(reference_type, {}),
                (catalog_lookup or {}).get(reference_type, {}),
            ):
                for candidate in lookup.values():
                    candidate_text = str(candidate).strip()
                    if candidate_text:
                        values.add(candidate_text)
            known_ids_by_type[reference_type] = values

        known_group_ids = {
            str(value).strip()
            for lookup in ((related_lookup or {}).get("group", {}), (catalog_lookup or {}).get("group", {}))
            for value in lookup.values()
            if str(value).strip()
        }
        for entry in [*conditions, *actions]:
            if not isinstance(entry, dict):
                continue
            field = str(entry.get("field", "")).strip().lower()
            value_text = str(entry.get("value", "")).strip()
            if _is_placeholder_reference_token(value_text):
                row.setdefault("validation_overrides", {})
                row["validation_overrides"]["blocked_reason"] = (
                    f"Placeholder token detected for '{field}'. Replace it with a real Zendesk value."
                )
                info["blocked"] = True
                warnings.append(
                    f"Blocked: placeholder token used for '{field}'."
                )
                break
            if field == "assignee_id":
                if re.search(r"\bassign\b", info["title"], flags=re.IGNORECASE) and not re.search(
                    r"\b(agent|assignee|user)\b",
                    info["title"],
                    flags=re.IGNORECASE,
                ):
                    entry["field"] = "group_id"
                    alias_counter["assignee_id->group_id"] = alias_counter.get("assignee_id->group_id", 0) + 1
                    warnings.append(
                        "Converted assignee_id to group_id based on title intent (team/group assignment heuristic)."
                    )
                    field = "group_id"
            if field == "group_id" and value_text.isdigit() and known_group_ids and value_text not in known_group_ids:
                row.setdefault("validation_overrides", {})
                row["validation_overrides"]["blocked_reason"] = (
                    "group_id does not match any known Zendesk group in current context. "
                    "Use a valid group name/id or resync context."
                )
                info["blocked"] = True
                warnings.append(
                    f"Blocked: group_id '{value_text}' is not present in synced group catalog."
                )
                break
            reference_type = REFERENCE_OBJECT_BY_FIELD.get(field)
            if (
                reference_type in known_ids_by_type
                and value_text.isdigit()
                and known_ids_by_type.get(reference_type)
                and value_text not in known_ids_by_type.get(reference_type, set())
            ):
                row.setdefault("validation_overrides", {})
                row["validation_overrides"]["blocked_reason"] = (
                    f"{field} '{value_text}' is not present in synced {reference_type} catalog."
                )
                info["blocked"] = True
                warnings.append(
                    f"Blocked: {field} '{value_text}' does not match any synced {reference_type}."
                )
                break

    if object_type in {"triggers", "automations", "macros"} and not list(row.get("actions", []) or []):
        row.setdefault("validation_overrides", {})
        row["validation_overrides"]["blocked_reason"] = (
            "No valid actions remained after canonicalization; add at least one Zendesk action."
        )
        info["blocked"] = True
        warnings.append(
            "No valid actions remained after canonicalization; this record is blocked from deploy."
        )

    if alias_counter:
        info["alias_mappings"] = [f"{key} ({count})" for key, count in sorted(alias_counter.items())]
    if warnings:
        info["warnings"] = warnings
        for warning in warnings:
            _append_dependency_note(row, warning)
    return info


def _find_reference_id_from_lookup(
    raw_value: object,
    *,
    object_type: str,
    related_lookup: dict[str, dict[str, str]] | None,
    catalog_lookup: dict[str, dict[str, str]] | None,
) -> tuple[str | None, str | None]:
    text_value = str(raw_value or "").strip()
    if not text_value:
        return None, None
    if text_value.isdigit():
        return text_value, "direct_id"

    normalized_value = _normalize_lookup_name(text_value)
    related_bucket = (related_lookup or {}).get(object_type, {})
    catalog_bucket = (catalog_lookup or {}).get(object_type, {})

    for key in (text_value.lower(), normalized_value):
        if key and key in related_bucket:
            return str(related_bucket[key]).strip(), "selected context"
    for key in (text_value.lower(), normalized_value):
        if key and key in catalog_bucket:
            return str(catalog_bucket[key]).strip(), "catalog context"
    return None, None


def _extract_article_value_from_row(row: dict, aliases: set[str]) -> object | None:
    for bucket_name in ("actions", "conditions"):
        bucket = row.get(bucket_name, [])
        if isinstance(bucket, dict):
            bucket = [bucket]
        if not isinstance(bucket, list):
            continue
        for entry in bucket:
            if not isinstance(entry, dict):
                continue
            field = str(entry.get("field", "")).strip().lower()
            if field in aliases:
                return entry.get("value")
    for key, value in row.items():
        if str(key).strip().lower() in aliases:
            return value
    return None


def _canonicalize_article_record(
    row: dict,
    *,
    reference_catalog: dict[str, list[dict]],
    related_lookup: dict[str, dict[str, str]] | None,
    catalog_lookup: dict[str, dict[str, str]] | None,
) -> dict:
    info = {
        "title": str(row.get("title", "Untitled article")).strip() or "Untitled article",
        "object_type": "articles",
        "alias_mappings": [],
        "defaults_applied": [],
        "warnings": [],
        "blocked": False,
    }

    locale_value = _extract_article_value_from_row(row, ARTICLE_FIELD_ALIASES["locale"])
    locale_text = str(locale_value or "").strip().lower() or "en-us"
    _set_action_value(row, "locale", locale_text)
    if not locale_value:
        info["defaults_applied"].append("locale=en-us")

    body_value = _extract_article_value_from_row(row, ARTICLE_FIELD_ALIASES["body"])
    body_text = str(body_value or "").strip()
    if not body_text:
        body_text = f"<p>{info['title']}</p>"
        info["defaults_applied"].append("body=title_template")
    _set_action_value(row, "body", body_text)

    draft_value = _extract_article_value_from_row(row, ARTICLE_FIELD_ALIASES["draft"])
    published_value = _extract_article_value_from_row(row, ARTICLE_FIELD_ALIASES["published"])
    draft_bool = _coerce_bool(draft_value)
    published_bool = _coerce_bool(published_value)
    if draft_bool is None and published_bool is not None:
        draft_bool = not published_bool
        info["alias_mappings"].append("published->draft")
    if draft_bool is not None:
        _set_action_value(row, "draft", draft_bool)

    section_raw = _extract_article_value_from_row(row, ARTICLE_FIELD_ALIASES["section_id"])
    section_id, lookup_source = _find_reference_id_from_lookup(
        section_raw,
        object_type="section",
        related_lookup=related_lookup,
        catalog_lookup=catalog_lookup,
    )
    if section_id:
        _set_action_value(row, "section_id", section_id)
        if lookup_source and lookup_source != "direct_id":
            info["alias_mappings"].append(f"section_id resolved from {lookup_source}")
    else:
        fallback_section = None
        section_rows = reference_catalog.get("sections", [])
        if isinstance(section_rows, list) and section_rows:
            first = section_rows[0]
            if isinstance(first, dict):
                first_id = str(first.get("id", "")).strip()
                if first_id.isdigit():
                    fallback_section = first_id
        if fallback_section:
            _set_action_value(row, "section_id", fallback_section)
            info["defaults_applied"].append("section_id=default_first_section")
            info["warnings"].append(
                f"section_id was not explicit; defaulted to section_id={fallback_section} from context."
            )
        else:
            row.setdefault("validation_overrides", {})
            row["validation_overrides"]["blocked_reason"] = (
                "Article requires a numeric section_id. Provide a valid section or include it in context."
            )
            info["blocked"] = True
            info["warnings"].append(
                "Article blocked: unable to resolve numeric section_id from prompt or context."
            )

    for warning in info["warnings"]:
        _append_dependency_note(row, warning)
    return info


def _parse_custom_field_options(raw: object) -> tuple[list[dict[str, str]], list[str]]:
    warnings: list[str] = []
    options: list[dict[str, str]] = []

    def _clean_option_name(name: str) -> str:
        cleaned = str(name or "").strip().strip("\"'")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        cleaned = re.sub(
            r"\s*-\s*(?:a\s+)?(?:drop[\s-]?down|single[\s-]?select|multi[\s-]?select|multiselect|text|textarea|number|integer|decimal|date|checkbox)\b.*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(
            r"\s*\b(?:with|having)\s+(?:options?|values?)\s*:.*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        return cleaned

    def _add_option(name: str, value: str | None = None) -> None:
        cleaned_name = _clean_option_name(name)
        if not cleaned_name:
            return
        raw_value = str(value or "").strip()
        if re.search(r"(dropdown|drop_down|drop-down|called|with_options|with-values)", raw_value, flags=re.IGNORECASE):
            raw_value = ""
        normalized_value = _slugify_option_value(raw_value or cleaned_name)
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


def _extract_ticket_field_specs_from_prompt(prompt: str) -> list[dict[str, object]]:
    text = str(prompt or "").strip()
    if not text:
        return []
    normalized = re.sub(r"\s{2,}", " ", text)
    normalized = re.sub(r"\s+-\s+", "\n- ", normalized)
    normalized = re.sub(r"\s+(\d+)\.\s+", "\n\\1. ", normalized)
    specs: list[dict[str, object]] = []
    seen_titles: set[str] = set()
    field_pattern = re.compile(
        r"(?:^[-*]\s*|^\d+\.\s*|^)\s*(?:a\s+)?"
        r"(?P<kind>drop[\s-]?down|single[\s-]?select|multi[\s-]?select|multiselect|text|textarea|number|integer|decimal|date|checkbox|regexp)"
        r"(?:\s+field)?\s+(?:called|named)\s+[\"']?(?P<title>[^\"'\n:]{2,120})[\"']?"
        r"(?P<rest>.*)$",
        flags=re.IGNORECASE,
    )
    options_pattern = re.compile(
        r"(?:with\s+)?(?:options?|values?)\s*:?\s*(?P<values>.+)$",
        flags=re.IGNORECASE,
    )

    for line in normalized.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        match = field_pattern.search(candidate)
        if not match:
            continue
        raw_title = str(match.group("title") or "").strip().strip(" .,:;")
        if not raw_title:
            continue
        title_key = _normalize_title_for_dedupe(raw_title)
        if not title_key or title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        field_type = _normalize_ticket_field_type(str(match.group("kind") or "").strip())
        rest = str(match.group("rest") or "").strip()
        options: list[str] = []
        opt_match = options_pattern.search(rest)
        if opt_match:
            raw_values = str(opt_match.group("values") or "").strip()
            options = [part.strip() for part in re.split(r"[,|;/]", raw_values) if part.strip()]
        specs.append(
            {
                "title": raw_title,
                "field_type": field_type or "text",
                "options": options[:20],
            }
        )
    return specs


def _extract_ticket_field_option_hints(prompt: str, *, field_title: str | None = None) -> list[str]:
    text = prompt.strip()
    if not text:
        return []
    if field_title:
        title_key = _normalize_title_for_dedupe(field_title)
        for spec in _extract_ticket_field_specs_from_prompt(text):
            spec_title_key = _normalize_title_for_dedupe(str(spec.get("title", "")).strip())
            if not spec_title_key or not title_key:
                continue
            if spec_title_key == title_key or spec_title_key in title_key or title_key in spec_title_key:
                options = [
                    str(item).strip()
                    for item in list(spec.get("options", []) or [])
                    if str(item).strip()
                ]
                if options:
                    return options
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
            r"\bas\b",
            r"\bincluding\b",
            r"\bincludes\b",
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
        return _normalize_title_for_constraint_match(cleaned)

    text = prompt.strip().lower()

    def _is_valid_title_constraint_context(*, full_text: str, match_start: int) -> bool:
        window_start = max(0, int(match_start) - 64)
        context = full_text[window_start:int(match_start)]
        disallowed_context = (
            "company called",
            "business called",
            "organization called",
            "instance called",
            "workspace called",
        )
        if any(token in context for token in disallowed_context):
            return False
        return True
    explicit = {
        "title": None,
        "tag": None,
        "status": None,
        "field_type": None,
        "field_options_requested": False,
    }

    title_patterns = [
        r"\b(?:trigger|automation|macro|view|group|ticket\s*form|form|ticket\s*field|field|article|rule)\s+(?:called|named)\s+([a-z0-9][a-z0-9 _-]{0,120})",
        r"\bname(?:\s+it)?\s+(?:as|to)?\s*([a-z0-9][a-z0-9 _-]{0,120})",
    ]
    for pattern in title_patterns:
        for match in re.finditer(pattern, text):
            if not _is_valid_title_constraint_context(full_text=text, match_start=match.start()):
                continue
            normalized_title = _normalize_title_constraint(match.group(1))
            if normalized_title:
                explicit["title"] = normalized_title
                break
        if explicit["title"]:
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
    requested_title = _normalize_title_for_constraint_match(constraints.get("title") or "")
    requested_tag = str(constraints.get("tag") or "").strip().lower()
    requested_status = str(constraints.get("status") or "").strip().lower()
    requested_field_type = str(constraints.get("field_type") or "").strip().lower()
    requested_field_options = bool(constraints.get("field_options_requested"))

    if requested_title:
        generated_title_candidates = [
            _normalize_title_for_constraint_match(row.get("title", ""))
            for row in generated_rows
        ]
        title_match = any(
            candidate and (
                requested_title == candidate
                or requested_title in candidate
                or candidate in requested_title
            )
            for candidate in generated_title_candidates
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


def _merge_context_notes(base_notes: str | None, appended_notes: str | None) -> str:
    primary = str(base_notes or "").strip()
    extra = str(appended_notes or "").strip()
    if primary and extra:
        return f"{primary} {extra}".strip()
    return primary or extra


def _apply_inference_assumptions_to_preview(records: list[dict], assumptions: list[dict]) -> list[dict]:
    if not assumptions:
        return records
    assumption_messages = [
        str(item.get("message", "")).strip()
        for item in assumptions
        if isinstance(item, dict) and str(item.get("message", "")).strip()
    ]
    if not assumption_messages:
        return records
    warning_line = "Inference assumptions applied: " + " | ".join(assumption_messages[:3])
    for row in records:
        row_warnings = list(row.get("warnings", []) or [])
        if warning_line not in row_warnings:
            row_warnings.append(warning_line)
        row["warnings"] = row_warnings
    return records


def _apply_dependency_resolution(
    rows: list[dict],
    *,
    related_lookup: dict[str, dict[str, str]],
    catalog_lookup: dict[str, dict[str, str]],
    dependency_mode: str,
) -> tuple[list[dict], dict]:
    if not related_lookup and not catalog_lookup:
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

                normalized_value = _normalize_lookup_name(value)
                resolved = related_lookup.get(expected_object, {}).get(value.lower())
                lookup_source = "selected context"
                if not resolved and normalized_value:
                    resolved = related_lookup.get(expected_object, {}).get(normalized_value)
                if not resolved:
                    resolved = catalog_lookup.get(expected_object, {}).get(value.lower())
                    lookup_source = "catalog context"
                if not resolved and normalized_value:
                    resolved = catalog_lookup.get(expected_object, {}).get(normalized_value)
                    lookup_source = "catalog context"
                if resolved:
                    entry["value"] = resolved
                    resolved_links += 1
                    row_notes.append(
                        f"{field}: mapped '{value}' to ID {resolved} from {lookup_source}."
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
        if not actions and object_type != "ticket_forms":
            row_warnings.append("No actions defined for this record.")
        if object_type == "ticket_forms" and not actions and not blocked_reason:
            blocked_reason = (
                "Ticket form has no field linkage actions. Provide field references or include field context."
            )
            validation_status = "failed"
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
        prompt_option_hints = _extract_ticket_field_option_hints(
            prompt,
            field_title=info["title"],
        )
        if prompt_option_hints:
            raw_options = prompt_option_hints
            info["alias_mappings"].append("inferred custom_field_options from prompt")
    parsed_options, option_warnings = _parse_custom_field_options(raw_options)
    cleaned_options: list[dict[str, str]] = []
    dropped_options = 0
    for option in parsed_options:
        if not isinstance(option, dict):
            dropped_options += 1
            continue
        option_name = str(option.get("name", "")).strip()
        option_value = str(option.get("value", "")).strip()
        if _is_placeholder_reference_token(option_name) or _is_placeholder_reference_token(option_value):
            dropped_options += 1
            continue
        if len(option_name) > 100:
            dropped_options += 1
            continue
        cleaned_options.append({"name": option_name, "value": option_value})
    if dropped_options > 0:
        option_warnings.append(
            f"Dropped {dropped_options} malformed/placeholder option value(s) during cleanup."
        )
    parsed_options = cleaned_options
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


def _extract_ticket_form_field_hints_from_prompt(
    *,
    prompt: str,
    reference_catalog: dict[str, list[dict]],
) -> list[str]:
    text = str(prompt or "").strip()
    if not text:
        return []
    lowered = text.lower()

    hints: list[str] = []
    seen: set[str] = set()

    known_names: list[str] = []
    for item in reference_catalog.get("ticket_fields", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if name:
            known_names.append(name)

    # Prefer exact matches against known field names in prompt text.
    for name in sorted(set(known_names), key=len, reverse=True):
        name_lower = name.lower()
        if name_lower and name_lower in lowered:
            key = name_lower
            if key not in seen:
                seen.add(key)
                hints.append(name)

    # Parse "including fields ..." / "with fields ..." clauses as fallback hints.
    clause_match = re.search(
        r"\b(?:including|include|with|add)\s+fields?\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if clause_match:
        raw_clause = str(clause_match.group(1) or "").strip()
        raw_clause = re.split(r"[.!?]", raw_clause, maxsplit=1)[0]
        for token in re.split(r",|;|\band\b|\&", raw_clause, flags=re.IGNORECASE):
            candidate = str(token).strip(" \"'`.")
            if not candidate:
                continue
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            hints.append(candidate)

    return hints[:20]


def _canonicalize_generated_rows(
    *,
    rows: list[dict],
    prompt: str,
    reference_catalog: dict[str, list[dict]],
    existing_index: dict[str, dict[str, dict]],
    related_lookup: dict[str, dict[str, str]] | None,
    catalog_lookup: dict[str, dict[str, str]] | None,
    settings,
) -> tuple[list[dict], dict, dict, dict]:
    normalized_rows: list[dict] = []
    field_inference_records: list[dict] = []
    form_resolution = {
        "resolved_ids": 0,
        "auto_created_fields": 0,
        "unresolved": [],
        "forms_processed": 0,
        "inference_hints_count": 0,
        "inference_sources": [],
    }
    trigger_article_summary = {
        "rules_processed": 0,
        "articles_processed": 0,
        "alias_mappings": 0,
        "defaults_applied": 0,
        "warnings": 0,
        "blocked_records": 0,
    }

    existing_field_map: dict[str, str] = {}
    for item in reference_catalog.get("ticket_fields", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().lower()
        item_id = str(item.get("id", "")).strip()
        if name and item_id:
            existing_field_map[name] = item_id
    known_field_ids = {
        str(value).strip()
        for value in existing_field_map.values()
        if str(value).strip()
    }

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
        elif row["object_type"] in {"triggers", "automations", "views", "macros"}:
            info = _canonicalize_rule_record(
                row,
                object_type=row["object_type"],
                related_lookup=related_lookup,
                catalog_lookup=catalog_lookup,
            )
            trigger_article_summary["rules_processed"] += 1
            trigger_article_summary["alias_mappings"] += len(info.get("alias_mappings", []))
            trigger_article_summary["warnings"] += len(info.get("warnings", []))
            if bool(info.get("blocked")):
                trigger_article_summary["blocked_records"] += 1
        elif row["object_type"] == "articles":
            info = _canonicalize_article_record(
                row,
                reference_catalog=reference_catalog,
                related_lookup=related_lookup,
                catalog_lookup=catalog_lookup,
            )
            trigger_article_summary["articles_processed"] += 1
            trigger_article_summary["alias_mappings"] += len(info.get("alias_mappings", []))
            trigger_article_summary["defaults_applied"] += len(info.get("defaults_applied", []))
            trigger_article_summary["warnings"] += len(info.get("warnings", []))
            if bool(info.get("blocked")):
                trigger_article_summary["blocked_records"] += 1
        normalized_rows.append(row)

    for row in normalized_rows:
        if str(row.get("object_type", "")).strip().lower() != "ticket_forms":
            continue
        form_resolution["forms_processed"] += 1
        references = _extract_ticket_form_field_references(row)
        if not references:
            inferred_hints = _extract_ticket_form_field_hints_from_prompt(
                prompt=prompt,
                reference_catalog=reference_catalog,
            )
            if inferred_hints:
                references = inferred_hints
                form_resolution["inference_hints_count"] += len(inferred_hints)
                sources = set(form_resolution.get("inference_sources", []) or [])
                sources.add("prompt+catalog")
                form_resolution["inference_sources"] = sorted(sources)
                dependency_notes = list(row.get("dependency_notes", []) or [])
                dependency_notes.append(
                    "Form field references inferred from prompt/context because generated form actions were sparse."
                )
                row["dependency_notes"] = dependency_notes
        if not references:
            row.setdefault("validation_overrides", {})
            row["validation_overrides"]["blocked_reason"] = (
                "No ticket form field references could be inferred or resolved. "
                "Specify at least one field name or include ticket field context."
            )
            continue

        resolved_ids: list[int] = []
        referenced_names: list[str] = []
        for ref in references:
            ref_text = str(ref).strip()
            if not ref_text:
                continue
            if _is_placeholder_reference_token(ref_text):
                row.setdefault("validation_overrides", {})
                row["validation_overrides"]["blocked_reason"] = (
                    "Ticket form contains placeholder field reference tokens. "
                    "Replace placeholders with real field names or IDs."
                )
                form_resolution["unresolved"].append(ref_text)
                continue
            if ref_text.isdigit():
                if known_field_ids and ref_text not in known_field_ids:
                    row.setdefault("validation_overrides", {})
                    row["validation_overrides"]["blocked_reason"] = (
                        f"ticket_field_id '{ref_text}' is not present in synced ticket field catalog."
                    )
                    form_resolution["unresolved"].append(ref_text)
                    continue
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
    canonicalization = {
        "trigger_article": trigger_article_summary,
    }
    return normalized_rows, field_inference, form_resolution, canonicalization


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
    compatibility_first: bool = False,
    compatibility_only: bool = False,
    model_override: str | None = None,
    api_key_override: str | None = None,
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
            compatibility_first=compatibility_first,
            compatibility_only=compatibility_only,
            model_override=model_override,
            api_key_override=api_key_override,
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
                    compatibility_first=compatibility_first,
                    compatibility_only=compatibility_only,
                    model_override=model_override,
                    api_key_override=api_key_override,
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
                compatibility_first=compatibility_first,
                compatibility_only=compatibility_only,
                model_override=model_override,
                api_key_override=api_key_override,
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
                compatibility_first=compatibility_first,
                compatibility_only=compatibility_only,
                model_override=model_override,
                api_key_override=api_key_override,
            )
    runtime_metrics = GrokClient.get_last_call_metrics("generator")
    return generated_data, runtime_metrics, context_profile


def _normalize_appscript_action_result(result: object, *, action: str) -> dict:
    if not isinstance(result, dict):
        return {
            "action": action,
            "status": "error",
            "detail": f"Apps Script action '{action}' returned malformed payload.",
            "http_status": None,
            "data": {},
        }
    status = str(result.get("status", "")).strip().lower() or "error"
    detail = result.get("detail")
    if status not in {"ok", "error", "skipped"}:
        status = "error"
    if detail is None and status == "error":
        detail = f"Apps Script action '{action}' failed without detail."
    return {
        "action": str(result.get("action", action)),
        "status": status,
        "detail": detail,
        "http_status": result.get("http_status"),
        "data": result.get("data", {}) if isinstance(result.get("data", {}), dict) else {},
    }


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
    llm_routes_metadata = _build_llm_routes_metadata(
        planner_route=planner_route,
        clarifier_route=clarifier_route,
        generator_route=generator_route,
        settings=settings,
    )
    related_objects = [item.model_dump() for item in request.related_objects]
    focus_object_types = _normalize_focus_object_types(request.focus_object_types)
    focus_object_type_set = set(focus_object_types)
    reference_catalog = {
        key: [item.model_dump() for item in values]
        for key, values in (request.reference_catalog or {}).items()
    }
    related_lookup = _build_related_lookup(related_objects)
    catalog_lookup = _build_catalog_lookup(reference_catalog)
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
    planner_telemetry: dict = {}
    generator_telemetry: dict = {}
    chunk_estimate: dict = {"estimated_count": 1, "sources": ["default"], "numeric_matches": [], "enumerated_items": 0}
    estimated_requested_records = 1
    pre_planner_object_targets: dict[str, int] = {}
    force_wave_chunk_path = False
    force_wave_chunk_reason = "standard_prompt"
    planner_bypassed = False
    orchestration_mode = "explicit_fast_path"
    orchestration_metadata: dict = {
        "mode": orchestration_mode,
        "waves": [],
        "reconciliation_summary": {
            "create": 0,
            "reuse": 0,
            "update": 0,
            "blocked": 0,
        },
        "assumptions_applied": [],
    }

    try:
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
        chunk_estimate = _estimate_requested_record_count(request.prompt)
        estimated_requested_records = int(chunk_estimate.get("estimated_count", 1) or 1)
        inferred_object_type_for_explicitness = _infer_object_type_from_prompt(
            prompt=request.prompt,
            focus_object_types=focus_object_types,
        )
        prompt_explicit_for_bypass = _is_explicit_enough_for_generation(
            request.prompt,
            inferred_object_type_for_explicitness,
        )
        pre_planner_object_targets = _extract_object_type_targets(
            prompt=request.prompt,
            focus_object_types=focus_object_types,
            estimated_count=estimated_requested_records,
            chunk_estimate=chunk_estimate,
        )
        force_wave_chunk_path, force_wave_chunk_reason = _should_force_wave_chunk_path(
            prompt=request.prompt,
            estimated_count=estimated_requested_records,
            object_targets=pre_planner_object_targets,
            chunk_estimate=chunk_estimate,
        )
        planner_bypassed = (
            estimated_requested_records == 1
            and prompt_explicit_for_bypass
            and not force_wave_chunk_path
        )
        store.append_status(batch_id, "request_validated", "Incoming request validated.")
    except Exception as exc:  # noqa: BLE001
        failure_reason = f"Request initialization failed: {exc}"
        next_step = "Retry generation after resolving request initialization/runtime issues."
        failure_metadata = {
            "benchmark": {"enabled": benchmark_mode},
            "llm_routes": llm_routes_metadata,
            "failure": {
                "failure_stage": "generate",
                "failure_code": "generate_runtime_error",
                "failure_reason": failure_reason,
                "next_step": next_step,
            },
        }
        status_written = False
        try:
            store.append_status(batch_id, "failed", failure_reason)
            status_written = True
        except Exception:  # noqa: BLE001
            status_written = False
        try:
            store.update_batch(
                batch_id,
                {
                    "status": "failed",
                    "metadata": failure_metadata,
                },
            )
        except Exception:  # noqa: BLE001
            # Best-effort fallback to avoid leaving the batch in received state.
            fallback_batch = store.get_batch(batch_id) or dict(batch)
            history = list(fallback_batch.get("status_history", []) or [])
            if not status_written:
                history.append({"status": "failed", "message": failure_reason, "at": _utc_now()})
            fallback_batch.update(
                {
                    "status": "failed",
                    "updated_at": _utc_now(),
                    "status_history": history,
                    "metadata": failure_metadata,
                }
            )
            store.save_batch(fallback_batch)
        raise GenerateFailureError(
            code="generate_runtime_error",
            reason=failure_reason,
            next_step=next_step,
        ) from exc

    async def _call_planner(*, allow_fallback: bool) -> dict:
        try:
            return await run_planner(
                request.prompt,
                dependency_mode=request.dependency_mode,
                focus_object_types=focus_object_types,
                related_objects=planner_context_bundle["related_objects"],
                reference_catalog=planner_context_bundle["reference_catalog"],
                recent_batch_context=planner_context_bundle["recent_batch_context"],
                context_notes=planner_context_bundle["context_notes"],
                allow_fallback=allow_fallback,
            )
        except RuntimeError as planner_exc:
            if _is_rate_limited_error(planner_exc):
                raise GenerateFailureError(
                    code="rate_limited",
                    reason="Planner request was rate-limited before generation started.",
                    next_step="Wait for the provider rate-limit window to reset, then retry this prompt.",
                ) from planner_exc
            raise

    if force_wave_chunk_path:
        store.append_status(
            batch_id,
            "planning",
            (
                "Large multi-item prompt detected; forcing wave/chunk orchestration "
                f"({force_wave_chunk_reason})."
            ),
        )

    if planner_bypassed:
        store.append_status(
            batch_id,
            "planning",
            "Planner bypassed for explicit single-item prompt; using deterministic inference.",
        )
        plan = _build_deterministic_planner_plan(
            prompt=request.prompt,
            dependency_mode=request.dependency_mode,
            focus_object_types=focus_object_types,
            estimated_count=estimated_requested_records,
        )
    else:
        store.append_status(batch_id, "planning", "Planner call in progress.")
        try:
            plan = await _call_planner(allow_fallback=False)
        except RuntimeError as exc:
            if isinstance(exc, GenerateFailureError):
                raise
            if _is_deterministic_llm_error(exc):
                store.append_status(
                    batch_id,
                    "planning",
                    "Planner returned deterministic JSON failure; switching to heuristic planner fallback.",
                )
                plan = await _call_planner(allow_fallback=True)
            else:
                store.append_status(
                    batch_id,
                    "planning",
                    "Planner retrying with compact context to stay within model limits.",
                )
                planner_context_bundle = llm_context_aggressive
                try:
                    plan = await _call_planner(allow_fallback=False)
                except RuntimeError as compact_exc:
                    if isinstance(compact_exc, GenerateFailureError):
                        raise
                    if _is_deterministic_llm_error(compact_exc):
                        store.append_status(
                            batch_id,
                            "planning",
                            "Compact planner attempt hit deterministic JSON failure; using heuristic planner fallback.",
                        )
                        plan = await _call_planner(allow_fallback=True)
                    else:
                        plan = await _call_planner(allow_fallback=True)
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
    inference_result = _build_inference_assumptions(
        prompt=request.prompt,
        plan=plan,
        focus_object_types=focus_object_types,
        reference_catalog=reference_catalog,
        existing_index=existing_object_index,
    )
    resolved_object_type = _normalize_object_type(
        str(inference_result.get("resolved_object_type") or plan.get("object_type", "triggers"))
    )
    plan["object_type"] = resolved_object_type
    inference_assumptions = list(inference_result.get("assumptions", []) or [])
    base_object_match = inference_result.get("base_object_match")
    inference_confidence = float(inference_result.get("inference_confidence", 0.0) or 0.0)
    prompt_explicit = _is_explicit_enough_for_generation(request.prompt, resolved_object_type)
    object_targets = dict(pre_planner_object_targets) or _extract_object_type_targets(
        prompt=request.prompt,
        focus_object_types=focus_object_types,
        estimated_count=estimated_requested_records,
        chunk_estimate=chunk_estimate,
    )
    use_business_blueprint, business_reason = _is_business_blueprint_prompt(
        prompt=request.prompt,
        focus_object_types=focus_object_types,
        estimated_count=estimated_requested_records,
        prompt_explicit=prompt_explicit,
        object_targets=object_targets,
    )
    if force_wave_chunk_path:
        use_business_blueprint = True
        business_reason = force_wave_chunk_reason
    blueprint_payload: dict = {}
    orchestration_backlog: list[dict] = []
    try:
        if use_business_blueprint and not planner_bypassed:
            orchestration_mode = "business_blueprint"
            store.append_status(
                batch_id,
                "business_blueprinting",
                "Compiling business brief into executable blueprint.",
            )
            try:
                blueprint_payload, blueprint_telemetry = await _run_business_blueprint_compiler(
                    prompt=request.prompt,
                    dependency_mode=request.dependency_mode,
                    focus_object_types=focus_object_types,
                    object_targets=object_targets,
                    planner_route=planner_route,
                )
                if not planner_telemetry and isinstance(blueprint_telemetry, dict):
                    planner_telemetry = blueprint_telemetry
            except LLMRequestError as exc:
                if str(exc.error_class or "").strip().lower() == "rate_limited":
                    raise GenerateFailureError(
                        code="rate_limited",
                        reason="Business blueprint compiler was rate-limited before generation started.",
                        next_step="Wait for the provider rate-limit window to reset, then retry this prompt.",
                    ) from exc
                raise GenerateFailureError(
                    code="business_blueprint_failed",
                    reason=f"Business blueprint compiler failed: {exc}",
                    next_step="Retry generation after resolving model compatibility/runtime issues.",
                ) from exc
            except Exception as exc:  # noqa: BLE001
                raise GenerateFailureError(
                    code="business_blueprint_failed",
                    reason=f"Business blueprint compiler failed: {exc}",
                    next_step="Retry generation after resolving compiler/runtime issues.",
                ) from exc

            store.append_status(
                batch_id,
                "backlog_building",
                "Building deterministic backlog and dependency order.",
            )
            orchestration_backlog = _build_orchestration_backlog(
                blueprint=blueprint_payload,
                prompt=request.prompt,
                dependency_mode=request.dependency_mode,
                focus_object_types=focus_object_types,
            )
            if not orchestration_backlog:
                raise GenerateFailureError(
                    code="backlog_build_failed",
                    reason="Business blueprint produced no executable backlog items.",
                    next_step="Refine the business request with object scope or provide object focus selections.",
                )
            estimated_requested_records = max(
                1,
                sum(int(item.get("target_count", 1) or 1) for item in orchestration_backlog),
            )
            inference_assumptions.append(
                {
                    "kind": "business_blueprint_mode",
                    "message": (
                        f"Smart Auto activated business blueprint orchestration ({business_reason}); "
                        f"{len(orchestration_backlog)} backlog items scheduled across waves."
                    ),
                }
            )
            orchestration_metadata = {
                "mode": orchestration_mode,
                "activation_reason": business_reason,
                "blueprint": blueprint_payload,
                "backlog_size": len(orchestration_backlog),
                "waves": [],
                "reconciliation_summary": {
                    "create": 0,
                    "reuse": 0,
                    "update": 0,
                    "blocked": 0,
                },
                "assumptions_applied": list(blueprint_payload.get("assumptions", []) or []),
            }
        else:
            orchestration_mode = "explicit_fast_path"
            orchestration_metadata = {
                "mode": orchestration_mode,
                "activation_reason": business_reason,
                "waves": [],
                "reconciliation_summary": {
                    "create": 0,
                    "reuse": 0,
                    "update": 0,
                    "blocked": 0,
                },
                "assumptions_applied": [],
            }
    except GenerateFailureError as exc:
        store.append_status(batch_id, "failed", exc.reason)
        store.update_batch(
            batch_id,
            {
                "status": "failed",
                "metadata": {
                    "benchmark": {"enabled": benchmark_mode},
                    "planning": {
                        "planner_bypassed": planner_bypassed,
                        "mode": "deterministic" if planner_bypassed else "llm",
                        "estimated_requested_records": estimated_requested_records,
                        "force_wave_chunk_path": force_wave_chunk_path,
                        "force_wave_chunk_reason": force_wave_chunk_reason,
                    },
                    "orchestration": orchestration_metadata,
                    "llm_routes": llm_routes_metadata,
                    "llm_runtime": {
                        "planner": planner_telemetry,
                    },
                    "failure": {
                        "failure_stage": "generate",
                        "failure_code": exc.code,
                        "failure_reason": exc.reason,
                        "next_step": exc.next_step,
                    },
                },
            },
        )
        raise

    merged_context_notes = _merge_context_notes(
        request.context_notes,
        str(inference_result.get("context_notes", "")).strip(),
    )
    generator_request = request.model_copy(
        update={
            "context_notes": merged_context_notes or request.context_notes,
        }
    )
    planning_summary = _build_planning_summary(
        plan=plan,
        request=request,
        normalized_focus_object_types=focus_object_types,
        related_objects=related_objects,
        reference_catalog=reference_catalog,
        prompt_explicit=prompt_explicit,
        llm_context_bundle=planner_context_bundle,
    )
    planning_summary["orchestration_mode"] = orchestration_mode
    chunk_plan = _build_chunk_plan(
        settings=settings,
        estimated_count=estimated_requested_records,
        object_type=str(plan.get("object_type", "")),
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
    if (
        orchestration_mode != "business_blueprint"
        and chunking_metadata["activated"]
        and chunking_metadata["exceeds_cap"]
    ):
        guidance = _build_chunk_split_guidance(
            total_chunks=chunking_metadata["total_chunks"],
            max_chunks=chunking_metadata["max_chunks"],
            chunk_size=chunking_metadata["chunk_size"],
            estimated_count=chunking_metadata["estimated_requested_records"],
        )
        chunking_metadata["final_status"] = "failed"
        chunking_metadata["abort_reason"] = (
            f"Estimated chunk count {chunking_metadata['total_chunks']} exceeds cap "
            f"{chunking_metadata['max_chunks']}."
        )
        failure_reason = (
            str(guidance[0].get("question", "")).strip()
            if isinstance(guidance, list) and guidance
            else (
                "Estimated chunk count exceeds the allowed cap; split the request into smaller batches."
            )
        )
        store.append_status(batch_id, "failed", failure_reason)
        store.update_batch(
            batch_id,
            {
                "status": "failed",
                "planning_summary": planning_summary,
                "metadata": {
                    "benchmark": {"enabled": benchmark_mode},
                    "planning": {
                        "planner_bypassed": planner_bypassed,
                        "mode": "deterministic" if planner_bypassed else "llm",
                        "estimated_requested_records": estimated_requested_records,
                        "force_wave_chunk_path": force_wave_chunk_path,
                        "force_wave_chunk_reason": force_wave_chunk_reason,
                    },
                    "inference_assumptions": inference_assumptions,
                    "base_object_match": base_object_match,
                    "inference_confidence": inference_confidence,
                    "orchestration": orchestration_metadata,
                    "chunking": chunking_metadata,
                    "llm_routes": llm_routes_metadata,
                    "llm_runtime": {
                        "planner": planner_telemetry,
                    },
                    "llm_context": {
                        "planner_profile": planner_context_bundle.get("profile"),
                        "planner_counts": planner_context_bundle.get("counts", {}),
                        "planner_limits": planner_context_bundle.get("limits", {}),
                    },
                    "context_notes": merged_context_notes or "",
                    "recent_batch_context": request.recent_batch_context,
                    "failure": {
                        "failure_stage": "generate",
                        "failure_code": "chunking_cap_exceeded",
                        "failure_reason": failure_reason,
                        "next_step": "Split this request into smaller batches and submit again.",
                    },
                },
            },
        )
        raise GenerateFailureError(
            code="chunking_cap_exceeded",
            reason=failure_reason,
            next_step="Split this request into smaller batches and submit again.",
        )

    store.append_status(batch_id, "schemas_selected", "Schemas selected from registry.")
    store.append_status(batch_id, "generating", "Generator call in progress.")
    single_item_compatibility_first = (
        int(chunking_metadata.get("estimated_requested_records", 1) or 1) == 1
        and not bool(chunking_metadata.get("activated"))
        and not force_wave_chunk_path
    )
    deterministic_failover_threshold = OBJECT_DETERMINISTIC_FAILOVER_THRESHOLD
    if force_wave_chunk_path:
        deterministic_failover_threshold = max(
            int(getattr(settings, "llm_wave_object_deterministic_failover_threshold", 3) or 3),
            OBJECT_DETERMINISTIC_FAILOVER_THRESHOLD,
        )
    chunking_metadata["forced_wave_chunk_path"] = force_wave_chunk_path
    chunking_metadata["forced_wave_chunk_reason"] = force_wave_chunk_reason
    chunking_metadata["deterministic_failover_threshold"] = deterministic_failover_threshold
    chunked_form_field_compatibility_first = (
        bool(chunking_metadata.get("activated"))
        and _force_compatibility_payload_shape(str(plan.get("object_type", "")))
    )
    chunked_ticket_fields_compatibility_only = (
        bool(chunking_metadata.get("activated"))
        and _normalize_object_type(str(plan.get("object_type", ""))) == "ticket_fields"
    )
    generator_mode_order = _generator_mode_order(
        compatibility_first=(
            single_item_compatibility_first
            or chunked_form_field_compatibility_first
        ),
        compatibility_only=chunked_ticket_fields_compatibility_only,
    )
    primary_generator_model = str(settings.llm_model_generator or "").strip()
    primary_generator_api_key = str(settings.xai_api_key or "").strip()
    generated_data: list[dict] = []
    generator_chunk_telemetry: list[dict] = []
    chunked_titles: list[str] = []
    chunked_title_set: set[str] = set()
    failure_count_by_object_type: dict[str, int] = {}
    forced_deterministic_object_types: set[str] = set()
    total_duplicates_dropped = 0
    total_generated_before_dedupe = 0
    total_pacing_wait_ms = 0.0
    try:
        if orchestration_mode == "business_blueprint":
            store.append_status(batch_id, "wave_execution", "Executing deterministic orchestration waves.")
            wave_buckets: dict[int, list[dict]] = {}
            for item in orchestration_backlog:
                wave = int(item.get("wave", _resolve_wave_for_object_type(str(item.get("object_type", "")))) or 1)
                wave_buckets.setdefault(wave, []).append(item)
            ordered_waves = sorted(wave_buckets.keys())
            total_wave_count = len(ordered_waves)
            chunk_targets_manifest: list[int] = []

            for wave_position, wave in enumerate(ordered_waves, start=1):
                wave_items = wave_buckets.get(wave, [])
                wave_meta = {
                    "wave": wave,
                    "position": wave_position,
                    "total_waves": total_wave_count,
                    "generator_model": _resolve_wave_generator_model(settings=settings, wave=int(wave)),
                    "status": "running",
                    "items": [],
                    "created": 0,
                    "reused": 0,
                    "updated": 0,
                    "blocked": 0,
                    "chunks": 0,
                    "generated_records": 0,
                    "deduped_records_added": 0,
                }
                orchestration_metadata.setdefault("waves", []).append(wave_meta)

                for item in wave_items:
                    object_type = _normalize_object_type(str(item.get("object_type", "triggers")))
                    target_count = max(int(item.get("target_count", 1) or 1), 1)
                    reconciliation = _reconcile_backlog_item(
                        item=item,
                        prompt=request.prompt,
                        dependency_mode=request.dependency_mode,
                        existing_index=existing_object_index,
                    )
                    reconcile_mode = str(reconciliation.get("mode", "create")).strip().lower()
                    if reconcile_mode in orchestration_metadata["reconciliation_summary"]:
                        orchestration_metadata["reconciliation_summary"][reconcile_mode] += 1

                    item_meta = {
                        "backlog_id": str(item.get("backlog_id", "")),
                        "object_type": object_type,
                        "target_count": target_count,
                        "generator_model": _resolve_wave_generator_model(settings=settings, wave=int(wave)),
                        "reconcile_mode": reconcile_mode,
                        "match_score": float(reconciliation.get("match_score", 0.0) or 0.0),
                        "match_source": str(reconciliation.get("match_source", "none")),
                        "base_object": reconciliation.get("base_object"),
                        "status": "running",
                        "chunks": 0,
                        "generated_records": 0,
                        "deduped_records_added": 0,
                    }
                    wave_meta["items"].append(item_meta)

                    if reconcile_mode == "blocked":
                        wave_meta["blocked"] += 1
                        item_meta["status"] = "failed"
                        raise GenerateFailureError(
                            code="wave_dependency_resolution_failed",
                            reason=str(
                                reconciliation.get("blocked_reason")
                                or f"Dependency resolution blocked for {object_type}."
                            ),
                            next_step=(
                                "Adjust dependency mode or provide missing existing objects in the "
                                "reference catalog before retrying."
                            ),
                        )

                    if reconcile_mode == "create":
                        wave_meta["created"] += 1
                    elif reconcile_mode == "reuse":
                        wave_meta["reused"] += 1
                    elif reconcile_mode == "update":
                        wave_meta["updated"] += 1

                    item_chunk_plan = _build_chunk_plan(
                        settings=settings,
                        estimated_count=target_count,
                        object_type=object_type,
                    )
                    if item_chunk_plan.get("exceeds_cap"):
                        raise GenerateFailureError(
                            code="wave_generation_failed",
                            reason=(
                                f"Wave item {item_meta['backlog_id']} ({object_type}) requires "
                                f"{item_chunk_plan.get('total_chunks')} chunks, which exceeds cap "
                                f"{item_chunk_plan.get('max_chunks')}."
                            ),
                            next_step=(
                                "Reduce requested record count for this object type and retry "
                                "generation."
                            ),
                        )
                    item_chunk_targets = (
                        list(item_chunk_plan.get("chunk_targets", []) or [])
                        if bool(item_chunk_plan.get("activated"))
                        else [target_count]
                    )
                    if not item_chunk_targets:
                        item_chunk_targets = [target_count]

                    item_plan = dict(plan)
                    item_plan["object_type"] = object_type
                    item_plan["intent"] = _build_wave_prompt(
                        prompt=request.prompt,
                        item=item,
                        reconciliation=reconciliation,
                    )
                    item_plan["dependency_notes"] = (
                        f"Business-blueprint wave execution. backlog_id={item_meta['backlog_id']}; "
                        f"wave={wave}; reconcile_mode={reconcile_mode}."
                    )
                    compatibility_first_for_item = (
                        (
                            _force_compatibility_payload_shape(object_type)
                            or _is_wave3_rule_object_type(object_type)
                        )
                        and len(item_chunk_targets) > 0
                    )
                    compatibility_only_for_item = (
                        (object_type == "ticket_fields" and len(item_chunk_targets) > 1)
                        or _is_wave3_rule_object_type(object_type)
                    )
                    item_mode_order = _generator_mode_order(
                        compatibility_first=compatibility_first_for_item or target_count == 1,
                        compatibility_only=compatibility_only_for_item,
                    )
                    item_generator_model = _resolve_wave_generator_model(
                        settings=settings,
                        wave=int(wave),
                    )
                    item_generator_api_key = _resolve_wave_api_key(
                        settings=settings,
                        wave=int(wave),
                    )

                    for local_chunk_index, item_chunk_target in enumerate(item_chunk_targets, start=1):
                        model_hint = (
                            f", model={item_generator_model}"
                            if item_generator_model and item_generator_model != primary_generator_model
                            else ""
                        )
                        if chunk_targets_manifest:
                            wait_seconds = max(settings.llm_auto_chunk_pacing_seconds, 0.0)
                            if settings.llm_auto_chunk_pacing_jitter_seconds > 0:
                                wait_seconds += random.uniform(0.0, settings.llm_auto_chunk_pacing_jitter_seconds)
                            if wait_seconds > 0:
                                store.append_status(
                                    batch_id,
                                    "generating",
                                    (
                                        f"Pacing before wave {wave_position}/{total_wave_count} "
                                        f"{object_type} chunk {local_chunk_index}/{len(item_chunk_targets)} "
                                        f"({wait_seconds:.2f}s)."
                                    ),
                                )
                                await asyncio.sleep(wait_seconds)
                                total_pacing_wait_ms += wait_seconds * 1000.0

                        store.append_status(
                            batch_id,
                            "generating",
                            (
                                f"Wave {wave_position}/{total_wave_count} {object_type} "
                                f"chunk {local_chunk_index}/{len(item_chunk_targets)} "
                                f"(target={int(item_chunk_target)}{model_hint})."
                            ),
                        )

                        chunk_instruction = _build_chunk_instruction(
                            chunk_index=local_chunk_index,
                            chunk_total=len(item_chunk_targets),
                            target_count=int(item_chunk_target),
                        )
                        used_deterministic_fallback = False
                        forced_deterministic_for_chunk = False
                        forced_reason: str | None = None
                        wave_routes = _build_wave_generator_routes(
                            settings=settings,
                            wave=int(wave),
                        )
                        if not wave_routes:
                            raise GenerateFailureError(
                                code="wave_generation_failed",
                                reason=(
                                    f"No API key/model route is configured for wave {wave_position}/"
                                    f"{total_wave_count} ({object_type})."
                                ),
                                next_step=(
                                    "Configure at least one generator model/API key route for this wave and retry."
                                ),
                            )
                        active_route = dict(wave_routes[0])
                        selected_model_for_chunk = active_route.get("model")
                        selected_api_key_for_chunk = active_route.get("api_key")
                        selected_api_key_profile = str(active_route.get("profile", "primary")).strip() or "primary"
                        chunk_route_failovers: list[dict[str, str]] = []
                        chunk_error: RuntimeError | None = None
                        fallback_reason: str | None = None
                        chunk_rows: list[dict] = []
                        chunk_runtime_metrics: dict = {}
                        context_profile = "standard"
                        if object_type in forced_deterministic_object_types:
                            forced_deterministic_for_chunk = True
                            forced_count = int(failure_count_by_object_type.get(object_type, 0) or 0)
                            forced_reason = (
                                f"Object type '{object_type}' forced to deterministic mode after "
                                f"{forced_count} generation failures in this run."
                            )
                            chunk_rows = _build_deterministic_chunk_rows(
                                object_type=object_type,
                                target_count=int(item_chunk_target),
                                prompt=request.prompt,
                                reference_catalog=reference_catalog,
                                existing_titles=chunked_titles[-200:],
                                generated_rows=generated_data,
                                reason=forced_reason,
                            )
                            if not chunk_rows:
                                raise GenerateFailureError(
                                    code="wave_generation_failed",
                                    reason=(
                                        f"Deterministic failover could not build valid {object_type} rows for "
                                        f"wave {wave_position}/{total_wave_count} chunk "
                                        f"{local_chunk_index}/{len(item_chunk_targets)}."
                                    ),
                                    next_step=(
                                        "Retry with narrower per-object scope and explicit action/condition hints "
                                        "for this object type."
                                    ),
                                )
                            used_deterministic_fallback = True
                            fallback_reason = forced_reason
                            context_profile = "forced_deterministic"
                            chunk_runtime_metrics = {
                                "pre_request_wait_ms": 0.0,
                                "retry_count": 0,
                                "final_status": "forced_deterministic",
                                "http_status": None,
                                "model": "deterministic_fallback",
                            }
                        else:
                            try:
                                chunk_rows, chunk_runtime_metrics, context_profile = await _run_generator_with_context_fallback(
                                    plan=item_plan,
                                    request=generator_request,
                                    focus_object_types=focus_object_types,
                                    standard_context_bundle=generator_context_bundle,
                                    aggressive_context_bundle=llm_context_aggressive,
                                    chunk_instruction=chunk_instruction,
                                    chunk_target_count=int(item_chunk_target),
                                    chunk_index=local_chunk_index,
                                    chunk_total=len(item_chunk_targets),
                                    existing_titles=chunked_titles[-200:],
                                    compatibility_first=compatibility_first_for_item,
                                    compatibility_only=compatibility_only_for_item,
                                    model_override=selected_model_for_chunk,
                                    api_key_override=selected_api_key_for_chunk,
                                )
                                failure_count_by_object_type[object_type] = 0
                            except RuntimeError as chunk_exc:
                                chunk_error = chunk_exc
                        if chunk_error is not None and _is_rate_limited_error(chunk_error):
                            for fallback_route in wave_routes[1:]:
                                fallback_profile = (
                                    str(fallback_route.get("profile", "")).strip() or "fallback"
                                )
                                fallback_model = str(fallback_route.get("model", "")).strip()
                                fallback_key = str(fallback_route.get("api_key", "")).strip()
                                if not fallback_model or not fallback_key:
                                    continue
                                if (
                                    fallback_model == str(selected_model_for_chunk or "").strip()
                                    and fallback_key == str(selected_api_key_for_chunk or "").strip()
                                ):
                                    continue
                                store.append_status(
                                    batch_id,
                                    "generating",
                                    (
                                        f"Wave {wave_position}/{total_wave_count} {object_type} "
                                        f"chunk {local_chunk_index}/{len(item_chunk_targets)} "
                                        f"rate-limited on {selected_api_key_profile}; switching to "
                                        f"{fallback_profile} route."
                                    ),
                                )
                                chunk_route_failovers.append(
                                    {
                                        "from_profile": selected_api_key_profile,
                                        "to_profile": fallback_profile,
                                        "from_model": str(selected_model_for_chunk or ""),
                                        "to_model": fallback_model,
                                    }
                                )
                                selected_model_for_chunk = fallback_model
                                selected_api_key_for_chunk = fallback_key
                                selected_api_key_profile = fallback_profile
                                try:
                                    chunk_rows, chunk_runtime_metrics, context_profile = await _run_generator_with_context_fallback(
                                        plan=item_plan,
                                        request=generator_request,
                                        focus_object_types=focus_object_types,
                                        standard_context_bundle=generator_context_bundle,
                                        aggressive_context_bundle=llm_context_aggressive,
                                        chunk_instruction=chunk_instruction,
                                        chunk_target_count=int(item_chunk_target),
                                        chunk_index=local_chunk_index,
                                        chunk_total=len(item_chunk_targets),
                                        existing_titles=chunked_titles[-200:],
                                        compatibility_first=compatibility_first_for_item,
                                        compatibility_only=compatibility_only_for_item,
                                        model_override=selected_model_for_chunk,
                                        api_key_override=selected_api_key_for_chunk,
                                    )
                                    chunk_error = None
                                    failure_count_by_object_type[object_type] = 0
                                    chunk_runtime_metrics = dict(chunk_runtime_metrics or {})
                                    chunk_runtime_metrics["route_failover"] = True
                                    break
                                except RuntimeError as route_chunk_exc:
                                    chunk_error = route_chunk_exc
                                    if not _is_rate_limited_error(chunk_error):
                                        break
                        if chunk_error is not None:
                            object_failure_count = int(failure_count_by_object_type.get(object_type, 0) or 0) + 1
                            failure_count_by_object_type[object_type] = object_failure_count
                            if object_failure_count >= deterministic_failover_threshold:
                                if object_type not in forced_deterministic_object_types:
                                    forced_deterministic_object_types.add(object_type)
                                    store.append_status(
                                        batch_id,
                                        "generating",
                                        (
                                            f"Object-level failover activated for {object_type}: "
                                            f"{object_failure_count} generation failures in this run. "
                                            "Remaining chunks will use deterministic synthesis."
                                        ),
                                    )
                            if _can_use_deterministic_chunk_fallback(object_type):
                                chunk_rows = _build_deterministic_chunk_rows(
                                    object_type=object_type,
                                    target_count=int(item_chunk_target),
                                    prompt=request.prompt,
                                    reference_catalog=reference_catalog,
                                    existing_titles=chunked_titles[-200:],
                                    generated_rows=generated_data,
                                    reason=str(chunk_error),
                                )
                                if not chunk_rows:
                                    raise GenerateFailureError(
                                        code="wave_generation_failed",
                                        reason=(
                                            f"Deterministic fallback could not build valid {object_type} rows for "
                                            f"wave {wave_position}/{total_wave_count} chunk "
                                            f"{local_chunk_index}/{len(item_chunk_targets)}."
                                        ),
                                        next_step=(
                                            "Retry with narrower per-object scope and explicit action/condition hints "
                                            "for this rule object."
                                        ),
                                    ) from chunk_error
                                used_deterministic_fallback = True
                                fallback_reason = str(chunk_error)
                                context_profile = "deterministic_fallback"
                                chunk_runtime_metrics = {
                                    "pre_request_wait_ms": 0.0,
                                    "retry_count": 0,
                                    "final_status": "fallback",
                                    "http_status": None,
                                    "model": "deterministic_fallback",
                                }
                                store.append_status(
                                    batch_id,
                                    "generating",
                                    (
                                        f"Wave {wave_position}/{total_wave_count} {object_type} "
                                        f"chunk {local_chunk_index}/{len(item_chunk_targets)} used deterministic "
                                        "fallback record synthesis."
                                    ),
                                )
                            else:
                                raise chunk_error
                        object_failure_count_for_chunk = int(failure_count_by_object_type.get(object_type, 0) or 0)
                        if context_profile == "aggressive":
                            store.append_status(
                                batch_id,
                                "generating",
                                (
                                    f"Wave {wave_position}/{total_wave_count} {object_type} "
                                    f"chunk {local_chunk_index}/{len(item_chunk_targets)} used compact context."
                                ),
                            )
                        topup_reason = (
                            "Model returned fewer rows than chunk target; deterministic top-up applied."
                        )
                        chunk_rows, deterministic_topup_count = _supplement_chunk_rows_to_target(
                            object_type=object_type,
                            target_count=int(item_chunk_target),
                            chunk_rows=chunk_rows,
                            prompt=request.prompt,
                            reference_catalog=reference_catalog,
                            existing_titles=chunked_titles[-200:],
                            generated_rows=generated_data,
                            reason=topup_reason,
                        )
                        if deterministic_topup_count > 0:
                            used_deterministic_fallback = True
                            if fallback_reason:
                                fallback_reason = f"{fallback_reason} | {topup_reason}"
                            else:
                                fallback_reason = topup_reason
                            store.append_status(
                                batch_id,
                                "generating",
                                (
                                    f"Wave {wave_position}/{total_wave_count} {object_type} "
                                    f"chunk {local_chunk_index}/{len(item_chunk_targets)} topped up "
                                    f"{deterministic_topup_count} record(s) deterministically."
                                ),
                            )
                        chunk_rows, mismatched_object_count = _enforce_expected_object_type(
                            rows=chunk_rows,
                            expected_object_type=object_type,
                        )
                        if mismatched_object_count > 0:
                            mismatch_reason = (
                                f"Filtered {mismatched_object_count} row(s) with wrong object_type for "
                                f"{object_type} wave chunk."
                            )
                            store.append_status(
                                batch_id,
                                "generating",
                                (
                                    f"Wave {wave_position}/{total_wave_count} {object_type} "
                                    f"chunk {local_chunk_index}/{len(item_chunk_targets)} dropped "
                                    f"{mismatched_object_count} mismatched row(s)."
                                ),
                            )
                            chunk_rows, alignment_topup_count = _supplement_chunk_rows_to_target(
                                object_type=object_type,
                                target_count=int(item_chunk_target),
                                chunk_rows=chunk_rows,
                                prompt=request.prompt,
                                reference_catalog=reference_catalog,
                                existing_titles=chunked_titles[-200:],
                                generated_rows=generated_data,
                                reason=mismatch_reason,
                            )
                            if alignment_topup_count > 0:
                                used_deterministic_fallback = True
                                deterministic_topup_count += alignment_topup_count
                                if fallback_reason:
                                    fallback_reason = f"{fallback_reason} | {mismatch_reason}"
                                else:
                                    fallback_reason = mismatch_reason

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

                        chunk_targets_manifest.append(int(item_chunk_target))
                        wave_meta["chunks"] += 1
                        wave_meta["generated_records"] += raw_chunk_count
                        wave_meta["deduped_records_added"] += added_after_dedupe
                        item_meta["chunks"] += 1
                        item_meta["generated_records"] += raw_chunk_count
                        item_meta["deduped_records_added"] += added_after_dedupe

                        chunk_entry = {
                            "chunk_index": len(chunk_targets_manifest),
                            "chunk_total": len(chunk_targets_manifest),
                            "target_records": int(item_chunk_target),
                            "generated_records": raw_chunk_count,
                            "deduped_records_added": added_after_dedupe,
                            "duplicates_dropped": dropped,
                            "context_profile": context_profile,
                            "pre_request_wait_ms": chunk_runtime_metrics.get("pre_request_wait_ms"),
                            "retry_count": chunk_runtime_metrics.get("retry_count"),
                            "final_status": chunk_runtime_metrics.get("final_status"),
                            "http_status": chunk_runtime_metrics.get("http_status"),
                            "model": chunk_runtime_metrics.get("model"),
                            "model_requested": selected_model_for_chunk,
                            "model_fallback_from": chunk_runtime_metrics.get("model_fallback_from"),
                            "api_key_profile": selected_api_key_profile,
                            "key_route_failovers": chunk_route_failovers,
                            "mode_order": item_mode_order,
                            "wave": wave,
                            "object_type": object_type,
                            "backlog_id": item_meta["backlog_id"],
                            "reconcile_mode": reconcile_mode,
                            "deterministic_fallback": used_deterministic_fallback,
                            "forced_deterministic": forced_deterministic_for_chunk,
                            "object_failure_count_in_run": object_failure_count_for_chunk,
                            "forced_reason": forced_reason,
                            "fallback_reason": fallback_reason,
                            "deterministic_topup_records": deterministic_topup_count,
                            "mismatched_object_rows_dropped": mismatched_object_count,
                        }
                        generator_chunk_telemetry.append(chunk_entry)
                    item_meta["status"] = "completed"
                wave_meta["status"] = "completed"

            chunking_metadata["activated"] = bool(len(chunk_targets_manifest) > 1)
            chunking_metadata["chunk_targets"] = chunk_targets_manifest or [max(1, estimated_requested_records)]
            chunking_metadata["total_chunks"] = len(chunking_metadata["chunk_targets"])
            chunking_metadata["estimated_requested_records"] = max(
                int(estimated_requested_records or 1),
                sum(int(item.get("target_count", 1) or 1) for item in orchestration_backlog),
            )
            chunking_metadata["exceeds_cap"] = False
        elif chunking_metadata["activated"]:
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
                used_deterministic_fallback = False
                forced_deterministic_for_chunk = False
                forced_reason: str | None = None
                fallback_reason: str | None = None
                current_object_type = _normalize_object_type(str(plan.get("object_type", "")))
                chunk_rows: list[dict] = []
                chunk_runtime_metrics: dict = {}
                context_profile = "standard"
                if current_object_type in forced_deterministic_object_types:
                    forced_deterministic_for_chunk = True
                    forced_count = int(failure_count_by_object_type.get(current_object_type, 0) or 0)
                    forced_reason = (
                        f"Object type '{current_object_type}' forced to deterministic mode after "
                        f"{forced_count} generation failures in this run."
                    )
                    chunk_rows = _build_deterministic_chunk_rows(
                        object_type=current_object_type,
                        target_count=int(target_count),
                        prompt=request.prompt,
                        reference_catalog=reference_catalog,
                        existing_titles=chunked_titles[-200:],
                        generated_rows=generated_data,
                        reason=forced_reason,
                    )
                    if not chunk_rows:
                        raise GenerateFailureError(
                            code="chunk_generation_failed",
                            reason=(
                                f"Deterministic failover could not build valid {current_object_type} rows "
                                f"for chunk {index}/{len(chunk_targets)}."
                            ),
                            next_step=(
                                "Retry with narrower scope and explicit action/condition hints for this object "
                                "type."
                            ),
                        )
                    used_deterministic_fallback = True
                    fallback_reason = forced_reason
                    context_profile = "forced_deterministic"
                    chunk_runtime_metrics = {
                        "pre_request_wait_ms": 0.0,
                        "retry_count": 0,
                        "final_status": "forced_deterministic",
                        "http_status": None,
                        "model": "deterministic_fallback",
                    }
                else:
                    try:
                        chunk_rows, chunk_runtime_metrics, context_profile = await _run_generator_with_context_fallback(
                            plan=plan,
                            request=generator_request,
                            focus_object_types=focus_object_types,
                            standard_context_bundle=generator_context_bundle,
                            aggressive_context_bundle=llm_context_aggressive,
                            chunk_instruction=chunk_instruction,
                            chunk_target_count=int(target_count),
                            chunk_index=index,
                            chunk_total=len(chunk_targets),
                            existing_titles=chunked_titles[-200:],
                            compatibility_first=chunked_form_field_compatibility_first,
                            compatibility_only=chunked_ticket_fields_compatibility_only,
                        )
                        failure_count_by_object_type[current_object_type] = 0
                    except RuntimeError as chunk_exc:
                        object_failure_count = int(
                            failure_count_by_object_type.get(current_object_type, 0) or 0
                        ) + 1
                        failure_count_by_object_type[current_object_type] = object_failure_count
                        if object_failure_count >= deterministic_failover_threshold:
                            if current_object_type not in forced_deterministic_object_types:
                                forced_deterministic_object_types.add(current_object_type)
                                store.append_status(
                                    batch_id,
                                    "generating",
                                    (
                                        f"Object-level failover activated for {current_object_type}: "
                                        f"{object_failure_count} generation failures in this run. "
                                        "Remaining chunks will use deterministic synthesis."
                                    ),
                                )
                        if _can_use_deterministic_chunk_fallback(current_object_type) and (
                            _is_deterministic_llm_error(chunk_exc)
                            or _is_rate_limited_error(chunk_exc)
                            or _is_schema_validation_failure(chunk_exc)
                        ):
                            chunk_rows = _build_deterministic_chunk_rows(
                                object_type=current_object_type,
                                target_count=int(target_count),
                                prompt=request.prompt,
                                reference_catalog=reference_catalog,
                                existing_titles=chunked_titles[-200:],
                                generated_rows=generated_data,
                                reason=str(chunk_exc),
                            )
                            if not chunk_rows:
                                raise GenerateFailureError(
                                    code="chunk_generation_failed",
                                    reason=(
                                        f"Deterministic fallback could not build valid {current_object_type} rows "
                                        f"for chunk {index}/{len(chunk_targets)}."
                                    ),
                                    next_step=(
                                        "Retry with narrower scope and explicit action/condition hints for this object "
                                        "type."
                                    ),
                                ) from chunk_exc
                            used_deterministic_fallback = True
                            fallback_reason = str(chunk_exc)
                            context_profile = "deterministic_fallback"
                            chunk_runtime_metrics = {
                                "pre_request_wait_ms": 0.0,
                                "retry_count": 0,
                                "final_status": "fallback",
                                "http_status": None,
                                "model": "deterministic_fallback",
                            }
                            store.append_status(
                                batch_id,
                                "generating",
                                f"Chunk {index}/{len(chunk_targets)} used deterministic fallback record synthesis.",
                            )
                        else:
                            raise
                object_failure_count_for_chunk = int(
                    failure_count_by_object_type.get(current_object_type, 0) or 0
                )
                if context_profile == "aggressive":
                    store.append_status(
                        batch_id,
                        "generating",
                        f"Chunk {index}/{len(chunk_targets)} used compact context profile.",
                    )
                topup_reason = (
                    "Model returned fewer rows than chunk target; deterministic top-up applied."
                )
                chunk_rows, deterministic_topup_count = _supplement_chunk_rows_to_target(
                    object_type=current_object_type,
                    target_count=int(target_count),
                    chunk_rows=chunk_rows,
                    prompt=request.prompt,
                    reference_catalog=reference_catalog,
                    existing_titles=chunked_titles[-200:],
                    generated_rows=generated_data,
                    reason=topup_reason,
                )
                if deterministic_topup_count > 0:
                    used_deterministic_fallback = True
                    if fallback_reason:
                        fallback_reason = f"{fallback_reason} | {topup_reason}"
                    else:
                        fallback_reason = topup_reason
                    store.append_status(
                        batch_id,
                        "generating",
                        (
                            f"Chunk {index}/{len(chunk_targets)} topped up "
                            f"{deterministic_topup_count} record(s) deterministically."
                        ),
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
                    "mode_order": generator_mode_order,
                    "deterministic_fallback": used_deterministic_fallback,
                    "forced_deterministic": forced_deterministic_for_chunk,
                    "object_failure_count_in_run": object_failure_count_for_chunk,
                    "forced_reason": forced_reason,
                    "fallback_reason": fallback_reason,
                    "deterministic_topup_records": deterministic_topup_count,
                    "object_type": _normalize_object_type(str(plan.get("object_type", ""))),
                    "wave": _resolve_wave_for_object_type(str(plan.get("object_type", ""))),
                }
                generator_chunk_telemetry.append(chunk_entry)
        else:
            generated_data, generator_telemetry, context_profile = await _run_generator_with_context_fallback(
                plan=plan,
                request=generator_request,
                focus_object_types=focus_object_types,
                standard_context_bundle=generator_context_bundle,
                aggressive_context_bundle=llm_context_aggressive,
                chunk_instruction=None,
                chunk_target_count=None,
                chunk_index=None,
                chunk_total=None,
                existing_titles=None,
                compatibility_first=single_item_compatibility_first,
                compatibility_only=False,
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
                    "mode_order": generator_mode_order,
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
        chunking_metadata["forced_deterministic_object_types"] = sorted(
            forced_deterministic_object_types
        )
        chunking_metadata["object_failure_counts"] = dict(failure_count_by_object_type)
        if orchestration_mode == "business_blueprint":
            orchestration_metadata["final_status"] = "failed"
            orchestration_metadata["abort_reason"] = str(exc)
        runtime_metrics = GrokClient.get_last_call_metrics("generator")
        generator_error_metadata: dict = {}
        default_failure_code = (
            "wave_generation_failed"
            if orchestration_mode == "business_blueprint"
            else "chunk_generation_failed"
        )
        failure_code = default_failure_code
        failure_message = str(exc)
        next_step_message = (
            "Retry generation after resolving the failed wave item or reduce per-object scope."
            if orchestration_mode == "business_blueprint"
            else "Retry generation or reduce request scope/chunk size."
        )
        status_message_prefix = (
            "Wave execution failed"
            if orchestration_mode == "business_blueprint"
            else (
                "Generation failed during chunking"
                if chunking_metadata.get("activated")
                else "Generation failed"
            )
        )
        if isinstance(exc, GenerateFailureError):
            failure_code = exc.code or default_failure_code
            failure_message = exc.reason or str(exc)
            next_step_message = exc.next_step or next_step_message
        if isinstance(exc, GeneratorStructuredOutputError):
            error_meta = exc.as_metadata()
            generator_error_metadata = error_meta
        terminal_error_class = _extract_terminal_error_class(
            exc=exc,
            runtime_metrics=runtime_metrics,
            generator_error_metadata=generator_error_metadata,
        )
        if not isinstance(exc, GenerateFailureError):
            if terminal_error_class == "rate_limited":
                failure_code = "rate_limited"
                failure_message = "Generator request was rate-limited before a valid payload was produced."
                next_step_message = (
                    "Wait for the provider rate-limit window to reset, then retry this request. "
                    "If this keeps happening, reduce request frequency or batch size."
                )
                status_message_prefix = "Generation rate limited"
            elif terminal_error_class == "unsupported_response_format":
                failure_code = "unsupported_response_format"
                failure_message = "Generator response format is unsupported for the current model profile."
                next_step_message = (
                    "Retry with the compatibility profile or adjust model/response format settings."
                )
                status_message_prefix = "Generator response format unsupported"
            elif terminal_error_class == "schema_validation_failure":
                failure_code = "generator_json_validation_failed"
                failure_message = (
                    "Generator could not produce schema-valid JSON after recovery attempts."
                )
                next_step_message = (
                    "Retry with narrower scope and explicit Zendesk action fields "
                    "(status/group_id/set_tags/field_type/custom_field_options)."
                )
                status_message_prefix = "Generator JSON validation failed"
                if isinstance(exc, GeneratorStructuredOutputError) and not benchmark_mode and exc.corrective_example:
                    next_step_message += f" Example: {exc.corrective_example}"
            elif isinstance(exc, GeneratorStructuredOutputError):
                failure_code = "generator_json_validation_failed"
                failure_message = (
                    "Generator could not produce schema-valid JSON after recovery attempts."
                )
                next_step_message = (
                    "Retry with narrower scope and explicit Zendesk action fields "
                    "(status/group_id/set_tags/field_type/custom_field_options)."
                )
                status_message_prefix = "Generator JSON validation failed"
                if not benchmark_mode and exc.corrective_example:
                    next_step_message += f" Example: {exc.corrective_example}"
        store.append_status(batch_id, "failed", f"{status_message_prefix}: {exc}")
        store.update_batch(
            batch_id,
            {
                "planning_summary": planning_summary,
                "metadata": {
                    "benchmark": {
                        "enabled": benchmark_mode,
                    },
                    "planning": {
                        "planner_bypassed": planner_bypassed,
                        "mode": "deterministic" if planner_bypassed else "llm",
                        "estimated_requested_records": estimated_requested_records,
                        "force_wave_chunk_path": force_wave_chunk_path,
                        "force_wave_chunk_reason": force_wave_chunk_reason,
                    },
                    "inference_assumptions": inference_assumptions,
                    "base_object_match": base_object_match,
                    "inference_confidence": inference_confidence,
                    "orchestration": orchestration_metadata,
                    "chunking": chunking_metadata,
                    "llm_routes": llm_routes_metadata,
                    "llm_runtime": {
                        "planner": planner_telemetry,
                        "generator": runtime_metrics,
                        "generator_chunks": generator_chunk_telemetry,
                        "generator_error": generator_error_metadata,
                        "terminal_error_class": terminal_error_class,
                    },
                    "context_notes": merged_context_notes or "",
                    "recent_batch_context": request.recent_batch_context,
                    "failure": {
                        "failure_stage": "generate",
                        "failure_code": failure_code,
                        "failure_reason": failure_message,
                        "next_step": next_step_message,
                    },
                },
            },
        )
        raise GenerateFailureError(
            code=failure_code,
            reason=failure_message,
            next_step=next_step_message,
        ) from exc

    if generator_chunk_telemetry:
        total_chunk_entries = len(generator_chunk_telemetry)
        for idx, entry in enumerate(generator_chunk_telemetry, start=1):
            if isinstance(entry, dict):
                entry["chunk_index"] = idx
                entry["chunk_total"] = total_chunk_entries

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
    chunking_metadata["forced_deterministic_object_types"] = sorted(
        forced_deterministic_object_types
    )
    chunking_metadata["object_failure_counts"] = dict(failure_count_by_object_type)
    if orchestration_mode == "business_blueprint":
        orchestration_metadata["final_status"] = "ok"
        orchestration_metadata["generated_records"] = len(generated_data)
    generator_telemetry = GrokClient.get_last_call_metrics("generator")
    store.append_status(batch_id, "generated", "Structured records generated.")
    dependency_resolution: dict = {}
    focus_diagnostics: dict = {}
    duplicate_candidates: list[dict] = []
    generation_safety: dict = {}
    preview_records: list[dict] = []
    validation_summary = ValidationSummary(passed=0, warnings=0, blocked=0)
    generated_counts: dict[str, int] = {}
    staging_metadata: dict = {}
    validation_metadata: dict = {}
    appscript_preview_metadata: dict = {}
    field_inference_metadata: dict = {}
    form_field_resolution_metadata: dict = {}
    canonicalization_metadata: dict = {}

    def _build_shared_metadata() -> dict:
        return {
            "benchmark": {
                "enabled": benchmark_mode,
            },
            "planning": {
                "planner_bypassed": planner_bypassed,
                "mode": "deterministic" if planner_bypassed else "llm",
                "estimated_requested_records": estimated_requested_records,
                "force_wave_chunk_path": force_wave_chunk_path,
                "force_wave_chunk_reason": force_wave_chunk_reason,
            },
            "orchestration": orchestration_metadata,
            "chunking": chunking_metadata,
            "llm_routes": llm_routes_metadata,
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
            "inference_assumptions": inference_assumptions,
            "base_object_match": base_object_match,
            "inference_confidence": inference_confidence,
            "context_notes": merged_context_notes or "",
            "recent_batch_context": request.recent_batch_context,
            "ambiguity_score": ambiguity_score,
            "ambiguity_threshold": ambiguity_threshold,
        }

    def _raise_post_generation_failure(
        *,
        failure_code: str,
        failure_reason: str,
        next_step: str,
        stage_status_message: str,
    ) -> None:
        store.append_status(batch_id, "failed", stage_status_message)
        shared_metadata = _build_shared_metadata()
        store.update_batch(
            batch_id,
            {
                "status": "failed",
                "planning_summary": planning_summary,
                "metadata": {
                    **shared_metadata,
                    "validation_phase_status": "failed",
                    "dependency_resolution": dependency_resolution,
                    "focus_violations": focus_diagnostics.get("mismatches", []),
                    "focus_diagnostics": focus_diagnostics,
                    "duplicate_candidates": duplicate_candidates,
                    "generation_safety": generation_safety,
                    "field_inference": field_inference_metadata,
                    "form_field_resolution": form_field_resolution_metadata,
                    "canonicalization": canonicalization_metadata,
                    "staging": staging_metadata,
                    "validation": validation_metadata,
                    "preview_roundtrip": appscript_preview_metadata,
                    "failure": {
                        "failure_stage": "generate",
                        "failure_code": failure_code,
                        "failure_reason": failure_reason,
                        "next_step": next_step,
                    },
                },
            },
        )
        raise GenerateFailureError(
            code=failure_code,
            reason=failure_reason,
            next_step=next_step,
        )

    post_generation_stage = "canonicalization"
    try:
        generated_data, field_inference_metadata, form_field_resolution_metadata, canonicalization_metadata = _canonicalize_generated_rows(
            rows=generated_data,
            prompt=request.prompt,
            reference_catalog=reference_catalog,
            existing_index=existing_object_index,
            related_lookup=related_lookup,
            catalog_lookup=catalog_lookup,
            settings=settings,
        )

        post_generation_stage = "dependency_resolution"
        generated_data, dependency_resolution = _apply_dependency_resolution(
            generated_data,
            related_lookup=related_lookup,
            catalog_lookup=catalog_lookup,
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

        post_generation_stage = "preview"
        preview_records, validation_summary = _build_preview_records(plan, generated_data)
        preview_records = _apply_generation_safety_to_preview(preview_records, generation_safety)
        preview_records = _apply_inference_assumptions_to_preview(preview_records, inference_assumptions)
        validation_summary = _recompute_validation_summary(preview_records)
        generated_counts = _count_generated(preview_records)

        post_generation_stage = "staging"
        store.append_status(batch_id, "staging", "Staging batch to Google Sheets.")
        appscript_payload = {
            "batch_id": batch_id,
            "prompt": request.prompt,
            "requester": request.requester,
            "status": "staging",
            "target_environment": request.target_environment,
            "created_at": created_at,
            "planning_summary": planning_summary,
            "records": preview_records,
        }
        use_legacy_roundtrip = True
        if appscript.enabled:
            combined_roundtrip_raw = await appscript.invoke(
                action="stage_validate_preview",
                payload=appscript_payload,
            )
            combined_roundtrip = _normalize_appscript_action_result(
                combined_roundtrip_raw,
                action="stage_validate_preview",
            )
            if combined_roundtrip.get("status") == "ok":
                use_legacy_roundtrip = False
                combined_data = combined_roundtrip.get("data", {})
                stage_result = (
                    combined_data.get("staging", {})
                    if isinstance(combined_data.get("staging"), dict)
                    else {}
                )
                validation_result = (
                    combined_data.get("validation", {})
                    if isinstance(combined_data.get("validation"), dict)
                    else {}
                )
                preview_result = (
                    combined_data.get("preview", {})
                    if isinstance(combined_data.get("preview"), dict)
                    else {}
                )
                summary_result = (
                    combined_data.get("summary", {})
                    if isinstance(combined_data.get("summary"), dict)
                    else {}
                )
                staging_metadata = {
                    "mode": "appscript_combined",
                    "status": combined_roundtrip.get("status"),
                    "detail": combined_roundtrip.get("detail"),
                    "http_status": combined_roundtrip.get("http_status"),
                    "result": stage_result,
                    "summary": summary_result,
                }
                validation_metadata = {
                    "mode": "appscript_combined",
                    "status": combined_roundtrip.get("status"),
                    "detail": combined_roundtrip.get("detail"),
                    "http_status": combined_roundtrip.get("http_status"),
                    "result": validation_result,
                    "summary": summary_result.get("validation_summary", {}),
                }
                appscript_preview_metadata = {
                    "mode": "appscript_combined",
                    "status": combined_roundtrip.get("status"),
                    "detail": combined_roundtrip.get("detail"),
                    "http_status": combined_roundtrip.get("http_status"),
                    "result": {
                        "status": preview_result.get("status"),
                        "validation_summary": preview_result.get("validation_summary", {}),
                        "generated_counts": preview_result.get("generated_counts", {}),
                    },
                }
                store.append_status(
                    batch_id,
                    "staged",
                    "Batch staged via combined Apps Script pipeline.",
                )
                post_generation_stage = "validation"
                store.append_status(
                    batch_id,
                    "validating",
                    "Running row-level validation via combined Apps Script pipeline.",
                )
                summary_candidates: list[dict] = []
                for candidate in (
                    validation_result.get("summary"),
                    summary_result.get("validation_summary"),
                    preview_result.get("validation_summary"),
                ):
                    if isinstance(candidate, dict):
                        summary_candidates.append(candidate)
                for summary_data in summary_candidates:
                    try:
                        validation_summary = ValidationSummary(**summary_data)
                        break
                    except Exception:
                        continue
            else:
                store.append_status(
                    batch_id,
                    "staging",
                    "Combined Apps Script pipeline unavailable; using legacy staging/validation/preview actions.",
                )

        if use_legacy_roundtrip:
            if appscript.enabled:
                appscript_stage_raw = await appscript.invoke(
                    action="write_batch_to_sheets",
                    payload=appscript_payload,
                )
                appscript_stage = _normalize_appscript_action_result(
                    appscript_stage_raw,
                    action="write_batch_to_sheets",
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

            post_generation_stage = "validation"
            store.append_status(batch_id, "validating", "Running row-level validation.")
            if appscript.enabled:
                appscript_validation_raw = await appscript.invoke(
                    action="validate_batch",
                    payload={"batch_id": batch_id},
                )
                appscript_validation = _normalize_appscript_action_result(
                    appscript_validation_raw,
                    action="validate_batch",
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

            if appscript.enabled:
                appscript_preview_raw = await appscript.invoke(
                    action="get_batch_preview",
                    payload={"batch_id": batch_id},
                )
                appscript_preview = _normalize_appscript_action_result(
                    appscript_preview_raw,
                    action="get_batch_preview",
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
        shared_metadata = _build_shared_metadata()
        batch = store.update_batch(
            batch_id,
            {
                "status": "preview_ready",
                "records": preview_records,
                "generated_counts": generated_counts,
                "validation_summary": validation_summary.model_dump(),
                "planning_summary": planning_summary,
                "metadata": {
                    **shared_metadata,
                    "validation_phase_status": final_status,
                    "dependency_resolution": dependency_resolution,
                    "focus_violations": focus_diagnostics.get("mismatches", []),
                    "focus_diagnostics": focus_diagnostics,
                    "duplicate_candidates": duplicate_candidates,
                    "generation_safety": generation_safety,
                    "field_inference": field_inference_metadata,
                    "form_field_resolution": form_field_resolution_metadata,
                    "canonicalization": canonicalization_metadata,
                    "staging": staging_metadata,
                    "validation": validation_metadata,
                    "preview_roundtrip": appscript_preview_metadata,
                    "failure": None,
                },
            },
        )
    except GenerateFailureError:
        raise
    except Exception as exc:  # noqa: BLE001
        if post_generation_stage == "canonicalization":
            _raise_post_generation_failure(
                failure_code="generate_canonicalization_failed",
                failure_reason=f"Canonicalization failed: {exc}",
                next_step="Retry with narrower scope or adjust malformed fields in generated payload.",
                stage_status_message=f"Canonicalization failed: {exc}",
            )
        if post_generation_stage == "dependency_resolution":
            _raise_post_generation_failure(
                failure_code="generate_dependency_resolution_failed",
                failure_reason=f"Dependency resolution failed: {exc}",
                next_step="Retry generation and ensure referenced objects exist in selected context/catalog.",
                stage_status_message=f"Dependency resolution failed: {exc}",
            )
        if post_generation_stage == "staging":
            _raise_post_generation_failure(
                failure_code="generate_staging_failed",
                failure_reason=f"Staging failed: {exc}",
                next_step="Retry after resolving Apps Script/Sheets staging connectivity issues.",
                stage_status_message=f"Staging failed: {exc}",
            )
        if post_generation_stage == "validation":
            _raise_post_generation_failure(
                failure_code="generate_validation_failed",
                failure_reason=f"Validation failed: {exc}",
                next_step="Retry after resolving Apps Script/Sheets validation issues.",
                stage_status_message=f"Validation failed: {exc}",
            )
        _raise_post_generation_failure(
            failure_code="generate_preview_failed",
            failure_reason=f"Preview finalization failed: {exc}",
            next_step="Retry generation after resolving preview/runtime issues.",
            stage_status_message=f"Preview finalization failed: {exc}",
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
    batch = _recover_stale_deploying_batch_if_needed(
        batch_id=batch_id,
        batch=batch,
        store=store,
    )
    return JobStatusResponse(**batch)


def _parse_batch_timestamp(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _recover_stale_deploying_batch_if_needed(
    *,
    batch_id: str,
    batch: dict,
    store,
) -> dict:
    status = str(batch.get("status", "")).strip().lower()
    if status != "deploying":
        return batch
    settings = get_settings()
    stale_seconds = max(float(getattr(settings, "deploy_stale_recovery_seconds", 300.0)), 30.0)
    last_ts = _parse_batch_timestamp(batch.get("updated_at"))
    age_seconds = time.time() - last_ts if last_ts > 0 else stale_seconds + 1.0
    if age_seconds < stale_seconds:
        return batch

    reason = f"Recovered stale deploying batch after {int(age_seconds)}s without progress."
    metadata = (
        batch.get("metadata", {})
        if isinstance(batch.get("metadata", {}), dict)
        else {}
    )
    existing_zendesk_deploy = (
        metadata.get("zendesk_deploy", {})
        if isinstance(metadata.get("zendesk_deploy", {}), dict)
        else {}
    )
    failure_payload = {
        "failure_stage": "deploy",
        "failure_code": "deploy_watchdog_timeout",
        "failure_reason": reason,
        "next_step": "Run deploy again explicitly after verifying credentials/connectivity.",
    }
    store.update_batch(
        batch_id,
        {
            "status": "deploy_failed",
            "metadata": {
                **metadata,
                "zendesk_deploy": {
                    **existing_zendesk_deploy,
                    "terminal_error_class": "stale_recovery",
                    "failed_at": _utc_now(),
                    "watchdog_seconds": float(settings.deploy_watchdog_seconds),
                    "failure": failure_payload,
                },
                "failure": failure_payload,
            },
        },
    )
    store.append_status(batch_id, "deploy_failed", reason)
    return store.get_batch(batch_id) or batch


def list_recent_batches(limit: int = 20) -> JobListResponse:
    store = get_batch_store()
    batches = list(store.list_batches())
    for index, batch in enumerate(batches):
        if str(batch.get("status", "")).strip().lower() != "deploying":
            continue
        batches[index] = _recover_stale_deploying_batch_if_needed(
            batch_id=str(batch.get("batch_id", "")),
            batch=batch,
            store=store,
        )
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
    batch = _recover_stale_deploying_batch_if_needed(
        batch_id=batch_id,
        batch=batch,
        store=store,
    )
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
        appscript_result_raw = await appscript.invoke(
            action="update_approval_status",
            payload={
                "batch_id": batch_id,
                "approved_by": approved_by,
                "records": decisions,
            },
        )
        appscript_result = _normalize_appscript_action_result(
            appscript_result_raw,
            action="update_approval_status",
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

    def _parse_iso_timestamp(value: object) -> float:
        text = str(value or "").strip()
        if not text:
            return 0.0
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return 0.0

    def _finalize_deploy_failure(
        *,
        code: str,
        reason: str,
        next_step: str,
        terminal_error_class: str,
        summary: dict | None = None,
        results: list | None = None,
        execution_log: dict | None = None,
    ) -> dict:
        current = store.get_batch(batch_id) or batch
        metadata = current.get("metadata", {}) if isinstance(current.get("metadata", {}), dict) else {}
        existing_zendesk_deploy = (
            metadata.get("zendesk_deploy", {})
            if isinstance(metadata.get("zendesk_deploy", {}), dict)
            else {}
        )
        failure_payload = {
            "failure_stage": "deploy",
            "failure_code": code,
            "failure_reason": reason,
            "next_step": next_step,
        }
        deploy_metadata = {
            **existing_zendesk_deploy,
            "summary": summary or existing_zendesk_deploy.get("summary", {}),
            "results": results or existing_zendesk_deploy.get("results", []),
            "execution_log": execution_log
            or existing_zendesk_deploy.get("execution_log", {}),
            "terminal_error_class": terminal_error_class,
            "failed_at": _utc_now(),
            "watchdog_seconds": float(settings.deploy_watchdog_seconds),
            "failure": failure_payload,
        }
        store.update_batch(
            batch_id,
            {
                "status": "deploy_failed",
                "metadata": {
                    **metadata,
                    "zendesk_deploy": deploy_metadata,
                    "failure": failure_payload,
                },
            },
        )
        store.append_status(batch_id, "deploy_failed", reason)
        return failure_payload

    stale_seconds = max(float(getattr(settings, "deploy_stale_recovery_seconds", 300.0)), 30.0)
    current_status = str(batch.get("status", "")).strip().lower()
    if current_status == "deploying":
        last_ts = _parse_iso_timestamp(batch.get("updated_at"))
        age_seconds = time.time() - last_ts if last_ts > 0 else stale_seconds + 1.0
        if age_seconds >= stale_seconds:
            stale_reason = (
                f"Recovered stale deploying batch after {int(age_seconds)}s without progress."
            )
            _finalize_deploy_failure(
                code="deploy_watchdog_timeout",
                reason=stale_reason,
                next_step="Run deploy again explicitly after verifying credentials/connectivity.",
                terminal_error_class="stale_recovery",
            )
            store.append_status(
                batch_id,
                "approved",
                "Stale deploy state recovered. Batch is ready for explicit redeploy.",
            )
            batch = store.get_batch(batch_id) or batch
        else:
            raise RuntimeError(
                "Batch is already deploying. Wait for completion or retry after watchdog timeout window."
            )

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
    store.append_status(batch_id, "deploying", "Deploy phase start: validating approved records and dependencies.")

    watchdog_seconds = max(float(getattr(settings, "deploy_watchdog_seconds", 240.0)), 30.0)
    deployment: dict = {}
    summary: dict = {}
    results: list = []
    updated_records: list[dict] = []
    dependency_auto_create: dict = {}
    execution_log_result: dict = {}
    terminal_error_class = "ok"

    try:
        deployment = await asyncio.wait_for(
            deploy_records_to_zendesk(
                subdomain=subdomain,
                email=email,
                api_token=api_token,
                records=records,
                dry_run=dry_run,
                on_existing=on_existing,
            ),
            timeout=watchdog_seconds,
        )
    except asyncio.TimeoutError as exc:
        terminal_error_class = "watchdog_timeout"
        reason = (
            f"Deployment exceeded watchdog timeout ({int(watchdog_seconds)}s) and was finalized as failed."
        )
        _finalize_deploy_failure(
            code="deploy_watchdog_timeout",
            reason=reason,
            next_step="Run deploy again explicitly after reducing batch scope or checking API responsiveness.",
            terminal_error_class=terminal_error_class,
        )
        raise RuntimeError(reason) from exc
    except Exception as exc:  # noqa: BLE001
        terminal_error_class = "deploy_runtime_error"
        reason = f"Deploy runtime error: {exc}"
        _finalize_deploy_failure(
            code="deploy_runtime_error",
            reason=reason,
            next_step="Retry deployment explicitly after resolving the runtime issue above.",
            terminal_error_class=terminal_error_class,
        )
        raise RuntimeError(reason) from exc

    try:
        dependency_auto_create = (
            deployment.get("dependency_auto_create", {})
            if isinstance(deployment, dict)
            else {}
        )
        dependency_events = (
            dependency_auto_create.get("events", [])
            if isinstance(dependency_auto_create, dict)
            else []
        )
        if isinstance(dependency_events, list) and dependency_events:
            for item in dependency_events[:10]:
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
        summary = deployment.get("summary", {}) if isinstance(deployment, dict) else {}
        results = deployment.get("results", []) if isinstance(deployment, dict) else []

        for item in results[:15]:
            if not isinstance(item, dict):
                continue
            store.append_status(
                batch_id,
                "deploying",
                (
                    f"Record {str(item.get('record_id', '?'))}: "
                    f"{str(item.get('deployment_status', 'pending'))} | "
                    f"{str(item.get('object_type', 'object'))}"
                ),
            )

        result_by_record = {
            item.get("record_id"): item
            for item in results
            if isinstance(item, dict)
        }
        for row in records:
            record_id = row.get("record_id")
            result = result_by_record.get(record_id, {})
            row["deployment_status"] = result.get("deployment_status", row.get("deployment_status", "pending"))
            row["zendesk_object_id"] = result.get("zendesk_object_id")
            row["execution_message"] = result.get("execution_message", "")
            updated_records.append(row)

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
                execution_log = _normalize_appscript_action_result(
                    execution_log,
                    action="write_execution_log",
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

        metadata = batch.get("metadata", {}) if isinstance(batch.get("metadata", {}), dict) else {}
        failure_metadata = (
            {
                "failure_stage": "deploy",
                "failure_code": "deploy_failed",
                "failure_reason": final_message,
                "next_step": "Review failed rows in execution details and resolve those issues before redeploying.",
            }
            if final_status in {"deploy_failed", "deployed_partial"} and failed > 0
            else None
        )
        store.update_batch(
            batch_id,
            {
                "records": updated_records,
                "metadata": {
                    **metadata,
                    "zendesk_deploy": {
                        "summary": summary,
                        "results": results,
                        "base_url": deployment.get("base_url"),
                        "execution_log": execution_log_result,
                        "dependency_auto_create": dependency_auto_create,
                        "terminal_error_class": terminal_error_class,
                        "watchdog_seconds": watchdog_seconds,
                    },
                    "failure": failure_metadata,
                },
            },
        )
        store.append_status(batch_id, final_status, final_message)
        store.append_status(batch_id, final_status, "Deploy phase finished.")

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
                "terminal_error_class": terminal_error_class,
                "watchdog_seconds": watchdog_seconds,
                "failure": failure_metadata,
            },
        }
    except Exception as exc:  # noqa: BLE001
        terminal_error_class = "deploy_post_processing_error"
        reason = f"Deploy finalization error: {exc}"
        _finalize_deploy_failure(
            code="deploy_runtime_error",
            reason=reason,
            next_step="Retry deployment explicitly after resolving deploy finalization/runtime errors.",
            terminal_error_class=terminal_error_class,
            summary=summary,
            results=results,
            execution_log=execution_log_result,
        )
        raise RuntimeError(reason) from exc
