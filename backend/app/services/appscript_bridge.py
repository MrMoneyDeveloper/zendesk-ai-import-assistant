import time

import httpx

from app.core.settings import get_settings
from app.services.perf_capture import emit_perf_event


class AppScriptBridgeService:
    def __init__(self) -> None:
        self.settings = get_settings()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.appscript_web_app_url and self.settings.appscript_api_key)

    async def invoke(
        self,
        action: str,
        payload: dict | None = None,
        method: str = "POST",
    ) -> dict:
        started = time.perf_counter()
        method_upper = method.upper()
        url = self.settings.appscript_web_app_url.strip()
        if not url:
            emit_perf_event(
                "appscript_call",
                {
                    "action": action,
                    "method": method_upper,
                    "status": "skipped",
                    "http_status": None,
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                    "reason": "web_app_url_not_configured",
                },
            )
            return {
                "action": action,
                "status": "skipped",
                "detail": "APPS_SCRIPT_WEB_APP_URL is not configured.",
                "http_status": None,
                "data": {},
            }

        if method_upper == "POST" and not self.settings.appscript_api_key:
            emit_perf_event(
                "appscript_call",
                {
                    "action": action,
                    "method": method_upper,
                    "status": "skipped",
                    "http_status": None,
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                    "reason": "api_key_not_configured",
                },
            )
            return {
                "action": action,
                "status": "skipped",
                "detail": "APPS_SCRIPT_API_KEY is not configured.",
                "http_status": None,
                "data": {},
            }

        try:
            async with httpx.AsyncClient(
                timeout=self.settings.appscript_timeout_seconds,
                follow_redirects=True,
            ) as client:
                if method_upper == "GET":
                    params = {"action": action}
                    # Health endpoint is intentionally public; avoid key in URL logs.
                    if action != "health":
                        params["api_key"] = self.settings.appscript_api_key
                    response = await client.get(
                        url,
                        params=params,
                    )
                else:
                    response = await client.post(
                        url,
                        json={
                            "action": action,
                            "api_key": self.settings.appscript_api_key,
                            "payload": payload or {},
                        },
                    )

            try:
                data = response.json()
            except ValueError:
                data = {"raw": response.text}

            status = "ok" if response.is_success else "error"
            if response.is_success and isinstance(data, dict) and data.get("ok") is False:
                status = "error"

            detail = None
            if status == "error":
                if isinstance(data, dict) and data.get("error"):
                    detail = f"Apps Script error: {data.get('error')}"
                else:
                    detail = f"Apps Script returned HTTP {response.status_code}."

            emit_perf_event(
                "appscript_call",
                {
                    "action": action,
                    "method": method_upper,
                    "status": status,
                    "http_status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                },
            )

            return {
                "action": action,
                "status": status,
                "detail": detail,
                "http_status": response.status_code,
                "data": data if isinstance(data, dict) else {"result": data},
            }
        except httpx.HTTPError as exc:
            emit_perf_event(
                "appscript_call",
                {
                    "action": action,
                    "method": method_upper,
                    "status": "error",
                    "http_status": None,
                    "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                    "error": str(exc),
                },
            )
            return {
                "action": action,
                "status": "error",
                "detail": str(exc),
                "http_status": None,
                "data": {},
            }
