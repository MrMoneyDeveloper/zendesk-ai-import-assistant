import asyncio
from collections import Counter
from datetime import UTC, datetime
import json
import math
from pathlib import Path
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
    CheckpointDecisionResponse,
    CheckpointItem,
    CheckpointListResponse,
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
from app.services.gemini_supervisor import (
    GeminiSupervisor,
    GeminiSupervisorError,
    evaluate_supervisor_bundle,
)
from app.services.generator import GeneratorStructuredOutputError, run_generator
from app.services.planner import run_planner
from app.services.progress_narrator import (
    ProgressNarrator,
    build_chunk_message,
    build_wave_complete_message,
    build_wave_start_message,
)
from app.services.appscript_bridge import AppScriptBridgeService
from app.services.sheets_service import SheetsService
from app.services.usage_telemetry import (
    build_usage_report,
    reset_usage_session,
    start_usage_session,
)
from app.services.zendesk import (
    check_zendesk_help_center_readiness,
    deploy_records_to_zendesk,
)

TAB_OBJECT_TYPES = {
    "brand": "brands",
    "brands": "brands",
    "category": "categories",
    "categories": "categories",
    "section": "sections",
    "sections": "sections",
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

UPDATE_FOCUS_BY_CONTEXT_TYPE = {
    "brand": "brands",
    "category": "categories",
    "section": "sections",
    "trigger": "triggers",
    "automation": "automations",
    "macro": "macros",
    "view": "views",
    "group": "groups",
    "ticket_form": "ticket_forms",
    "ticket_field": "ticket_fields",
    "article": "articles",
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
    "brands": {"brands"},
    "categories": {"categories", "help_centers", "brands"},
    "sections": {"sections", "categories", "help_centers", "brands"},
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
ZENDESK_SYSTEM_TICKET_FIELD_NAMES = frozenset(
    {
        "subject",
        "description",
        "status",
        "priority",
        "type",
        "assignee",
        "group",
        "requester",
        "organization",
        "tags",
        "cc",
        "followers",
    }
)
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
    "tag": "current_tags",
    "tags": "current_tags",
    "add_tag": "current_tags",
    "add_tags": "current_tags",
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
    "brands": [r"\bbrand\b", r"\bbrands\b"],
    "categories": [r"\bcategory\b", r"\bcategories\b"],
    "sections": [r"\bsection\b", r"\bsections\b"],
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
    0: ["brands"],
    1: ["categories", "sections"],
    2: ["groups", "ticket_fields"],
    3: ["ticket_forms", "views"],
    4: ["triggers", "macros", "automations"],
    5: ["articles"],
}

ORCHESTRATION_WAVE_BY_OBJECT = {
    object_type: wave
    for wave, object_types in ORCHESTRATION_WAVES.items()
    for object_type in object_types
}
WAVE3_RULE_OBJECT_TYPES = {"triggers", "macros", "automations"}
OBJECT_DETERMINISTIC_FAILOVER_THRESHOLD = 3
DEPARTMENT_COVERAGE_SOURCE = "department_coverage"
DEPARTMENT_TEMPLATE_FIRST_OBJECT_TYPES = frozenset(
    {
        "categories",
        "sections",
        "groups",
        "ticket_fields",
        "ticket_forms",
        "views",
        "triggers",
        "macros",
        "automations",
        "articles",
    }
)
DEPARTMENT_HEAVY_MINIMUMS = {
    "groups": 1,
    "ticket_forms": 1,
    "views": 2,
    "triggers": 3,
    "macros": 3,
    "automations": 2,
    "articles": 2,
}
DEPARTMENT_COVERAGE_OBJECT_TYPES = tuple(DEPARTMENT_HEAVY_MINIMUMS.keys())
DEPARTMENT_SHARED_FIELD_FALLBACKS = [
    "Department",
    "Customer Segment",
    "Vehicle Type",
    "Issue Category",
    "Incident Severity",
    "Payment Status",
    "KYC Status",
    "Fleet Size",
    "Requested Outcome",
]
DEPARTMENT_VIEW_COLUMNS = [
    "requester",
    "priority",
    "status",
    "vehicle_type",
    "issue_category",
    "incident_severity",
    "payment_status",
    "kyc_status",
    "assignee",
    "updated_at",
]

ARTICLE_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "content" / "article_templates"
ARTICLE_TEMPLATE_INDEX = {
    "billing": {
        "filename": "billing.md",
        "keywords": ("billing", "invoice", "payment", "charge", "refund"),
    },
    "claims": {
        "filename": "claims.md",
        "keywords": ("claim", "payout", "benefit", "benefits"),
    },
    "onboarding": {
        "filename": "onboarding.md",
        "keywords": ("onboard", "onboarding", "new customer", "new member", "getting started"),
    },
    "troubleshooting": {
        "filename": "troubleshooting.md",
        "keywords": ("troubleshoot", "error", "issue", "problem", "fix"),
    },
    "policy": {
        "filename": "policy.md",
        "keywords": ("policy", "compliance", "regulation", "terms"),
    },
}

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
    _score("brands", [r"\bbrand\b"])
    _score("categories", [r"\bcategory\b"])
    _score("sections", [r"\bsection\b"])
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
    bypass_reason: str = "single_item_explicit_prompt",
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
        "dependency_notes": f"Deterministic planner bypass applied: {bypass_reason}.",
        "llm": {
            "task": "planner",
            "model": "deterministic_inference",
            "strict_schema": False,
            "telemetry": {
                "bypassed": True,
                "bypass_reason": bypass_reason,
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
        "default_provider": str(
            getattr(settings, "llm_default_provider", "groq") or "groq"
        ).strip(),
        "default_model": str(
            getattr(settings, "gemini_default_model", "") or generator_route.model
        ).strip(),
        "fallback_policy": {
            "provider": "groq",
            "gemini_max_retries": int(
                getattr(settings, "gemini_default_max_retries", 0) or 0
            ),
            "gemini_failure_threshold": int(
                getattr(settings, "gemini_default_failure_threshold", 1) or 1
            ),
            "gemini_cooldown_seconds": int(
                getattr(settings, "gemini_default_cooldown_seconds", 0) or 0
            ),
        },
        "planner": planner_route.__dict__,
        "clarifier": clarifier_route.__dict__,
        "generator": generator_route.__dict__,
        "generator_wave3": str(settings.llm_model_generator_wave3 or "").strip() or None,
        "generator_wave4": str(settings.llm_model_generator_wave4 or "").strip() or None,
        "generator_secondary": str(getattr(settings, "llm_model_generator_secondary", "") or "").strip() or None,
        "generator_tertiary": str(getattr(settings, "llm_model_generator_tertiary", "") or "").strip() or None,
        "gemini_supervisor": {
            "enabled": bool(getattr(settings, "gemini_supervisor_enabled", False)),
            "model": str(getattr(settings, "gemini_supervisor_model", "") or "").strip() or None,
            "api_key_configured": bool(str(getattr(settings, "gemini_api_key", "") or "").strip()),
            "auto_apply_patches": bool(
                getattr(settings, "gemini_supervisor_auto_apply_patches", True)
            ),
            "max_concurrency": int(
                getattr(settings, "gemini_supervisor_max_concurrency", 1) or 1
            ),
            "min_request_interval_seconds": float(
                getattr(
                    settings,
                    "gemini_supervisor_min_request_interval_seconds",
                    0.0,
                )
                or 0.0
            ),
            "rate_limit_retries": int(
                getattr(settings, "gemini_supervisor_rate_limit_retries", 0) or 0
            ),
        },
        "progress_narrator": {
            "enabled": bool(getattr(settings, "progress_narrator_enabled", False)),
            "provider": str(
                getattr(settings, "progress_narrator_provider", "groq") or "groq"
            ).strip(),
            "model": str(getattr(settings, "progress_narrator_model", "") or "").strip()
            or None,
            "max_calls_per_batch": int(
                getattr(settings, "progress_narrator_max_calls_per_batch", 0) or 0
            ),
        },
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


def _select_wave_generator_route(
    routes: list[dict],
    cursor: int,
) -> tuple[dict, int]:
    if not routes:
        return {
            "model": "deterministic_template",
            "api_key": None,
            "profile": "department_template",
        }, max(int(cursor), 0)
    safe_cursor = max(int(cursor), 0)
    return dict(routes[safe_cursor % len(routes)]), safe_cursor + 1


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

    # Keep explicit object sets large enough to avoid review-call inflation,
    # while bounding long-form content so one response remains repairable.
    if normalized == "ticket_fields":
        return max(1, min(base_chunk_size, 5)), 2
    if normalized in {"ticket_forms", "views", "articles"}:
        return max(1, min(base_chunk_size, 3)), 2
    if normalized in {"triggers", "macros", "automations"}:
        return max(1, min(base_chunk_size, 4)), min(base_trigger_min_records, 3)

    return base_chunk_size, base_trigger_min_records


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _new_batch_id() -> str:
    return f"BATCH-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6].upper()}"


def _build_received_batch(
    request: ImportAssistantGenerateRequest,
    *,
    batch_id: str,
) -> dict:
    created_at = _utc_now()
    return {
        "batch_id": batch_id,
        "status": "received",
        "prompt": request.prompt,
        "requester": request.requester,
        "target_environment": request.target_environment,
        "mode": request.mode,
        "operation_mode": request.operation_mode,
        "conversation_id": request.conversation_id,
        "created_at": created_at,
        "updated_at": created_at,
        "status_history": [
            {
                "status": "received",
                "message": (
                    "Batch accepted. Preparing the department manifest, dependency order, "
                    "and model lanes."
                ),
                "at": created_at,
                "source": "pipeline",
            }
        ],
        "records": [],
        "generated_counts": {},
        "validation_summary": {"passed": 0, "warnings": 0, "blocked": 0},
        "planning_summary": {},
        "metadata": {
            "operation": {
                "operation_mode": request.operation_mode,
                "instance_sync_id": request.instance_sync_id,
                "target_object_id": request.update_target.id if request.update_target else None,
                "target_object_type": request.update_target.object_type if request.update_target else None,
                "target_name": request.update_target.name if request.update_target else None,
            },
            "run_control": _normalize_run_control({}),
            "checkpoints": [],
            "rollback": {},
        },
    }


def reserve_import_assistant_batch(
    request: ImportAssistantGenerateRequest,
    *,
    batch_id: str | None = None,
) -> JobStatusResponse:
    """Persist a pollable job before long-running generation begins."""
    resolved_batch_id = str(batch_id or "").strip() or _new_batch_id()
    batch = _build_received_batch(request, batch_id=resolved_batch_id)
    get_batch_store().save_batch(batch)
    return JobStatusResponse(**batch)


def mark_import_assistant_batch_failed(
    batch_id: str,
    *,
    stage: str,
    code: str,
    reason: str,
    next_step: str,
) -> None:
    """Best-effort terminal state for failures outside the generation service."""
    store = get_batch_store()
    batch = store.get_batch(batch_id)
    if not batch:
        return
    metadata = _metadata_dict(batch)
    metadata["failure"] = {
        "failure_stage": stage,
        "failure_code": code,
        "failure_reason": reason,
        "next_step": next_step,
    }
    if str(batch.get("status") or "").strip().lower() != "failed":
        store.append_status(batch_id, "failed", reason)
    store.update_batch(batch_id, {"status": "failed", "metadata": metadata})


def _summarize_validation_errors(
    validation_errors: list[dict[str, object]] | None,
    *,
    limit: int = 3,
) -> str:
    rows = validation_errors if isinstance(validation_errors, list) else []
    snippets: list[str] = []
    for item in rows[: max(limit, 1)]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "request").strip() or "request"
        message = str(item.get("message") or "Invalid value.").strip() or "Invalid value."
        snippets.append(f"{path}: {message}")
    if not snippets:
        return "Request validation failed."
    return "Request validation failed. " + "; ".join(snippets)


def create_request_validation_failed_batch(
    *,
    request_payload: dict | None,
    validation_errors: list[dict[str, object]] | None,
    compaction: dict | None = None,
) -> str:
    store = get_batch_store()
    payload = request_payload if isinstance(request_payload, dict) else {}
    now = _utc_now()
    batch_id = _new_batch_id()
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        prompt = "Request validation failed before generation."
    if len(prompt) > 12000:
        prompt = prompt[:12000]

    requester = str(payload.get("requester") or "local-user").strip() or "local-user"
    target_environment = str(payload.get("target_environment") or "sandbox").strip() or "sandbox"
    mode = str(payload.get("mode") or "generate_validate_preview").strip() or "generate_validate_preview"
    failure_reason = _summarize_validation_errors(validation_errors)
    next_step = "Adjust the invalid fields shown in validation_errors and retry."
    request_validation_metadata = {
        "validation_errors": validation_errors if isinstance(validation_errors, list) else [],
        "compaction_applied": bool((compaction or {}).get("applied", False)),
    }
    if isinstance(compaction, dict):
        request_validation_metadata["compaction"] = compaction

    batch = {
        "batch_id": batch_id,
        "status": "failed",
        "prompt": prompt,
        "requester": requester,
        "target_environment": target_environment,
        "mode": mode,
        "conversation_id": str(payload.get("conversation_id") or "").strip() or None,
        "created_at": now,
        "updated_at": now,
        "status_history": [
            {
                "status": "received",
                "message": "Batch accepted for request validation logging.",
                "at": now,
            },
            {
                "status": "failed",
                "message": failure_reason,
                "at": now,
            },
        ],
        "records": [],
        "generated_counts": {},
        "validation_summary": {"passed": 0, "warnings": 0, "blocked": 0},
        "planning_summary": {},
        "metadata": {
            "run_control": _normalize_run_control({}),
            "checkpoints": [],
            "rollback": {},
            "failure": {
                "failure_stage": "request",
                "failure_code": "request_validation_failed",
                "failure_reason": failure_reason,
                "next_step": next_step,
            },
            "request_validation": request_validation_metadata,
        },
    }
    store.save_batch(batch)
    return batch_id


TERMINAL_RUN_STATUSES = {
    "preview_ready",
    "failed",
    "approved",
    "partially_approved",
    "deployed",
    "deployed_partial",
    "deploy_failed",
}


def _normalize_run_control(raw: dict | None) -> dict:
    source = raw if isinstance(raw, dict) else {}
    return {
        "pause_requested": bool(source.get("pause_requested", False)),
        "pause_after_wave": bool(source.get("pause_after_wave", False)),
        "cancel_requested": bool(source.get("cancel_requested", False)),
        "cancel_reason": str(source.get("cancel_reason") or ""),
        "updated_at": str(source.get("updated_at") or ""),
        "updated_by": str(source.get("updated_by") or ""),
    }


def set_batch_run_control(
    *,
    batch_id: str,
    action: str,
    requested_by: str = "local-user",
) -> dict:
    store = get_batch_store()
    batch = store.get_batch(batch_id)
    if not batch:
        raise KeyError(batch_id)

    metadata = batch.get("metadata", {}) if isinstance(batch.get("metadata", {}), dict) else {}
    control = _normalize_run_control(metadata.get("run_control", {}))
    action_key = str(action or "").strip().lower()

    if action_key == "pause":
        control["pause_requested"] = True
    elif action_key == "resume":
        control["pause_requested"] = False
        control["pause_after_wave"] = False
    elif action_key == "cancel":
        control["cancel_requested"] = True
        control["pause_requested"] = False
        if not control.get("cancel_reason"):
            control["cancel_reason"] = "manual_cancel"
    elif action_key == "pause_at_next_wave":
        control["pause_after_wave"] = True
    elif action_key == "clear_pause_after_wave":
        control["pause_after_wave"] = False
    else:
        raise ValueError(f"Unsupported run control action: {action_key}")

    control["updated_at"] = _utc_now()
    control["updated_by"] = str(requested_by or "local-user")

    store.update_batch(
        batch_id,
        {
            "metadata": {
                **metadata,
                "run_control": control,
            }
        },
    )
    updated = store.get_batch(batch_id) or batch
    return {
        "batch_id": updated.get("batch_id"),
        "status": updated.get("status"),
        "run_control": control,
    }


def _normalize_checkpoint_item(raw: dict | None) -> dict:
    source = raw if isinstance(raw, dict) else {}
    return {
        "checkpoint_id": str(source.get("checkpoint_id") or ""),
        "wave": max(int(source.get("wave", 0) or 0), 0),
        "created_at": str(source.get("created_at") or _utc_now()),
        "status": str(source.get("status") or "pending"),
        "summary": source.get("summary", {}) if isinstance(source.get("summary", {}), dict) else {},
        "preview_snapshot_ref": str(source.get("preview_snapshot_ref") or ""),
        "decision_at": str(source.get("decision_at") or "") or None,
        "decision_by": str(source.get("decision_by") or "") or None,
        "decision_note": str(source.get("decision_note") or "") or None,
    }


def _metadata_dict(batch: dict | None) -> dict:
    if not isinstance(batch, dict):
        return {}
    metadata = batch.get("metadata", {})
    if not isinstance(metadata, dict):
        return {}
    return metadata


def _checkpoint_list_from_metadata(metadata: dict | None) -> list[dict]:
    if not isinstance(metadata, dict):
        return []
    source = metadata.get("checkpoints", [])
    if not isinstance(source, list):
        return []
    checkpoints: list[dict] = []
    for item in source:
        normalized = _normalize_checkpoint_item(item if isinstance(item, dict) else {})
        if normalized["checkpoint_id"]:
            checkpoints.append(normalized)
    return checkpoints


def _build_checkpoint_summary(
    *,
    wave: int,
    wave_position: int,
    total_waves: int,
    wave_meta: dict | None,
    generated_counts: dict | None,
) -> dict:
    meta = wave_meta if isinstance(wave_meta, dict) else {}
    counts = generated_counts if isinstance(generated_counts, dict) else {}
    return {
        "wave": wave,
        "wave_position": wave_position,
        "total_waves": total_waves,
        "chunk_count": int(meta.get("chunks", 0) or 0),
        "created": int(meta.get("created", 0) or 0),
        "reused": int(meta.get("reused", 0) or 0),
        "updated": int(meta.get("updated", 0) or 0),
        "blocked": int(meta.get("blocked", 0) or 0),
        "generated_records": int(meta.get("generated_records", 0) or 0),
        "generated_counts": counts,
    }


def _append_wave_checkpoint(
    *,
    batch_id: str,
    wave: int,
    wave_position: int,
    total_waves: int,
    wave_meta: dict | None,
    generated_counts: dict | None,
) -> dict:
    store = get_batch_store()
    batch = store.get_batch(batch_id)
    if not batch:
        raise KeyError(batch_id)
    metadata = _metadata_dict(batch)
    checkpoints = _checkpoint_list_from_metadata(metadata)
    checkpoint_id = f"CHK-{wave:02d}-{uuid4().hex[:8].upper()}"
    summary = _build_checkpoint_summary(
        wave=wave,
        wave_position=wave_position,
        total_waves=total_waves,
        wave_meta=wave_meta,
        generated_counts=generated_counts,
    )
    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "wave": int(wave),
        "created_at": _utc_now(),
        "status": "pending",
        "summary": summary,
        "preview_snapshot_ref": f"/api/import-assistant/preview/{batch_id}?checkpoint={checkpoint_id}",
        "decision_at": None,
        "decision_by": None,
        "decision_note": None,
    }
    checkpoints.append(checkpoint)
    store.update_batch(
        batch_id,
        {
            "metadata": {
                **metadata,
                "checkpoints": checkpoints,
            }
        },
    )
    store.append_status(
        batch_id,
        str(batch.get("status") or "generating"),
        (
            f"Checkpoint {checkpoint_id} created for wave {wave_position}/{total_waves}. "
            "Generation continues while confirmation is pending."
        ),
    )
    return checkpoint


def get_job_checkpoints(batch_id: str) -> CheckpointListResponse:
    store = get_batch_store()
    batch = store.get_batch(batch_id)
    if not batch:
        raise KeyError(batch_id)
    metadata = _metadata_dict(batch)
    checkpoints = _checkpoint_list_from_metadata(metadata)
    return CheckpointListResponse(
        batch_id=batch_id,
        status=str(batch.get("status", "received")),
        checkpoints=[CheckpointItem(**item) for item in checkpoints],
    )


async def _execute_checkpoint_rollback(
    *,
    batch_id: str,
    checkpoint_id: str,
    note: str,
    requested_by: str,
) -> dict:
    store = get_batch_store()
    appscript = AppScriptBridgeService()
    batch = store.get_batch(batch_id)
    if not batch:
        raise KeyError(batch_id)

    metadata = _metadata_dict(batch)
    records = list(batch.get("records", []) or [])
    rollback_payload = {
        "status": "rollback_started",
        "checkpoint_id": checkpoint_id,
        "requested_by": requested_by,
        "note": note,
        "started_at": _utc_now(),
        "local": {"status": "pending"},
        "sheet": {"status": "pending"},
        "deploy_undo": {"status": "not_applicable"},
    }
    store.update_batch(
        batch_id,
        {
            "metadata": {
                **metadata,
                "rollback": rollback_payload,
            }
        },
    )
    store.append_status(batch_id, str(batch.get("status") or "generating"), "Rollback started.")

    rollback_payload["local"] = {
        "status": "ok",
        "cleared_records": len(records),
    }
    store.update_batch(
        batch_id,
        {
            "records": [],
            "generated_counts": {},
            "validation_summary": {"passed": 0, "warnings": 0, "blocked": 0},
            "metadata": {
                **_metadata_dict(store.get_batch(batch_id)),
                "rollback": rollback_payload,
            },
        },
    )

    if appscript.enabled:
        rollback_result_raw = await appscript.invoke(
            action="rollback_batch",
            payload={
                "batch_id": batch_id,
                "checkpoint_id": checkpoint_id,
                "note": note,
            },
        )
        rollback_result = _normalize_appscript_action_result(
            rollback_result_raw,
            action="rollback_batch",
        )
        if rollback_result.get("status") == "ok":
            rollback_payload["sheet"] = {
                "status": "ok",
                "detail": rollback_result.get("detail"),
                "http_status": rollback_result.get("http_status"),
                "result": rollback_result.get("data", {}),
            }
        else:
            rollback_payload["sheet"] = {
                "status": "fallback_mark_aborted",
                "detail": rollback_result.get("detail")
                or "rollback_batch unavailable; sheet rows should be marked aborted manually.",
                "http_status": rollback_result.get("http_status"),
            }
    else:
        rollback_payload["sheet"] = {
            "status": "skipped",
            "detail": "Apps Script bridge not configured.",
            "http_status": None,
        }

    sheet_status = str(rollback_payload.get("sheet", {}).get("status", ""))
    if sheet_status == "ok":
        rollback_payload["status"] = "rollback_completed"
        failure_code = "checkpoint_rejected"
    else:
        rollback_payload["status"] = "rollback_partial"
        failure_code = "rollback_partial"
    rollback_payload["completed_at"] = _utc_now()

    current = store.get_batch(batch_id) or batch
    current_meta = _metadata_dict(current)
    store.update_batch(
        batch_id,
        {
            "status": "failed",
            "metadata": {
                **current_meta,
                "rollback": rollback_payload,
                "failure": {
                    "failure_stage": "generate",
                    "failure_code": failure_code,
                    "failure_reason": (
                        "Checkpoint rejected; rollback completed."
                        if failure_code == "checkpoint_rejected"
                        else "Checkpoint rejected; rollback partially completed."
                    ),
                    "next_step": (
                        "Review rollback metadata and rerun generate."
                        if failure_code == "checkpoint_rejected"
                        else "Review rollback metadata and complete manual cleanup before rerunning."
                    ),
                },
            },
        },
    )
    store.append_status(
        batch_id,
        "failed",
        (
            "Rollback completed."
            if failure_code == "checkpoint_rejected"
            else "Rollback finished with partial cleanup."
        ),
    )
    return rollback_payload


async def decide_job_checkpoint(
    *,
    batch_id: str,
    checkpoint_id: str,
    decision: str,
    requested_by: str = "local-user",
    note: str = "",
) -> CheckpointDecisionResponse:
    store = get_batch_store()
    batch = store.get_batch(batch_id)
    if not batch:
        raise KeyError(batch_id)

    decision_key = str(decision or "").strip().lower()
    if decision_key not in {"accept", "reject"}:
        raise ValueError("Unsupported checkpoint decision.")

    metadata = _metadata_dict(batch)
    checkpoints = _checkpoint_list_from_metadata(metadata)
    target_index = -1
    for index, checkpoint in enumerate(checkpoints):
        if str(checkpoint.get("checkpoint_id")) == checkpoint_id:
            target_index = index
            break
    if target_index < 0:
        raise KeyError(checkpoint_id)

    checkpoint = checkpoints[target_index]
    checkpoint["status"] = "accepted" if decision_key == "accept" else "rejected"
    checkpoint["decision_at"] = _utc_now()
    checkpoint["decision_by"] = str(requested_by or "local-user")
    checkpoint["decision_note"] = str(note or "").strip() or None
    checkpoints[target_index] = checkpoint

    run_control = _normalize_run_control(metadata.get("run_control", {}))
    rollback_metadata: dict = {}
    if decision_key == "reject":
        run_control["cancel_requested"] = True
        run_control["pause_requested"] = False
        run_control["pause_after_wave"] = False
        run_control["cancel_reason"] = f"checkpoint_rejected:{checkpoint_id}"
        run_control["updated_at"] = _utc_now()
        run_control["updated_by"] = str(requested_by or "local-user")
    store.update_batch(
        batch_id,
        {
            "metadata": {
                **metadata,
                "checkpoints": checkpoints,
                "run_control": run_control,
            }
        },
    )
    store.append_status(
        batch_id,
        str((store.get_batch(batch_id) or batch).get("status") or "generating"),
        (
            f"Checkpoint {checkpoint_id} accepted by {requested_by}."
            if decision_key == "accept"
            else f"Checkpoint {checkpoint_id} rejected by {requested_by}. Cancelling run and starting rollback."
        ),
    )

    if decision_key == "reject":
        rollback_metadata = await _execute_checkpoint_rollback(
            batch_id=batch_id,
            checkpoint_id=checkpoint_id,
            note=note,
            requested_by=requested_by,
        )
        latest = store.get_batch(batch_id) or batch
    else:
        latest = store.get_batch(batch_id) or batch
        rollback_metadata = _metadata_dict(latest).get("rollback", {})

    refreshed_checkpoints = _checkpoint_list_from_metadata(_metadata_dict(latest))
    target = next(
        (item for item in refreshed_checkpoints if item.get("checkpoint_id") == checkpoint_id),
        checkpoint,
    )
    return CheckpointDecisionResponse(
        batch_id=batch_id,
        status=str(latest.get("status", "received")),
        checkpoint=CheckpointItem(**target),
        rollback=rollback_metadata if isinstance(rollback_metadata, dict) else {},
        run_control=_normalize_run_control(_metadata_dict(latest).get("run_control", {})),
    )


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
        "brand": "brands",
        "brands": "brands",
        "category": "categories",
        "categories": "categories",
        "section": "sections",
        "sections": "sections",
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
        "team": "groups",
        "teams": "groups",
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
        r"triggers?|automations?|macros?|views?|groups?|teams?|brands?|categories?|sections?|"
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
        r"triggers?|automations?|macros?|views?|groups?|teams?|brands?|categories?|sections?|"
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
        "brands": [r"\b(\d{1,4})\s+brands?\b"],
        "categories": [r"\b(\d{1,4})\s+categories\b", r"\b(\d{1,4})\s+category\b"],
        "sections": [r"\b(\d{1,4})\s+sections\b", r"\b(\d{1,4})\s+section\b"],
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

    article_categories = _extract_article_category_names(text)
    if article_categories and targets.get("articles", 0) > 0:
        dependency_count = len(article_categories)
        targets["categories"] = max(int(targets.get("categories", 0) or 0), dependency_count)
        targets["sections"] = max(int(targets.get("sections", 0) or 0), dependency_count)

    explicit_group_names = _extract_inline_support_team_names(text)
    if explicit_group_names:
        targets["groups"] = max(
            int(targets.get("groups", 0) or 0),
            len(explicit_group_names),
        )

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


def _should_bypass_planner_for_explicit_manifest(
    *,
    prompt: str,
    object_targets: dict[str, int],
    force_wave_chunk_reason: str,
) -> bool:
    if str(force_wave_chunk_reason or "").strip() != "create_following_numbered_prompt":
        return False
    object_type_count = sum(1 for value in object_targets.values() if int(value or 0) > 0)
    named_specs = len(
        re.findall(
            r"\b(?:called|named)\s+[\"'][^\"']{2,180}[\"']",
            str(prompt or ""),
            flags=re.IGNORECASE,
        )
    )
    numbered_sections = len(
        re.findall(r"(?m)^\s*\d+\.\s+", str(prompt or ""))
    )
    return bool(object_type_count >= 4 and named_specs >= 8 and numbered_sections >= 4)


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
        raw_priority = item.get("priority", wave)
        try:
            priority = int(raw_priority or wave)
        except (TypeError, ValueError):
            priority = {
                "critical": 0,
                "high": 1,
                "medium": wave,
                "normal": wave,
                "low": 9,
            }.get(str(raw_priority or "").strip().lower(), wave)
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


def _merge_blueprint_targets_with_explicit_counts(
    targets: list[dict],
    explicit_counts: dict[str, int],
) -> list[dict]:
    merged = [dict(item) for item in targets if isinstance(item, dict)]
    by_type = {
        _normalize_object_type(str(item.get("object_type", ""))): item
        for item in merged
        if str(item.get("object_type", "")).strip()
    }
    for raw_type, raw_count in explicit_counts.items():
        object_type = _normalize_object_type(str(raw_type))
        count = max(int(raw_count or 0), 0)
        if count <= 0:
            continue
        if object_type in by_type:
            by_type[object_type]["target_count"] = max(
                int(by_type[object_type].get("target_count", 0) or 0),
                count,
            )
            continue
        item = {
            "object_type": object_type,
            "target_count": count,
            "priority": _resolve_wave_for_object_type(object_type),
            "wave": _resolve_wave_for_object_type(object_type),
            "source": "explicit_prompt",
        }
        merged.append(item)
        by_type[object_type] = item
    return sorted(
        merged,
        key=lambda item: (
            int(item.get("wave", 9)),
            int(item.get("priority", 9)),
            str(item.get("object_type", "")),
        ),
    )


def _coverage_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _split_prompt_list(raw: object) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    text = re.sub(r"\s+\band\b\s+", ", ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    items: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,;]", text):
        cleaned = str(part).strip(" .:-")
        cleaned = re.sub(r"^(?:and|or|the)\s+", "", cleaned, flags=re.IGNORECASE).strip()
        if not cleaned:
            continue
        key = _coverage_key(cleaned)
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(cleaned)
    return items


def _extract_named_list_after(prompt: str, pattern: str) -> list[str]:
    match = re.search(pattern, str(prompt or ""), flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    raw = str(match.group("items") or "").strip()
    raw = re.split(r"(?:\n\s*[-*]\s+|\n\s*[A-Z][A-Za-z /&-]{2,40}\s*:)", raw, maxsplit=1)[0]
    return _split_prompt_list(raw)


def _extract_department_specs_from_prompt(prompt: str) -> list[dict[str, str]]:
    lines = str(prompt or "").splitlines()
    specs: list[dict[str, str]] = []
    seen: set[str] = set()
    in_department_block = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_department_block and specs:
                break
            continue
        if re.search(r"\bdepartments?\b\s*:", stripped, flags=re.IGNORECASE):
            in_department_block = True
            continue
        if not in_department_block:
            continue
        if not re.match(r"^[-*]\s+", stripped):
            if specs:
                break
            continue
        item = re.sub(r"^[-*]\s+", "", stripped).strip()
        match = re.match(r"(?P<name>[^:]{2,90})(?::\s*(?P<description>.*))?$", item)
        if not match:
            continue
        name = str(match.group("name") or "").strip(" .")
        description = str(match.group("description") or "").strip()
        if not name or len(name) > 90:
            continue
        key = _coverage_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        specs.append(
            {
                "name": name,
                "description": description,
                "slug": _slugify_option_value(name),
            }
        )
    return specs[:20]


def _extract_inline_support_team_names(prompt: str) -> list[str]:
    raw_text = str(prompt or "")
    text = re.sub(r"\s+", " ", raw_text).strip()
    if not text:
        return []

    names: list[str] = []
    seen: set[str] = set()

    def add_name(value: str) -> None:
        name = re.sub(r"\s+", " ", str(value or "")).strip(" .,:;\"'")
        key = _coverage_key(name)
        if not key or key in seen or len(name) > 100:
            return
        seen.add(key)
        names.append(name)

    inline_pattern = re.compile(
        r"\b(?:has|have)\s+(?:(?:\d+|[a-z]+)\s+)?(?:named\s+)?"
        r"(?:support\s+)?teams?\s*:\s*(?P<items>[^.]+)",
        flags=re.IGNORECASE,
    )
    for match in inline_pattern.finditer(text):
        for item in _split_prompt_list(match.group("items")):
            add_name(item)

    group_heading_pattern = re.compile(
        r"^(?:#{1,6}\s*)?(?:\*\*|__)?(?:zendesk\s+)?"
        r"(?:support\s+)?groups?(?:\*\*|__)?\s*:?\s*$",
        flags=re.IGNORECASE,
    )
    in_group_section = False
    section_item_count = 0
    for raw_line in raw_text.splitlines():
        stripped = raw_line.strip()
        if group_heading_pattern.match(stripped):
            in_group_section = True
            section_item_count = 0
            continue
        if not in_group_section:
            continue
        if re.match(r"^#{1,6}\s+", stripped):
            in_group_section = False
            continue
        if not stripped:
            continue

        bullet = re.match(r"^[-*+]\s+(?P<item>.+)$", stripped)
        if not bullet:
            if section_item_count:
                in_group_section = False
            continue

        item = re.sub(r"^\[[ xX]\]\s*", "", bullet.group("item")).strip()
        item = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", item)
        item = re.sub(r"[*_`]+", "", item).strip()
        name = re.split(
            r"\s+(?:-|\u2013|\u2014)\s+|:\s*",
            item,
            maxsplit=1,
        )[0].strip()
        if not name:
            continue
        add_name(name)
        section_item_count += 1

    return names[:40]


def _extract_article_category_names(prompt: str) -> list[str]:
    text = re.sub(r"\s+", " ", str(prompt or "")).strip()
    if not text:
        return []
    names: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"\bin\s+(?:the\s+)?category\s+[\"']?"
        r"(?P<name>[a-z0-9][a-z0-9 &/\-]{1,80}?)[\"']?"
        r"(?=\s+that\b|\s+which\b|[,.;]|$)",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        name = str(match.group("name") or "").strip(" .,:;\"'")
        key = _coverage_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names[:40]


def _extract_article_specs_from_prompt(prompt: str) -> list[dict[str, str]]:
    text = str(prompt or "")
    if not text.strip():
        return []
    pattern = re.compile(
        r"[-*]\s+(?:an?\s+)?article\s+called\s+[\"'](?P<title>[^\"']{2,180})[\"']"
        r"\s+in\s+(?:the\s+)?category\s+[\"']?(?P<category>[a-z0-9][a-z0-9 &/\-]{1,80}?)[\"']?"
        r"\s+(?:that|which)\s+(?P<requirements>.*?)"
        r"(?=\n\s*[-*]\s+(?:an?\s+)?article\s+called\b|\n\s*\d+\.\s|\Z)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    specs: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        title = re.sub(r"\s+", " ", str(match.group("title") or "")).strip()
        category = re.sub(r"\s+", " ", str(match.group("category") or "")).strip(" .,:;\"'")
        requirements = re.sub(
            r"\s+",
            " ",
            str(match.group("requirements") or ""),
        ).strip(" .")
        key = _coverage_key(title)
        if not key or key in seen or not category:
            continue
        seen.add(key)
        specs.append(
            {
                "title": title,
                "category": category,
                "requirements": requirements,
            }
        )
    return specs[:80]


def _extract_ticket_form_specs_from_prompt(prompt: str) -> list[dict[str, object]]:
    text = str(prompt or "")
    if not text.strip():
        return []
    pattern = re.compile(
        r"[-*]\s+(?:a\s+)?form\s+called\s+[\"'](?P<title>[^\"']{2,120})[\"']"
        r"\s+that\s+includes\s+fields?\s+in\s+order\s*:\s*(?P<fields>.*?)"
        r"(?=\n\s*[-*]\s+(?:a\s+)?form\s+called\b|\n\s*\d+\.\s|\Z)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    specs: list[dict[str, object]] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        title = re.sub(r"\s+", " ", str(match.group("title") or "")).strip()
        key = _coverage_key(title)
        if not key or key in seen:
            continue
        raw_fields = re.sub(r"\s+", " ", str(match.group("fields") or "")).strip(" .")
        fields = _split_prompt_list(raw_fields)
        if not fields:
            continue
        seen.add(key)
        specs.append({"title": title, "fields": fields[:30]})
    return specs[:40]


def _extract_trigger_specs_from_prompt(prompt: str) -> list[dict[str, object]]:
    text = str(prompt or "")
    if not text.strip():
        return []
    pattern = re.compile(
        r"[-*]\s+(?P<body>When\s+.*?)"
        r"(?=\n\s*[-*]\s+When\b|\n\s*\d+\.\s|\Z)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    field_titles = [
        str(spec.get("title", "")).strip()
        for spec in _extract_ticket_field_specs_from_prompt(text)
        if str(spec.get("title", "")).strip()
    ]
    specs: list[dict[str, object]] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        body = re.sub(r"\s+", " ", str(match.group("body") or "")).strip(" .")
        conditions: list[dict[str, str]] = []
        if re.search(r"\bticket\s+is\s+created\b", body, flags=re.IGNORECASE):
            conditions.append({"field": "status", "operator": "is", "value": "new"})

        title_parts: list[str] = []
        for field_title in field_titles:
            value_match = re.search(
                rf"\b{re.escape(field_title)}\s+is\s+[\"'](?P<value>[^\"']+)[\"']",
                body,
                flags=re.IGNORECASE,
            )
            if not value_match:
                continue
            display_value = re.sub(r"\s+", " ", value_match.group("value")).strip()
            conditions.append(
                {
                    "field": f"custom_field_{_slugify_option_value(field_title)}",
                    "operator": "is",
                    "value": _slugify_option_value(display_value),
                }
            )
            title_parts.append(display_value)

        group_match = re.search(
            r"\bassign(?:s)?(?:\s+the\s+ticket)?\s+to\s+"
            r"(?P<group>[^,.]+?)(?=\s*,|\s+and\s+(?:add|set)\b|\.|$)",
            body,
            flags=re.IGNORECASE,
        )
        tag_match = re.search(
            r"\badd(?:s)?\s+(?:the\s+)?tag\s+[\"'](?P<tag>[a-z0-9_-]+)[\"']",
            body,
            flags=re.IGNORECASE,
        )
        priority_match = re.search(
            r"\bset(?:s)?\s+priority\s+to\s+(?P<priority>low|normal|high|urgent)\b",
            body,
            flags=re.IGNORECASE,
        )
        actions: list[dict[str, str]] = []
        group_name = re.sub(r"\s+", " ", group_match.group("group")).strip() if group_match else ""
        if group_name:
            actions.append({"field": "group_id", "value": group_name})
        if tag_match:
            actions.append({"field": "current_tags", "value": tag_match.group("tag").lower()})
        if priority_match:
            actions.append({"field": "priority", "value": priority_match.group("priority").lower()})

        title = " - ".join(title_parts) if title_parts else (group_name or "Ticket")
        title = f"{title} Routing"
        title_key = _normalize_title_for_dedupe(title)
        if not title_key or title_key in seen:
            title = f"{title} {len(specs) + 1}"
            title_key = _normalize_title_for_dedupe(title)
        seen.add(title_key)
        specs.append(
            {
                "title": title,
                "conditions": conditions,
                "actions": actions,
                "source_text": body,
            }
        )
    return specs[:80]


def _extract_macro_specs_from_prompt(prompt: str) -> list[dict[str, object]]:
    text = str(prompt or "")
    if not text.strip():
        return []
    pattern = re.compile(
        r"[-*]\s+(?:a\s+)?macro\s+called\s+[\"'](?P<title>[^\"']{2,160})[\"']"
        r"(?P<body>.*?)"
        r"(?=\n\s*[-*]\s+(?:a\s+)?macro\s+called\b|\n\s*\d+\.\s|\Z)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    specs: list[dict[str, object]] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        title = re.sub(r"\s+", " ", str(match.group("title") or "")).strip()
        title_key = _normalize_title_for_dedupe(title)
        if not title_key or title_key in seen:
            continue
        body = str(match.group("body") or "")
        reply_match = re.search(
            r"\bsends?\s+(?:a\s+)?reply\s*:\s*[\"'](?P<text>.*?)[\"']",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        note_match = re.search(
            r"\b(?:adds?\s+)?(?:an\s+)?internal\s+note\s*:\s*[\"'](?P<text>.*?)[\"']",
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        comment_match = reply_match or note_match
        comment = (
            re.sub(r"\s+", " ", str(comment_match.group("text") or "")).strip()
            if comment_match
            else ""
        )
        group_match = re.search(
            r"\bassign(?:s)?(?:\s+the\s+ticket)?\s+to\s+"
            r"(?P<group>[^,.]+?)(?=\s*,|\s+and\s+(?:add|set)\b|\.|$)",
            body,
            flags=re.IGNORECASE,
        )
        priority_match = re.search(
            r"\bset(?:s)?\s+priority\s+to\s+(?P<priority>low|normal|high|urgent)\b",
            body,
            flags=re.IGNORECASE,
        )
        tags = [
            value.lower()
            for value in re.findall(
                r"\badd(?:s)?\s+(?:the\s+)?tag\s+[\"']([a-z0-9_-]+)[\"']",
                body,
                flags=re.IGNORECASE,
            )
        ]
        seen.add(title_key)
        specs.append(
            {
                "title": title,
                "comment": comment,
                "comment_is_public": True if reply_match else (False if note_match else None),
                "group": (
                    re.sub(r"\s+", " ", group_match.group("group")).strip()
                    if group_match
                    else ""
                ),
                "priority": priority_match.group("priority").lower() if priority_match else "",
                "tags": list(dict.fromkeys(tags)),
            }
        )
    return specs[:80]


def _extract_view_specs_from_prompt(prompt: str) -> list[dict[str, object]]:
    text = str(prompt or "")
    section_match = re.search(
        r"\n\s*\d+\.\s+[^\n:]*\bviews?\s*:\s*(?P<section>.*?)"
        r"(?=\n\s*\d+\.\s|\Z)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not section_match:
        return []
    trigger_specs = _extract_trigger_specs_from_prompt(text)
    specs: list[dict[str, object]] = []
    for match in re.finditer(
        r"[-*]\s+(?P<body>.*?)(?=\n\s*[-*]\s+|\Z)",
        section_match.group("section"),
        flags=re.DOTALL,
    ):
        body = re.sub(r"\s+", " ", str(match.group("body") or "")).strip(" .")
        if not re.search(r"\btickets?\b", body, flags=re.IGNORECASE):
            continue
        unassigned = bool(re.search(r"\bunassigned\b", body, flags=re.IGNORECASE))
        descriptor_match = re.search(
            r"\bopen\s+(?P<descriptor>.*?)\s+tickets?\b",
            body,
            flags=re.IGNORECASE,
        )
        descriptor = (
            re.sub(r"\s+", " ", descriptor_match.group("descriptor")).strip()
            if descriptor_match
            else ("Unassigned" if unassigned else "Support")
        )
        if unassigned:
            descriptor = "Unassigned"
        conditions: list[dict[str, str]] = [
            {"field": "status", "operator": "is", "value": "open"}
        ]

        if unassigned:
            conditions.append({"field": "assignee_id", "operator": "is", "value": ""})
        else:
            descriptor_tokens = set(_coverage_key(descriptor).split())
            best_tag = ""
            best_score = -10_000
            for trigger_spec in trigger_specs:
                title_tokens = set(_coverage_key(str(trigger_spec.get("title", ""))).split())
                overlap = len(descriptor_tokens & title_tokens)
                extra = len(title_tokens - descriptor_tokens - {"routing"})
                score = (overlap * 10) - extra
                tag = next(
                    (
                        str(action.get("value", "")).strip()
                        for action in list(trigger_spec.get("actions", []) or [])
                        if isinstance(action, dict)
                        and str(action.get("field", "")).strip().lower() in {"current_tags", "set_tags"}
                    ),
                    "",
                )
                if tag and overlap > 0 and score > best_score:
                    best_tag = tag
                    best_score = score
            if best_tag:
                conditions.append(
                    {"field": "current_tags", "operator": "includes", "value": best_tag}
                )

            group_match = re.search(
                r"\bassigned\s+to\s+(?P<group>.+?)(?=\s+sorted\b|$)",
                body,
                flags=re.IGNORECASE,
            )
            group_name = (
                re.sub(r"\s+", " ", group_match.group("group")).strip(" ,.")
                if group_match
                else ""
            )
            if group_name:
                conditions.append({"field": "group_id", "operator": "is", "value": group_name})

        if re.search(r"\bpriority\s+descending\b", body, flags=re.IGNORECASE):
            sort_by, sort_order = "priority", "desc"
        elif re.search(r"\b(?:oldest\s+first|creation\s+date)\b", body, flags=re.IGNORECASE):
            sort_by, sort_order = "created", "asc"
        else:
            sort_by, sort_order = "updated", "desc"
        columns = ["status", "priority", "description", "requester", "assignee", "updated"]
        if sort_by == "created":
            columns = ["status", "created", "description", "requester", "assignee", "updated"]
        specs.append(
            {
                "title": "All Unassigned Tickets" if unassigned else f"Open {descriptor} Tickets",
                "conditions": conditions,
                "actions": [
                    {"field": "output_columns", "value": columns},
                    {"field": "sort_by", "value": sort_by},
                    {"field": "sort_order", "value": sort_order},
                ],
            }
        )
    return specs[:80]


def _extract_explicit_ticket_fields_from_prompt(prompt: str) -> list[str]:
    fields = _extract_named_list_after(
        prompt,
        r"\bticket\s+fields?\s+for\s+(?P<items>[^.\n]+)",
    )
    if fields:
        return fields[:40]
    return []


def _extract_explicit_ticket_forms_from_prompt(prompt: str) -> list[str]:
    forms = _extract_named_list_after(
        prompt,
        r"\bticket\s+forms?\s+for\s+(?P<items>[^.\n]+)",
    )
    if forms:
        return forms[:40]
    return []


def _extract_help_center_topics_from_prompt(prompt: str) -> list[str]:
    topics = _extract_named_list_after(
        prompt,
        r"\bhelp\s+center\b[^\n.]*?\bfor\s+(?P<items>[^.\n]+)",
    )
    return topics[:40]


def _extract_tag_hints_from_prompt(prompt: str) -> list[str]:
    tags = _extract_named_list_after(
        prompt,
        r"\btags?\s+(?:such\s+as|including)\s+(?P<items>[^.\n]+)",
    )
    return [
        re.sub(r"[^a-z0-9_-]+", "", tag.strip().lower())
        for tag in tags[:80]
        if re.sub(r"[^a-z0-9_-]+", "", tag.strip().lower())
    ]


def _match_department_form(department_name: str, explicit_forms: list[str], used: set[str]) -> str:
    department_key = _coverage_key(department_name)
    department_words = {
        word
        for word in department_key.split()
        if word not in {"and", "or", "support", "operations", "ops", "success", "team"}
    }
    best_form = ""
    best_score = 0
    for form in explicit_forms:
        form_key = _coverage_key(form)
        if form_key in used:
            continue
        form_words = set(form_key.split())
        score = len(department_words & form_words)
        if "customer" in department_words and "general" in form_words:
            score += 2
        if {"vip", "enterprise"} & department_words and {"vip", "enterprise"} & form_words:
            score += 2
        if "compliance" in department_words and "review" in form_words:
            score += 1
        if score > best_score:
            best_score = score
            best_form = form
    if best_form:
        used.add(_coverage_key(best_form))
        return best_form
    return f"{department_name} Support"


def _match_department_topic(department_name: str, topics: list[str]) -> str:
    if not topics:
        return department_name
    department_key = _coverage_key(department_name)
    department_words = set(department_key.split())
    best_topic = topics[0]
    best_score = 0
    for topic in topics:
        topic_words = set(_coverage_key(topic).split())
        score = len(department_words & topic_words)
        if "finance" in department_words and {"payment", "payments", "billing"} & topic_words:
            score += 2
        if "claims" in department_words and {"claim", "claims", "incident", "incidents"} & topic_words:
            score += 2
        if "fleet" in department_words and {"fleet", "onboarding"} & topic_words:
            score += 2
        if "technical" in department_words and {"technical", "troubleshooting"} & topic_words:
            score += 2
        if "compliance" in department_words and {"compliance", "kyc"} & topic_words:
            score += 2
        if {"vip", "enterprise"} & department_words and {"vip", "enterprise"} & topic_words:
            score += 2
        if score > best_score:
            best_score = score
            best_topic = topic
    return best_topic


def _match_department_tag(department_name: str, tag_hints: list[str]) -> str:
    department_key = _coverage_key(department_name)
    keyword_sets = [
        (("claims", "incident"), ("claim", "claims", "incident", "incidents", "safety")),
        (("finance", "payment"), ("finance", "payment", "payments", "billing", "refund")),
        (("fleet", "onboarding"), ("fleet", "onboard", "onboarding")),
        (("technical",), ("technical", "tech", "device", "bug")),
        (("compliance", "kyc"), ("compliance", "kyc", "privacy")),
        (("vip", "enterprise"), ("vip", "enterprise")),
        (("customer",), ("customer", "general", "account")),
    ]
    for tag in tag_hints:
        tag_key = _coverage_key(tag)
        for department_tokens, tag_tokens in keyword_sets:
            if any(token in department_key for token in department_tokens) and any(
                token in tag_key for token in tag_tokens
            ):
                return tag
    slug = _slugify_option_value(department_name)
    return f"apex_{slug}" if not slug.startswith("apex_") else slug


def _build_department_coverage_manifest(
    *,
    prompt: str,
    focus_object_types: list[str] | None = None,
) -> dict:
    departments = _extract_department_specs_from_prompt(prompt)
    explicit_forms = _extract_explicit_ticket_forms_from_prompt(prompt)
    ticket_fields = _extract_explicit_ticket_fields_from_prompt(prompt)
    help_topics = _extract_help_center_topics_from_prompt(prompt)
    tags = _extract_tag_hints_from_prompt(prompt)

    if len(departments) < 2:
        return {
            "enabled": False,
            "profile": "none",
            "reason": "fewer_than_two_named_departments",
            "departments": departments,
            "explicit_forms": explicit_forms,
            "ticket_fields": ticket_fields,
            "help_center_topics": help_topics,
            "tags": tags,
        }

    if not ticket_fields:
        ticket_fields = list(DEPARTMENT_SHARED_FIELD_FALLBACKS)
    if not help_topics:
        help_topics = [department["name"] for department in departments]

    used_forms: set[str] = set()
    enriched_departments: list[dict] = []
    for department in departments:
        name = str(department.get("name", "")).strip()
        if not name:
            continue
        form_title = _match_department_form(name, explicit_forms, used_forms)
        topic = _match_department_topic(name, help_topics)
        tag = _match_department_tag(name, tags)
        enriched_departments.append(
            {
                **department,
                "group_title": name,
                "form_title": form_title,
                "topic": topic,
                "tag": tag,
                "minimums": dict(DEPARTMENT_HEAVY_MINIMUMS),
            }
        )

    target_counts = {
        "categories": max(len(help_topics), 1),
        "sections": max(len(help_topics), 1),
        "groups": len(enriched_departments),
        "ticket_fields": max(len(ticket_fields), 1),
        "ticket_forms": len(enriched_departments),
        "views": len(enriched_departments) * DEPARTMENT_HEAVY_MINIMUMS["views"],
        "triggers": len(enriched_departments) * DEPARTMENT_HEAVY_MINIMUMS["triggers"],
        "macros": len(enriched_departments) * DEPARTMENT_HEAVY_MINIMUMS["macros"],
        "automations": len(enriched_departments) * DEPARTMENT_HEAVY_MINIMUMS["automations"],
        "articles": len(enriched_departments) * DEPARTMENT_HEAVY_MINIMUMS["articles"],
    }
    focus_set = {_normalize_object_type(item) for item in (focus_object_types or []) if str(item).strip()}
    if focus_set:
        target_counts = {
            object_type: count
            for object_type, count in target_counts.items()
            if object_type in focus_set
        }

    return {
        "enabled": True,
        "profile": "heavy",
        "coverage_mode": "preview_with_warnings",
        "wave4_strategy": "hybrid_by_type",
        "source": "department_bullet_list",
        "departments": enriched_departments,
        "explicit_forms": explicit_forms,
        "ticket_fields": ticket_fields,
        "help_center_topics": help_topics,
        "tags": tags,
        "minimums": dict(DEPARTMENT_HEAVY_MINIMUMS),
        "target_counts": target_counts,
        "target_total": sum(int(value or 0) for value in target_counts.values()),
    }


def _department_manifest_target_objects(
    manifest: dict,
    *,
    fallback_targets: dict[str, int] | None = None,
) -> list[dict]:
    targets: dict[str, int] = {
        _normalize_object_type(key): max(int(value or 0), 0)
        for key, value in (fallback_targets or {}).items()
    }
    if manifest.get("enabled"):
        for key, value in dict(manifest.get("target_counts", {}) or {}).items():
            object_type = _normalize_object_type(str(key))
            targets[object_type] = max(int(targets.get(object_type, 0) or 0), int(value or 0))
    return [
        {
            "object_type": object_type,
            "target_count": count,
            "priority": _resolve_wave_for_object_type(object_type),
            "wave": _resolve_wave_for_object_type(object_type),
            "source": DEPARTMENT_COVERAGE_SOURCE if manifest.get("enabled") else "fallback",
        }
        for object_type, count in targets.items()
        if count > 0
    ]


def _merge_blueprint_targets_with_manifest(target_objects: list[dict], manifest: dict) -> list[dict]:
    if not manifest.get("enabled"):
        return target_objects
    merged: dict[str, dict] = {}
    for item in target_objects:
        object_type = _normalize_object_type(str(item.get("object_type", "")))
        if not object_type:
            continue
        merged[object_type] = dict(item)
    for item in _department_manifest_target_objects(manifest):
        object_type = _normalize_object_type(str(item.get("object_type", "")))
        existing = merged.get(object_type, {})
        merged[object_type] = {
            **existing,
            **item,
            "target_count": max(
                int(existing.get("target_count", 0) or 0),
                int(item.get("target_count", 0) or 0),
            ),
            "source": DEPARTMENT_COVERAGE_SOURCE,
        }
    return sorted(
        merged.values(),
        key=lambda item: (int(item.get("wave", 9)), int(item.get("priority", 9)), str(item.get("object_type", ""))),
    )


def _build_department_coverage_backlog(
    *,
    manifest: dict,
    dependency_mode: str,
    focus_object_types: list[str],
) -> list[dict]:
    if not manifest.get("enabled"):
        return []
    allowed_focus = {_normalize_object_type(item) for item in focus_object_types if str(item).strip()}
    rows: list[dict] = []

    def add_item(
        object_type: str,
        *,
        target_count: int = 1,
        priority_offset: int = 0,
        department: dict | None = None,
        topic: str = "",
        title_hint: str = "",
        fields: list[str] | None = None,
        coverage_kind: str = "",
    ) -> None:
        normalized = _normalize_object_type(object_type)
        if allowed_focus and normalized not in allowed_focus:
            return
        wave = _resolve_wave_for_object_type(normalized)
        row = {
            "object_type": normalized,
            "target_count": max(int(target_count or 1), 1),
            "wave": wave,
            "priority": (wave * 100) + int(priority_offset or 0),
            "source": DEPARTMENT_COVERAGE_SOURCE,
            "dependency_mode": dependency_mode,
            "coverage_kind": coverage_kind or normalized,
        }
        if department:
            row["department"] = department
            row["department_name"] = str(department.get("name", "")).strip()
            row["department_slug"] = str(department.get("slug", "")).strip()
            row["department_tag"] = str(department.get("tag", "")).strip()
            row["form_title"] = str(department.get("form_title", "")).strip()
            row["topic"] = str(department.get("topic", "")).strip()
        if topic:
            row["topic"] = topic
        if title_hint:
            row["title_hint"] = title_hint
        if fields:
            row["fields"] = fields
            if not department:
                row["department_names"] = departments
        rows.append(row)

    topics = [str(item).strip() for item in list(manifest.get("help_center_topics", []) or []) if str(item).strip()]
    fields = [str(item).strip() for item in list(manifest.get("ticket_fields", []) or []) if str(item).strip()]
    departments = [item for item in list(manifest.get("departments", []) or []) if isinstance(item, dict)]

    for index, topic in enumerate(topics, start=1):
        add_item("categories", priority_offset=index, topic=topic, title_hint=f"{topic} Support")
        add_item("sections", priority_offset=50 + index, topic=topic, title_hint=topic)

    if fields:
        add_item(
            "ticket_fields",
            target_count=len(fields),
            priority_offset=10,
            fields=fields,
            coverage_kind="shared_ticket_fields",
        )

    for index, department in enumerate(departments, start=1):
        add_item("groups", priority_offset=index, department=department)
    for index, department in enumerate(departments, start=1):
        add_item("ticket_forms", priority_offset=index, department=department)
        add_item(
            "views",
            target_count=DEPARTMENT_HEAVY_MINIMUMS["views"],
            priority_offset=50 + index,
            department=department,
        )
    for index, department in enumerate(departments, start=1):
        add_item(
            "triggers",
            target_count=DEPARTMENT_HEAVY_MINIMUMS["triggers"],
            priority_offset=index,
            department=department,
        )
        add_item(
            "automations",
            target_count=DEPARTMENT_HEAVY_MINIMUMS["automations"],
            priority_offset=50 + index,
            department=department,
        )
        add_item(
            "macros",
            target_count=DEPARTMENT_HEAVY_MINIMUMS["macros"],
            priority_offset=100 + index,
            department=department,
        )
    for index, department in enumerate(departments, start=1):
        add_item(
            "articles",
            target_count=DEPARTMENT_HEAVY_MINIMUMS["articles"],
            priority_offset=index,
            department=department,
        )

    rows = sorted(
        rows,
        key=lambda row: (int(row.get("wave", 9)), int(row.get("priority", 999)), str(row.get("title_hint", ""))),
    )
    for index, row in enumerate(rows, start=1):
        row["backlog_id"] = f"BL-{index:03d}"
    return rows


def _supervisor_review_bundle_key(item: dict) -> str:
    wave = int(item.get("wave", 0) or 0)
    department = str(item.get("department_name", "")).strip()
    topic = str(item.get("topic", "")).strip()
    object_type = _normalize_object_type(str(item.get("object_type", "")))
    coverage_kind = str(item.get("coverage_kind", "")).strip()
    if wave == 1:
        return f"topic:{_coverage_key(topic or item.get('title_hint') or object_type)}"
    if wave == 2 and coverage_kind == "shared_ticket_fields":
        return "shared:ticket_fields"
    if department:
        return f"department:{_coverage_key(department)}"
    return f"type:{object_type}"


def _build_department_supervisor_review_manifest(backlog: list[dict]) -> list[dict]:
    units: dict[tuple[int, str], dict] = {}
    for item in backlog:
        if str(item.get("source", "")).strip() != DEPARTMENT_COVERAGE_SOURCE:
            continue
        wave = int(item.get("wave", 0) or 0)
        department = str(item.get("department_name", "")).strip()
        topic = str(item.get("topic", "")).strip()
        object_type = _normalize_object_type(str(item.get("object_type", "")))
        bundle_key = _supervisor_review_bundle_key(item)
        key = (wave, bundle_key)
        unit = units.setdefault(
            key,
            {
                "review_unit_id": f"wave-{wave}:{bundle_key}",
                "wave": wave,
                "bundle_key": bundle_key,
                "department_name": department,
                "topic": topic,
                "object_types": [],
                "backlog_ids": [],
            },
        )
        if object_type and object_type not in unit["object_types"]:
            unit["object_types"].append(object_type)
        backlog_id = str(item.get("backlog_id", "")).strip()
        if backlog_id:
            unit["backlog_ids"].append(backlog_id)
    return sorted(units.values(), key=lambda item: (item["wave"], item["bundle_key"]))


def _planner_used_heuristic_fallback(plan: object) -> bool:
    if not isinstance(plan, dict):
        return False
    return any(
        "planner fallback used" in str(reason or "").strip().lower()
        for reason in plan.get("ambiguity_reasons", [])
    )


async def _run_business_blueprint_compiler(
    *,
    prompt: str,
    dependency_mode: str,
    focus_object_types: list[str],
    object_targets: dict[str, int],
    planner_route,
    deterministic_only: bool = False,
    deterministic_reason: str | None = None,
) -> tuple[dict, dict]:
    coverage_manifest = _build_department_coverage_manifest(
        prompt=prompt,
        focus_object_types=focus_object_types,
    )
    fallback_blueprint = {
        "mode": "deterministic_fallback",
        "capabilities": ["inference_first", "match_existing_or_create"],
        "assumptions": [
            "No clarification loop is used; missing details are inferred from prompt and catalog context.",
        ],
        "priorities": ["deterministic_wave_order", "reuse_before_create", "fail_whole_run"],
        "dependency_hints": [],
        "coverage_manifest": coverage_manifest,
        "target_objects": _department_manifest_target_objects(
            coverage_manifest,
            fallback_targets=object_targets,
        ),
    }
    if not str(prompt or "").strip():
        return fallback_blueprint, {"bypassed": True, "reason": "empty_prompt"}
    if coverage_manifest.get("enabled"):
        return fallback_blueprint, {
            "bypassed": True,
            "reason": "department_coverage_manifest",
        }
    if deterministic_only:
        return fallback_blueprint, {
            "bypassed": True,
            "reason": (
                str(deterministic_reason or "").strip()
                or "explicit_numbered_operating_model"
            ),
        }

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
                "object_type values must be one of: brands,categories,sections,groups,ticket_fields,ticket_forms,views,triggers,macros,automations,articles. "
                "Respect this deterministic wave order: wave0 brands, wave1 categories+sections, "
                "wave2 groups+ticket_fields, wave3 ticket_forms+views, wave4 triggers+macros+automations, wave5 articles."
            ),
        },
        {
            "role": "user",
            "content": str(user_payload),
        },
    ]

    def compile_blueprint(raw: str) -> dict:
        payload = extract_json_payload(raw)
        if not isinstance(payload, dict):
            raise ValueError("Business compiler response must be a JSON object.")
        target_objects = _normalize_blueprint_target_objects(
            payload.get("target_objects"),
            fallback_targets=object_targets,
        )
        target_objects = _merge_blueprint_targets_with_explicit_counts(
            target_objects,
            object_targets,
        )
        target_objects = _merge_blueprint_targets_with_manifest(
            target_objects,
            coverage_manifest,
        )
        return {
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
            "coverage_manifest": coverage_manifest,
            "target_objects": target_objects,
        }

    provider_attempts: list[dict] = []
    fallback_reason = "compiler_output_invalid"
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
        blueprint = compile_blueprint(raw)
        telemetry = GrokClient.get_last_call_metrics("planner")
        return blueprint, telemetry
    except LLMRequestError as exc:
        fallback_reason = str(exc.error_class or "llm_request_failed").strip().lower()
        provider_attempts.append(
            {
                "profile": "gemini_then_primary",
                "error_class": fallback_reason,
                "http_status": exc.http_status,
            }
        )
        if fallback_reason == "rate_limited":
            for route in _build_wave_generator_routes(
                settings=client.settings,
                wave=1,
            ):
                if str(route.get("profile", "")).strip() == "primary":
                    continue
                profile = str(route.get("profile", "")).strip() or "fallback"
                try:
                    raw = await client.chat(
                        messages,
                        temperature=0.0,
                        model=str(route.get("model", "")).strip(),
                        max_output_tokens=max(
                            280,
                            int(planner_route.max_output_tokens or 300),
                        ),
                        response_schema=None,
                        strict_schema=False,
                        task="planner",
                        response_format_override="json_object",
                        api_key_override=str(route.get("api_key", "")).strip(),
                        prefer_provider="groq",
                    )
                    blueprint = compile_blueprint(raw)
                    telemetry = dict(GrokClient.get_last_call_metrics("planner") or {})
                    telemetry.update(
                        {
                            "blueprint_failover_used": True,
                            "blueprint_failover_profile": profile,
                            "blueprint_provider_attempts": provider_attempts,
                        }
                    )
                    return blueprint, telemetry
                except LLMRequestError as route_exc:
                    provider_attempts.append(
                        {
                            "profile": profile,
                            "error_class": str(
                                route_exc.error_class or "llm_request_failed"
                            ).strip().lower(),
                            "http_status": route_exc.http_status,
                        }
                    )
                    fallback_reason = str(
                        route_exc.error_class or fallback_reason
                    ).strip().lower()
                except Exception as route_exc:  # noqa: BLE001
                    provider_attempts.append(
                        {
                            "profile": profile,
                            "error_class": type(route_exc).__name__,
                            "http_status": None,
                        }
                    )
                    fallback_reason = type(route_exc).__name__
    except Exception as exc:  # noqa: BLE001
        fallback_reason = type(exc).__name__
        provider_attempts.append(
            {
                "profile": "gemini_then_primary",
                "error_class": fallback_reason,
                "http_status": None,
            }
        )

    telemetry = dict(GrokClient.get_last_call_metrics("planner") or {})
    telemetry.update(
        {
            "blueprint_fallback_used": True,
            "blueprint_fallback_reason": fallback_reason,
            "blueprint_provider_attempts": provider_attempts,
        }
    )
    return fallback_blueprint, telemetry


def _build_orchestration_backlog(
    *,
    blueprint: dict,
    prompt: str,
    dependency_mode: str,
    focus_object_types: list[str],
) -> list[dict]:
    manifest = blueprint.get("coverage_manifest", {}) if isinstance(blueprint, dict) else {}
    if isinstance(manifest, dict) and manifest.get("enabled"):
        return _build_department_coverage_backlog(
            manifest=manifest,
            dependency_mode=dependency_mode,
            focus_object_types=focus_object_types,
        )

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

    coverage_lines: list[str] = []
    if str(item.get("source", "")).strip() == DEPARTMENT_COVERAGE_SOURCE:
        department_name = str(item.get("department_name", "")).strip()
        form_title = str(item.get("form_title", "")).strip()
        topic = str(item.get("topic", "")).strip()
        tag = str(item.get("department_tag", "")).strip()
        title_hint = str(item.get("title_hint", "")).strip()
        fields = [str(field).strip() for field in list(item.get("fields", []) or []) if str(field).strip()]
        coverage_lines.append("Coverage mode: department-first full operating model.")
        if department_name:
            coverage_lines.append(f"Department target: {department_name}.")
        if form_title:
            coverage_lines.append(f"Department ticket form: {form_title}.")
        if topic:
            coverage_lines.append(f"Help-center topic/section target: {topic}.")
        if tag:
            coverage_lines.append(f"Required department tag: {tag}.")
        if title_hint:
            coverage_lines.append(f"Title/topic hint: {title_hint}.")
        if fields:
            coverage_lines.append(f"Shared ticket fields to create/reference: {', '.join(fields)}.")
        coverage_lines.append(
            "Use clear dependency notes by name for generated groups, forms, fields, categories, and sections."
        )

    coverage_suffix = "\n" + "\n".join(coverage_lines) if coverage_lines else ""

    return (
        f"Business brief: {prompt}\n"
        f"Wave object type: {object_type}\n"
        f"Target records for this wave item: {target_count}\n"
        f"Execution mode: {mode}. {mode_instruction}{base_suffix}"
        f"{coverage_suffix}"
    )


def _load_article_template_text(template_key: str) -> str | None:
    spec = ARTICLE_TEMPLATE_INDEX.get(template_key)
    if not isinstance(spec, dict):
        return None
    filename = str(spec.get("filename", "")).strip()
    if not filename:
        return None
    template_path = ARTICLE_TEMPLATE_DIR / filename
    try:
        if not template_path.exists():
            return None
        return template_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _select_article_template_key(prompt: str) -> str | None:
    text = str(prompt or "").strip().lower()
    if not text:
        return None
    best_key: str | None = None
    best_score = 0
    for key, spec in ARTICLE_TEMPLATE_INDEX.items():
        keywords = tuple(spec.get("keywords", ()) if isinstance(spec, dict) else ())
        score = sum(1 for token in keywords if token and token in text)
        if score > best_score:
            best_score = score
            best_key = key
    if best_score <= 0:
        return None
    return best_key


def _extract_company_name_from_prompt(prompt: str) -> str | None:
    text = str(prompt or "").strip()
    if not text:
        return None
    patterns = (
        r"\bcompany\s+(?:called|named)\s+([A-Z][A-Za-z0-9&' .-]{2,80})\b",
        r"\b(?:for|about)\s+([A-Z][A-Za-z0-9&' .-]{2,80})\b",
        r"\bcompany\s*(?:name)?\s*(?:is|=|:)\s*([A-Za-z0-9&' .-]{2,80})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        candidate = str(match.group(1) or "").strip(" .,:;")
        if candidate:
            return candidate
    return None


def _build_article_template_variables(
    *,
    prompt: str,
    reference_catalog: dict[str, list[dict]],
) -> dict[str, str]:
    brand_rows = reference_catalog.get("brands", []) if isinstance(reference_catalog, dict) else []
    first_brand = brand_rows[0] if isinstance(brand_rows, list) and brand_rows else {}
    brand_name = str(first_brand.get("name", "")).strip() if isinstance(first_brand, dict) else ""
    company_name = _extract_company_name_from_prompt(prompt) or brand_name or "Your Company"
    product_match = re.search(
        r"\b(?:for|about)\s+([a-z0-9][a-z0-9 &/_-]{2,80})\s+(?:support|workflows?|operations?)\b",
        str(prompt or "").lower(),
    )
    product_or_service = (
        str(product_match.group(1) or "").strip().title()
        if product_match
        else "your services"
    )
    return {
        "company_name": company_name,
        "brand_name": brand_name or company_name,
        "product_or_service": product_or_service,
        "support_hours": "Monday-Friday, 08:00-17:00",
        "contact_channel": "support portal",
    }


def _render_article_template(
    *,
    template_text: str,
    variables: dict[str, str],
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    rendered = str(template_text or "")
    for key, value in variables.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    unresolved = sorted(set(re.findall(r"\{\{([a-zA-Z0-9_]+)\}\}", rendered)))
    for token in unresolved:
        warnings.append(f"Template variable '{token}' was not provided; default text retained.")
    return rendered.strip(), warnings


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
        "brands",
        "categories",
        "sections",
        "groups",
        "ticket_fields",
        "ticket_forms",
        "views",
        "triggers",
        "macros",
        "automations",
        "articles",
    }


def _should_use_department_template_first(
    backlog_item: dict,
    object_type: str,
    *,
    strategy: str = "template",
) -> bool:
    if str((backlog_item or {}).get("source", "")).strip() != DEPARTMENT_COVERAGE_SOURCE:
        return False
    normalized_object_type = _normalize_object_type(object_type)
    if normalized_object_type not in DEPARTMENT_TEMPLATE_FIRST_OBJECT_TYPES:
        return False
    if str(strategy or "template").strip().lower() == "hybrid":
        return normalized_object_type not in {"macros", "articles"}
    return True


def _should_use_explicit_template_first(
    *,
    prompt: str,
    object_type: str,
    target_count: int,
) -> bool:
    normalized = _normalize_object_type(object_type)
    required = max(int(target_count or 1), 1)
    if normalized == "groups":
        return len(_extract_inline_support_team_names(prompt)) >= required
    if normalized == "categories" or normalized == "sections":
        return len(_extract_article_category_names(prompt)) >= required
    if normalized == "ticket_fields":
        return len(_extract_ticket_field_specs_from_prompt(prompt)) >= required
    if normalized == "ticket_forms":
        return len(_extract_ticket_form_specs_from_prompt(prompt)) >= required
    if normalized == "views":
        return len(_extract_view_specs_from_prompt(prompt)) >= required
    if normalized == "triggers":
        return len(_extract_trigger_specs_from_prompt(prompt)) >= required
    if normalized == "macros":
        return len(_extract_macro_specs_from_prompt(prompt)) >= required
    return False


def _field_options_for_department_manifest(field_title: str, departments: list[str]) -> list[str]:
    key = _coverage_key(field_title)
    if key == "department":
        return departments or ["Customer Support", "Finance Operations"]
    option_map = {
        "customer segment": ["Gig Worker", "Small Business", "Fleet Account", "VIP / Enterprise"],
        "vehicle type": ["Electric Scooter", "Delivery E-bike", "Small EV Fleet"],
        "issue category": [
            "Account Question",
            "Payment Issue",
            "Login Problem",
            "Damaged Vehicle",
            "Theft Report",
            "Accident Claim",
            "KYC Review",
            "App Bug",
            "GPS Device Issue",
            "Charger Problem",
            "Battery Diagnostic",
            "Enterprise Escalation",
        ],
        "incident severity": ["Low", "Medium", "High", "Urgent Safety Incident"],
        "payment status": ["Current", "Failed Payment", "Disputed", "Refund Pending", "Payoff Requested"],
        "kyc status": ["Not Started", "Pending Review", "Approved", "Rejected", "More Info Required"],
        "fleet size": ["1 Rider", "2-5 Riders", "6-20 Riders", "21+ Riders"],
        "requested outcome": ["Information", "Correction", "Refund", "Escalation", "Vehicle Handover", "Technical Fix"],
    }
    return option_map.get(key, [])


def _html_paragraphs(*paragraphs: str) -> str:
    return "".join(
        f"<p>{re.sub(r'<[^>]+>', '', str(paragraph).strip())}</p>"
        for paragraph in paragraphs
        if str(paragraph).strip()
    )


def _department_copy_guidance(department_name: str, topic: str) -> dict[str, str]:
    key = _coverage_key(f"{department_name} {topic}")
    profiles = [
        (
            {"claim", "claims", "incident", "incidents"},
            {
                "evidence": "the incident date and time, location, vehicle ID, photos, safety status, and any police or insurance reference",
                "process": "triage immediate safety risk, validate the incident evidence, and coordinate the claim or recovery path",
                "urgent": "For an active safety risk, stop using the vehicle when safe to do so and contact local emergency services before updating the ticket.",
            },
        ),
        (
            {"finance", "payment", "payments", "billing"},
            {
                "evidence": "the account holder, transaction date, amount, payment or bank reference, supporting statement, and requested correction",
                "process": "reconcile the account ledger, verify settlement or refund state, and document the financial outcome",
                "urgent": "Do not include full card numbers, passwords, or one-time PINs in the ticket.",
            },
        ),
        (
            {"fleet", "onboarding"},
            {
                "evidence": "the company account, rider roster, required identity documents, vehicle allocation, activation state, and target handover date",
                "process": "verify onboarding documents, resolve activation blockers, and coordinate vehicle handover readiness",
                "urgent": "Call out any rider or handover deadline that is already at risk so the queue can prioritize the blocker.",
            },
        ),
        (
            {"technical", "troubleshooting", "device", "app"},
            {
                "evidence": "the vehicle or device ID, app and device version, timestamps, screenshots, diagnostic results, and troubleshooting already attempted",
                "process": "reproduce the fault, isolate app, device, charger, battery, GPS, or telematics causes, and provide the next diagnostic action",
                "urgent": "For overheating, smoke, damaged wiring, or unsafe vehicle behavior, stop use and move away from the equipment before reporting details.",
            },
        ),
        (
            {"compliance", "kyc", "privacy", "regulatory"},
            {
                "evidence": "the legal account name, document type, jurisdiction, submission date, review notice, and the specific compliance or privacy outcome requested",
                "process": "verify the review stage, identify the regulatory or document gap, and route restricted evidence to the authorized reviewer",
                "urgent": "Use only approved secure attachment channels for identity documents and never place passwords or authentication codes in comments.",
            },
        ),
        (
            {"vip", "enterprise", "partner"},
            {
                "evidence": "the organization and account, fleet size, impacted riders or vehicles, business impact, deadline, partner contact, and requested resolution",
                "process": "assign an enterprise owner, coordinate dependent teams, and maintain one consolidated resolution path",
                "urgent": "State any operational outage, contractual milestone, or safety impact clearly so the escalation priority can be validated.",
            },
        ),
    ]
    key_tokens = set(key.split())
    for tokens, guidance in profiles:
        if key_tokens & tokens:
            return guidance
    return {
        "evidence": "the account email, contact details, issue summary, relevant timestamps, screenshots or documents, and the outcome requested",
        "process": "confirm the request type, resolve first-line checks, and route any specialist work with complete context",
        "urgent": "Do not include passwords, one-time PINs, or full payment credentials in the ticket.",
    }


def _department_coverage_row_templates(
    *,
    object_type: str,
    target_count: int,
    backlog_item: dict,
    fallback_note: str,
    make_unique_title,
    company_name: str | None = None,
) -> list[dict]:
    if str(backlog_item.get("source", "")).strip() != DEPARTMENT_COVERAGE_SOURCE:
        return []
    normalized = _normalize_object_type(object_type)
    requested_count = max(int(target_count or 1), 1)
    company_label = str(company_name or "the company").strip()
    department = backlog_item.get("department") if isinstance(backlog_item.get("department"), dict) else {}
    department_name = str(backlog_item.get("department_name") or department.get("name") or "").strip()
    department_description = str(department.get("description", "")).strip()
    form_title = str(backlog_item.get("form_title") or department.get("form_title") or "").strip()
    topic = str(backlog_item.get("topic") or department.get("topic") or "").strip()
    tag = str(backlog_item.get("department_tag") or department.get("tag") or "").strip()
    title_hint = str(backlog_item.get("title_hint", "")).strip()
    fields = [
        str(field).strip()
        for field in list(backlog_item.get("fields", []) or [])
        if str(field).strip()
    ]
    department_names = [
        str(item.get("name", "")).strip()
        for item in list(backlog_item.get("department_names", []) or [])
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    ]
    if not department_names and department_name:
        department_names = [department_name]

    dependency_note = (
        f"Department coverage template for {department_name}."
        if department_name
        else "Department coverage template."
    )
    notes = [fallback_note, dependency_note]
    if form_title:
        notes.append(f"Depends on ticket form: {form_title}.")
    if department_name and normalized not in {"groups", "ticket_fields"}:
        notes.append(f"Depends on group: {department_name}.")
    if topic and normalized == "articles":
        notes.append(f"Depends on same-batch help center section: {topic}.")

    if normalized == "categories":
        base = title_hint or (f"{topic} Support" if topic else "Support Knowledge Base")
        return [
            {
                "object_type": "categories",
                "title": make_unique_title(base, 1),
                "conditions": [],
                "actions": [{"field": "locale", "value": "en-us"}],
                "dependency_notes": notes,
            }
        ]

    if normalized == "sections":
        base = title_hint or topic or "General Support"
        category_title = f"{topic} Support" if topic else "Support Knowledge Base"
        return [
            {
                "object_type": "sections",
                "title": make_unique_title(base, 1),
                "conditions": [],
                "actions": [
                    {"field": "locale", "value": "en-us"},
                    {"field": "category_name", "value": category_title},
                ],
                "dependency_notes": [*notes, f"Depends on same-batch help center category: {category_title}."],
            }
        ]

    if normalized == "groups" and department_name:
        description = department_description or f"Owns {department_name.lower()} support workflows."
        return [
            {
                "object_type": "groups",
                "title": make_unique_title(department_name, 1),
                "conditions": [],
                "actions": [{"field": "description", "value": description}],
                "dependency_notes": notes,
            }
        ]

    if normalized == "ticket_fields":
        field_titles = fields or list(DEPARTMENT_SHARED_FIELD_FALLBACKS)
        rows: list[dict] = []
        all_departments = department_names or ([department_name] if department_name else [])
        for index, field_title in enumerate(field_titles[:requested_count], start=1):
            options = _field_options_for_department_manifest(field_title, all_departments)
            field_type = "tagger" if options else "text"
            actions: list[dict] = [{"field": "field_type", "value": field_type}]
            if options:
                actions.append(
                    {
                        "field": "custom_field_options",
                        "value": [
                            {"name": option[:80], "value": _slugify_option_value(option)}
                            for option in options[:30]
                        ],
                    }
                )
            rows.append(
                {
                    "object_type": "ticket_fields",
                    "title": make_unique_title(field_title, index),
                    "conditions": [],
                    "actions": actions,
                    "dependency_notes": notes,
                }
            )
        return rows

    if normalized == "ticket_forms" and department_name:
        form_name = form_title or f"{department_name} Support"
        form_fields = fields or list(DEPARTMENT_SHARED_FIELD_FALLBACKS)
        return [
            {
                "object_type": "ticket_forms",
                "title": make_unique_title(form_name, 1),
                "conditions": [],
                "actions": [{"field": "ticket_field_names", "value": form_fields[:12]}],
                "dependency_notes": [*notes, f"References shared ticket fields: {', '.join(form_fields[:12])}."],
            }
        ]

    if normalized == "views" and department_name:
        variants = [
            (
                f"{department_name} Open Queue",
                [
                    {"field": "status", "operator": "less_than", "value": "solved"},
                    {"field": "group_id", "operator": "is", "value": department_name},
                ],
            ),
            (
                f"{department_name} Escalations",
                [
                    {"field": "status", "operator": "less_than", "value": "solved"},
                    {"field": "priority", "operator": "greater_than", "value": "normal"},
                    {"field": "group_id", "operator": "is", "value": department_name},
                ],
            ),
        ]
        rows = []
        for index in range(1, requested_count + 1):
            title, conditions = variants[(index - 1) % len(variants)]
            rows.append(
                {
                    "object_type": "views",
                    "title": make_unique_title(title, index),
                    "conditions": [dict(item) for item in conditions],
                    "actions": [{"field": "output_columns", "value": list(DEPARTMENT_VIEW_COLUMNS)}],
                    "dependency_notes": notes,
                }
            )
        return rows

    if normalized == "triggers" and department_name:
        tag_value = tag or _slugify_option_value(department_name)
        variants = [
            (
                f"Routing: {department_name} Intake",
                [
                    {"field": "ticket_form_id", "operator": "is", "value": form_title or department_name},
                    {"field": "status", "operator": "is", "value": "new"},
                ],
                [
                    {"field": "group_id", "value": department_name},
                    {"field": "current_tags", "value": tag_value},
                ],
            ),
            (
                f"Escalation: {department_name} High Priority",
                [
                    {"field": "priority", "operator": "greater_than", "value": "normal"},
                    {"field": "status", "operator": "less_than", "value": "solved"},
                ],
                [
                    {"field": "group_id", "value": department_name},
                    {"field": "current_tags", "value": f"{tag_value} {tag_value}_escalated"},
                ],
            ),
            (
                f"Signal: {department_name} Tagged Follow-Up",
                [
                    {"field": "current_tags", "operator": "includes", "value": tag_value},
                    {"field": "status", "operator": "less_than", "value": "solved"},
                ],
                [
                    {"field": "group_id", "value": department_name},
                    {"field": "current_tags", "value": f"{tag_value} {tag_value}_routed"},
                ],
            ),
        ]
        rows = []
        for index in range(1, requested_count + 1):
            title, conditions, actions = variants[(index - 1) % len(variants)]
            rows.append(
                {
                    "object_type": "triggers",
                    "title": make_unique_title(title, index),
                    "conditions": [dict(item) for item in conditions],
                    "actions": [dict(item) for item in actions],
                    "dependency_notes": notes,
                }
            )
        return rows

    if normalized == "automations" and department_name:
        tag_value = tag or _slugify_option_value(department_name)
        variants = [
            (
                f"Automation: {department_name} Stale Open Follow-Up",
                [
                    {"field": "group_id", "operator": "is", "value": department_name},
                    {"field": "status", "operator": "less_than", "value": "solved"},
                    {"field": "hours_since_update", "operator": "greater_than", "value": "24"},
                ],
                [
                    {"field": "current_tags", "value": f"{tag_value} stale_follow_up"},
                    {"field": "priority", "value": "high"},
                ],
            ),
            (
                f"Automation: {department_name} Escalation Reminder",
                [
                    {"field": "group_id", "operator": "is", "value": department_name},
                    {"field": "priority", "operator": "greater_than", "value": "normal"},
                    {"field": "hours_since_update", "operator": "greater_than", "value": "12"},
                ],
                [
                    {"field": "current_tags", "value": f"{tag_value} escalation_reminder"},
                    {"field": "status", "value": "open"},
                ],
            ),
        ]
        rows = []
        for index in range(1, requested_count + 1):
            title, conditions, actions = variants[(index - 1) % len(variants)]
            rows.append(
                {
                    "object_type": "automations",
                    "title": make_unique_title(title, index),
                    "conditions": [dict(item) for item in conditions],
                    "actions": [dict(item) for item in actions],
                    "dependency_notes": notes,
                }
            )
        return rows

    if normalized == "macros" and department_name:
        tag_value = tag or _slugify_option_value(department_name)
        guidance = _department_copy_guidance(department_name, topic)
        variants = [
            (
                f"{department_name} First Response",
                (
                    f"Thanks for contacting {company_label}. The {department_name} team has received your request and will "
                    f"{guidance['process']}. To avoid delays, reply with {guidance['evidence']}. We will keep progress and decisions in this ticket."
                ),
            ),
            (
                f"{department_name} Missing Information Request",
                (
                    f"The {department_name} team needs more information before the next review. Please reply with {guidance['evidence']}, "
                    f"and confirm the outcome you need. {guidance['urgent']}"
                ),
            ),
            (
                f"{department_name} Escalation Acknowledgement",
                (
                    f"Your request has been escalated to a {department_name} specialist. The specialist will {guidance['process']}. "
                    "Please keep related updates and new evidence on this ticket so the escalation remains coordinated. "
                    f"{guidance['urgent']}"
                ),
            ),
        ]
        rows = []
        for index in range(1, requested_count + 1):
            title, body = variants[(index - 1) % len(variants)]
            rows.append(
                {
                    "object_type": "macros",
                    "title": make_unique_title(title, index),
                    "conditions": [],
                    "actions": [
                        {"field": "comment_value", "value": body},
                        {"field": "current_tags", "value": f"{tag_value} macro_response"},
                    ],
                    "dependency_notes": notes,
                }
            )
        return rows

    if normalized == "articles":
        article_topic = topic or department_name or "Support"
        guidance = _department_copy_guidance(department_name, article_topic)
        variants = [
            (
                f"{article_topic}: What to Expect",
                _html_paragraphs(
                    f"This article explains how {company_label} handles {article_topic.lower()} requests.",
                    f"Before contacting support, gather {guidance['evidence']}. Describe the business or customer impact and the outcome you need.",
                    f"The assigned team will {guidance['process']}. It will record evidence checks, ownership changes, and the resolution decision on the same ticket.",
                    f"{guidance['urgent']} Add new evidence to the existing ticket instead of opening duplicates, because duplicate requests can split context and delay ownership.",
                ),
            ),
            (
                f"{article_topic}: Required Information",
                _html_paragraphs(
                    f"Use this checklist when submitting a {article_topic.lower()} request.",
                    f"Include {guidance['evidence']}. Also select the closest issue category, severity, customer segment, and requested outcome on the support form.",
                    "Make screenshots and documents legible, include relevant dates and references, and explain what has already been tried. Do not send credentials or authentication codes.",
                    f"After submission, the team will {guidance['process']}. {guidance['urgent']}",
                ),
            ),
        ]
        rows = []
        for index in range(1, requested_count + 1):
            title, body = variants[(index - 1) % len(variants)]
            rows.append(
                {
                    "object_type": "articles",
                    "title": make_unique_title(title, index),
                    "conditions": [],
                    "actions": [
                        {"field": "locale", "value": "en-us"},
                        {"field": "section_name", "value": article_topic},
                        {"field": "body", "value": body},
                    ],
                    "dependency_notes": notes,
                }
            )
        return rows

    return []


def _build_deterministic_chunk_rows(
    *,
    object_type: str,
    target_count: int,
    prompt: str,
    reference_catalog: dict[str, list[dict]],
    existing_titles: list[str] | None,
    generated_rows: list[dict] | None,
    reason: str,
    backlog_item: dict | None = None,
    excluded_chunk_id: str = "",
) -> list[dict]:
    normalized_object_type = _normalize_object_type(object_type)
    requested_count = max(int(target_count or 1), 1)
    prompt_text = str(prompt or "").strip()
    explicit_chunk_offset = max(int((backlog_item or {}).get("_chunk_offset", 0) or 0), 0)
    excluded_chunk_id = str(excluded_chunk_id or "").strip()
    generated_rows = [
        row
        for row in (generated_rows or [])
        if isinstance(row, dict)
        and (
            not excluded_chunk_id
            or str(row.get("_supervisor_chunk_id", "")).strip() != excluded_chunk_id
        )
    ]
    existing_title_keys = {
        _normalize_title_for_dedupe(str(row.get("title", "")))
        for row in generated_rows
        if _normalize_object_type(str(row.get("object_type", ""))) == normalized_object_type
        and str(row.get("title", "")).strip()
    }
    if not generated_rows and not excluded_chunk_id:
        existing_title_keys.update(
            _normalize_title_for_dedupe(title)
            for title in (existing_titles or [])
            if str(title).strip()
        )

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
    normalized_reason = str(reason or "").lower()
    if "hybrid deterministic structure prepared" in normalized_reason:
        fallback_note = (
            f"Deterministic {normalized_object_type} structure prepared for model content drafting."
        )
    elif "template-first" in normalized_reason or "template-first generation" in normalized_reason:
        fallback_note = (
            f"Deterministic {normalized_object_type} template used for department coverage. "
            f"Reason: {reason}"
        )
    else:
        fallback_note = (
            f"Deterministic {normalized_object_type} fallback used because model output could not be parsed. "
            f"Source error: {reason}"
        )
    coverage_rows = _department_coverage_row_templates(
        object_type=normalized_object_type,
        target_count=requested_count,
        backlog_item=backlog_item or {},
        fallback_note=fallback_note,
        make_unique_title=_next_unique_title,
        company_name=_extract_company_name_from_prompt(prompt_text),
    )
    if coverage_rows:
        return coverage_rows

    if normalized_object_type == "triggers":
        trigger_specs = _extract_trigger_specs_from_prompt(prompt_text)
        selected_specs = trigger_specs[
            explicit_chunk_offset : explicit_chunk_offset + requested_count
        ]
        if selected_specs:
            for index, spec in enumerate(selected_specs, start=1):
                rows.append(
                    {
                        "object_type": "triggers",
                        "title": str(spec.get("title", "")).strip(),
                        "conditions": [
                            dict(item)
                            for item in list(spec.get("conditions", []) or [])
                            if isinstance(item, dict)
                        ],
                        "actions": [
                            dict(item)
                            for item in list(spec.get("actions", []) or [])
                            if isinstance(item, dict)
                        ],
                        "dependency_notes": [
                            fallback_note,
                            "Compiled from the matching explicit trigger rule in the prompt.",
                        ],
                    }
                )
            return rows

    if normalized_object_type == "macros":
        macro_specs = _extract_macro_specs_from_prompt(prompt_text)
        selected_specs = macro_specs[
            explicit_chunk_offset : explicit_chunk_offset + requested_count
        ]
        if selected_specs:
            for index, spec in enumerate(selected_specs, start=1):
                actions: list[dict[str, object]] = []
                comment = str(spec.get("comment", "")).strip()
                if comment:
                    actions.append({"field": "comment_value", "value": comment})
                    if spec.get("comment_is_public") is not None:
                        actions.append(
                            {
                                "field": "comment_mode_is_public",
                                "value": bool(spec.get("comment_is_public")),
                            }
                        )
                group_name = str(spec.get("group", "")).strip()
                if group_name:
                    actions.append({"field": "group_id", "value": group_name})
                priority = str(spec.get("priority", "")).strip().lower()
                if priority:
                    actions.append({"field": "priority", "value": priority})
                tags = [str(item).strip() for item in list(spec.get("tags", []) or []) if str(item).strip()]
                if tags:
                    actions.append({"field": "current_tags", "value": " ".join(tags)})
                rows.append(
                    {
                        "object_type": "macros",
                        "title": str(spec.get("title", "")).strip(),
                        "conditions": [],
                        "actions": actions,
                        "dependency_notes": [
                            fallback_note,
                            "Compiled from the matching explicit macro definition in the prompt.",
                        ],
                    }
                )
            return rows

    if normalized_object_type == "views":
        view_specs = _extract_view_specs_from_prompt(prompt_text)
        selected_specs = view_specs[
            explicit_chunk_offset : explicit_chunk_offset + requested_count
        ]
        if selected_specs:
            for spec in selected_specs:
                rows.append(
                    {
                        "object_type": "views",
                        "title": str(spec.get("title", "")).strip(),
                        "conditions": [
                            dict(item)
                            for item in list(spec.get("conditions", []) or [])
                            if isinstance(item, dict)
                        ],
                        "actions": [
                            dict(item)
                            for item in list(spec.get("actions", []) or [])
                            if isinstance(item, dict)
                        ],
                        "dependency_notes": [
                            fallback_note,
                            "Compiled from the matching explicit view definition in the prompt.",
                        ],
                    }
                )
            return rows

    if normalized_object_type == "ticket_fields":
        field_specs = _extract_ticket_field_specs_from_prompt(prompt_text)
        used_titles = {
            _normalize_title_for_dedupe(str(row.get("title", "")).strip())
            for row in (generated_rows or [])
            if isinstance(row, dict)
            and _normalize_object_type(str(row.get("object_type", ""))) == "ticket_fields"
            and str(row.get("title", "")).strip()
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
                    "dependency_notes": [
                        fallback_note,
                        *(
                            [
                                "Dropdown options added because later prompt rules explicitly reference them: "
                                + ", ".join(str(item) for item in spec.get("implied_options_added", []))
                            ]
                            if spec.get("implied_options_added")
                            else []
                        ),
                    ],
                }
            )
        return rows

    if normalized_object_type == "groups":
        team_names = _extract_inline_support_team_names(prompt_text)
        used_team_names = {
            _normalize_title_for_dedupe(str(row.get("title", "")))
            for row in (generated_rows or [])
            if isinstance(row, dict)
            and _normalize_object_type(str(row.get("object_type", ""))) == "groups"
        }
        available_team_names = [
            name
            for name in team_names
            if _normalize_title_for_dedupe(name) not in used_team_names
        ]
        title_hints = _extract_title_hints(prompt_text)
        base_title = title_hints[0] if title_hints else "Generated Support Group"
        description_match = re.search(
            r"\b(?:for|supports?)\s+([a-z0-9][a-z0-9 &/_-]{2,120})",
            prompt_text,
            flags=re.IGNORECASE,
        )
        description_hint = str(description_match.group(1)).strip() if description_match else ""
        for index in range(1, requested_count + 1):
            title = (
                available_team_names[index - 1]
                if index <= len(available_team_names)
                else base_title
            )
            actions = []
            if description_hint:
                actions.append({"field": "description", "value": f"Support group for {description_hint}."})
            rows.append(
                {
                    "object_type": "groups",
                    "title": _next_unique_title(title, index),
                    "conditions": [],
                    "actions": actions,
                    "dependency_notes": [fallback_note],
                }
            )
        return rows

    if normalized_object_type == "brands":
        title_hints = _extract_title_hints(prompt_text)
        base_title = title_hints[0] if title_hints else "Generated Brand"
        for index in range(1, requested_count + 1):
            rows.append(
                {
                    "object_type": "brands",
                    "title": _next_unique_title(base_title, index),
                    "conditions": [],
                    "actions": [
                        {"field": "subdomain", "value": _slugify_option_value(f"{base_title}-{index}")[:25]},
                    ],
                    "dependency_notes": [fallback_note],
                }
            )
        return rows

    if normalized_object_type == "categories":
        category_names = _extract_article_category_names(prompt_text)
        used_category_names = {
            _normalize_title_for_dedupe(str(row.get("title", "")))
            for row in (generated_rows or [])
            if isinstance(row, dict)
            and _normalize_object_type(str(row.get("object_type", ""))) == "categories"
        }
        available_category_names = [
            name
            for name in category_names
            if _normalize_title_for_dedupe(name) not in used_category_names
        ]
        title_hints = _extract_title_hints(prompt_text)
        base_title = title_hints[0] if title_hints else "General Information"
        for index in range(1, requested_count + 1):
            title = (
                available_category_names[index - 1]
                if index <= len(available_category_names)
                else base_title
            )
            rows.append(
                {
                    "object_type": "categories",
                    "title": _next_unique_title(title, index),
                    "conditions": [],
                    "actions": [
                        {"field": "locale", "value": "en-us"},
                    ],
                    "dependency_notes": [fallback_note],
                }
            )
        return rows

    if normalized_object_type == "sections":
        category_id: str | None = None
        for category in reference_catalog.get("categories", []) or []:
            if not isinstance(category, dict):
                continue
            candidate = str(category.get("id", "")).strip()
            if candidate.isdigit():
                category_id = candidate
                break
        category_names = _extract_article_category_names(prompt_text)
        used_section_names = {
            _normalize_title_for_dedupe(str(row.get("title", "")))
            for row in (generated_rows or [])
            if isinstance(row, dict)
            and _normalize_object_type(str(row.get("object_type", ""))) == "sections"
        }
        available_section_names = [
            name
            for name in category_names
            if _normalize_title_for_dedupe(name) not in used_section_names
        ]
        title_hints = _extract_title_hints(prompt_text)
        base_title = title_hints[0] if title_hints else "General Support"
        for index in range(1, requested_count + 1):
            title = (
                available_section_names[index - 1]
                if index <= len(available_section_names)
                else base_title
            )
            actions = [{"field": "locale", "value": "en-us"}]
            if category_id:
                actions.append({"field": "category_id", "value": category_id})
            elif title in category_names:
                actions.append({"field": "category_name", "value": title})
            rows.append(
                {
                    "object_type": "sections",
                    "title": _next_unique_title(title, index),
                    "conditions": [],
                    "actions": actions,
                    "dependency_notes": [fallback_note],
                }
            )
        return rows

    if normalized_object_type == "ticket_forms":
        form_specs = _extract_ticket_form_specs_from_prompt(prompt_text)
        used_form_names = {
            _normalize_title_for_dedupe(str(row.get("title", "")))
            for row in (generated_rows or [])
            if isinstance(row, dict)
            and _normalize_object_type(str(row.get("object_type", ""))) == "ticket_forms"
        }
        available_form_specs = [
            spec
            for spec in form_specs
            if _normalize_title_for_dedupe(str(spec.get("title", ""))) not in used_form_names
        ]
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
            form_spec = (
                available_form_specs[index - 1]
                if index <= len(available_form_specs)
                else {}
            )
            form_title = str(form_spec.get("title", "")).strip() or "Generated Intake Form"
            form_fields = [
                str(item).strip()
                for item in list(form_spec.get("fields", []) or [])
                if str(item).strip()
            ] or referenced_fields[:8]
            rows.append(
                {
                    "object_type": "ticket_forms",
                    "title": _next_unique_title(form_title, index),
                    "conditions": [],
                    "actions": [{"field": "ticket_field_names", "value": form_fields[:30]}],
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
        article_specs = _extract_article_specs_from_prompt(prompt_text)
        used_article_names = {
            _normalize_title_for_dedupe(str(row.get("title", "")))
            for row in (generated_rows or [])
            if isinstance(row, dict)
            and _normalize_object_type(str(row.get("object_type", ""))) == "articles"
        }
        if "_chunk_offset" in (backlog_item or {}):
            available_article_specs = article_specs[
                explicit_chunk_offset : explicit_chunk_offset + requested_count
            ]
        else:
            available_article_specs = [
                spec
                for spec in article_specs
                if _normalize_title_for_dedupe(spec.get("title", "")) not in used_article_names
            ]
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
            spec = (
                available_article_specs[index - 1]
                if index <= len(available_article_specs)
                else {}
            )
            article_title = str(spec.get("title", "")).strip() or base_title
            category_name = str(spec.get("category", "")).strip()
            requirements = str(spec.get("requirements", "")).strip()
            body = _html_paragraphs(
                f"This guide explains {article_title.lower()}.",
                requirements
                or "Review the relevant policy details, gather supporting documents, and contact support with the outcome you need.",
                "Keep the request and supporting evidence together so the assigned team can confirm next steps, timing, and any follow-up requirements.",
            )
            actions = [
                {"field": "locale", "value": "en-us"},
                {"field": "body", "value": body},
            ]
            if section_id:
                actions.append({"field": "section_id", "value": section_id})
            elif category_name:
                actions.append({"field": "section_name", "value": category_name})
            rows.append(
                {
                    "object_type": "articles",
                    "title": (
                        article_title
                        if "_chunk_offset" in (backlog_item or {}) and spec
                        else _next_unique_title(article_title, index)
                    ),
                    "conditions": [],
                    "actions": actions,
                    "dependency_notes": [
                        fallback_note,
                        *(
                            [f"Depends on same-batch help center section: {category_name}."]
                            if category_name and not section_id
                            else []
                        ),
                    ],
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
            actions = [{"field": "current_tags", "value": normalized_tag}]
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


def _replace_content_action(row: dict, fields: set[str], value: str) -> None:
    actions = row.get("actions", [])
    actions = [dict(item) for item in actions if isinstance(item, dict)]
    normalized_fields = {str(item).strip().lower() for item in fields}
    selected_field = next(
        (
            str(action.get("field", "")).strip().lower()
            for action in actions
            if str(action.get("field", "")).strip().lower() in normalized_fields
        ),
        sorted(normalized_fields)[0],
    )
    row["actions"] = [
        action
        for action in actions
        if str(action.get("field", "")).strip().lower() not in normalized_fields
    ]
    row["actions"].append({"field": selected_field, "value": value})


async def _draft_department_content_rows(
    *,
    object_type: str,
    target_count: int,
    prompt: str,
    reference_catalog: dict[str, list[dict]],
    existing_titles: list[str],
    generated_rows: list[dict],
    backlog_item: dict,
    model: str,
    api_key: str,
    repair_reasons: list[str] | None = None,
) -> tuple[list[dict], int]:
    normalized_object_type = _normalize_object_type(object_type)
    if normalized_object_type not in {"macros", "articles"}:
        raise ValueError(f"Hybrid content drafting does not support {normalized_object_type}.")

    baseline_rows = _build_deterministic_chunk_rows(
        object_type=normalized_object_type,
        target_count=target_count,
        prompt=prompt,
        reference_catalog=reference_catalog,
        existing_titles=existing_titles,
        generated_rows=generated_rows,
        reason=f"Hybrid deterministic structure prepared for {normalized_object_type} content drafting.",
        backlog_item=backlog_item,
    )
    if not baseline_rows:
        return [], 0

    department = str(backlog_item.get("department_name") or "Support").strip() or "Support"
    topic = str(backlog_item.get("topic") or department).strip() or department
    requirements = (
        "For each macro, write a concise customer-facing response of 90-150 words with specific "
        "evidence requests, next steps, expected outcome, and an urgent escalation instruction when relevant."
        if normalized_object_type == "macros"
        else (
            "For each article, write a practical body of 160-240 words covering preparation, required "
            "evidence, process, escalation, safety/privacy cautions, and expected outcome."
        )
    )
    if repair_reasons:
        requirements += " Correct these failed quality gates: " + " | ".join(
            str(reason).strip() for reason in repair_reasons[:8] if str(reason).strip()
        )
    draft_targets = [
        {
            "title": str(row.get("title", "")).strip(),
            "current_body": _action_text_for_content_draft(row, normalized_object_type),
        }
        for row in baseline_rows
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "You draft human-facing Zendesk content. Return exactly one JSON object with key 'drafts'. "
                "Each draft must contain only 'title' and 'body'. Preserve every supplied title exactly."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "business": _extract_company_name_from_prompt(prompt) or "the company",
                    "department": department,
                    "topic": topic,
                    "object_type": normalized_object_type,
                    "requirements": requirements,
                    "draft_targets": draft_targets,
                },
                ensure_ascii=False,
            ),
        },
    ]
    normalized_model = str(model).strip().lower()
    reasoning_effort = None
    if normalized_model == "qwen/qwen3-32b":
        reasoning_effort = "none"
    elif "gpt-oss" in normalized_model:
        reasoning_effort = "low"
    client = GrokClient()
    raw = await client.chat(
        messages,
        temperature=0.2,
        model=model,
        max_output_tokens=get_settings().department_content_draft_max_output_tokens,
        response_schema=None,
        strict_schema=False,
        task="generator",
        response_format_override="json_object",
        api_key_override=api_key,
        reasoning_effort=reasoning_effort,
        reasoning_format="hidden",
    )
    metrics_getter = getattr(GrokClient, "get_last_call_metrics", None)
    draft_metrics = metrics_getter("generator") if callable(metrics_getter) else {}
    draft_provider = str(draft_metrics.get("provider") or "Primary model").strip()
    payload = extract_json_payload(raw)
    raw_drafts = payload.get("drafts", []) if isinstance(payload, dict) else []
    drafts = [item for item in raw_drafts if isinstance(item, dict)]
    drafts_by_title = {
        _normalize_title_for_dedupe(str(item.get("title", ""))): item
        for item in drafts
        if str(item.get("title", "")).strip()
    }

    applied = 0
    minimum_chars = 220 if normalized_object_type == "macros" else 500
    content_fields = (
        {"comment_value", "comment_value_html", "body"}
        if normalized_object_type == "macros"
        else {"body", "article_body"}
    )
    for index, row in enumerate(baseline_rows):
        title_key = _normalize_title_for_dedupe(str(row.get("title", "")))
        draft = drafts_by_title.get(title_key)
        if draft is None and index < len(drafts):
            draft = drafts[index]
        body = str((draft or {}).get("body") or "").strip()
        if len(body) < minimum_chars:
            continue
        _replace_content_action(row, content_fields, body)
        hybrid_structure_marker = (
            f"Hybrid deterministic structure prepared for {normalized_object_type} content drafting."
        )
        warnings = row.get("warnings", [])
        warnings = list(warnings) if isinstance(warnings, list) else [str(warnings)]
        row["warnings"] = [
            str(warning).strip()
            for warning in warnings
            if str(warning).strip() and hybrid_structure_marker not in str(warning)
        ]
        notes = row.get("dependency_notes", [])
        notes = list(notes) if isinstance(notes, list) else [str(notes)]
        notes = [
            str(note).strip()
            for note in notes
            if str(note).strip() and hybrid_structure_marker not in str(note)
        ]
        notes.append(
            f"{draft_provider} content draft applied for {department}; "
            "deploy-safe structure retained by backend."
        )
        row["dependency_notes"] = list(dict.fromkeys(str(item).strip() for item in notes if str(item).strip()))
        applied += 1
    return baseline_rows, applied


def _action_text_for_content_draft(row: dict, object_type: str) -> str:
    fields = (
        {"comment_value", "comment_value_html", "body"}
        if object_type == "macros"
        else {"body", "article_body"}
    )
    for action in list(row.get("actions", []) or []):
        if not isinstance(action, dict):
            continue
        if str(action.get("field", "")).strip().lower() in fields:
            return _truncate_text(str(action.get("value") or ""), 1200)
    return ""


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
    backlog_item: dict | None = None,
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

    supplement_backlog_item = dict(backlog_item or {})
    if "_chunk_offset" in supplement_backlog_item:
        supplement_backlog_item["_chunk_offset"] = (
            max(int(supplement_backlog_item.get("_chunk_offset", 0) or 0), 0)
            + len(current_rows)
        )
    supplements = _build_deterministic_chunk_rows(
        object_type=object_type,
        target_count=missing,
        prompt=prompt,
        reference_catalog=reference_catalog,
        existing_titles=title_seed,
        generated_rows=list(generated_rows or []) + current_rows,
        reason=reason,
        backlog_item=supplement_backlog_item,
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


def _drop_wildcard_reference_conditions(rows: list[dict]) -> int:
    removed = 0
    wildcard_values = {"*", "all", "any", "all forms", "any form", "all groups", "any group"}
    reference_fields = {"ticket_form", "ticket_form_id", "form", "form_id", "group", "group_id"}
    for row in rows:
        if not isinstance(row, dict):
            continue
        conditions = row.get("conditions", [])
        conditions = conditions if isinstance(conditions, list) else []
        kept: list[dict] = []
        row_removed = 0
        for entry in conditions:
            if not isinstance(entry, dict):
                continue
            field = str(entry.get("field", "")).strip().lower()
            value = re.sub(r"\s+", " ", str(entry.get("value", "")).strip().lower())
            if field in reference_fields and value in wildcard_values:
                removed += 1
                row_removed += 1
                continue
            kept.append(entry)
        row["conditions"] = kept
        if row_removed:
            _append_dependency_note(
                row,
                "Wildcard form/group condition removed; absence of that condition already means all values.",
            )
    return removed


def _apply_explicit_article_dependencies(rows: list[dict], *, prompt: str) -> int:
    specs_by_title = {
        _normalize_title_for_dedupe(str(spec.get("title", ""))): spec
        for spec in _extract_article_specs_from_prompt(prompt)
        if str(spec.get("title", "")).strip()
    }
    applied = 0
    section_aliases = ARTICLE_FIELD_ALIASES["section_id"]
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _normalize_object_type(str(row.get("object_type", ""))) != "articles":
            continue
        section_entries = [
            entry
            for bucket in ("conditions", "actions")
            for entry in list(row.get(bucket, []) or [])
            if isinstance(entry, dict)
            and str(entry.get("field", "")).strip().lower() in section_aliases
            and str(entry.get("value", "")).strip()
        ]
        if any(
            str(entry.get("field", "")).strip().lower() == "section_id"
            and str(entry.get("value", "")).strip().isdigit()
            for entry in section_entries
        ):
            continue
        spec = specs_by_title.get(_normalize_title_for_dedupe(str(row.get("title", ""))))
        category = str((spec or {}).get("category", "")).strip()
        if not category:
            continue
        has_exact_section_name = any(
            str(entry.get("field", "")).strip().lower() == "section_name"
            and _normalize_title_for_dedupe(str(entry.get("value", "")))
            == _normalize_title_for_dedupe(category)
            for entry in section_entries
        )
        if has_exact_section_name:
            continue
        _drop_row_entries_by_aliases(row, section_aliases)
        _set_action_value(row, "section_name", category)
        _append_dependency_note(row, f"Depends on same-batch help center section: {category}.")
        applied += 1
    return applied


def _truncate_text(value: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _prepare_update_request(
    request: ImportAssistantGenerateRequest,
) -> tuple[ImportAssistantGenerateRequest, dict]:
    if request.operation_mode != "update" or request.update_target is None:
        return request, {"operation_mode": "create"}

    target = request.update_target
    target_focus = UPDATE_FOCUS_BY_CONTEXT_TYPE.get(target.object_type)
    if not target_focus:
        raise ValueError(f"Update is not supported for object type '{target.object_type}'.")
    instruction = (
        "EXACT UPDATE MODE. Modify only update_target and return exactly one complete "
        f"{target_focus} record representing its final state. Preserve every unchanged condition and "
        "action from update_target.snapshot. Conditions may include scope=all or scope=any. "
        "Do not create a replacement object, invent IDs, or change object type. Preserve the title "
        "unless the user explicitly asks to rename it."
    )
    notes = " ".join(
        part
        for part in [instruction, str(request.context_notes or "").strip()]
        if part
    )
    prepared = request.model_copy(
        update={
            "dependency_mode": "force_existing_only",
            "focus_object_types": [target_focus],
            "related_objects": [target],
            "context_notes": notes,
        }
    )
    return prepared, {
        "operation_mode": "update",
        "instance_sync_id": request.instance_sync_id,
        "target_object_id": target.id,
        "target_object_type": target.object_type,
        "target_name": target.name,
        "target_updated_at": target.updated_at,
        "target_snapshot_hash": target.snapshot_hash,
        "target_context_included": True,
        "target_snapshot_fields": sorted((target.snapshot or {}).keys()),
    }


def _snapshot_conditions(snapshot: dict, object_type: str) -> list[dict]:
    if object_type == "views":
        raw_groups = {"all": snapshot.get("all", []), "any": snapshot.get("any", [])}
    else:
        raw_conditions = snapshot.get("conditions", {})
        raw_groups = raw_conditions if isinstance(raw_conditions, dict) else {"all": raw_conditions}
    output: list[dict] = []
    for scope in ("all", "any"):
        entries = raw_groups.get(scope, []) if isinstance(raw_groups, dict) else []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not str(entry.get("field", "")).strip():
                continue
            normalized = {
                "field": str(entry.get("field", "")).strip(),
                "operator": str(entry.get("operator", "is")).strip() or "is",
                "scope": scope,
            }
            if "value" in entry:
                normalized["value"] = entry.get("value")
            output.append(normalized)
    return output


def _snapshot_actions(snapshot: dict, object_type: str) -> list[dict]:
    raw_actions = snapshot.get("actions", [])
    if isinstance(raw_actions, list) and raw_actions:
        return [dict(item) for item in raw_actions if isinstance(item, dict) and item.get("field")]

    actions: list[dict] = []

    def add(field: str, value: object) -> None:
        if value is None or value == "" or value == []:
            return
        actions.append({"field": field, "value": value})

    if object_type == "views":
        output = snapshot.get("output", {}) if isinstance(snapshot.get("output"), dict) else {}
        add("output_columns", output.get("columns"))
        add("sort_by", output.get("sort_by"))
        add("sort_order", output.get("sort_order"))
    elif object_type == "ticket_forms":
        add("ticket_field_ids", snapshot.get("ticket_field_ids"))
    elif object_type == "ticket_fields":
        add("field_type", snapshot.get("type"))
        for field in (
            "tag",
            "title_in_portal",
            "custom_field_options",
            "agent_can_edit",
            "visible_in_portal",
            "editable_in_portal",
            "required",
            "required_in_portal",
        ):
            if field in snapshot:
                add(field, snapshot.get(field))
    elif object_type == "groups":
        add("description", snapshot.get("description"))
    elif object_type == "brands":
        add("subdomain", snapshot.get("subdomain"))
    elif object_type == "categories":
        add("locale", snapshot.get("locale"))
        add("description", snapshot.get("description"))
    elif object_type == "sections":
        add("category_id", snapshot.get("category_id"))
        add("locale", snapshot.get("locale"))
        add("description", snapshot.get("description"))
    elif object_type == "articles":
        add("section_id", snapshot.get("section_id"))
        add("body", snapshot.get("body"))
        add("locale", snapshot.get("locale"))
        add("label_names", snapshot.get("label_names"))
    return actions


def _snapshot_configuration(target: dict, object_type: str) -> dict:
    snapshot = target.get("snapshot", {}) if isinstance(target.get("snapshot"), dict) else {}
    return {
        "title": str(snapshot.get("title") or snapshot.get("name") or target.get("name") or "").strip(),
        "active": snapshot.get("active"),
        "conditions": _snapshot_conditions(snapshot, object_type),
        "actions": _snapshot_actions(snapshot, object_type),
    }


def _configuration_change_summary(before: dict, after: dict) -> list[dict]:
    summary: list[dict] = []
    for field, label in (
        ("title", "Name"),
        ("active", "Status"),
        ("conditions", "Conditions"),
        ("actions", "Actions"),
    ):
        before_value = before.get(field)
        after_value = after.get(field)
        changed = json.dumps(before_value, sort_keys=True, default=str) != json.dumps(
            after_value,
            sort_keys=True,
            default=str,
        )
        entry = {"field": field, "label": label, "changed": changed}
        if isinstance(before_value, list) or isinstance(after_value, list):
            entry["before_count"] = len(before_value or [])
            entry["after_count"] = len(after_value or [])
        else:
            entry["before"] = before_value
            entry["after"] = after_value
        summary.append(entry)
    return summary


def _bind_update_target_to_generated_rows(
    rows: list[dict],
    *,
    request: ImportAssistantGenerateRequest,
) -> tuple[list[dict], dict]:
    if request.operation_mode != "update" or request.update_target is None:
        return rows, {"operation_mode": "create", "applied": False}

    target = request.update_target.model_dump()
    expected_type = UPDATE_FOCUS_BY_CONTEXT_TYPE[request.update_target.object_type]
    before = _snapshot_configuration(target, expected_type)
    matching_rows = [
        dict(row)
        for row in rows
        if _normalize_object_type(str(row.get("object_type", ""))) == expected_type
    ]
    binding_warnings: list[str] = []
    if matching_rows:
        row = matching_rows[0]
        if len(rows) != 1:
            binding_warnings.append(
                f"Update generation returned {len(rows)} records; only the exact target record was retained."
            )
    else:
        row = {
            "object_type": expected_type,
            "title": before.get("title") or request.update_target.name,
            "conditions": list(before.get("conditions", []) or []),
            "actions": list(before.get("actions", []) or []),
        }
        row.setdefault("validation_overrides", {})["blocked_reason"] = (
            f"Update generation did not return the selected {expected_type.rstrip('s')} type."
        )

    rename_requested = bool(
        re.search(
            r"\b(?:rename|change\s+(?:the\s+)?(?:name|title))\b",
            request.prompt,
            flags=re.IGNORECASE,
        )
    )
    if not rename_requested:
        row["title"] = before.get("title") or request.update_target.name
    if not list(row.get("conditions", []) or []) and before.get("conditions"):
        row["conditions"] = list(before.get("conditions", []) or [])
    if not list(row.get("actions", []) or []) and before.get("actions"):
        row["actions"] = list(before.get("actions", []) or [])
    if "active" not in row and before.get("active") is not None:
        row["active"] = before.get("active")

    additive_tag_requested = bool(
        re.search(
            r"\badd(?:s|ed|ing)?\b[^.\n]{0,80}\btags?\b",
            request.prompt,
            flags=re.IGNORECASE,
        )
    )
    replace_tags_requested = bool(
        re.search(
            r"\b(?:replace|overwrite|reset|set)\s+(?:all\s+)?(?:the\s+)?tags?\b",
            request.prompt,
            flags=re.IGNORECASE,
        )
    )
    if additive_tag_requested and not replace_tags_requested:
        baseline_action_signatures = {
            json.dumps(action, sort_keys=True, default=str)
            for action in list(before.get("actions", []) or [])
            if isinstance(action, dict)
        }
        normalized_actions: list[dict] = []
        converted = 0
        for action in list(row.get("actions", []) or []):
            if not isinstance(action, dict):
                continue
            normalized_action = dict(action)
            if (
                str(normalized_action.get("field", "")).strip().lower() == "set_tags"
                and json.dumps(normalized_action, sort_keys=True, default=str)
                not in baseline_action_signatures
            ):
                normalized_action["field"] = "current_tags"
                converted += 1
            normalized_actions.append(normalized_action)
        if converted:
            row["actions"] = normalized_actions
            binding_warnings.append(
                "Converted a generated set_tags action to current_tags so the additive update cannot erase existing ticket tags."
            )

    after = {
        "title": str(row.get("title") or "").strip(),
        "active": row.get("active"),
        "conditions": list(row.get("conditions", []) or []),
        "actions": list(row.get("actions", []) or []),
    }
    change_summary = _configuration_change_summary(before, after)
    if not any(item.get("changed") for item in change_summary):
        row.setdefault("validation_overrides", {})["blocked_reason"] = (
            "The proposed update does not change the synchronized Zendesk object."
        )

    dependency_notes = list(row.get("dependency_notes", []) or [])
    dependency_notes.append(
        f"Exact update target: {request.update_target.object_type} '{request.update_target.name}' "
        f"(Zendesk ID {request.update_target.id})."
    )
    dependency_notes.extend(binding_warnings)
    row.update(
        {
            "object_type": expected_type,
            "operation_mode": "update",
            "target_object_id": request.update_target.id,
            "target_object_type": request.update_target.object_type,
            "target_updated_at": request.update_target.updated_at,
            "target_snapshot_hash": request.update_target.snapshot_hash,
            "zendesk_object_id": request.update_target.id,
            "before_configuration": before,
            "after_configuration": after,
            "change_summary": change_summary,
            "dependency_notes": list(dict.fromkeys(str(item) for item in dependency_notes if str(item).strip())),
        }
    )
    return [row], {
        "operation_mode": "update",
        "applied": True,
        "instance_sync_id": request.instance_sync_id,
        "target_object_id": request.update_target.id,
        "target_object_type": request.update_target.object_type,
        "target_name": request.update_target.name,
        "source_record_count": len(rows),
        "retained_record_count": 1,
        "changed_fields": [item["field"] for item in change_summary if item.get("changed")],
        "warnings": binding_warnings,
    }


def _compact_related_objects(related_objects: list[dict], *, max_items: int) -> list[dict]:
    output: list[dict] = []
    for item in related_objects:
        if len(output) >= max_items:
            break
        if not isinstance(item, dict):
            continue
        compact = {
            "object_type": str(item.get("object_type", "")).strip(),
            "id": str(item.get("id", "")).strip(),
            "name": _truncate_text(str(item.get("name", "")), 180),
            "description": _truncate_text(str(item.get("description", "")), 300),
        }
        snapshot = item.get("snapshot")
        if isinstance(snapshot, dict) and snapshot:
            allowed_snapshot_fields = {
                "id",
                "title",
                "name",
                "active",
                "conditions",
                "all",
                "any",
                "actions",
                "output",
                "type",
                "tag",
                "custom_field_options",
                "ticket_field_ids",
                "description",
                "subdomain",
                "locale",
                "category_id",
                "section_id",
                "body",
                "draft",
                "label_names",
                "agent_can_edit",
                "visible_in_portal",
                "editable_in_portal",
                "required",
                "required_in_portal",
                "title_in_portal",
                "updated_at",
            }
            compact["snapshot"] = {
                key: (
                    _truncate_text(value, 20000)
                    if isinstance(value, str)
                    else value
                )
                for key, value in snapshot.items()
                if key in allowed_snapshot_fields
            }
            compact["snapshot_hash"] = str(item.get("snapshot_hash") or "")
            compact["updated_at"] = str(item.get("updated_at") or "")
            compact["update_target"] = True
        output.append(compact)
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
        if normalized_field in {"current_tags", "set_tags"}:
            value = _normalize_set_tags_value(value)
            if not value:
                dropped_invalid += 1
                warnings.append(
                    f"Dropped empty tag entry from {bucket_name}; {normalized_field} requires at least one tag."
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
            scope = str(
                entry.get("scope") or entry.get("condition_scope") or "all"
            ).strip().lower()
            normalized_entry["scope"] = "any" if scope == "any" else "all"
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
    prompt: str,
    reference_catalog: dict[str, list[dict]],
    related_lookup: dict[str, dict[str, str]] | None,
    catalog_lookup: dict[str, dict[str, str]] | None,
    generated_section_lookup: dict[str, str] | None = None,
) -> dict:
    info = {
        "title": str(row.get("title", "Untitled article")).strip() or "Untitled article",
        "object_type": "articles",
        "alias_mappings": [],
        "defaults_applied": [],
        "warnings": [],
        "blocked": False,
        "template_key": None,
    }

    locale_value = _extract_article_value_from_row(row, ARTICLE_FIELD_ALIASES["locale"])
    locale_text = str(locale_value or "").strip().lower() or "en-us"
    _set_action_value(row, "locale", locale_text)
    if not locale_value:
        info["defaults_applied"].append("locale=en-us")

    body_value = _extract_article_value_from_row(row, {"body"})
    if body_value in (None, ""):
        body_value = _extract_article_value_from_row(
            row,
            ARTICLE_FIELD_ALIASES["body"] - {"body"},
        )
    body_text = str(body_value or "").strip()
    if not body_text:
        template_key = _select_article_template_key(prompt)
        if template_key:
            template_text = _load_article_template_text(template_key)
            if template_text:
                variables = _build_article_template_variables(
                    prompt=prompt,
                    reference_catalog=reference_catalog,
                )
                rendered, template_warnings = _render_article_template(
                    template_text=template_text,
                    variables=variables,
                )
                if rendered:
                    body_text = rendered
                    info["template_key"] = template_key
                    info["defaults_applied"].append(f"body=template:{template_key}")
                    info["warnings"].extend(template_warnings)
        if not body_text:
            body_text = f"<p>{info['title']}</p>"
            info["defaults_applied"].append("body=title_template")
    _drop_row_entries_by_aliases(row, ARTICLE_FIELD_ALIASES["body"])
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
    if not str(section_raw or "").strip():
        article_title_key = _normalize_title_for_dedupe(str(row.get("title", "")))
        matching_spec = next(
            (
                spec
                for spec in _extract_article_specs_from_prompt(prompt)
                if _normalize_title_for_dedupe(str(spec.get("title", ""))) == article_title_key
            ),
            None,
        )
        inferred_section_name = str((matching_spec or {}).get("category", "")).strip()
        if inferred_section_name:
            section_raw = inferred_section_name
            info["defaults_applied"].append("section_name=explicit_article_spec")
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
        section_text = str(section_raw or "").strip()
        generated_section_name = None
        if section_text:
            generated_section_name = (generated_section_lookup or {}).get(_normalize_lookup_name(section_text))
        if generated_section_name:
            _drop_row_entries_by_aliases(row, ARTICLE_FIELD_ALIASES["section_id"])
            _set_action_value(row, "section_name", generated_section_name)
            info["defaults_applied"].append("section_name=same_batch_dependency")
            info["warnings"].append(
                f"section_name '{generated_section_name}' will be resolved after same-batch section deployment."
            )
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
    for spec in specs:
        if str(spec.get("field_type", "")) not in {"tagger", "multiselect"}:
            continue
        title = str(spec.get("title", "")).strip()
        if not title:
            continue
        options = [str(item).strip() for item in list(spec.get("options", []) or []) if str(item).strip()]
        option_keys = {_coverage_key(item) for item in options}
        implied: list[str] = []
        condition_pattern = re.compile(
            rf"\b{re.escape(title)}\s+(?:is|equals?)\s+[\"'](?P<value>[^\"']{{1,80}})[\"']",
            flags=re.IGNORECASE,
        )
        for match in condition_pattern.finditer(text):
            value = str(match.group("value") or "").strip()
            key = _coverage_key(value)
            if not key or key in option_keys:
                continue
            option_keys.add(key)
            options.append(value)
            implied.append(value)
        spec["options"] = options[:30]
        if implied:
            spec["implied_options_added"] = implied[:10]
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
                if field in {"current_tags", "set_tags", "tags"} and requested_tag in value:
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
    resolved_links = 0
    unresolved_links = 0
    unresolved_samples: list[str] = []
    patched_rows: list[dict] = []
    same_batch_lookup: dict[str, dict[str, str]] = {
        "brand": {},
        "category": {},
        "section": {},
        "group": {},
        "ticket_form": {},
    }
    generated_reference_types = {
        "brands": "brand",
        "categories": "category",
        "sections": "section",
        "groups": "group",
        "ticket_forms": "ticket_form",
    }
    for row in rows:
        if not isinstance(row, dict):
            continue
        object_type = _normalize_object_type(str(row.get("object_type", "")))
        reference_type = generated_reference_types.get(object_type)
        title = str(row.get("title", "")).strip()
        if reference_type and title:
            same_batch_lookup.setdefault(reference_type, {})[_normalize_lookup_name(title)] = title

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
                same_batch_reference = same_batch_lookup.get(expected_object, {}).get(normalized_value)
                if same_batch_reference:
                    resolved_links += 1
                    row_notes.append(
                        f"{field}: '{value}' references same-batch {expected_object} '{same_batch_reference}'."
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
        if not actions and object_type not in {"ticket_forms", "brands", "categories", "sections"}:
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
            row_warnings.extend(
                note
                for note in dependency_notes
                if not str(note).startswith("Exact update target:")
            )

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
                "zendesk_object_id": item.get("zendesk_object_id"),
                "execution_message": "",
                "operation_mode": str(item.get("operation_mode") or "create"),
                "target_object_id": item.get("target_object_id"),
                "target_object_type": item.get("target_object_type"),
                "target_updated_at": item.get("target_updated_at"),
                "target_snapshot_hash": item.get("target_snapshot_hash"),
                "before_configuration": item.get("before_configuration"),
                "after_configuration": item.get("after_configuration"),
                "change_summary": list(item.get("change_summary", []) or []),
                "chunk_id": item.get("chunk_id") or item.get("_supervisor_chunk_id"),
                "record_key": item.get("record_key") or item.get("_supervisor_record_key"),
                "department": item.get("department") or item.get("department_name"),
                "topic": item.get("topic"),
                "source_provider": item.get("source_provider"),
                "source_model": item.get("source_model"),
            }
        )

    return records, ValidationSummary(passed=passed, warnings=warnings, blocked=blocked)


def _count_generated(records: list[dict]) -> dict[str, int]:
    counts = Counter(row.get("object_type", "recommendations") for row in records)
    return dict(counts)


def _row_search_text(row: dict) -> str:
    parts = [
        str(row.get("title", "")),
        str(row.get("preview_summary", "")),
        " ".join(str(item) for item in list(row.get("warnings", []) or [])),
        " ".join(str(item) for item in list(row.get("dependency_notes", []) or [])),
    ]
    for bucket_name in ("conditions", "actions"):
        for entry in list(row.get(bucket_name, []) or []):
            if isinstance(entry, dict):
                parts.append(str(entry.get("field", "")))
                parts.append(str(entry.get("value", "")))
    return _coverage_key(" ".join(parts))


def _row_matches_department(row: dict, department: dict) -> bool:
    search_text = _row_search_text(row)
    department_name = str(department.get("name", "")).strip()
    form_title = str(department.get("form_title", "")).strip()
    tag = str(department.get("tag", "")).strip()
    candidates = [
        department_name,
        form_title,
        tag,
        str(department.get("slug", "")).strip(),
    ]
    for candidate in candidates:
        key = _coverage_key(candidate)
        if key and key in search_text:
            return True
    department_words = [
        word
        for word in _coverage_key(department_name).split()
        if len(word) > 3 and word not in {"support", "operations", "success"}
    ]
    return bool(department_words and any(word in search_text for word in department_words))


def _evaluate_department_coverage(
    *,
    manifest: dict,
    records: list[dict],
) -> dict:
    if not isinstance(manifest, dict) or not manifest.get("enabled"):
        return {
            "enabled": False,
            "status": "skipped",
            "reason": str((manifest or {}).get("reason", "not_enabled")) if isinstance(manifest, dict) else "not_enabled",
            "departments": [],
            "missing": [],
            "totals": {"required": 0, "generated": len(records)},
        }

    departments = [item for item in list(manifest.get("departments", []) or []) if isinstance(item, dict)]
    minimums = dict(manifest.get("minimums", {}) or DEPARTMENT_HEAVY_MINIMUMS)
    missing: list[dict] = []
    department_results: list[dict] = []
    total_required = 0
    total_matched = 0

    for department in departments:
        department_name = str(department.get("name", "")).strip()
        object_counts: dict[str, int] = {}
        object_missing: dict[str, int] = {}
        for object_type in DEPARTMENT_COVERAGE_OBJECT_TYPES:
            required = max(int(minimums.get(object_type, 0) or 0), 0)
            if required <= 0:
                continue
            count = len(
                [
                    row
                    for row in records
                    if _normalize_object_type(str(row.get("object_type", ""))) == object_type
                    and _row_matches_department(row, department)
                ]
            )
            object_counts[object_type] = count
            total_required += required
            total_matched += min(count, required)
            if count < required:
                deficit = required - count
                object_missing[object_type] = deficit
                missing.append(
                    {
                        "department": department_name,
                        "object_type": object_type,
                        "required": required,
                        "found": count,
                        "missing": deficit,
                    }
                )
        department_results.append(
            {
                "department": department_name,
                "status": "warning" if object_missing else "passed",
                "counts": object_counts,
                "missing": object_missing,
                "tag": department.get("tag"),
                "form_title": department.get("form_title"),
                "topic": department.get("topic"),
            }
        )

    shared_counts = {
        "ticket_fields": len(
            [row for row in records if _normalize_object_type(str(row.get("object_type", ""))) == "ticket_fields"]
        ),
        "categories": len(
            [row for row in records if _normalize_object_type(str(row.get("object_type", ""))) == "categories"]
        ),
        "sections": len(
            [row for row in records if _normalize_object_type(str(row.get("object_type", ""))) == "sections"]
        ),
    }
    shared_required = {
        "ticket_fields": len(list(manifest.get("ticket_fields", []) or [])),
        "categories": len(list(manifest.get("help_center_topics", []) or [])),
        "sections": len(list(manifest.get("help_center_topics", []) or [])),
    }
    shared_missing: dict[str, int] = {}
    for object_type, required in shared_required.items():
        if shared_counts.get(object_type, 0) < required:
            shared_missing[object_type] = required - shared_counts.get(object_type, 0)
            missing.append(
                {
                    "department": "shared",
                    "object_type": object_type,
                    "required": required,
                    "found": shared_counts.get(object_type, 0),
                    "missing": shared_missing[object_type],
                }
            )

    generated_type_counts = Counter(
        _normalize_object_type(str(row.get("object_type", "")))
        for row in records
    )
    global_missing: dict[str, int] = {}
    for object_type, required_value in dict(manifest.get("target_counts", {}) or {}).items():
        normalized_type = _normalize_object_type(str(object_type))
        if normalized_type in shared_required:
            continue
        required = max(int(required_value or 0), 0)
        found = int(generated_type_counts.get(normalized_type, 0) or 0)
        if found >= required:
            continue
        deficit = required - found
        global_missing[normalized_type] = deficit
        missing.append(
            {
                "department": "global",
                "object_type": normalized_type,
                "required": required,
                "found": found,
                "missing": deficit,
            }
        )

    status = "warning" if missing else "passed"
    return {
        "enabled": True,
        "profile": manifest.get("profile", "heavy"),
        "coverage_mode": manifest.get("coverage_mode", "preview_with_warnings"),
        "status": status,
        "departments": department_results,
        "missing": missing,
        "missing_count": len(missing),
        "shared": {
            "counts": shared_counts,
            "required": shared_required,
            "missing": shared_missing,
        },
        "global": {
            "counts": dict(generated_type_counts),
            "required": dict(manifest.get("target_counts", {}) or {}),
            "missing": global_missing,
        },
        "totals": {
            "required": total_required + sum(shared_required.values()),
            "matched": total_matched + sum(min(shared_counts.get(key, 0), value) for key, value in shared_required.items()),
            "generated": len(records),
        },
    }


def _apply_coverage_gate_to_preview(records: list[dict], coverage_gate: dict) -> list[dict]:
    if not coverage_gate.get("enabled") or coverage_gate.get("status") != "warning" or not records:
        return records
    missing = list(coverage_gate.get("missing", []) or [])
    if not missing:
        return records
    samples = [
        f"{item.get('department')}: {item.get('object_type')} missing {item.get('missing')}"
        for item in missing[:6]
        if isinstance(item, dict)
    ]
    summary = "Coverage gate warning: " + "; ".join(samples)
    patched = [dict(row) for row in records]
    first = patched[0]
    warnings = list(first.get("warnings", []) or [])
    if summary not in warnings:
        warnings.append(summary)
    first["warnings"] = warnings
    if first.get("validation_status") == "passed":
        first["validation_status"] = "warning"
    return patched


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
    if normalized_type in {"tagger", "multiselect"} and raw_options in (None, "", []):
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
    if normalized_type in {"tagger", "multiselect"}:
        if parsed_options:
            _set_action_value(row, "custom_field_options", parsed_options)
        else:
            row.setdefault("validation_overrides", {})
            row["validation_overrides"]["blocked_reason"] = (
                "Dropdown/multi-select fields require custom_field_options, but no valid options were generated."
            )
            info["warnings"].append("Missing custom_field_options for dropdown/multi-select field.")
    elif parsed_options:
        info["warnings"].append(
            f"Ignored custom_field_options because field_type={normalized_type} does not support options."
        )

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
        "system_field_references": 0,
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
        "template_applied_count": 0,
        "template_keys": [],
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
    generated_section_lookup: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _normalize_object_type(str(row.get("object_type", ""))) != "sections":
            continue
        section_title = str(row.get("title", "")).strip()
        if not section_title:
            continue
        section_aliases = [
            section_title,
            *[
                str(item).strip()
                for item in list(row.get("_supervisor_title_aliases", []) or [])
                if str(item).strip()
            ],
        ]
        for action in list(row.get("actions", []) or []):
            if not isinstance(action, dict):
                continue
            if str(action.get("field", "")).strip().lower() != "category_name":
                continue
            category_name = str(action.get("value", "")).strip()
            if not category_name:
                continue
            section_aliases.append(category_name)
            topic_alias = re.sub(r"\s+support\s*$", "", category_name, flags=re.IGNORECASE).strip()
            if topic_alias:
                section_aliases.append(topic_alias)
        for alias in section_aliases:
            alias_key = _normalize_lookup_name(alias)
            if alias_key:
                generated_section_lookup[alias_key] = section_title

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
                prompt=prompt,
                reference_catalog=reference_catalog,
                related_lookup=related_lookup,
                catalog_lookup=catalog_lookup,
                generated_section_lookup=generated_section_lookup,
            )
            trigger_article_summary["articles_processed"] += 1
            trigger_article_summary["alias_mappings"] += len(info.get("alias_mappings", []))
            trigger_article_summary["defaults_applied"] += len(info.get("defaults_applied", []))
            trigger_article_summary["warnings"] += len(info.get("warnings", []))
            if bool(info.get("blocked")):
                trigger_article_summary["blocked_records"] += 1
            template_key = str(info.get("template_key", "")).strip()
            if template_key:
                trigger_article_summary["template_applied_count"] += 1
                existing_template_keys = set(trigger_article_summary.get("template_keys", []))
                if template_key not in existing_template_keys:
                    trigger_article_summary["template_keys"] = [
                        *trigger_article_summary.get("template_keys", []),
                        template_key,
                    ]
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

            if ref_text.lower() in ZENDESK_SYSTEM_TICKET_FIELD_NAMES:
                referenced_names.append(ref_text)
                form_resolution["system_field_references"] += 1
                dependency_notes = list(row.get("dependency_notes", []) or [])
                dependency_notes.append(
                    f"Zendesk system field '{ref_text}' will be resolved by exact name at deployment time."
                )
                row["dependency_notes"] = dependency_notes
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
        "operation_mode": request.operation_mode,
        "target_object_id": request.update_target.id if request.update_target else None,
        "target_object_type": request.update_target.object_type if request.update_target else None,
        "target_name": request.update_target.name if request.update_target else None,
        "target_snapshot_hash": request.update_target.snapshot_hash if request.update_target else None,
        "instance_sync_id": request.instance_sync_id if request.update_target else None,
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


async def _generate_import_assistant_batch_impl(
    request: ImportAssistantGenerateRequest,
    *,
    reserved_batch_id: str | None = None,
) -> ImportAssistantGenerateResponse:
    store = get_batch_store()
    sheets = SheetsService()
    appscript = AppScriptBridgeService()
    settings = get_settings()
    request, operation_metadata = _prepare_update_request(request)
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

    batch_id = str(reserved_batch_id or "").strip() or _new_batch_id()
    batch = store.get_batch(batch_id) if reserved_batch_id else None
    if not batch:
        batch = _build_received_batch(request, batch_id=batch_id)
        store.save_batch(batch)
    created_at = str(batch.get("created_at") or _utc_now())
    planner_telemetry: dict = {}
    generator_telemetry: dict = {}
    pause_notified = False
    chunk_estimate: dict = {"estimated_count": 1, "sources": ["default"], "numeric_matches": [], "enumerated_items": 0}
    estimated_requested_records = 1
    pre_planner_object_targets: dict[str, int] = {}
    pre_planner_coverage_manifest: dict = {}
    force_wave_chunk_path = False
    force_wave_chunk_reason = "standard_prompt"
    planner_bypassed = False
    explicit_manifest_planner_bypass = False
    orchestration_mode = "explicit_fast_path"
    orchestration_metadata: dict = {
        "mode": orchestration_mode,
        "department_generation_strategy": settings.department_generation_strategy,
        "department_content_draft_max_output_tokens": (
            settings.department_content_draft_max_output_tokens
        ),
        "waves": [],
        "reconciliation_summary": {
            "create": 0,
            "reuse": 0,
            "update": 0,
            "blocked": 0,
        },
        "assumptions_applied": [],
    }
    supervisor = GeminiSupervisor()
    supervisor_metadata: dict = {
        "enabled": bool(settings.gemini_supervisor_enabled),
        "available": bool(supervisor.enabled),
        "availability_reason": (
            "ok"
            if supervisor.enabled
            else (
                "missing_api_key"
                if settings.gemini_supervisor_enabled
                else "disabled"
            )
        ),
        "model": settings.gemini_supervisor_model,
        "auto_apply_patches": bool(settings.gemini_supervisor_auto_apply_patches),
        "max_concurrency": int(settings.gemini_supervisor_max_concurrency),
        "approval_threshold": float(settings.gemini_supervisor_approval_threshold),
        "max_regeneration_retries": int(settings.gemini_supervisor_max_regeneration_retries),
        "review_grouping": settings.gemini_supervisor_review_grouping,
        "reviews": [],
        "memory": [],
        "verified_memory": [],
        "review_units": [],
        "blocked_chunk_ids": [],
        "remaining_manifest_coverage": {},
        "call_counts": {"consolidated": 0, "retry": 0, "fallback": 0, "total": 0},
        "auto_applied_patches": [],
        "patch_counts": {"applied": 0, "rejected": 0, "skipped": 0},
        "usage": {
            "calls": 0,
            "retries": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "thought_tokens": 0,
            "total_tokens": 0,
            "elapsed_ms_sum": 0.0,
        },
        "failures": [],
    }
    supervisor_semaphore = asyncio.Semaphore(settings.gemini_supervisor_max_concurrency)
    supervisor_tasks: list[asyncio.Task] = []
    supervisor_pending_chunks: list[dict] = []
    supervisor_review_unit_runtime: dict[str, dict] = {}
    progress_narrator = ProgressNarrator()
    progress_narration_tasks: list[asyncio.Task] = []
    progress_narration_calls_scheduled = 0
    progress_metadata: dict = {
        "enabled": bool(progress_narrator.enabled),
        "mode": "deterministic_events_plus_wave_narration",
        "provider": settings.progress_narrator_provider,
        "model": settings.progress_narrator_model,
        "scheduled_calls": 0,
        "completed_calls": 0,
        "fallback_count": 0,
        "appscript_flushes": 0,
        "appscript_flush_failures": 0,
        "wave_checkpoint_successes": 0,
        "wave_checkpoint_failures": 0,
        "wave_checkpoints": [],
        "events": [],
    }

    def _append_progress_event(
        message: str,
        *,
        source: str,
        wave: int | None = None,
        department: str | None = None,
        object_type: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict:
        event = {
            "event_id": f"EVT-{uuid4().hex[:12].upper()}",
            "status": "wave_execution",
            "message": str(message or "").strip(),
            "at": _utc_now(),
            "source": source,
            "provider": provider,
            "model": model,
            "wave": wave,
            "department": department,
            "object_type": object_type,
        }
        append_event = getattr(store, "append_status_event", None)
        if callable(append_event):
            append_event(
                batch_id,
                "wave_execution",
                event["message"],
                context=event,
            )
        else:
            _append_control_status(event["message"])
        progress_metadata.setdefault("events", []).append(event)
        progress_metadata["events"] = progress_metadata["events"][-80:]
        return event

    def _schedule_progress_narration(
        *,
        deterministic_message: str,
        event_context: dict,
    ) -> None:
        nonlocal progress_narration_calls_scheduled
        max_calls = max(int(settings.progress_narrator_max_calls_per_batch), 0)
        if not progress_narrator.enabled or progress_narration_calls_scheduled >= max_calls:
            return
        progress_narration_calls_scheduled += 1
        progress_metadata["scheduled_calls"] = progress_narration_calls_scheduled

        async def run_narration() -> None:
            result = await progress_narrator.narrate(
                deterministic_message=deterministic_message,
                event_context=event_context,
            )
            progress_metadata["completed_calls"] = int(
                progress_metadata.get("completed_calls", 0) or 0
            ) + 1
            if result.fallback_used:
                progress_metadata["fallback_count"] = int(
                    progress_metadata.get("fallback_count", 0) or 0
                ) + 1
            event = _append_progress_event(
                result.message,
                source=result.source,
                wave=event_context.get("wave"),
                department=event_context.get("department"),
                object_type=event_context.get("object_type"),
                provider=result.provider,
                model=result.model,
            )
            if appscript.enabled:
                sync_result = await appscript.invoke(
                    action="append_progress_events",
                    payload={"batch_id": batch_id, "events": [event]},
                    timeout_seconds=min(settings.appscript_timeout_seconds, 8.0),
                )
                if str(sync_result.get("status", "")).strip().lower() == "ok":
                    progress_metadata["appscript_flushes"] = int(
                        progress_metadata.get("appscript_flushes", 0) or 0
                    ) + 1
                else:
                    progress_metadata["appscript_flush_failures"] = int(
                        progress_metadata.get("appscript_flush_failures", 0) or 0
                    ) + 1

        progress_narration_tasks.append(asyncio.create_task(run_narration()))

    async def _await_progress_narrations(timeout_seconds: float = 8.0) -> None:
        if not progress_narration_tasks:
            return
        tasks = list(progress_narration_tasks)
        pending_tasks = [task for task in tasks if not task.done()]
        pending: set[asyncio.Task] = set()
        if pending_tasks:
            _, pending = await asyncio.wait(
                pending_tasks,
                timeout=max(float(timeout_seconds), 0.1),
            )
        for task in pending:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            progress_metadata["task_failures"] = int(
                progress_metadata.get("task_failures", 0) or 0
            ) + len(failures)
        if pending:
            progress_metadata["cancelled_calls"] = int(
                progress_metadata.get("cancelled_calls", 0) or 0
            ) + len(pending)
        progress_narration_tasks.clear()

    def _append_control_status(message: str) -> None:
        current = store.get_batch(batch_id) or {}
        current_status = str(current.get("status") or "generating").strip() or "generating"
        store.append_status(batch_id, current_status, message)

    def _append_supervisor_status(message: str) -> None:
        append_event = getattr(store, "append_status_event", None)
        if callable(append_event):
            append_event(batch_id, "supervisor_review", message)
            return
        _append_control_status(f"Gemini supervisor: {message}")

    def _merge_control_metadata(payload: dict) -> dict:
        current_metadata = _metadata_dict(store.get_batch(batch_id))
        return {
            **payload,
            "run_control": _normalize_run_control(current_metadata.get("run_control", {})),
            "checkpoints": _checkpoint_list_from_metadata(current_metadata),
            "rollback": (
                current_metadata.get("rollback", {})
                if isinstance(current_metadata.get("rollback", {}), dict)
                else {}
            ),
        }

    def _sync_supervisor_memory_to_context() -> None:
        memory = [
            str(item).strip()
            for item in supervisor_metadata.get("memory", [])
            if str(item).strip()
        ][-20:]
        if not memory:
            return
        memory_line = "Gemini supervisor memory: " + " | ".join(memory)
        try:
            bundles = (generator_context_bundle, llm_context_aggressive)
        except NameError:
            return
        for bundle in bundles:
            current = str(bundle.get("context_notes", "") or "").strip()
            if memory_line in current:
                continue
            bundle["context_notes"] = _truncate_text(
                " ".join(part for part in [current, memory_line] if part),
                settings.llm_context_max_notes_chars,
            )

    def _supervisor_allowed_references(extra_rows: list[dict] | None = None) -> dict[str, list[str]]:
        output: dict[str, list[str]] = {"groups": [], "ticket_forms": [], "ticket_fields": []}
        for object_type in output:
            for item in reference_catalog.get(object_type, []) or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("title") or "").strip()
                if name and name not in output[object_type]:
                    output[object_type].append(name)
        for row in [*generated_data, *(extra_rows or [])]:
            if not isinstance(row, dict):
                continue
            if str(row.get("_supervisor_state", "")).strip() == "blocked":
                continue
            object_type = _normalize_object_type(str(row.get("object_type", "")))
            if object_type not in output:
                continue
            title = str(row.get("title", "")).strip()
            if title and title not in output[object_type]:
                output[object_type].append(title)
        return output

    def _compact_supervisor_records(extra_rows: list[dict] | None = None) -> list[dict]:
        def compact_entries(raw_entries: object) -> list[dict]:
            entries = raw_entries if isinstance(raw_entries, list) else []
            compacted: list[dict] = []
            for item in entries[:12]:
                if not isinstance(item, dict):
                    continue
                field = str(item.get("field", "")).strip()
                if not field:
                    continue
                value = item.get("value")
                entry = {"field": field}
                operator = str(item.get("operator", "")).strip()
                if operator:
                    entry["operator"] = operator
                if field.lower() in {"body", "comment_value", "comment_value_html"}:
                    text = str(value or "")
                    entry["value"] = {
                        "chars": len(text),
                        "excerpt": _truncate_text(text, 160),
                    }
                elif isinstance(value, list):
                    compact_values: list[object] = []
                    for child in value[:30]:
                        if isinstance(child, dict):
                            compact_values.append(
                                {
                                    key: _truncate_text(child.get(key), 100)
                                    for key in ("name", "value")
                                    if child.get(key) not in (None, "")
                                }
                            )
                        else:
                            compact_values.append(_truncate_text(child, 100))
                    entry["value"] = compact_values
                    if len(value) > len(compact_values):
                        entry["value_count"] = len(value)
                elif isinstance(value, dict):
                    entry["value"] = {
                        str(key): _truncate_text(child, 100)
                        for key, child in list(value.items())[:12]
                    }
                else:
                    entry["value"] = _truncate_text(value, 180)
                compacted.append(entry)
            return compacted

        pending_ids = {
            str(item.get("chunk_id", ""))
            for item in supervisor_pending_chunks
            if str(item.get("chunk_id", ""))
        }
        output: list[dict] = []
        seen: set[str] = set()
        for row in [*generated_data, *(extra_rows or [])]:
            if not isinstance(row, dict):
                continue
            record_key = str(row.get("_supervisor_record_key") or "").strip()
            fallback_key = (
                f"{_normalize_object_type(str(row.get('object_type', '')))}:"
                f"{_normalize_title_for_dedupe(str(row.get('title', '')))}"
            )
            key = record_key or fallback_key
            if key in seen:
                continue
            seen.add(key)
            chunk_id = str(row.get("_supervisor_chunk_id") or "").strip()
            state = "pending" if chunk_id in pending_ids else "approved"
            if str(row.get("_supervisor_state") or "").strip():
                state = str(row.get("_supervisor_state"))
            output.append(
                {
                    "record_key": key,
                    "chunk_id": chunk_id,
                    "state": state,
                    "object_type": _normalize_object_type(str(row.get("object_type", ""))),
                    "title": str(row.get("title", ""))[:180],
                    "conditions": compact_entries(row.get("conditions", [])),
                    "actions": compact_entries(row.get("actions", [])),
                    "dependency_notes": [
                        _truncate_text(item, 180)
                        for item in list(row.get("dependency_notes", []) or [])[:6]
                        if str(item).strip()
                    ],
                }
            )
        return output

    def _remaining_supervisor_coverage() -> dict:
        approved_rows = [
            row
            for row in generated_data
            if str(row.get("_supervisor_state", "approved")).strip() == "approved"
        ]
        pending_rows = [
            row
            for row in generated_data
            if str(row.get("_supervisor_state", "")).strip() == "pending"
        ]
        try:
            coverage = _evaluate_department_coverage(
                manifest=coverage_manifest,
                records=approved_rows,
            )
        except Exception:  # noqa: BLE001
            return {}
        return {
            "status": coverage.get("status"),
            "totals": coverage.get("totals", {}),
            "missing": list(coverage.get("missing", []) or [])[:80],
            "pending_counts": _count_generated(pending_rows),
        }

    def _build_department_review_units(chunks: list[dict], *, wave: int) -> list[dict]:
        grouped: dict[str, list[dict]] = {}
        for chunk in chunks:
            grouped.setdefault(_supervisor_review_bundle_key(chunk), []).append(chunk)
        units: list[dict] = []
        for bundle_key, members in grouped.items():
            rows = [row for member in members for row in member.get("rows", []) if isinstance(row, dict)]
            units.append(
                {
                    "review_unit_id": f"wave-{wave}:{bundle_key}",
                    "bundle_key": bundle_key,
                    "wave": wave,
                    "rows": rows,
                    "chunks": members,
                    "chunk_specs": [dict(member.get("gate_spec", {})) for member in members],
                    "object_types": sorted(
                        {str(member.get("object_type", "")) for member in members if str(member.get("object_type", ""))}
                    ),
                    "department_name": next(
                        (str(member.get("department_name", "")) for member in members if str(member.get("department_name", ""))),
                        "",
                    ),
                    "topic": next(
                        (str(member.get("topic", "")) for member in members if str(member.get("topic", ""))),
                        "",
                    ),
                }
            )
        return sorted(units, key=lambda item: item["review_unit_id"])

    def _commit_verified_supervisor_memory(rows: list[dict], *, chunk_id: str) -> None:
        verified = supervisor_metadata.setdefault("verified_memory", [])
        memory = supervisor_metadata.setdefault("memory", [])
        for row in rows:
            title = str(row.get("title", "")).strip()
            object_type = _normalize_object_type(str(row.get("object_type", "")))
            if not title or not object_type:
                continue
            fact = {
                "chunk_id": chunk_id,
                "object_type": object_type,
                "title": title,
                "dependencies": [str(item) for item in list(row.get("dependency_notes", []) or [])[:6]],
            }
            key = (chunk_id, object_type, _normalize_title_for_dedupe(title))
            if any(
                (
                    str(item.get("chunk_id", "")),
                    str(item.get("object_type", "")),
                    _normalize_title_for_dedupe(str(item.get("title", ""))),
                )
                == key
                for item in verified
                if isinstance(item, dict)
            ):
                continue
            verified.append(fact)
            memory_line = f"Verified {object_type}: {title}."
            if memory_line not in memory:
                memory.append(memory_line)
        supervisor_metadata["verified_memory"] = verified[-200:]
        supervisor_metadata["memory"] = memory[-60:]

    def _record_supervisor_result(
        *,
        result: dict,
        context: dict,
    ) -> None:
        review = result.get("review", {}) if isinstance(result.get("review", {}), dict) else {}
        patch_summary = (
            result.get("patch_summary", {})
            if isinstance(result.get("patch_summary", {}), dict)
            else {}
        )
        patch_results = list(patch_summary.get("patch_results", []) or [])
        telemetry = result.get("telemetry", {}) if isinstance(result.get("telemetry", {}), dict) else {}
        if telemetry:
            usage = supervisor_metadata.setdefault("usage", {})
            usage["calls"] = int(usage.get("calls", 0) or 0) + 1
            usage["retries"] = int(usage.get("retries", 0) or 0) + int(
                telemetry.get("retry_count", 0) or 0
            )
            for token_key in ("input_tokens", "output_tokens", "thought_tokens", "total_tokens"):
                usage[token_key] = int(usage.get(token_key, 0) or 0) + int(
                    telemetry.get(token_key, 0) or 0
                )
            usage["elapsed_ms_sum"] = round(
                float(usage.get("elapsed_ms_sum", 0.0) or 0.0)
                + float(telemetry.get("elapsed_ms", 0.0) or 0.0),
                2,
            )
        applied = int(patch_summary.get("applied", 0) or 0)
        rejected = int(patch_summary.get("rejected", 0) or 0)
        skipped = len([item for item in patch_results if item.get("status") == "skipped"])
        counts = supervisor_metadata.setdefault(
            "patch_counts",
            {"applied": 0, "rejected": 0, "skipped": 0},
        )
        counts["applied"] = int(counts.get("applied", 0) or 0) + applied
        counts["rejected"] = int(counts.get("rejected", 0) or 0) + rejected
        counts["skipped"] = int(counts.get("skipped", 0) or 0) + skipped

        for patch_result in patch_results:
            if patch_result.get("status") == "applied":
                supervisor_metadata.setdefault("auto_applied_patches", []).append(
                    {
                        **context,
                        "operation": patch_result.get("operation"),
                        "target_index": patch_result.get("target_index"),
                        "target_title": patch_result.get("target_title"),
                        "reason": patch_result.get("reason"),
                    }
                )
        if len(supervisor_metadata.get("auto_applied_patches", [])) > 100:
            supervisor_metadata["auto_applied_patches"] = supervisor_metadata["auto_applied_patches"][-100:]

        review_entry = {
            **context,
            "status": result.get("status", "reviewed"),
            "model": settings.gemini_supervisor_model,
            "approved": bool(review.get("approved", False)),
            "quality_score": review.get("quality_score"),
            "raw_quality_score": result.get("gate", {}).get(
                "raw_quality_score", review.get("quality_score")
            ),
            "effective_quality_score": result.get("gate", {}).get("effective_quality_score"),
            "effective_approved": result.get("gate", {}).get("effective_approved"),
            "approval_gate_reasons": list(result.get("gate", {}).get("approval_gate_reasons", []) or [])[:12],
            "chunk_assessments": list(result.get("gate", {}).get("chunk_assessments", []) or [])[:20],
            "context_gaps": list(review.get("context_gaps", []) or [])[:6],
            "dependency_issues": list(review.get("dependency_issues", []) or [])[:6],
            "requires_regeneration": bool(review.get("requires_regeneration", False)),
            "public_reasoning_summary": _truncate_text(
                str(review.get("public_reasoning_summary", "")),
                500,
            ),
            "patches_requested": len(review.get("patches", []) or []),
            "patches_applied": applied,
            "patches_rejected": rejected,
            "patch_results": patch_results[:12],
            "latency_ms": result.get("latency_ms", 0),
            "attempt_count": telemetry.get("attempt_count", 0),
            "retry_count": telemetry.get("retry_count", 0),
            "input_tokens": telemetry.get("input_tokens", 0),
            "output_tokens": telemetry.get("output_tokens", 0),
            "thought_tokens": telemetry.get("thought_tokens", 0),
            "total_tokens": telemetry.get("total_tokens", 0),
            "reason": result.get("reason", ""),
            "memory_suggestions": list(review.get("memory_delta", []) or [])[:8],
        }
        supervisor_metadata.setdefault("reviews", []).append(review_entry)
        if len(supervisor_metadata["reviews"]) > 100:
            supervisor_metadata["reviews"] = supervisor_metadata["reviews"][-100:]

    async def _run_supervisor_review(
        *,
        rows: list[dict],
        context: dict,
        object_type: str,
        wave: int | None,
        wave_position: int | None,
        chunk_index: int,
        chunk_total: int,
        blueprint: dict | None,
        chunk_specs: list[dict] | None = None,
        review_scope: dict | None = None,
        call_kind: str = "consolidated",
    ) -> dict:
        chunk_specs = list(chunk_specs or [])
        allowed_references = _supervisor_allowed_references(rows)
        current_row_ids = {id(row) for row in rows}
        reserved_titles: dict[str, list[str]] = {}
        for generated_row in generated_data:
            if not isinstance(generated_row, dict) or id(generated_row) in current_row_ids:
                continue
            generated_object_type = _normalize_object_type(
                str(generated_row.get("object_type", ""))
            )
            generated_title = str(generated_row.get("title", "")).strip()
            if generated_object_type and generated_title:
                reserved_titles.setdefault(generated_object_type, []).append(generated_title)
        cumulative_records = _compact_supervisor_records(rows)
        remaining_coverage = _remaining_supervisor_coverage()
        if not settings.gemini_supervisor_enabled:
            result = {
                "status": "skipped",
                "reason": "disabled",
                "records": rows,
                "review": {},
                "patch_summary": {"applied": 0, "rejected": 0, "patch_results": []},
                "latency_ms": 0,
            }
            _record_supervisor_result(result=result, context=context)
            return result
        if not supervisor.configured:
            result = {
                "status": "skipped",
                "reason": "missing_api_key",
                "records": rows,
                "review": {},
                "patch_summary": {"applied": 0, "rejected": 0, "patch_results": []},
                "latency_ms": 0,
            }
            _record_supervisor_result(result=result, context=context)
            return result
        async with supervisor_semaphore:
            supervisor_metadata.setdefault("call_counts", {}).setdefault(call_kind, 0)
            supervisor_metadata["call_counts"][call_kind] += 1
            supervisor_metadata["call_counts"]["total"] = sum(
                int(supervisor_metadata["call_counts"].get(kind, 0) or 0)
                for kind in ("consolidated", "retry", "fallback")
            )
            _append_supervisor_status(
                (
                    f"Gemini reviewing {object_type} chunk "
                    f"{chunk_index}/{chunk_total} for safe patches."
                )
            )
            try:
                result = await supervisor.review_and_patch_chunk(
                    prompt=request.prompt,
                    records=rows,
                    object_type=object_type,
                    wave=wave,
                    wave_position=wave_position,
                    chunk_index=chunk_index,
                    chunk_total=chunk_total,
                    blueprint=blueprint,
                    supervisor_memory=list(supervisor_metadata.get("memory", []) or []),
                    reference_catalog=reference_catalog,
                    review_scope=review_scope,
                    cumulative_records=cumulative_records,
                    remaining_manifest_coverage=remaining_coverage,
                    allowed_references=allowed_references,
                    reserved_titles=reserved_titles,
                    update_target=(
                        _compact_related_objects(
                            [request.update_target.model_dump()],
                            max_items=1,
                        )[0]
                        if request.operation_mode == "update" and request.update_target is not None
                        else None
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                failure = {
                    **context,
                    "error": _truncate_text(str(exc), 500),
                    "strict": bool(settings.gemini_supervisor_strict_mode),
                }
                supervisor_metadata.setdefault("failures", []).append(failure)
                if len(supervisor_metadata["failures"]) > 50:
                    supervisor_metadata["failures"] = supervisor_metadata["failures"][-50:]
                _append_supervisor_status(
                    f"Gemini review skipped after error: {_truncate_text(str(exc), 220)}"
                )
                if settings.gemini_supervisor_strict_mode:
                    return {"status": "failed", "failure": failure}
                fallback_review = {
                    "approved": True,
                    "quality_score": 1.0,
                    "requires_regeneration": False,
                    "chunk_assessments": [
                        {
                            "chunk_id": str(spec.get("chunk_id", "")),
                            "approved": True,
                            "quality_score": 1.0,
                            "blocking_issues": [],
                            "requires_regeneration": False,
                        }
                        for spec in chunk_specs
                    ],
                }
                gate = evaluate_supervisor_bundle(
                    rows=rows,
                    chunk_specs=chunk_specs,
                    review=fallback_review,
                    approval_threshold=settings.gemini_supervisor_approval_threshold,
                    allowed_references=allowed_references,
                )
                error_result = {
                    "status": "error",
                    "reason": str(exc),
                    "records": rows,
                    "review": fallback_review,
                    "gate": {**gate, "supervisor_unavailable": True},
                    "patch_summary": {"applied": 0, "rejected": 0, "patch_results": []},
                    "telemetry": getattr(exc, "telemetry", {}),
                    "latency_ms": float(getattr(exc, "telemetry", {}).get("elapsed_ms", 0.0) or 0.0),
                    "_review_unit_id": str((review_scope or {}).get("review_unit_id", "")),
                }
                _record_supervisor_result(result=error_result, context=context)
                return error_result

            patched_rows = result.get("records", rows)
            if isinstance(patched_rows, list) and len(patched_rows) == len(rows):
                for index, patched_row in enumerate(patched_rows):
                    if isinstance(patched_row, dict):
                        patched_snapshot = dict(patched_row)
                        rows[index].clear()
                        rows[index].update(patched_snapshot)
            gate = evaluate_supervisor_bundle(
                rows=rows,
                chunk_specs=chunk_specs,
                review=result.get("review", {}),
                approval_threshold=settings.gemini_supervisor_approval_threshold,
                allowed_references=allowed_references,
            )
            gate["approval_gate_reasons"] = list(
                dict.fromkeys(
                    reason
                    for item in gate.get("chunk_assessments", [])
                    for reason in item.get("approval_gate_reasons", [])
                )
            )
            result["gate"] = gate
            result["_review_unit_id"] = str((review_scope or {}).get("review_unit_id", ""))
            _record_supervisor_result(result=result, context=context)
            patch_summary = result.get("patch_summary", {})
            applied_count = (
                int(patch_summary.get("applied", 0) or 0)
                if isinstance(patch_summary, dict)
                else 0
            )
            _append_supervisor_status(
                (
                    f"Gemini review completed for {object_type} chunk "
                    f"{chunk_index}/{chunk_total}; applied={applied_count}."
                )
            )
            return result

    def _schedule_supervisor_review(
        *,
        rows: list[dict],
        object_type: str,
        wave: int | None,
        wave_position: int | None,
        chunk_index: int,
        chunk_total: int,
        backlog_id: str = "",
        chunk_specs: list[dict] | None = None,
        review_scope: dict | None = None,
        call_kind: str = "consolidated",
    ) -> None:
        if not settings.gemini_supervisor_enabled:
            return
        context = {
            "wave": wave,
            "wave_position": wave_position,
            "chunk_index": chunk_index,
            "chunk_total": chunk_total,
            "object_type": object_type,
            "backlog_id": backlog_id,
            "review_unit_id": str((review_scope or {}).get("review_unit_id", "")),
            "bundle_key": str((review_scope or {}).get("bundle_key", "")),
            "call_kind": call_kind,
        }
        normalized_specs = list(chunk_specs or [])
        if not normalized_specs:
            fallback_chunk_id = f"{backlog_id or object_type}:{chunk_index}"
            exact_update = bool(
                request.operation_mode == "update" and request.update_target is not None
            )
            for index, row in enumerate(rows):
                row.setdefault("_supervisor_chunk_id", fallback_chunk_id)
                row.setdefault("_supervisor_record_key", f"{fallback_chunk_id}:{index}")
            normalized_specs = [
                {
                    "chunk_id": fallback_chunk_id,
                    "object_type": object_type,
                    "target_count": len(rows) or 1,
                    "operation_mode": "update" if exact_update else "create",
                    "require_group_routing": not exact_update,
                    "require_routing_tag": not exact_update,
                }
            ]
        task = asyncio.create_task(
            _run_supervisor_review(
                rows=rows,
                context=context,
                object_type=object_type,
                wave=wave,
                wave_position=wave_position,
                chunk_index=chunk_index,
                chunk_total=chunk_total,
                blueprint=blueprint_payload,
                chunk_specs=normalized_specs,
                review_scope=review_scope,
                call_kind=call_kind,
            )
        )
        supervisor_tasks.append(task)

    def _queue_department_supervisor_chunk(
        *,
        rows: list[dict],
        item: dict,
        object_type: str,
        wave: int,
        chunk_index: int,
        target_count: int,
        retry_callback=None,
        fallback_callback=None,
    ) -> None:
        backlog_id = str(item.get("backlog_id", "")).strip() or f"wave-{wave}-{object_type}"
        chunk_id = f"{backlog_id}:{chunk_index}"
        for row_index, row in enumerate(rows):
            row["_supervisor_chunk_id"] = chunk_id
            row["_supervisor_record_key"] = f"{chunk_id}:{row_index}"
            row["_supervisor_state"] = "pending"
        expected_titles: list[str] = []
        if object_type == "groups" and item.get("department_name"):
            expected_titles = [str(item.get("department_name"))]
        elif object_type == "ticket_forms" and item.get("form_title"):
            expected_titles = [str(item.get("form_title"))]
        elif object_type in {"views", "triggers", "macros", "articles"}:
            explicit_specs = {
                "views": _extract_view_specs_from_prompt,
                "triggers": _extract_trigger_specs_from_prompt,
                "macros": _extract_macro_specs_from_prompt,
                "articles": _extract_article_specs_from_prompt,
            }[object_type](request.prompt)
            offset = max(int(item.get("_chunk_offset", 0) or 0), 0)
            expected_titles = [
                str(spec.get("title", "")).strip()
                for spec in explicit_specs[offset : offset + max(int(target_count or 1), 1)]
                if str(spec.get("title", "")).strip()
            ]
        gate_spec = {
            "chunk_id": chunk_id,
            "object_type": object_type,
            "target_count": max(int(target_count or 1), 1),
            "department_name": str(item.get("department_name", "")),
            "topic": str(item.get("topic", "")),
            "coverage_kind": str(item.get("coverage_kind", "")),
            "fields": list(item.get("fields", []) or []),
            "expected_titles": expected_titles,
            "operation_mode": "create",
            "require_group_routing": object_type == "triggers",
            "require_routing_tag": object_type == "triggers",
        }
        supervisor_pending_chunks.append(
            {
                "chunk_id": chunk_id,
                "rows": rows,
                "gate_spec": gate_spec,
                "object_type": object_type,
                "wave": wave,
                "backlog_id": backlog_id,
                "department_name": str(item.get("department_name", "")),
                "topic": str(item.get("topic", "")),
                "title_hint": str(item.get("title_hint", "")),
                "coverage_kind": str(item.get("coverage_kind", "")),
                "retry_callback": retry_callback,
                "fallback_callback": fallback_callback,
                "regeneration_attempts": 0,
                "fallback_after_supervisor_failure": False,
            }
        )

    def _schedule_department_wave_reviews(wave: int) -> None:
        chunks = [
            chunk
            for chunk in supervisor_pending_chunks
            if int(chunk.get("wave", 0) or 0) == int(wave)
            and not chunk.get("review_scheduled")
        ]
        for unit in _build_department_review_units(chunks, wave=int(wave)):
            unit_id = unit["review_unit_id"]
            for chunk in unit["chunks"]:
                chunk["review_scheduled"] = True
                chunk["review_unit_id"] = unit_id
            supervisor_review_unit_runtime[unit_id] = unit
            supervisor_metadata.setdefault("review_units", []).append(
                {
                    "review_unit_id": unit_id,
                    "bundle_key": unit["bundle_key"],
                    "wave": unit["wave"],
                    "department_name": unit["department_name"],
                    "topic": unit["topic"],
                    "object_types": unit["object_types"],
                    "chunk_ids": [chunk["chunk_id"] for chunk in unit["chunks"]],
                    "status": "scheduled",
                }
            )
            _schedule_supervisor_review(
                rows=unit["rows"],
                object_type="department_bundle",
                wave=int(wave),
                wave_position=None,
                chunk_index=1,
                chunk_total=1,
                backlog_id=unit_id,
                chunk_specs=unit["chunk_specs"],
                review_scope={
                    "review_unit_id": unit_id,
                    "bundle_key": unit["bundle_key"],
                    "wave": unit["wave"],
                    "department_name": unit["department_name"],
                    "topic": unit["topic"],
                    "object_types": unit["object_types"],
                    "chunk_requirements": unit["chunk_specs"],
                },
                call_kind="consolidated",
            )

    async def _await_supervisor_reviews(stage: str) -> None:
        nonlocal generated_data, total_duplicates_dropped, chunked_titles, chunked_title_set
        if not supervisor_tasks:
            return
        pending = list(supervisor_tasks)
        supervisor_tasks.clear()
        _append_supervisor_status(
            f"Waiting for {len(pending)} Gemini supervisor review(s) before {stage}."
        )
        results = await asyncio.gather(*pending, return_exceptions=True)
        strict_failure = None
        for result in results:
            if isinstance(result, Exception):
                strict_failure = result
                supervisor_metadata.setdefault("failures", []).append(
                    {"stage": stage, "error": _truncate_text(str(result), 500)}
                )
                continue
            if isinstance(result, dict) and result.get("status") == "failed":
                strict_failure = GeminiSupervisorError(
                    str(result.get("failure", {}).get("error") or "Supervisor review failed.")
                )
        if strict_failure and settings.gemini_supervisor_strict_mode:
            raise GenerateFailureError(
                code="supervisor_review_failed",
                reason=f"Gemini supervisor review failed before {stage}: {strict_failure}",
                next_step="Fix Gemini supervisor configuration or disable strict supervisor mode and retry.",
            ) from strict_failure

        def replace_chunk_rows(chunk: dict, replacement_rows: list[dict]) -> None:
            nonlocal generated_data
            chunk_id = str(chunk.get("chunk_id", ""))
            _apply_explicit_article_dependencies(replacement_rows, prompt=request.prompt)
            first_index = next(
                (
                    index
                    for index, row in enumerate(generated_data)
                    if str(row.get("_supervisor_chunk_id", "")) == chunk_id
                ),
                len(generated_data),
            )
            generated_data = [
                row
                for row in generated_data
                if str(row.get("_supervisor_chunk_id", "")) != chunk_id
            ]
            for row_index, row in enumerate(replacement_rows):
                row["_supervisor_chunk_id"] = chunk_id
                row["_supervisor_record_key"] = f"{chunk_id}:{row_index}"
                row["_supervisor_state"] = "pending"
            generated_data[first_index:first_index] = replacement_rows
            chunk["rows"] = replacement_rows

        async def review_replacement(chunk: dict, *, call_kind: str) -> dict:
            rows = list(chunk.get("rows", []) or [])
            return await _run_supervisor_review(
                rows=rows,
                context={
                    "wave": chunk.get("wave"),
                    "wave_position": None,
                    "chunk_index": 1,
                    "chunk_total": 1,
                    "object_type": chunk.get("object_type"),
                    "backlog_id": chunk.get("backlog_id"),
                    "review_unit_id": f"{chunk.get('chunk_id')}:{call_kind}",
                    "bundle_key": chunk.get("chunk_id"),
                    "call_kind": call_kind,
                },
                object_type=str(chunk.get("object_type", "")),
                wave=int(chunk.get("wave", 0) or 0),
                wave_position=None,
                chunk_index=1,
                chunk_total=1,
                blueprint=blueprint_payload,
                chunk_specs=[dict(chunk.get("gate_spec", {}))],
                review_scope={
                    "review_unit_id": f"{chunk.get('chunk_id')}:{call_kind}",
                    "bundle_key": chunk.get("chunk_id"),
                    "wave": chunk.get("wave"),
                    "department_name": chunk.get("department_name"),
                    "topic": chunk.get("topic"),
                    "object_types": [chunk.get("object_type")],
                    "chunk_requirements": [dict(chunk.get("gate_spec", {}))],
                    "repair_attempt": call_kind,
                },
                call_kind=call_kind,
            )

        async def resolve_failed_chunk(chunk: dict, assessment: dict) -> bool:
            gate_reasons = list(assessment.get("approval_gate_reasons", []) or [])
            retry_callback = chunk.get("retry_callback")
            max_retries = int(settings.gemini_supervisor_max_regeneration_retries)
            if callable(retry_callback) and max_retries > 0:
                chunk["regeneration_attempts"] = int(chunk.get("regeneration_attempts", 0) or 0) + 1
                supervisor_metadata.setdefault("regeneration_attempts", []).append(
                    {
                        "chunk_id": chunk.get("chunk_id"),
                        "attempt": chunk["regeneration_attempts"],
                        "reasons": gate_reasons[:10],
                    }
                )
                _append_supervisor_status(
                    f"Regenerating failed chunk {chunk.get('chunk_id')} only."
                )
                try:
                    regenerated = await retry_callback(gate_reasons)
                except Exception as exc:  # noqa: BLE001
                    regenerated = []
                    gate_reasons.append(f"Targeted regeneration failed: {_truncate_text(str(exc), 240)}")
                if regenerated:
                    replace_chunk_rows(chunk, regenerated)
                    retry_result = await review_replacement(chunk, call_kind="retry")
                    retry_assessments = list(retry_result.get("gate", {}).get("chunk_assessments", []) or [])
                    if retry_assessments and retry_assessments[0].get("effective_approved"):
                        for row in chunk.get("rows", []):
                            row["_supervisor_state"] = "approved"
                        _commit_verified_supervisor_memory(
                            chunk.get("rows", []),
                            chunk_id=str(chunk.get("chunk_id", "")),
                        )
                        return True
                    if retry_assessments:
                        gate_reasons = list(retry_assessments[0].get("approval_gate_reasons", []) or [])

            fallback_callback = chunk.get("fallback_callback")
            if callable(fallback_callback):
                chunk["fallback_after_supervisor_failure"] = True
                try:
                    fallback_rows = fallback_callback(gate_reasons)
                except Exception as exc:  # noqa: BLE001
                    fallback_rows = []
                    gate_reasons.append(f"Deterministic fallback failed: {_truncate_text(str(exc), 240)}")
                if fallback_rows:
                    replace_chunk_rows(chunk, fallback_rows)
                    fallback_result = await review_replacement(chunk, call_kind="fallback")
                    fallback_assessments = list(
                        fallback_result.get("gate", {}).get("chunk_assessments", []) or []
                    )
                    if fallback_assessments and fallback_assessments[0].get("effective_approved"):
                        for row in chunk.get("rows", []):
                            row["_supervisor_state"] = "approved"
                        _commit_verified_supervisor_memory(
                            chunk.get("rows", []),
                            chunk_id=str(chunk.get("chunk_id", "")),
                        )
                        return True
                    if fallback_assessments:
                        gate_reasons = list(
                            fallback_assessments[0].get("approval_gate_reasons", []) or []
                        )

            blocked_reason = "Gemini supervisor quality gate failed after targeted retry and fallback."
            if gate_reasons:
                blocked_reason += " " + " | ".join(gate_reasons[:5])
            for row in chunk.get("rows", []):
                row["_supervisor_state"] = "blocked"
                row.setdefault("validation_overrides", {})["blocked_reason"] = blocked_reason
            blocked_ids = supervisor_metadata.setdefault("blocked_chunk_ids", [])
            if chunk.get("chunk_id") not in blocked_ids:
                blocked_ids.append(chunk.get("chunk_id"))
            return False

        for result in results:
            if not isinstance(result, dict):
                continue
            unit_id = str(result.get("_review_unit_id", ""))
            unit = supervisor_review_unit_runtime.get(unit_id)
            if not unit:
                continue
            assessments = {
                str(item.get("chunk_id", "")): item
                for item in result.get("gate", {}).get("chunk_assessments", []) or []
                if isinstance(item, dict)
            }
            unit_approved = True
            for chunk in unit.get("chunks", []):
                assessment = assessments.get(str(chunk.get("chunk_id", "")), {})
                if assessment.get("effective_approved"):
                    for row in chunk.get("rows", []):
                        row["_supervisor_state"] = "approved"
                    _commit_verified_supervisor_memory(
                        chunk.get("rows", []),
                        chunk_id=str(chunk.get("chunk_id", "")),
                    )
                else:
                    unit_approved = bool(await resolve_failed_chunk(chunk, assessment)) and unit_approved
            for entry in supervisor_metadata.get("review_units", []):
                if entry.get("review_unit_id") == unit_id:
                    entry["status"] = "approved" if unit_approved else "blocked"
                    break
        before = len(generated_data)
        generated_data, dropped = _dedupe_generated_rows(generated_data)
        total_duplicates_dropped += dropped
        chunked_titles = []
        chunked_title_set = set()
        for row in generated_data:
            title = str(row.get("title", "")).strip()
            title_key = _normalize_title_for_dedupe(title)
            if not title or not title_key or title_key in chunked_title_set:
                continue
            chunked_title_set.add(title_key)
            chunked_titles.append(title)
        _sync_supervisor_memory_to_context()
        supervisor_metadata["remaining_manifest_coverage"] = _remaining_supervisor_coverage()
        supervisor_metadata["total_duplicates_dropped_after_patch"] = int(
            supervisor_metadata.get("total_duplicates_dropped_after_patch", 0) or 0
        ) + dropped
        _append_supervisor_status(
            (
                f"Gemini supervisor reviews settled before {stage}; "
                f"records={len(generated_data)} dropped_after_patch={max(before - len(generated_data), 0)}."
            )
        )

    async def _honor_run_control(*, checkpoint: str, wave_checkpoint: bool = False) -> None:
        nonlocal pause_notified
        await asyncio.sleep(0)
        current = store.get_batch(batch_id) or {}
        metadata = _metadata_dict(current)
        control = _normalize_run_control(metadata.get("run_control", {}))
        if wave_checkpoint and control.get("pause_after_wave") and not control.get("pause_requested"):
            control["pause_requested"] = True
            control["pause_after_wave"] = False
            control["updated_at"] = _utc_now()
            control["updated_by"] = control.get("updated_by") or "local-user"
            store.update_batch(
                batch_id,
                {
                    "metadata": {
                        **metadata,
                        "run_control": control,
                    }
                },
            )
            _append_control_status(
                f"Visual pause flag reached at {checkpoint}. Generation continues in background."
            )
            pause_notified = True

        if control.get("cancel_requested"):
            cancel_reason = str(control.get("cancel_reason") or "").strip().lower()
            if cancel_reason.startswith("checkpoint_rejected:"):
                rejected_checkpoint = cancel_reason.split(":", 1)[-1] or "checkpoint"
                raise GenerateFailureError(
                    code="checkpoint_rejected",
                    reason=(
                        f"Checkpoint {rejected_checkpoint} was rejected during {checkpoint}. "
                        "Run cancelled and rollback requested."
                    ),
                    next_step="Review rollback outcome and run a new generate pass when ready.",
                )
            raise GenerateFailureError(
                code="run_cancelled",
                reason=f"Run cancelled by user during {checkpoint}.",
                next_step="Submit a new prompt when you are ready to run again.",
            )

        if control.get("pause_requested") and not pause_notified:
            _append_control_status(
                f"Visual pause enabled at {checkpoint}. Generation is still running in the background."
            )
            pause_notified = True
        if not control.get("pause_requested") and pause_notified:
            _append_control_status(f"Visual pause cleared at {checkpoint}.")
            pause_notified = False

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
        if request.operation_mode == "update" and request.update_target is not None:
            target_object_type = UPDATE_FOCUS_BY_CONTEXT_TYPE[request.update_target.object_type]
            chunk_estimate = {
                "estimated_count": 1,
                "sources": ["exact_update_target"],
                "numeric_matches": [],
                "enumerated_items": 0,
            }
            estimated_requested_records = 1
            inferred_object_type_for_explicitness = target_object_type
            prompt_explicit_for_bypass = True
            pre_planner_object_targets = {target_object_type: 1}
            pre_planner_coverage_manifest = {
                "enabled": False,
                "reason": "exact_update_target",
                "departments": [],
            }
            force_wave_chunk_path = False
            force_wave_chunk_reason = "exact_update_target"
            explicit_manifest_planner_bypass = False
            planner_bypassed = True
        else:
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
            pre_planner_coverage_manifest = _build_department_coverage_manifest(
                prompt=request.prompt,
                focus_object_types=focus_object_types,
            )
            force_wave_chunk_path, force_wave_chunk_reason = _should_force_wave_chunk_path(
                prompt=request.prompt,
                estimated_count=estimated_requested_records,
                object_targets=pre_planner_object_targets,
                chunk_estimate=chunk_estimate,
            )
            explicit_manifest_planner_bypass = _should_bypass_planner_for_explicit_manifest(
                prompt=request.prompt,
                object_targets=pre_planner_object_targets,
                force_wave_chunk_reason=force_wave_chunk_reason,
            )
            planner_bypassed = bool(
                pre_planner_coverage_manifest.get("enabled")
                or explicit_manifest_planner_bypass
                or (
                    estimated_requested_records == 1
                    and prompt_explicit_for_bypass
                    and not force_wave_chunk_path
                )
            )
        store.append_status(batch_id, "request_validated", "Incoming request validated.")
        await _honor_run_control(checkpoint="planning start")
    except Exception as exc:  # noqa: BLE001
        failure_reason = f"Request initialization failed: {exc}"
        next_step = "Retry generation after resolving request initialization/runtime issues."
        failure_metadata = _merge_control_metadata({
            "benchmark": {"enabled": benchmark_mode},
            "llm_routes": llm_routes_metadata,
            "supervisor": supervisor_metadata,
            "failure": {
                "failure_stage": "generate",
                "failure_code": "generate_runtime_error",
                "failure_reason": failure_reason,
                "next_step": next_step,
            },
        })
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
        planner_bypass_reason = (
            "exact_update_target"
            if request.operation_mode == "update"
            else (
                "department_coverage_manifest"
                if pre_planner_coverage_manifest.get("enabled")
                else (
                    "explicit_numbered_operating_model"
                    if explicit_manifest_planner_bypass
                    else "single_item_explicit_prompt"
                )
            )
        )
        store.append_status(
            batch_id,
            "planning",
            f"Planner bypassed for {planner_bypass_reason}; using deterministic inference.",
        )
        plan = _build_deterministic_planner_plan(
            prompt=request.prompt,
            dependency_mode=request.dependency_mode,
            focus_object_types=focus_object_types,
            estimated_count=estimated_requested_records,
            bypass_reason=planner_bypass_reason,
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
    planner_used_heuristic_fallback = _planner_used_heuristic_fallback(plan)
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
    if request.operation_mode == "update" and request.update_target is not None:
        resolved_object_type = UPDATE_FOCUS_BY_CONTEXT_TYPE[request.update_target.object_type]
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
    coverage_manifest = pre_planner_coverage_manifest or _build_department_coverage_manifest(
        prompt=request.prompt,
        focus_object_types=focus_object_types,
    )
    department_coverage_metadata: dict = {
        "enabled": bool(coverage_manifest.get("enabled")),
        "status": "pending" if coverage_manifest.get("enabled") else "skipped",
        "reason": coverage_manifest.get("reason", ""),
        "departments": [],
        "missing": [],
    }
    quality_gates_metadata: dict = {
        "coverage": department_coverage_metadata,
    }
    use_business_blueprint, business_reason = _is_business_blueprint_prompt(
        prompt=request.prompt,
        focus_object_types=focus_object_types,
        estimated_count=estimated_requested_records,
        prompt_explicit=prompt_explicit,
        object_targets=object_targets,
    )
    if request.operation_mode == "update":
        use_business_blueprint = False
        business_reason = "exact_update_target"
    if coverage_manifest.get("enabled"):
        use_business_blueprint = True
        if business_reason == "standard_record_prompt":
            business_reason = "department_coverage_manifest"
    if force_wave_chunk_path:
        use_business_blueprint = True
        business_reason = force_wave_chunk_reason
    blueprint_payload: dict = {}
    orchestration_backlog: list[dict] = []
    try:
        if use_business_blueprint and (
            not planner_bypassed
            or coverage_manifest.get("enabled")
            or explicit_manifest_planner_bypass
        ):
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
                    deterministic_only=(
                        explicit_manifest_planner_bypass
                        or planner_used_heuristic_fallback
                    ),
                    deterministic_reason=(
                        "planner_heuristic_fallback"
                        if planner_used_heuristic_fallback
                        else "explicit_numbered_operating_model"
                    ),
                )
                if not planner_telemetry and isinstance(blueprint_telemetry, dict):
                    planner_telemetry = blueprint_telemetry
                if isinstance(blueprint_telemetry, dict):
                    if blueprint_telemetry.get("blueprint_failover_used"):
                        profile = str(
                            blueprint_telemetry.get("blueprint_failover_profile")
                            or "alternate"
                        ).strip()
                        store.append_status(
                            batch_id,
                            "business_blueprinting",
                            (
                                "Primary blueprint model route was unavailable; "
                                f"continued with the {profile} Groq lane."
                            ),
                        )
                    elif blueprint_telemetry.get("blueprint_fallback_used"):
                        reason = str(
                            blueprint_telemetry.get("blueprint_fallback_reason")
                            or "model route unavailable"
                        ).strip()
                        store.append_status(
                            batch_id,
                            "business_blueprinting",
                            (
                                "Model blueprint routes were unavailable "
                                f"({reason}); continuing with the deterministic blueprint."
                            ),
                        )
                    elif (
                        str(blueprint_telemetry.get("reason") or "").strip()
                        == "planner_heuristic_fallback"
                    ):
                        store.append_status(
                            batch_id,
                            "business_blueprinting",
                            (
                                "Planner already selected deterministic inference; "
                                "skipped the redundant model compiler call."
                            ),
                        )
                if isinstance(blueprint_payload.get("coverage_manifest"), dict):
                    coverage_manifest = blueprint_payload["coverage_manifest"]
                    department_coverage_metadata = {
                        "enabled": bool(coverage_manifest.get("enabled")),
                        "status": "pending" if coverage_manifest.get("enabled") else "skipped",
                        "reason": coverage_manifest.get("reason", ""),
                        "departments": [],
                        "missing": [],
                    }
                    quality_gates_metadata["coverage"] = department_coverage_metadata
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
            if settings.gemini_supervisor_review_grouping == "department":
                supervisor_metadata["review_unit_plan"] = _build_department_supervisor_review_manifest(
                    orchestration_backlog
                )
                supervisor_metadata["planned_consolidated_calls"] = len(
                    supervisor_metadata["review_unit_plan"]
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
                "metadata": _merge_control_metadata({
                    "benchmark": {"enabled": benchmark_mode},
                    "planning": {
                        "planner_bypassed": planner_bypassed,
                        "mode": "deterministic" if planner_bypassed else "llm",
                        "estimated_requested_records": estimated_requested_records,
                        "force_wave_chunk_path": force_wave_chunk_path,
                        "force_wave_chunk_reason": force_wave_chunk_reason,
                    },
                    "coverage_manifest": coverage_manifest,
                    "department_coverage": department_coverage_metadata,
                    "quality_gates": quality_gates_metadata,
                    "orchestration": orchestration_metadata,
                    "supervisor": supervisor_metadata,
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
                }),
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
                "metadata": _merge_control_metadata({
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
                    "coverage_manifest": coverage_manifest,
                    "department_coverage": department_coverage_metadata,
                    "quality_gates": quality_gates_metadata,
                    "orchestration": orchestration_metadata,
                    "chunking": chunking_metadata,
                    "supervisor": supervisor_metadata,
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
                }),
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

    async def _persist_wave_checkpoint(
        *,
        wave: int,
        wave_position: int,
        total_waves: int,
    ) -> None:
        checkpoint_records, checkpoint_validation = _build_preview_records(plan, generated_data)
        checkpoint_counts = _count_generated(checkpoint_records)
        checkpoint_result = {
            "wave": int(wave),
            "wave_position": int(wave_position),
            "total_waves": int(total_waves),
            "record_count": len(checkpoint_records),
            "generated_counts": checkpoint_counts,
            "persisted_at": _utc_now(),
            "local_status": "persisted",
            "appscript_status": "pending",
        }
        checkpoint_history = progress_metadata.setdefault("wave_checkpoints", [])
        checkpoint_history.append(checkpoint_result)
        progress_metadata["wave_checkpoints"] = checkpoint_history[-10:]

        def checkpoint_metadata() -> dict:
            return _merge_control_metadata(
                {
                    "benchmark": {"enabled": benchmark_mode},
                    "operation": operation_metadata,
                    "planning": {
                        "planner_bypassed": planner_bypassed,
                        "mode": "deterministic" if planner_bypassed else "llm",
                        "estimated_requested_records": estimated_requested_records,
                        "force_wave_chunk_path": force_wave_chunk_path,
                        "force_wave_chunk_reason": force_wave_chunk_reason,
                    },
                    "coverage_manifest": coverage_manifest,
                    "department_coverage": department_coverage_metadata,
                    "quality_gates": quality_gates_metadata,
                    "orchestration": orchestration_metadata,
                    "chunking": chunking_metadata,
                    "supervisor": supervisor_metadata,
                    "progress_narration": progress_metadata,
                    "llm_routes": llm_routes_metadata,
                    "recovery_checkpoint": checkpoint_result,
                }
            )

        store.update_batch(
            batch_id,
            {
                "records": checkpoint_records,
                "generated_counts": checkpoint_counts,
                "validation_summary": checkpoint_validation.model_dump(),
                "planning_summary": planning_summary,
                "metadata": checkpoint_metadata(),
            },
        )

        checkpoint_enabled = bool(
            getattr(settings, "appscript_wave_checkpoint_enabled", False)
        )
        if not checkpoint_enabled:
            checkpoint_result["appscript_status"] = "disabled"
            store.update_batch(batch_id, {"metadata": checkpoint_metadata()})
            return
        if not appscript.enabled:
            checkpoint_result["appscript_status"] = "unavailable"
            checkpoint_result["detail"] = "Apps Script integration is not configured."
            progress_metadata["wave_checkpoint_failures"] = int(
                progress_metadata.get("wave_checkpoint_failures", 0) or 0
            ) + 1
            store.update_batch(batch_id, {"metadata": checkpoint_metadata()})
            return

        status_history = list(
            (store.get_batch(batch_id) or {}).get("status_history", []) or []
        )[-100:]
        timeout_seconds = float(
            getattr(settings, "appscript_wave_checkpoint_timeout_seconds", 20.0)
        )
        try:
            raw_result = await appscript.invoke(
                action="write_batch_to_sheets",
                payload={
                    "batch_id": batch_id,
                    "prompt": request.prompt,
                    "requester": request.requester,
                    "status": "generating_wave_checkpoint",
                    "target_environment": request.target_environment,
                    "created_at": created_at,
                    "planning_summary": planning_summary,
                    "records": checkpoint_records,
                    "metadata": checkpoint_metadata(),
                    "progress_events": status_history,
                },
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            raw_result = {
                "action": "write_batch_to_sheets",
                "status": "error",
                "detail": _truncate_text(str(exc), 300),
                "http_status": None,
                "data": {},
            }
        normalized_result = _normalize_appscript_action_result(
            raw_result,
            action="write_batch_to_sheets",
        )
        checkpoint_result["appscript_status"] = normalized_result.get("status", "error")
        checkpoint_result["http_status"] = normalized_result.get("http_status")
        detail = str(normalized_result.get("detail") or "").strip()
        if detail:
            checkpoint_result["detail"] = _truncate_text(detail, 300)
        if normalized_result.get("status") == "ok":
            progress_metadata["wave_checkpoint_successes"] = int(
                progress_metadata.get("wave_checkpoint_successes", 0) or 0
            ) + 1
        else:
            progress_metadata["wave_checkpoint_failures"] = int(
                progress_metadata.get("wave_checkpoint_failures", 0) or 0
            ) + 1
        store.update_batch(batch_id, {"metadata": checkpoint_metadata()})

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
                await _honor_run_control(
                    checkpoint=f"wave {wave_position}/{total_wave_count} start"
                )
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
                wave_route_cursor = 0
                _append_progress_event(
                    build_wave_start_message(
                        wave=int(wave),
                        wave_position=wave_position,
                        total_waves=total_wave_count,
                        items=wave_items,
                    ),
                    source="deterministic",
                    wave=int(wave),
                )

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
                        object_type == "ticket_fields"
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
                        chunk_backlog_item = dict(item)
                        chunk_offset = sum(
                            int(value or 0)
                            for value in item_chunk_targets[: local_chunk_index - 1]
                        )
                        chunk_backlog_item["_chunk_offset"] = chunk_offset
                        if isinstance(item.get("fields"), list):
                            chunk_backlog_item["fields"] = list(item.get("fields", []))[
                                chunk_offset : chunk_offset + int(item_chunk_target)
                            ]
                        department_template_first = _should_use_department_template_first(
                            chunk_backlog_item,
                            object_type,
                            strategy=settings.department_generation_strategy,
                        )
                        explicit_template_first = _should_use_explicit_template_first(
                            prompt=request.prompt,
                            object_type=object_type,
                            target_count=int(item_chunk_target),
                        )
                        template_first_for_chunk = bool(
                            department_template_first or explicit_template_first
                        )
                        chunk_item_plan = dict(item_plan)
                        chunk_item_plan["intent"] = _build_wave_prompt(
                            prompt=request.prompt,
                            item=chunk_backlog_item,
                            reconciliation=reconciliation,
                        )
                        await _honor_run_control(
                            checkpoint=(
                                f"wave {wave_position}/{total_wave_count} "
                                f"{object_type} chunk {local_chunk_index}/{len(item_chunk_targets)}"
                            )
                        )
                        if template_first_for_chunk:
                            model_hint = (
                                ", template=department"
                                if department_template_first
                                else ", template=explicit_prompt"
                            )
                        else:
                            model_hint = (
                                f", model={item_generator_model}"
                                if item_generator_model and item_generator_model != primary_generator_model
                                else ""
                            )
                        if chunk_targets_manifest and not template_first_for_chunk:
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

                        _append_progress_event(
                            build_chunk_message(
                                wave_position=wave_position,
                                total_waves=total_wave_count,
                                item=chunk_backlog_item,
                                object_type=object_type,
                                chunk_index=local_chunk_index,
                                chunk_total=len(item_chunk_targets),
                                target_count=int(item_chunk_target),
                                mode=(
                                    (
                                        "department_template"
                                        if department_template_first
                                        else "explicit_template"
                                    )
                                    if template_first_for_chunk
                                    else (
                                        "gemini"
                                        if settings.llm_default_provider == "gemini"
                                        else "active_route"
                                    )
                                ),
                            ),
                            source="deterministic",
                            wave=int(wave),
                            department=str(
                                chunk_backlog_item.get("department_name")
                                or chunk_backlog_item.get("topic")
                                or ""
                            ).strip()
                            or None,
                            object_type=object_type,
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
                        if not wave_routes and not template_first_for_chunk:
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
                        if wave_routes and not template_first_for_chunk:
                            active_route, wave_route_cursor = _select_wave_generator_route(
                                wave_routes,
                                wave_route_cursor,
                            )
                        elif wave_routes:
                            active_route = dict(wave_routes[0])
                        else:
                            active_route, _ = _select_wave_generator_route([], wave_route_cursor)
                        selected_model_for_chunk = active_route.get("model")
                        selected_api_key_for_chunk = active_route.get("api_key")
                        selected_api_key_profile = str(active_route.get("profile", "primary")).strip() or "primary"
                        chunk_route_failovers: list[dict[str, str]] = []
                        chunk_error: RuntimeError | None = None
                        fallback_reason: str | None = None
                        chunk_rows: list[dict] = []
                        chunk_runtime_metrics: dict = {}
                        context_profile = "standard"
                        content_drafts_applied = 0
                        if template_first_for_chunk:
                            forced_deterministic_for_chunk = True
                            forced_reason = (
                                f"Department coverage uses template-first generation for {object_type}."
                                if department_template_first
                                else f"Explicit prompt structure uses deterministic-first generation for {object_type}."
                            )
                            chunk_rows = _build_deterministic_chunk_rows(
                                object_type=object_type,
                                target_count=int(item_chunk_target),
                                prompt=request.prompt,
                                reference_catalog=reference_catalog,
                                existing_titles=chunked_titles[-200:],
                                generated_rows=generated_data,
                                reason=forced_reason,
                                backlog_item=chunk_backlog_item,
                            )
                            if not chunk_rows:
                                raise GenerateFailureError(
                                    code="wave_generation_failed",
                                    reason=(
                                        f"Department template could not build valid {object_type} rows for "
                                        f"wave {wave_position}/{total_wave_count} chunk "
                                        f"{local_chunk_index}/{len(item_chunk_targets)}."
                                    ),
                                    next_step=(
                                        "Retry with explicit department, group, form, and routing hints for this "
                                        "object type."
                                    ),
                                )
                            used_deterministic_fallback = True
                            fallback_reason = forced_reason
                            context_profile = (
                                "department_template"
                                if department_template_first
                                else "explicit_template"
                            )
                            chunk_runtime_metrics = {
                                "pre_request_wait_ms": 0.0,
                                "retry_count": 0,
                                "final_status": "template_first",
                                "http_status": None,
                                "model": "deterministic_template",
                            }
                        elif (
                            settings.department_generation_strategy == "hybrid"
                            and object_type in {"macros", "articles"}
                            and str(chunk_backlog_item.get("source", "")).strip()
                            == DEPARTMENT_COVERAGE_SOURCE
                        ):
                            try:
                                chunk_rows, content_drafts_applied = await _draft_department_content_rows(
                                    object_type=object_type,
                                    target_count=int(item_chunk_target),
                                    prompt=request.prompt,
                                    reference_catalog=reference_catalog,
                                    existing_titles=chunked_titles[-200:],
                                    generated_rows=generated_data,
                                    backlog_item=chunk_backlog_item,
                                    model=str(selected_model_for_chunk or ""),
                                    api_key=str(selected_api_key_for_chunk or ""),
                                )
                                chunk_runtime_metrics = GrokClient.get_last_call_metrics("generator")
                                context_profile = "department_hybrid_draft"
                                if content_drafts_applied < int(item_chunk_target):
                                    used_deterministic_fallback = True
                                    fallback_reason = (
                                        f"Primary model drafted {content_drafts_applied}/{int(item_chunk_target)} "
                                        f"{object_type}; backend template copy retained for missing drafts."
                                    )
                            except (RuntimeError, TypeError, ValueError) as draft_exc:
                                chunk_runtime_metrics = GrokClient.get_last_call_metrics("generator")
                                chunk_rows = _build_deterministic_chunk_rows(
                                    object_type=object_type,
                                    target_count=int(item_chunk_target),
                                    prompt=request.prompt,
                                    reference_catalog=reference_catalog,
                                    existing_titles=chunked_titles[-200:],
                                    generated_rows=generated_data,
                                    reason=f"Hybrid content drafting failed: {draft_exc}",
                                    backlog_item=chunk_backlog_item,
                                )
                                used_deterministic_fallback = True
                                fallback_reason = str(draft_exc)
                                context_profile = "department_hybrid_draft_fallback"
                        elif object_type in forced_deterministic_object_types:
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
                                backlog_item=chunk_backlog_item,
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
                                    plan=chunk_item_plan,
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
                            for fallback_route in wave_routes:
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
                                        plan=chunk_item_plan,
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
                                    backlog_item=chunk_backlog_item,
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
                            backlog_item=chunk_backlog_item,
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
                                backlog_item=chunk_backlog_item,
                            )
                            if alignment_topup_count > 0:
                                used_deterministic_fallback = True
                                deterministic_topup_count += alignment_topup_count
                                if fallback_reason:
                                    fallback_reason = f"{fallback_reason} | {mismatch_reason}"
                                else:
                                    fallback_reason = mismatch_reason

                        wildcard_conditions_removed = _drop_wildcard_reference_conditions(chunk_rows)
                        if wildcard_conditions_removed:
                            store.append_status(
                                batch_id,
                                "generating",
                                (
                                    f"Wave {wave_position}/{total_wave_count} {object_type} normalized "
                                    f"{wildcard_conditions_removed} wildcard reference condition(s)."
                                ),
                            )

                        article_dependencies_added = _apply_explicit_article_dependencies(
                            chunk_rows,
                            prompt=request.prompt,
                        )
                        if article_dependencies_added:
                            store.append_status(
                                batch_id,
                                "generating",
                                (
                                    f"Wave {wave_position}/{total_wave_count} articles linked "
                                    f"{article_dependencies_added} record(s) to explicit same-batch sections."
                                ),
                            )

                        is_grouped_supervisor_chunk = bool(
                            settings.gemini_supervisor_review_grouping == "department"
                        )
                        if is_grouped_supervisor_chunk:
                            retry_plan = dict(chunk_item_plan)
                            retry_item = dict(chunk_backlog_item)
                            retry_target = int(item_chunk_target)
                            retry_chunk_index = int(local_chunk_index)
                            retry_chunk_total = len(item_chunk_targets)
                            retry_model = selected_model_for_chunk
                            retry_api_key = selected_api_key_for_chunk
                            retry_compatibility_first = compatibility_first_for_item
                            retry_compatibility_only = compatibility_only_for_item
                            retry_template_first = template_first_for_chunk
                            retry_backlog_id = (
                                str(retry_item.get("backlog_id", "")).strip()
                                or f"wave-{wave}-{object_type}"
                            )
                            retry_source_chunk_id = f"{retry_backlog_id}:{retry_chunk_index}"

                            async def retry_callback(
                                gate_reasons,
                                *,
                                _plan=retry_plan,
                                _item=retry_item,
                                _target=retry_target,
                                _chunk_index=retry_chunk_index,
                                _chunk_total=retry_chunk_total,
                                _model=retry_model,
                                _api_key=retry_api_key,
                                _compatibility_first=retry_compatibility_first,
                                _compatibility_only=retry_compatibility_only,
                                _template_first=retry_template_first,
                                _object_type=object_type,
                                _source_chunk_id=retry_source_chunk_id,
                            ):
                                repair_reason = (
                                    "Supervisor repair required for this chunk only: "
                                    + " | ".join(str(reason) for reason in gate_reasons[:8])
                                )
                                replacement_context_rows = [
                                    row
                                    for row in generated_data
                                    if str(row.get("_supervisor_chunk_id", "")).strip()
                                    != _source_chunk_id
                                ]
                                replacement_existing_titles = [
                                    str(row.get("title", "")).strip()
                                    for row in replacement_context_rows
                                    if str(row.get("title", "")).strip()
                                ][-200:]
                                if _template_first:
                                    return _build_deterministic_chunk_rows(
                                        object_type=_object_type,
                                        target_count=_target,
                                        prompt=request.prompt,
                                        reference_catalog=reference_catalog,
                                        existing_titles=replacement_existing_titles,
                                        generated_rows=replacement_context_rows,
                                        reason=repair_reason,
                                        backlog_item=_item,
                                        excluded_chunk_id=_source_chunk_id,
                                    )
                                if (
                                    settings.department_generation_strategy == "hybrid"
                                    and _object_type in {"macros", "articles"}
                                    and str(_item.get("source", "")).strip()
                                    == DEPARTMENT_COVERAGE_SOURCE
                                    and _model
                                    and _api_key
                                ):
                                    repaired_rows, _ = await _draft_department_content_rows(
                                        object_type=_object_type,
                                        target_count=_target,
                                        prompt=request.prompt,
                                        reference_catalog=reference_catalog,
                                        existing_titles=replacement_existing_titles,
                                        generated_rows=replacement_context_rows,
                                        backlog_item=_item,
                                        model=str(_model),
                                        api_key=str(_api_key),
                                        repair_reasons=list(gate_reasons or []),
                                    )
                                    return repaired_rows
                                repair_instruction = (
                                    _build_chunk_instruction(
                                        chunk_index=_chunk_index,
                                        chunk_total=_chunk_total,
                                        target_count=_target,
                                    )
                                    + "\n"
                                    + repair_reason
                                    + " Return only the repaired source chunk; preserve object_type and target count."
                                )
                                repaired_rows, _, _ = await _run_generator_with_context_fallback(
                                    plan=_plan,
                                    request=generator_request,
                                    focus_object_types=focus_object_types,
                                    standard_context_bundle=generator_context_bundle,
                                    aggressive_context_bundle=llm_context_aggressive,
                                    chunk_instruction=repair_instruction,
                                    chunk_target_count=_target,
                                    chunk_index=_chunk_index,
                                    chunk_total=_chunk_total,
                                    existing_titles=replacement_existing_titles,
                                    compatibility_first=_compatibility_first,
                                    compatibility_only=_compatibility_only,
                                    model_override=_model,
                                    api_key_override=_api_key,
                                )
                                repaired_rows, _ = _enforce_expected_object_type(
                                    rows=repaired_rows,
                                    expected_object_type=_object_type,
                                )
                                repaired_rows, _ = _supplement_chunk_rows_to_target(
                                    object_type=_object_type,
                                    target_count=_target,
                                    chunk_rows=repaired_rows,
                                    prompt=request.prompt,
                                    reference_catalog=reference_catalog,
                                    existing_titles=replacement_existing_titles,
                                    generated_rows=replacement_context_rows,
                                    reason=repair_reason,
                                    backlog_item=_item,
                                )
                                return repaired_rows

                            def fallback_callback(
                                gate_reasons,
                                *,
                                _item=retry_item,
                                _target=retry_target,
                                _object_type=object_type,
                                _source_chunk_id=retry_source_chunk_id,
                            ):
                                reason = (
                                    "Supervisor retry exhausted; deterministic fallback applied: "
                                    + " | ".join(str(value) for value in gate_reasons[:8])
                                )
                                return _build_deterministic_chunk_rows(
                                    object_type=_object_type,
                                    target_count=_target,
                                    prompt=request.prompt,
                                    reference_catalog=reference_catalog,
                                    existing_titles=[
                                        str(row.get("title", "")).strip()
                                        for row in generated_data
                                        if str(row.get("_supervisor_chunk_id", "")).strip()
                                        != _source_chunk_id
                                        and str(row.get("title", "")).strip()
                                    ][-200:],
                                    generated_rows=generated_data,
                                    reason=reason,
                                    backlog_item=_item,
                                    excluded_chunk_id=_source_chunk_id,
                                )

                            _queue_department_supervisor_chunk(
                                rows=chunk_rows,
                                item=chunk_backlog_item,
                                object_type=object_type,
                                wave=int(wave),
                                chunk_index=local_chunk_index,
                                target_count=int(item_chunk_target),
                                retry_callback=retry_callback,
                                fallback_callback=fallback_callback,
                            )
                        else:
                            _schedule_supervisor_review(
                                rows=chunk_rows,
                                object_type=object_type,
                                wave=int(wave),
                                wave_position=wave_position,
                                chunk_index=local_chunk_index,
                                chunk_total=len(item_chunk_targets),
                                backlog_id=item_meta["backlog_id"],
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
                            "attempt_count": chunk_runtime_metrics.get("attempt_count"),
                            "elapsed_ms": chunk_runtime_metrics.get("elapsed_ms"),
                            "estimated_tokens": chunk_runtime_metrics.get("estimated_tokens"),
                            "input_tokens": chunk_runtime_metrics.get("input_tokens"),
                            "output_tokens": chunk_runtime_metrics.get("output_tokens"),
                            "total_tokens": chunk_runtime_metrics.get("total_tokens")
                            or chunk_runtime_metrics.get("used_tokens"),
                            "final_status": chunk_runtime_metrics.get("final_status"),
                            "http_status": chunk_runtime_metrics.get("http_status"),
                            "model": chunk_runtime_metrics.get("model"),
                            "provider": chunk_runtime_metrics.get("provider"),
                            "selected_provider": chunk_runtime_metrics.get("selected_provider"),
                            "provider_chain": chunk_runtime_metrics.get("provider_chain"),
                            "provider_fallback_used": bool(
                                chunk_runtime_metrics.get("fallback_used", False)
                            ),
                            "provider_fallback_reason": chunk_runtime_metrics.get("fallback_reason"),
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
                            "template_first": template_first_for_chunk,
                            "object_failure_count_in_run": object_failure_count_for_chunk,
                            "forced_reason": forced_reason,
                            "fallback_reason": fallback_reason,
                            "deterministic_topup_records": deterministic_topup_count,
                            "content_drafts_applied": content_drafts_applied,
                            "mismatched_object_rows_dropped": mismatched_object_count,
                        }
                        generator_chunk_telemetry.append(chunk_entry)
                    item_meta["status"] = "completed"
                if settings.gemini_supervisor_review_grouping == "department":
                    _schedule_department_wave_reviews(int(wave))
                await _await_supervisor_reviews(
                    f"wave {wave_position}/{total_wave_count} checkpoint"
                )
                wave_meta["status"] = "completed"
                cumulative_counts = _count_generated(generated_data)
                latest_review = (
                    supervisor_metadata.get("reviews", [])[-1]
                    if supervisor_metadata.get("reviews")
                    else {}
                )
                wave_complete_message = build_wave_complete_message(
                    wave=int(wave),
                    wave_position=wave_position,
                    total_waves=total_wave_count,
                    wave_meta=wave_meta,
                    cumulative_counts=cumulative_counts,
                    supervisor_summary=str(
                        latest_review.get("public_reasoning_summary")
                        or latest_review.get("reason")
                        or ""
                    ),
                )
                _append_progress_event(
                    wave_complete_message,
                    source="deterministic",
                    wave=int(wave),
                )
                _schedule_progress_narration(
                    deterministic_message=wave_complete_message,
                    event_context={
                        "wave": int(wave),
                        "wave_position": wave_position,
                        "total_waves": total_wave_count,
                        "generated_records": int(wave_meta.get("generated_records", 0) or 0),
                        "chunks": int(wave_meta.get("chunks", 0) or 0),
                        "blocked": int(wave_meta.get("blocked", 0) or 0),
                        "cumulative_counts": cumulative_counts,
                        "next_wave_purpose": (
                            "final validation and staging"
                            if wave_position >= total_wave_count
                            else f"wave {wave_position + 1}/{total_wave_count}"
                        ),
                    },
                )
                checkpoint_item = _append_wave_checkpoint(
                    batch_id=batch_id,
                    wave=int(wave),
                    wave_position=wave_position,
                    total_waves=total_wave_count,
                    wave_meta=wave_meta,
                    generated_counts=cumulative_counts,
                )
                wave_meta["checkpoint_id"] = checkpoint_item.get("checkpoint_id")
                await _persist_wave_checkpoint(
                    wave=int(wave),
                    wave_position=wave_position,
                    total_waves=total_wave_count,
                )
                await _honor_run_control(
                    checkpoint=f"wave {wave_position}/{total_wave_count} checkpoint",
                    wave_checkpoint=True,
                )

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
                await _honor_run_control(
                    checkpoint=f"chunk {index}/{len(chunk_targets)}"
                )
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

                current_object_type = _normalize_object_type(str(plan.get("object_type", "")))
                current_wave = _resolve_wave_for_object_type(current_object_type)
                chunk_routes = _build_wave_generator_routes(
                    settings=settings,
                    wave=current_wave,
                )
                active_route = (
                    dict(chunk_routes[(index - 1) % len(chunk_routes)])
                    if chunk_routes
                    else {}
                )
                selected_model_for_chunk = active_route.get("model")
                selected_api_key_for_chunk = active_route.get("api_key")
                selected_api_key_profile = (
                    str(active_route.get("profile", "primary")).strip() or "primary"
                )
                chunk_route_failovers: list[dict[str, str]] = []
                route_hint = (
                    f", lane={selected_api_key_profile}, model={selected_model_for_chunk}"
                    if selected_model_for_chunk
                    else ""
                )
                store.append_status(
                    batch_id,
                    "generating",
                    f"Generating chunk {index}/{len(chunk_targets)} (target={target_count}{route_hint}).",
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
                            model_override=selected_model_for_chunk,
                            api_key_override=selected_api_key_for_chunk,
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

                _schedule_supervisor_review(
                    rows=chunk_rows,
                    object_type=current_object_type,
                    wave=_resolve_wave_for_object_type(current_object_type),
                    wave_position=None,
                    chunk_index=index,
                    chunk_total=len(chunk_targets),
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
                    "provider": chunk_runtime_metrics.get("provider"),
                    "selected_provider": chunk_runtime_metrics.get("selected_provider"),
                    "provider_chain": chunk_runtime_metrics.get("provider_chain"),
                    "provider_fallback_used": bool(
                        chunk_runtime_metrics.get("fallback_used", False)
                    ),
                    "provider_fallback_reason": chunk_runtime_metrics.get("fallback_reason"),
                    "model_requested": selected_model_for_chunk,
                    "api_key_profile": selected_api_key_profile,
                    "key_route_failovers": chunk_route_failovers,
                    "mode_order": generator_mode_order,
                    "deterministic_fallback": used_deterministic_fallback,
                    "forced_deterministic": forced_deterministic_for_chunk,
                    "object_failure_count_in_run": object_failure_count_for_chunk,
                    "forced_reason": forced_reason,
                    "fallback_reason": fallback_reason,
                    "deterministic_topup_records": deterministic_topup_count,
                    "object_type": current_object_type,
                    "wave": current_wave,
                }
                generator_chunk_telemetry.append(chunk_entry)
            await _await_supervisor_reviews("chunked generation finalization")
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
            single_object_type = _normalize_object_type(str(plan.get("object_type", "")))
            _schedule_supervisor_review(
                rows=generated_data,
                object_type=single_object_type,
                wave=_resolve_wave_for_object_type(single_object_type),
                wave_position=None,
                chunk_index=1,
                chunk_total=1,
            )
            await _await_supervisor_reviews("single generation finalization")
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
        if supervisor_tasks:
            try:
                await _await_supervisor_reviews("failure handling")
            except Exception as supervisor_exc:  # noqa: BLE001
                supervisor_metadata.setdefault("failures", []).append(
                    {
                        "stage": "failure handling",
                        "error": _truncate_text(str(supervisor_exc), 500),
                    }
                )
        await _await_progress_narrations(timeout_seconds=2.0)
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
            if failure_code == "run_cancelled":
                status_message_prefix = "Run cancelled"
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
                    "(status/group_id/current_tags/field_type/custom_field_options)."
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
                    "(status/group_id/current_tags/field_type/custom_field_options)."
                )
                status_message_prefix = "Generator JSON validation failed"
                if not benchmark_mode and exc.corrective_example:
                    next_step_message += f" Example: {exc.corrective_example}"
        store.append_status(batch_id, "failed", f"{status_message_prefix}: {exc}")
        store.update_batch(
            batch_id,
            {
                "planning_summary": planning_summary,
                "metadata": _merge_control_metadata({
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
                    "coverage_manifest": coverage_manifest,
                    "department_coverage": department_coverage_metadata,
                    "quality_gates": quality_gates_metadata,
                    "orchestration": orchestration_metadata,
                    "chunking": chunking_metadata,
                    "supervisor": supervisor_metadata,
                    "progress_narration": progress_metadata,
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
                }),
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
    await _await_progress_narrations()
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
        current_metadata = _metadata_dict(store.get_batch(batch_id))
        return {
            "benchmark": {
                "enabled": benchmark_mode,
            },
            "operation": operation_metadata,
            "planning": {
                "planner_bypassed": planner_bypassed,
                "mode": "deterministic" if planner_bypassed else "llm",
                "estimated_requested_records": estimated_requested_records,
                "force_wave_chunk_path": force_wave_chunk_path,
                "force_wave_chunk_reason": force_wave_chunk_reason,
            },
            "orchestration": orchestration_metadata,
            "chunking": chunking_metadata,
            "coverage_manifest": coverage_manifest,
            "department_coverage": department_coverage_metadata,
            "quality_gates": quality_gates_metadata,
            "supervisor": supervisor_metadata,
            "progress_narration": progress_metadata,
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
            "run_control": _normalize_run_control(current_metadata.get("run_control", {})),
            "checkpoints": _checkpoint_list_from_metadata(current_metadata),
            "rollback": (
                current_metadata.get("rollback", {})
                if isinstance(current_metadata.get("rollback", {}), dict)
                else {}
            ),
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
        trigger_article_meta = (
            canonicalization_metadata.get("trigger_article", {})
            if isinstance(canonicalization_metadata.get("trigger_article", {}), dict)
            else {}
        )
        orchestration_metadata["template_usage"] = {
            "applied_count": int(trigger_article_meta.get("template_applied_count", 0) or 0),
            "template_keys": list(trigger_article_meta.get("template_keys", []) or []),
        }

        post_generation_stage = "dependency_resolution"
        generated_data, dependency_resolution = _apply_dependency_resolution(
            generated_data,
            related_lookup=related_lookup,
            catalog_lookup=catalog_lookup,
            dependency_mode=request.dependency_mode,
        )
        orchestration_metadata["taxonomy_resolution"] = {
            "resolved_links": int(dependency_resolution.get("resolved_links", 0) or 0),
            "unresolved_links": int(dependency_resolution.get("unresolved_links", 0) or 0),
            "unresolved_samples": list(dependency_resolution.get("unresolved_samples", []) or []),
        }
        if request.operation_mode == "update":
            generated_data, update_binding_metadata = _bind_update_target_to_generated_rows(
                generated_data,
                request=request,
            )
            operation_metadata = {**operation_metadata, **update_binding_metadata}
        generated_data, focus_diagnostics = _annotate_focus_object_constraints(
            generated_data,
            focus_object_types=focus_object_type_set,
        )
        if request.operation_mode == "update":
            duplicate_candidates = []
        else:
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
        department_coverage_metadata = _evaluate_department_coverage(
            manifest=coverage_manifest,
            records=preview_records,
        )
        quality_gates_metadata["coverage"] = department_coverage_metadata
        preview_records = _apply_coverage_gate_to_preview(
            preview_records,
            department_coverage_metadata,
        )
        validation_summary = _recompute_validation_summary(preview_records)
        generated_counts = _count_generated(preview_records)

        post_generation_stage = "staging"
        await _honor_run_control(checkpoint="staging handoff")
        store.append_status(batch_id, "staging", "Staging batch to Google Sheets.")
        staging_metadata_snapshot = _build_shared_metadata()
        current_status_history = list(
            (store.get_batch(batch_id) or {}).get("status_history", []) or []
        )
        appscript_payload = {
            "batch_id": batch_id,
            "prompt": request.prompt,
            "requester": request.requester,
            "status": "staging",
            "target_environment": request.target_environment,
            "created_at": created_at,
            "planning_summary": planning_summary,
            "records": preview_records,
            "metadata": staging_metadata_snapshot,
            "progress_events": current_status_history,
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
                    # Keep generate->preview resilient when Apps Script validation endpoint
                    # is unavailable; compute validation locally from normalized preview rows.
                    validation_summary = _recompute_validation_summary(preview_records)
                    validation_metadata["fallback"] = {
                        "mode": "local_in_memory",
                        "result": "validation summary recomputed from preview records",
                    }
                    validation_metadata["detail"] = (
                        appscript_validation.get("detail")
                        or "Apps Script validation failed; local validation fallback used."
                    )
                    store.append_status(
                        batch_id,
                        "validating",
                        "Apps Script validation unavailable; local validation fallback applied.",
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

        validation_summary = _recompute_validation_summary(preview_records)
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


async def generate_import_assistant_batch(
    request: ImportAssistantGenerateRequest,
    *,
    reserved_batch_id: str | None = None,
) -> ImportAssistantGenerateResponse:
    session, session_token = start_usage_session(
        f"import_assistant:{str(request.requester or 'local-user').strip() or 'local-user'}"
    )
    started = time.perf_counter()
    try:
        response = await _generate_import_assistant_batch_impl(
            request,
            reserved_batch_id=reserved_batch_id,
        )
        store = get_batch_store()
        batch = store.get_batch(response.batch_id) or {}
        metadata = _metadata_dict(batch)
        preliminary_elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        preliminary_usage_report = build_usage_report(
            session,
            status_history=list(batch.get("status_history", []) or []),
            total_elapsed_ms=preliminary_elapsed_ms,
        )
        appscript_metadata_sync: dict = {"status": "skipped", "detail": "not_configured"}
        appscript = AppScriptBridgeService()
        if appscript.enabled:
            sync_raw = await appscript.invoke(
                action="write_batch_metadata",
                payload={
                    "batch_id": response.batch_id,
                    "status": str(batch.get("status") or response.status),
                    "metadata": {
                        **metadata,
                        "usage_report": preliminary_usage_report,
                    },
                },
                timeout_seconds=min(get_settings().appscript_timeout_seconds, 10.0),
            )
            appscript_metadata_sync = _normalize_appscript_action_result(
                sync_raw,
                action="write_batch_metadata",
            )
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        usage_report = build_usage_report(
            session,
            status_history=list(batch.get("status_history", []) or []),
            total_elapsed_ms=elapsed_ms,
        )
        updated_batch = store.update_batch(
            response.batch_id,
            {
                "metadata": {
                    **metadata,
                    "usage_report": usage_report,
                    "appscript_metadata_sync": appscript_metadata_sync,
                }
            },
        )
        return response.model_copy(update={"metadata": updated_batch.get("metadata", {})})
    except BaseException:
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        store = get_batch_store()
        reserved_batch = store.get_batch(reserved_batch_id) if reserved_batch_id else None
        candidates = (
            [reserved_batch]
            if reserved_batch
            else [
                batch
                for batch in store.list_batches()
                if str(batch.get("requester", "")).strip() == str(request.requester or "").strip()
            ]
        )
        if candidates:
            batch = max(candidates, key=lambda item: str(item.get("created_at", "")))
            batch_id = str(batch.get("batch_id", "")).strip()
            if batch_id:
                metadata = _metadata_dict(batch)
                usage_report = build_usage_report(
                    session,
                    status_history=list(batch.get("status_history", []) or []),
                    total_elapsed_ms=elapsed_ms,
                )
                usage_report["incomplete"] = True
                store.update_batch(
                    batch_id,
                    {
                        "metadata": {
                            **metadata,
                            "usage_report": usage_report,
                        }
                    },
                )
        raise
    finally:
        reset_usage_session(session_token)


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
                conversation_id=str(batch.get("conversation_id") or "").strip() or None,
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
    effective_decisions = []
    counts = {"approved": 0, "skipped": 0, "edit_later": 0}
    for record in batch.get("records", []):
        decision = decision_map.get(record["record_id"], record.get("import_decision", "pending_review"))
        if record.get("validation_status") == "failed":
            decision = "blocked"
        record["import_decision"] = decision
        if record["record_id"] in decision_map:
            effective_decisions.append(
                {
                    "record_id": record["record_id"],
                    "import_decision": decision,
                }
            )
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
                "records": effective_decisions,
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
            fallback_rows = sheets.write_approval_log(batch_id, effective_decisions)
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
        rows_written = sheets.write_approval_log(batch_id, effective_decisions)
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
    deployment_scope: str = "support",
    help_center_url: str | None = None,
    brand_id: str | None = None,
    locale: str | None = None,
    article_mode: str = "draft",
    confirm_help_center_deploy: bool = False,
    confirm_article_publish: bool = False,
) -> dict:
    store = get_batch_store()
    appscript = AppScriptBridgeService()
    settings = get_settings()
    batch = store.get_batch(batch_id)
    if not batch:
        raise KeyError(batch_id)
    requested_scope_for_failure = str(deployment_scope or "support").strip().lower()

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
        prior_deploy_succeeded = any(
            str(row.get("deployment_status", "")).strip().lower() == "deployed"
            for row in list(current.get("records", []) or [])
            if isinstance(row, dict)
        )
        failure_status = (
            "deployed_partial"
            if requested_scope_for_failure == "help_center" or prior_deploy_succeeded
            else "deploy_failed"
        )
        store.update_batch(
            batch_id,
            {
                "status": failure_status,
                "metadata": {
                    **metadata,
                    "zendesk_deploy": deploy_metadata,
                    "failure": failure_payload,
                },
            },
        )
        store.append_status(batch_id, failure_status, reason)
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

    normalized_scope = str(deployment_scope or "support").strip().lower()
    if normalized_scope not in {"support", "help_center", "all"}:
        raise ValueError("deployment_scope must be support, help_center, or all.")
    normalized_article_mode = str(article_mode or "draft").strip().lower()
    if normalized_article_mode not in {"draft", "publish"}:
        raise ValueError("article_mode must be draft or publish.")

    help_center_types = {"categories", "sections", "articles"}

    def _normalized_record_type(row: dict) -> str:
        raw_type = str(row.get("object_type", "")).strip().lower()
        return TAB_OBJECT_TYPES.get(raw_type, raw_type)

    all_records = list(batch.get("records", []) or [])
    approved_help_center_records = [
        row
        for row in all_records
        if _normalized_record_type(row) in help_center_types
        and str(row.get("import_decision", "")).strip().lower() == "approved"
        and bool(row.get("deployable", False))
    ]
    includes_help_center = normalized_scope in {"help_center", "all"} and bool(
        approved_help_center_records
    )
    if includes_help_center and not confirm_help_center_deploy:
        raise ValueError(
            "Help Center deployment requires explicit confirmation. Verify the target brand first, "
            "then set confirm_help_center_deploy=true."
        )
    if (
        includes_help_center
        and normalized_article_mode == "publish"
        and not confirm_article_publish
    ):
        raise ValueError(
            "Publishing articles requires a separate explicit confirmation. "
            "Use article_mode=draft or set confirm_article_publish=true."
        )

    help_center_readiness: dict = {}
    if includes_help_center:
        help_center_readiness = await check_zendesk_help_center_readiness(
            subdomain=subdomain,
            email=email,
            api_token=api_token,
            help_center_url=help_center_url,
            brand_id=brand_id,
            locale=locale,
        )
        if not bool(help_center_readiness.get("ready", False)):
            current_metadata = (
                batch.get("metadata", {})
                if isinstance(batch.get("metadata", {}), dict)
                else {}
            )
            existing_deploy_metadata = (
                current_metadata.get("zendesk_deploy", {})
                if isinstance(current_metadata.get("zendesk_deploy", {}), dict)
                else {}
            )
            phase_payload = {
                "scope": normalized_scope,
                "state": "waiting_for_help_center",
                "attempted_at": _utc_now(),
                "readiness": help_center_readiness,
                "summary": {"attempted": 0, "deployed": 0, "failed": 0, "skipped": 0},
                "results": [],
            }
            phases = (
                existing_deploy_metadata.get("phases", {})
                if isinstance(existing_deploy_metadata.get("phases", {}), dict)
                else {}
            )
            store.update_batch(
                batch_id,
                {
                    "metadata": {
                        **current_metadata,
                        "zendesk_deploy": {
                            **existing_deploy_metadata,
                            "latest_phase": normalized_scope,
                            "help_center_readiness": help_center_readiness,
                            "phases": {**phases, "help_center": phase_payload},
                        },
                        "failure": None,
                    }
                },
            )
            message = str(
                help_center_readiness.get("detail")
                or "Help Center is not ready. Complete the manual Zendesk step and verify again."
            )
            store.append_status(batch_id, "deployed_partial", message)
            return {
                "batch_id": batch_id,
                "status": "deployed_partial",
                "summary": {"attempted": 0, "deployed": 0, "failed": 0, "skipped": 0},
                "results": [],
                "message": message,
                "metadata": {
                    "deployment_scope": normalized_scope,
                    "help_center_readiness": help_center_readiness,
                    "resume_available": True,
                    "failure": None,
                },
            }

    scoped_records = [
        row
        for row in all_records
        if (
            normalized_scope == "all"
            or (
                normalized_scope == "help_center"
                and _normalized_record_type(row) in help_center_types
            )
            or (
                normalized_scope == "support"
                and _normalized_record_type(row) not in help_center_types
            )
        )
    ]
    already_completed_record_ids = [
        str(row.get("record_id", "")).strip()
        for row in scoped_records
        if str(row.get("deployment_status", "")).strip().lower() == "deployed"
    ]
    phase_candidates = [
        row
        for row in scoped_records
        if str(row.get("deployment_status", "")).strip().lower() != "deployed"
    ]
    approved_records = [
        row for row in phase_candidates
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
    records = deployable_approved_records

    store.append_status(batch_id, "deploying", "Deploying approved records to Zendesk.")
    store.append_status(
        batch_id,
        "deploying",
        f"Deploy phase start ({normalized_scope}): validating approved records and dependencies.",
    )

    watchdog_seconds = max(float(getattr(settings, "deploy_watchdog_seconds", 240.0)), 30.0)
    deployment: dict = {}
    summary: dict = {}
    results: list = []
    updated_records: list[dict] = []
    dependency_auto_create: dict = {}
    deploy_sanitization_stats: dict = {}
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
                article_mode=normalized_article_mode,
                help_center_base_url=(
                    str(help_center_readiness.get("help_center_api_base_url") or "").strip()
                    if includes_help_center
                    else None
                ),
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
        deploy_sanitization_stats = (
            deployment.get("sanitization_stats", {})
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
        for row in all_records:
            record_id = row.get("record_id")
            result = result_by_record.get(record_id)
            if isinstance(result, dict):
                row["deployment_status"] = result.get(
                    "deployment_status",
                    row.get("deployment_status", "pending"),
                )
                if result.get("zendesk_object_id") is not None:
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
        terminal_deployment_states = {"deployed", "skipped"}
        pending_help_center_count = sum(
            1
            for row in updated_records
            if _normalized_record_type(row) in help_center_types
            and str(row.get("import_decision", "")).strip().lower() == "approved"
            and bool(row.get("deployable", False))
            and str(row.get("deployment_status", "")).strip().lower()
            not in terminal_deployment_states
        )
        pending_all_count = sum(
            1
            for row in updated_records
            if str(row.get("import_decision", "")).strip().lower() == "approved"
            and bool(row.get("deployable", False))
            and str(row.get("deployment_status", "")).strip().lower()
            not in terminal_deployment_states
        )
        any_successful_record = any(
            str(row.get("deployment_status", "")).strip().lower() == "deployed"
            for row in updated_records
        )

        if attempted == 0 and already_completed_record_ids and pending_all_count == 0:
            final_status = "deployed"
            final_message = "This deployment phase was already completed; no duplicate writes were attempted."
        elif attempted == 0 and normalized_scope == "support" and pending_help_center_count > 0:
            final_status = "deployed_partial"
            final_message = (
                "No approved Support objects required deployment. Help Center content is waiting for "
                "separate verification and confirmation."
            )
        elif attempted == 0:
            final_status = "deployed_partial"
            final_message = f"No approved deployable records were found in the {normalized_scope} phase."
        elif deployed == 0 and failed == 0:
            final_status = "deployed_partial"
            final_message = "No objects were created or updated. All approved items were skipped."
        elif failed > 0 and deployed > 0:
            final_status = "deployed_partial"
            final_message = "Deployment completed with partial failures."
        elif failed > 0 and deployed == 0:
            final_status = "deployed_partial" if any_successful_record else "deploy_failed"
            final_message = (
                f"The {normalized_scope} deployment phase failed; previously deployed records were preserved."
                if any_successful_record
                else "Deployment failed."
            )
        elif normalized_scope == "support" and pending_help_center_count > 0:
            final_status = "deployed_partial"
            final_message = (
                "Support objects deployed successfully. Help Center content is waiting for separate "
                "verification and confirmation."
            )
        elif pending_all_count > 0:
            final_status = "deployed_partial"
            final_message = "Deployment phase completed, with approved records still waiting in another phase."
        else:
            final_status = "deployed"
            final_message = "Deployment completed successfully."

        current_batch = store.get_batch(batch_id) or batch
        metadata = (
            current_batch.get("metadata", {})
            if isinstance(current_batch.get("metadata", {}), dict)
            else {}
        )
        existing_zendesk_deploy = (
            metadata.get("zendesk_deploy", {})
            if isinstance(metadata.get("zendesk_deploy", {}), dict)
            else {}
        )
        existing_phases = (
            existing_zendesk_deploy.get("phases", {})
            if isinstance(existing_zendesk_deploy.get("phases", {}), dict)
            else {}
        )
        phase_key = "help_center" if normalized_scope == "help_center" else normalized_scope
        phase_metadata = {
            "scope": normalized_scope,
            "state": final_status,
            "completed_at": _utc_now(),
            "summary": summary,
            "results": results,
            "already_completed_record_ids": already_completed_record_ids,
            "pending_help_center_count": pending_help_center_count,
            "article_mode": normalized_article_mode if includes_help_center else None,
            "readiness": help_center_readiness if includes_help_center else None,
        }
        prior_results = (
            existing_zendesk_deploy.get("results", [])
            if isinstance(existing_zendesk_deploy.get("results", []), list)
            else []
        )
        cumulative_results_by_id = {
            str(item.get("record_id", "")): item
            for item in prior_results
            if isinstance(item, dict) and str(item.get("record_id", "")).strip()
        }
        for item in results:
            if isinstance(item, dict) and str(item.get("record_id", "")).strip():
                cumulative_results_by_id[str(item.get("record_id"))] = item
        cumulative_results = list(cumulative_results_by_id.values())
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
                        **existing_zendesk_deploy,
                        "summary": summary,
                        "results": cumulative_results,
                        "base_url": deployment.get("base_url"),
                        "execution_log": execution_log_result,
                        "dependency_auto_create": dependency_auto_create,
                        "sanitization_stats": deploy_sanitization_stats,
                        "terminal_error_class": terminal_error_class,
                        "watchdog_seconds": watchdog_seconds,
                        "latest_phase": normalized_scope,
                        "phases": {**existing_phases, phase_key: phase_metadata},
                        "help_center_readiness": (
                            help_center_readiness
                            or existing_zendesk_deploy.get("help_center_readiness", {})
                        ),
                        "pending_help_center_count": pending_help_center_count,
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
                "deployment_scope": normalized_scope,
                "article_mode": normalized_article_mode if includes_help_center else None,
                "help_center_readiness": help_center_readiness,
                "resume_available": final_status == "deployed_partial",
                "already_completed_record_ids": already_completed_record_ids,
                "pending_help_center_count": pending_help_center_count,
                "dependency_auto_create": dependency_auto_create,
                "deploy_sanitization_stats": deploy_sanitization_stats,
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
