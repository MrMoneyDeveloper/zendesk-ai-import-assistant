import os

from app.core.settings import get_settings


os.environ["GEMINI_SUPERVISOR_ENABLED"] = "false"
os.environ.pop("GEMINI_API_KEY", None)
get_settings.cache_clear()
