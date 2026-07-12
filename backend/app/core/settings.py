import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / ".env.local", override=True)


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


def _as_csv_tuple(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    cleaned = [item.strip() for item in value.split(",")]
    normalized = tuple(item for item in cleaned if item)
    if not normalized:
        return default
    return normalized


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
    xai_api_key_wave3: str
    xai_api_key_wave4: str
    xai_api_key_secondary: str
    xai_api_key_tertiary: str
    xai_base_url: str
    xai_model: str
    xai_request_timeout_seconds: float
    xai_max_output_tokens: int
    xai_temperature: float
    xai_enabled: bool
    llm_model_planner: str
    llm_model_generator: str
    llm_model_generator_wave3: str
    llm_model_generator_wave4: str
    llm_model_generator_secondary: str
    llm_model_generator_tertiary: str
    llm_model_clarifier: str
    llm_planner_max_output_tokens: int
    llm_generator_max_output_tokens: int
    llm_clarifier_max_output_tokens: int
    llm_strict_schema_mode: bool
    llm_strict_schema_planner: bool | None
    llm_strict_schema_clarifier: bool | None
    llm_strict_schema_generator: bool | None
    llm_json_schema_supported_models: tuple[str, ...]
    llm_fallback_to_json_object: bool
    llm_ambiguity_threshold: float
    llm_min_deploy_confidence: float
    llm_retry_max_attempts: int
    llm_retry_backoff_seconds: float
    llm_circuit_breaker_enabled: bool
    llm_circuit_breaker_failures: int
    llm_circuit_breaker_window_seconds: int
    llm_circuit_breaker_cooldown_seconds: int
    llm_rate_guard_enabled: bool
    llm_rate_guard_safety_ratio: float
    llm_rate_guard_min_headroom_tokens: int
    llm_prewait_max_seconds_planner: float
    llm_prewait_max_seconds_generator: float
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
    benchmark_mode_enabled: bool
    diagnostics_mode_enabled: bool
    perf_capture_enabled: bool
    perf_capture_dir: str
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
    batch_store_max_entries: int
    batch_store_trim_runtime_metadata: bool
    appscript_web_app_url: str
    appscript_api_key: str
    appscript_timeout_seconds: float
    appscript_health_timeout_seconds: float
    integrations_health_cache_seconds: float
    deploy_watchdog_seconds: float
    deploy_stale_recovery_seconds: float
    zendesk_subdomain: str
    zendesk_email: str
    zendesk_api_token: str
    zendesk_target_environment: str
    zendesk_fallback_404_cooldown_seconds: int
    llm_wave_object_deterministic_failover_threshold: int
    gemini_supervisor_enabled: bool
    gemini_api_key: str
    gemini_supervisor_model: str
    gemini_supervisor_max_concurrency: int
    gemini_supervisor_min_request_interval_seconds: float
    gemini_supervisor_rate_limit_retries: int
    gemini_supervisor_auto_apply_patches: bool
    gemini_supervisor_include_thought_summary: bool
    gemini_supervisor_timeout_seconds: float
    gemini_supervisor_strict_mode: bool
    gemini_supervisor_approval_threshold: float
    gemini_supervisor_max_regeneration_retries: int
    gemini_supervisor_review_grouping: str


@lru_cache
def get_settings() -> Settings:
    api_key = os.getenv("XAI_API_KEY", "").strip()
    provider = _resolve_provider(api_key, os.getenv("LLM_PROVIDER", "auto"))
    default_model = _resolve_model(provider, os.getenv("XAI_MODEL", ""))
    default_max_tokens = _as_int(os.getenv("XAI_MAX_OUTPUT_TOKENS"), 1800)
    default_planner_model = "qwen/qwen3-32b" if provider == "groq" else default_model
    default_clarifier_model = "qwen/qwen3-32b" if provider == "groq" else default_model
    default_generator_model = "openai/gpt-oss-20b" if provider == "groq" else default_model

    default_schema_supported_models = ("openai/gpt-oss-20b", "grok-4.3", "grok-4.20")

    def _stage_prewait(default_seconds: float, env_name: str) -> float:
        return min(max(_as_float(os.getenv(env_name), default_seconds), 0.0), 30.0)

    return Settings(
        app_name=os.getenv("APP_NAME", "AI Zendesk Import Assistant"),
        frontend_origin=os.getenv("FRONTEND_ORIGIN", "http://localhost:5173"),
        llm_provider=provider,
        xai_api_key=api_key,
        xai_api_key_wave3=os.getenv("XAI_API_KEY_WAVE3", "").strip(),
        xai_api_key_wave4=os.getenv("XAI_API_KEY_WAVE4", "").strip(),
        xai_api_key_secondary=(
            os.getenv("XAI_API_KEY_SECONDARY", "").strip()
            or os.getenv("XAI_API_KEY_WAVE3", "").strip()
        ),
        xai_api_key_tertiary=(
            os.getenv("XAI_API_KEY_TERTIARY", "").strip()
            or os.getenv("XAI_API_KEY_WAVE4", "").strip()
        ),
        xai_base_url=_resolve_base_url(provider, os.getenv("XAI_BASE_URL", "")),
        xai_model=default_model,
        xai_request_timeout_seconds=_as_float(os.getenv("XAI_REQUEST_TIMEOUT_SECONDS"), 60.0),
        xai_max_output_tokens=default_max_tokens,
        xai_temperature=_as_float(os.getenv("XAI_TEMPERATURE"), 0.1),
        xai_enabled=_as_bool(os.getenv("XAI_ENABLED"), True),
        llm_model_planner=os.getenv("LLM_MODEL_PLANNER", "").strip() or default_planner_model,
        llm_model_generator=os.getenv("LLM_MODEL_GENERATOR", "").strip() or default_generator_model,
        llm_model_generator_wave3=os.getenv("LLM_MODEL_GENERATOR_WAVE3", "").strip(),
        llm_model_generator_wave4=os.getenv("LLM_MODEL_GENERATOR_WAVE4", "").strip(),
        llm_model_generator_secondary=(
            os.getenv("LLM_MODEL_GENERATOR_SECONDARY", "").strip()
            or os.getenv("LLM_MODEL_GENERATOR_WAVE3", "").strip()
            or default_generator_model
        ),
        llm_model_generator_tertiary=(
            os.getenv("LLM_MODEL_GENERATOR_TERTIARY", "").strip()
            or os.getenv("LLM_MODEL_GENERATOR_WAVE4", "").strip()
            or default_generator_model
        ),
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
        llm_json_schema_supported_models=_as_csv_tuple(
            os.getenv("LLM_JSON_SCHEMA_SUPPORTED_MODELS"),
            default_schema_supported_models,
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
        llm_circuit_breaker_enabled=_as_bool(os.getenv("LLM_CIRCUIT_BREAKER_ENABLED"), True),
        llm_circuit_breaker_failures=max(
            _as_int(os.getenv("LLM_CIRCUIT_BREAKER_FAILURES"), 2),
            1,
        ),
        llm_circuit_breaker_window_seconds=max(
            _as_int(os.getenv("LLM_CIRCUIT_BREAKER_WINDOW_SECONDS"), 300),
            30,
        ),
        llm_circuit_breaker_cooldown_seconds=max(
            _as_int(os.getenv("LLM_CIRCUIT_BREAKER_COOLDOWN_SECONDS"), 300),
            30,
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
        llm_prewait_max_seconds_planner=_stage_prewait(3.0, "LLM_PREWAIT_MAX_SECONDS_PLANNER"),
        llm_prewait_max_seconds_generator=_stage_prewait(6.0, "LLM_PREWAIT_MAX_SECONDS_GENERATOR"),
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
            _as_float(os.getenv("LLM_AUTO_CHUNK_PACING_SECONDS"), 0.15),
            0.0,
        ),
        llm_auto_chunk_pacing_jitter_seconds=max(
            _as_float(os.getenv("LLM_AUTO_CHUNK_PACING_JITTER_SECONDS"), 0.10),
            0.0,
        ),
        benchmark_mode_enabled=_as_bool(os.getenv("BENCHMARK_MODE"), False),
        diagnostics_mode_enabled=_as_bool(os.getenv("DIAGNOSTICS_MODE"), False),
        perf_capture_enabled=_as_bool(os.getenv("PERF_CAPTURE_ENABLED"), False),
        perf_capture_dir=os.getenv("PERF_CAPTURE_DIR", "").strip(),
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
        batch_store_max_entries=max(
            _as_int(os.getenv("BATCH_STORE_MAX_ENTRIES"), 250),
            25,
        ),
        batch_store_trim_runtime_metadata=_as_bool(
            os.getenv("BATCH_STORE_TRIM_RUNTIME_METADATA"),
            True,
        ),
        appscript_web_app_url=os.getenv("APPS_SCRIPT_WEB_APP_URL", "").strip(),
        appscript_api_key=os.getenv("APPS_SCRIPT_API_KEY", "").strip(),
        appscript_timeout_seconds=float(os.getenv("APPS_SCRIPT_TIMEOUT_SECONDS", "20")),
        appscript_health_timeout_seconds=max(
            _as_float(os.getenv("APPS_SCRIPT_HEALTH_TIMEOUT_SECONDS"), 3.0),
            0.5,
        ),
        integrations_health_cache_seconds=max(
            _as_float(os.getenv("INTEGRATIONS_HEALTH_CACHE_SECONDS"), 45.0),
            0.0,
        ),
        deploy_watchdog_seconds=max(
            _as_float(os.getenv("DEPLOY_WATCHDOG_SECONDS"), 240.0),
            30.0,
        ),
        deploy_stale_recovery_seconds=max(
            _as_float(os.getenv("DEPLOY_STALE_RECOVERY_SECONDS"), 300.0),
            30.0,
        ),
        zendesk_subdomain=os.getenv("ZENDESK_SUBDOMAIN", "").strip(),
        zendesk_email=os.getenv("ZENDESK_EMAIL", "").strip(),
        zendesk_api_token=os.getenv("ZENDESK_API_TOKEN", "").strip(),
        zendesk_target_environment=os.getenv("ZENDESK_TARGET_ENVIRONMENT", "sandbox").strip(),
        zendesk_fallback_404_cooldown_seconds=max(
            _as_int(os.getenv("ZENDESK_FALLBACK_404_COOLDOWN_SECONDS"), 1800),
            60,
        ),
        llm_wave_object_deterministic_failover_threshold=max(
            _as_int(os.getenv("LLM_WAVE_OBJECT_DETERMINISTIC_FAILOVER_THRESHOLD"), 3),
            2,
        ),
        gemini_supervisor_enabled=_as_bool(os.getenv("GEMINI_SUPERVISOR_ENABLED"), True),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        gemini_supervisor_model=(
            os.getenv("GEMINI_SUPERVISOR_MODEL", "").strip()
            or "gemini-3.1-flash-lite"
        ),
        gemini_supervisor_max_concurrency=max(
            _as_int(os.getenv("GEMINI_SUPERVISOR_MAX_CONCURRENCY"), 2),
            1,
        ),
        gemini_supervisor_min_request_interval_seconds=max(
            _as_float(os.getenv("GEMINI_SUPERVISOR_MIN_REQUEST_INTERVAL_SECONDS"), 0.0),
            0.0,
        ),
        gemini_supervisor_rate_limit_retries=max(
            _as_int(os.getenv("GEMINI_SUPERVISOR_RATE_LIMIT_RETRIES"), 3),
            0,
        ),
        gemini_supervisor_auto_apply_patches=_as_bool(
            os.getenv("GEMINI_SUPERVISOR_AUTO_APPLY_PATCHES"),
            True,
        ),
        gemini_supervisor_include_thought_summary=_as_bool(
            os.getenv("GEMINI_SUPERVISOR_INCLUDE_THOUGHT_SUMMARY"),
            True,
        ),
        gemini_supervisor_timeout_seconds=max(
            _as_float(os.getenv("GEMINI_SUPERVISOR_TIMEOUT_SECONDS"), 30.0),
            5.0,
        ),
        gemini_supervisor_strict_mode=_as_bool(
            os.getenv("GEMINI_SUPERVISOR_STRICT_MODE"),
            False,
        ),
        gemini_supervisor_approval_threshold=min(
            max(_as_float(os.getenv("GEMINI_SUPERVISOR_APPROVAL_THRESHOLD"), 0.80), 0.0),
            1.0,
        ),
        gemini_supervisor_max_regeneration_retries=max(
            _as_int(os.getenv("GEMINI_SUPERVISOR_MAX_REGENERATION_RETRIES"), 1),
            0,
        ),
        gemini_supervisor_review_grouping=(
            os.getenv("GEMINI_SUPERVISOR_REVIEW_GROUPING", "department").strip().lower()
            or "department"
        ),
    )
