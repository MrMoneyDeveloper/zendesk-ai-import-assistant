import os

from app.core.settings import get_settings


os.environ["GEMINI_SUPERVISOR_ENABLED"] = "false"
os.environ.pop("GEMINI_API_KEY", None)
os.environ["LLM_DEFAULT_PROVIDER"] = "groq"
os.environ["PROGRESS_NARRATOR_ENABLED"] = "false"
get_settings.cache_clear()
