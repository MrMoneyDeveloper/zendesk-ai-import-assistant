import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value.strip())
    except ValueError:
        return default


def _as_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


def _as_stage_bool(value: str | None, default: bool) -> bool | None:
    if value is None:
        return default
    cleaned = value.strip().lower()
    if cleaned in {"inherit", "global", "default"}:
        return None
    return cleaned in {"1", "true", "yes", "on"}


def _as_choice(value: str | None, default: str, allowed: set[str]) -> str:
    if value is None:
        return default
    cleaned = value.strip().lower()
    if cleaned in allowed:
        return cleaned
    return default


def _normalize_provider(value: str | None) -> str:
    if not value:
        return "auto"
    normalized = value.strip().lower()
    if normalized in {"xai", "grok", "groq", "auto"}:
        if normalized == "grok":
            return "xai"
        return normalized
    return "auto"


def _resolve_provider(api_key: str, configured_provider: str) -> str:
    provider = _normalize_provider(configured_provider)
    if provider != "auto":
        return provider
    if api_key.startswith("gsk_"):
        return "groq"
    return "xai"


def _resolve_base_url(provider: str, configured_base_url: str) -> str:
    base = configured_base_url.strip().rstrip("/")
    if provider == "groq":
        if not base or "api.x.ai" in base:
            return "https://api.groq.com/openai/v1"
        return base
    if not base:
        return "https://api.x.ai/v1"
    return base


def _resolve_model(provider: str, configured_model: str) -> str:
    model = configured_model.strip()
    if provider == "groq":
        if not model or model.startswith("grok-"):
            return "openai/gpt-oss-20b"
        return model
    if not model:
        return "grok-4.3"
    return model


@dataclass(frozen=True)
class Settings:
    app_name: str
    frontend_origin: str
    llm_provider: str
    xai_api_key: str
    xai_base_url: str
    xai_model: str
    xai_request_timeout_seconds: float
    xai_max_output_tokens: int
    xai_temperature: float
    xai_enabled: bool
    llm_model_planner: str
    llm_model_generator: str
    llm_model_clarifier: str
    llm_planner_max_output_tokens: int
    llm_generator_max_output_tokens: int
    llm_clarifier_max_output_tokens: int
    llm_strict_schema_mode: bool
    llm_strict_schema_planner: bool | None
    llm_strict_schema_clarifier: bool | None
    llm_strict_schema_generator: bool | None
    llm_fallback_to_json_object: bool
    llm_ambiguity_threshold: float
    llm_min_deploy_confidence: float
    llm_retry_max_attempts: int
    llm_retry_backoff_seconds: float
    llm_rate_guard_enabled: bool
    llm_rate_guard_safety_ratio: float
    llm_rate_guard_min_headroom_tokens: int
    llm_context_max_related_objects: int
    llm_context_max_entries_per_catalog: int
    llm_context_max_catalog_entries: int
    llm_context_max_recent_items: int
    llm_context_max_recent_chars: int
    llm_context_max_notes_chars: int
    llm_auto_chunk_enabled: bool
    llm_auto_chunk_size: int
    llm_auto_chunk_max_chunks: int
    llm_auto_chunk_trigger_min_records: int
    llm_auto_chunk_pacing_seconds: float
    llm_auto_chunk_pacing_jitter_seconds: float
    inference_policy: str
    form_missing_field_mode: str
    ticket_field_default_agent_can_edit: bool
    ticket_field_default_visible_in_portal: bool
    ticket_field_default_editable_in_portal: bool
    ticket_field_default_required: bool
    ticket_field_default_required_in_portal: bool
    attachment_max_file_size_bytes: int
    attachment_max_chars: int
    google_sheet_id: str
    google_service_account_file: str
    google_sheets_scope: str
    batch_store_file: str
    appscript_web_app_url: str
    appscript_api_key: str
    appscript_timeout_seconds: float
    zendesk_subdomain: str
    zendesk_email: str
    zendesk_api_token: str
    zendesk_target_environment: str


@lru_cache
def get_settings() -> Settings:
    api_key = os.getenv("XAI_API_KEY", "").strip()
    provider = _resolve_provider(api_key, os.getenv("LLM_PROVIDER", "auto"))
    default_model = _resolve_model(provider, os.getenv("XAI_MODEL", ""))
    default_max_tokens = _as_int(os.getenv("XAI_MAX_OUTPUT_TOKENS"), 1800)
    default_planner_model = "qwen/qwen3-32b" if provider == "groq" else default_model
    default_clarifier_model = "qwen/qwen3-32b" if provider == "groq" else default_model
    default_generator_model = "openai/gpt-oss-20b" if provider == "groq" else default_model

    return Settings(
        app_name=os.getenv("APP_NAME", "AI Zendesk Import Assistant"),
        frontend_origin=os.getenv("FRONTEND_ORIGIN", "http://localhost:5173"),
        llm_provider=provider,
        xai_api_key=api_key,
        xai_base_url=_resolve_base_url(provider, os.getenv("XAI_BASE_URL", "")),
        xai_model=default_model,
        xai_request_timeout_seconds=_as_float(os.getenv("XAI_REQUEST_TIMEOUT_SECONDS"), 60.0),
        xai_max_output_tokens=default_max_tokens,
        xai_temperature=_as_float(os.getenv("XAI_TEMPERATURE"), 0.1),
        xai_enabled=_as_bool(os.getenv("XAI_ENABLED"), True),
        llm_model_planner=os.getenv("LLM_MODEL_PLANNER", "").strip() or default_planner_model,
        llm_model_generator=os.getenv("LLM_MODEL_GENERATOR", "").strip() or default_generator_model,
        llm_model_clarifier=os.getenv("LLM_MODEL_CLARIFIER", "").strip() or default_clarifier_model,
        llm_planner_max_output_tokens=_as_int(
            os.getenv("LLM_PLANNER_MAX_OUTPUT_TOKENS"),
            min(default_max_tokens, 300),
        ),
        llm_generator_max_output_tokens=_as_int(
            os.getenv("LLM_GENERATOR_MAX_OUTPUT_TOKENS"),
            min(default_max_tokens, 900),
        ),
        llm_clarifier_max_output_tokens=_as_int(
            os.getenv("LLM_CLARIFIER_MAX_OUTPUT_TOKENS"),
            min(default_max_tokens, 240),
        ),
        llm_strict_schema_mode=_as_bool(os.getenv("LLM_STRICT_SCHEMA_MODE"), True),
        llm_strict_schema_planner=_as_stage_bool(
            os.getenv("LLM_STRICT_SCHEMA_PLANNER"),
            False,
        ),
        llm_strict_schema_clarifier=_as_stage_bool(
            os.getenv("LLM_STRICT_SCHEMA_CLARIFIER"),
            False,
        ),
        llm_strict_schema_generator=_as_stage_bool(
            os.getenv("LLM_STRICT_SCHEMA_GENERATOR"),
            True,
        ),
        llm_fallback_to_json_object=_as_bool(
            os.getenv("LLM_FALLBACK_TO_JSON_OBJECT"),
            True,
        ),
        llm_ambiguity_threshold=min(
            max(_as_float(os.getenv("LLM_AMBIGUITY_THRESHOLD"), 0.58), 0.0),
            1.0,
        ),
        llm_min_deploy_confidence=min(
            max(_as_float(os.getenv("LLM_MIN_DEPLOY_CONFIDENCE"), 0.65), 0.0),
            1.0,
        ),
        llm_retry_max_attempts=max(_as_int(os.getenv("LLM_RETRY_MAX_ATTEMPTS"), 2), 1),
        llm_retry_backoff_seconds=max(
            _as_float(os.getenv("LLM_RETRY_BACKOFF_SECONDS"), 1.0),
            0.0,
        ),
        llm_rate_guard_enabled=_as_bool(os.getenv("LLM_RATE_GUARD_ENABLED"), True),
        llm_rate_guard_safety_ratio=min(
            max(_as_float(os.getenv("LLM_RATE_GUARD_SAFETY_RATIO"), 0.8), 0.1),
            0.99,
        ),
        llm_rate_guard_min_headroom_tokens=max(
            _as_int(os.getenv("LLM_RATE_GUARD_MIN_HEADROOM_TOKENS"), 250),
            0,
        ),
        llm_context_max_related_objects=max(
            _as_int(os.getenv("LLM_CONTEXT_MAX_RELATED_OBJECTS"), 16),
            5,
        ),
        llm_context_max_entries_per_catalog=max(
            _as_int(os.getenv("LLM_CONTEXT_MAX_ENTRIES_PER_CATALOG"), 6),
            2,
        ),
        llm_context_max_catalog_entries=max(
            _as_int(os.getenv("LLM_CONTEXT_MAX_CATALOG_ENTRIES"), 36),
            10,
        ),
        llm_context_max_recent_items=max(
            _as_int(os.getenv("LLM_CONTEXT_MAX_RECENT_ITEMS"), 4),
            1,
        ),
        llm_context_max_recent_chars=max(
            _as_int(os.getenv("LLM_CONTEXT_MAX_RECENT_CHARS"), 140),
            60,
        ),
        llm_context_max_notes_chars=max(
            _as_int(os.getenv("LLM_CONTEXT_MAX_NOTES_CHARS"), 700),
            200,
        ),
        llm_auto_chunk_enabled=_as_bool(os.getenv("LLM_AUTO_CHUNK_ENABLED"), True),
        llm_auto_chunk_size=max(_as_int(os.getenv("LLM_AUTO_CHUNK_SIZE"), 6), 1),
        llm_auto_chunk_max_chunks=max(_as_int(os.getenv("LLM_AUTO_CHUNK_MAX_CHUNKS"), 12), 1),
        llm_auto_chunk_trigger_min_records=max(
            _as_int(os.getenv("LLM_AUTO_CHUNK_TRIGGER_MIN_RECORDS"), 7),
            2,
        ),
        llm_auto_chunk_pacing_seconds=max(
            _as_float(os.getenv("LLM_AUTO_CHUNK_PACING_SECONDS"), 0.35),
            0.0,
        ),
        llm_auto_chunk_pacing_jitter_seconds=max(
            _as_float(os.getenv("LLM_AUTO_CHUNK_PACING_JITTER_SECONDS"), 0.25),
            0.0,
        ),
        inference_policy=_as_choice(
            os.getenv("INFERENCE_POLICY"),
            "infer_warn",
            {"infer_warn", "ask_once", "strict_block"},
        ),
        form_missing_field_mode=_as_choice(
            os.getenv("FORM_MISSING_FIELD_MODE"),
            "auto_create",
            {"auto_create", "existing_only", "suggest_only"},
        ),
        ticket_field_default_agent_can_edit=_as_bool(
            os.getenv("TICKET_FIELD_DEFAULT_AGENT_CAN_EDIT"),
            True,
        ),
        ticket_field_default_visible_in_portal=_as_bool(
            os.getenv("TICKET_FIELD_DEFAULT_VISIBLE_IN_PORTAL"),
            True,
        ),
        ticket_field_default_editable_in_portal=_as_bool(
            os.getenv("TICKET_FIELD_DEFAULT_EDITABLE_IN_PORTAL"),
            False,
        ),
        ticket_field_default_required=_as_bool(
            os.getenv("TICKET_FIELD_DEFAULT_REQUIRED"),
            False,
        ),
        ticket_field_default_required_in_portal=_as_bool(
            os.getenv("TICKET_FIELD_DEFAULT_REQUIRED_IN_PORTAL"),
            False,
        ),
        attachment_max_file_size_bytes=_as_int(
            os.getenv("ATTACHMENT_MAX_FILE_SIZE_BYTES"),
            5 * 1024 * 1024,
        ),
        attachment_max_chars=_as_int(
            os.getenv("ATTACHMENT_MAX_CHARS"),
            12000,
        ),
        google_sheet_id=os.getenv("GOOGLE_SHEET_ID", "").strip(),
        google_service_account_file=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip(),
        google_sheets_scope=(
            os.getenv("GOOGLE_SHEETS_SCOPE", "").strip()
            or "https://www.googleapis.com/auth/spreadsheets"
        ),
        batch_store_file=(
            os.getenv("BATCH_STORE_FILE", "").strip()
            or str((BASE_DIR / "data" / "batches.json").resolve())
        ),
        appscript_web_app_url=os.getenv("APPS_SCRIPT_WEB_APP_URL", "").strip(),
        appscript_api_key=os.getenv("APPS_SCRIPT_API_KEY", "").strip(),
        appscript_timeout_seconds=float(os.getenv("APPS_SCRIPT_TIMEOUT_SECONDS", "20")),
        zendesk_subdomain=os.getenv("ZENDESK_SUBDOMAIN", "").strip(),
        zendesk_email=os.getenv("ZENDESK_EMAIL", "").strip(),
        zendesk_api_token=os.getenv("ZENDESK_API_TOKEN", "").strip(),
        zendesk_target_environment=os.getenv("ZENDESK_TARGET_ENVIRONMENT", "sandbox").strip(),
    )
