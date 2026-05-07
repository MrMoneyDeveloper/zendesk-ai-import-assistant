import httpx

from app.core.settings import get_settings


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
        url = self.settings.appscript_web_app_url.strip()
        if not url:
            return {
                "action": action,
                "status": "skipped",
                "detail": "APPS_SCRIPT_WEB_APP_URL is not configured.",
                "http_status": None,
                "data": {},
            }

        if method.upper() == "POST" and not self.settings.appscript_api_key:
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
                if method.upper() == "GET":
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

            return {
                "action": action,
                "status": status,
                "detail": detail,
                "http_status": response.status_code,
                "data": data if isinstance(data, dict) else {"result": data},
            }
        except httpx.HTTPError as exc:
            return {
                "action": action,
                "status": "error",
                "detail": str(exc),
                "http_status": None,
                "data": {},
            }
