import httpx


def _build_base_url(subdomain: str) -> str:
    return f"https://{subdomain}.zendesk.com"


async def validate_zendesk_credentials(subdomain: str, email: str, api_token: str) -> dict:
    base_url = _build_base_url(subdomain)
    url = f"{base_url}/api/v2/users/me.json"
    auth_user = f"{email}/token"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                url,
                auth=(auth_user, api_token),
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "detail": f"Zendesk request failed: {exc}",
            "subdomain": subdomain,
            "base_url": base_url,
            "account_name": None,
            "authenticated_user": None,
            "authenticated_user_role": None,
            "http_status": None,
        }

    payload: dict = {}
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if not response.is_success:
        error_detail = payload.get("error") if isinstance(payload, dict) else None
        description = payload.get("description") if isinstance(payload, dict) else None
        message = f"Zendesk authentication failed ({response.status_code})."
        if error_detail or description:
            message = (
                f"Zendesk authentication failed ({response.status_code}): "
                f"{error_detail or 'error'}"
            )
            if description:
                message += f" - {description}"

        return {
            "ok": False,
            "detail": message,
            "subdomain": subdomain,
            "base_url": base_url,
            "account_name": None,
            "authenticated_user": None,
            "authenticated_user_role": None,
            "http_status": response.status_code,
        }

    user = payload.get("user", {}) if isinstance(payload, dict) else {}
    return {
        "ok": True,
        "detail": "Zendesk credentials are valid.",
        "subdomain": subdomain,
        "base_url": base_url,
        "account_name": payload.get("account", {}).get("name") if isinstance(payload, dict) else None,
        "authenticated_user": user.get("name"),
        "authenticated_user_role": user.get("role"),
        "http_status": response.status_code,
    }
