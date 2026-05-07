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

    return Settings(
        app_name=os.getenv("APP_NAME", "AI Zendesk Import Assistant"),
        frontend_origin=os.getenv("FRONTEND_ORIGIN", "http://localhost:5173"),
        llm_provider=provider,
        xai_api_key=api_key,
        xai_base_url=_resolve_base_url(provider, os.getenv("XAI_BASE_URL", "")),
        xai_model=_resolve_model(provider, os.getenv("XAI_MODEL", "")),
        xai_request_timeout_seconds=float(os.getenv("XAI_REQUEST_TIMEOUT_SECONDS", "60")),
        xai_max_output_tokens=int(os.getenv("XAI_MAX_OUTPUT_TOKENS", "1800")),
        xai_temperature=float(os.getenv("XAI_TEMPERATURE", "0.1")),
        xai_enabled=_as_bool(os.getenv("XAI_ENABLED"), True),
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
