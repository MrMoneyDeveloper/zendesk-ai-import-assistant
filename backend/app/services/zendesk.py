import httpx
from datetime import UTC, datetime


def _build_base_url(subdomain: str) -> str:
    return f"https://{subdomain}.zendesk.com"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_subdomain(subdomain: str) -> str:
    cleaned = subdomain.strip().lower()
    if cleaned.startswith("https://"):
        cleaned = cleaned.removeprefix("https://")
    if cleaned.startswith("http://"):
        cleaned = cleaned.removeprefix("http://")
    if cleaned.endswith(".zendesk.com"):
        cleaned = cleaned.removesuffix(".zendesk.com")
    return cleaned.strip("/")


def _normalize_condition(condition: dict) -> dict:
    field = str(condition.get("field", "")).strip()
    operator = str(condition.get("operator", "is")).strip() or "is"
    normalized = {"field": field, "operator": operator}
    if "value" in condition:
        normalized["value"] = condition.get("value")
    return normalized


def _normalize_action(action: dict) -> dict:
    raw_field = str(action.get("field", "")).strip().lower()
    field_aliases = {
        "assign": "group_id",
        "group": "group_id",
        "group_name": "group_id",
        "assignee": "assignee_id",
    }
    normalized_field = field_aliases.get(raw_field, raw_field)
    return {
        "field": normalized_field,
        "value": action.get("value"),
    }


async def fetch_zendesk_reference_catalog(
    *,
    subdomain: str,
    email: str,
    api_token: str,
) -> dict:
    normalized_subdomain = _normalize_subdomain(subdomain)
    base_url = _build_base_url(normalized_subdomain)
    auth_user = f"{email}/token"
    warnings: list[str] = []

    catalogs: dict[str, list[dict]] = {
        "brands": [],
        "groups": [],
        "ticket_forms": [],
        "help_centers": [],
        "categories": [],
        "sections": [],
    }

    async with httpx.AsyncClient(timeout=25) as client:
        async def _safe_get(path: str) -> dict:
            try:
                response = await client.get(
                    f"{base_url}{path}",
                    auth=(auth_user, api_token),
                    headers={"Content-Type": "application/json"},
                )
            except httpx.HTTPError as exc:
                warnings.append(f"{path}: request failed ({exc})")
                return {}
            if not response.is_success:
                warnings.append(f"{path}: HTTP {response.status_code}")
                return {}
            try:
                return response.json() if response.text else {}
            except ValueError:
                warnings.append(f"{path}: response was not valid JSON.")
                return {}

        brands_payload = await _safe_get("/api/v2/brands.json")
        for item in brands_payload.get("brands", []) if isinstance(brands_payload, dict) else []:
            item_id = str(item.get("id", "")).strip()
            name = str(item.get("name", "")).strip()
            if item_id and name:
                catalogs["brands"].append(
                    {"object_type": "brand", "id": item_id, "name": name}
                )

        groups_payload = await _safe_get("/api/v2/groups.json")
        for item in groups_payload.get("groups", []) if isinstance(groups_payload, dict) else []:
            item_id = str(item.get("id", "")).strip()
            name = str(item.get("name", "")).strip()
            if item_id and name:
                catalogs["groups"].append(
                    {"object_type": "group", "id": item_id, "name": name}
                )

        forms_payload = await _safe_get("/api/v2/ticket_forms.json")
        for item in forms_payload.get("ticket_forms", []) if isinstance(forms_payload, dict) else []:
            item_id = str(item.get("id", "")).strip()
            name = str(item.get("name", "")).strip()
            if item_id and name:
                catalogs["ticket_forms"].append(
                    {"object_type": "ticket_form", "id": item_id, "name": name}
                )

        help_centers_payload = await _safe_get("/api/v2/help_center/help_centers.json")
        for item in (
            help_centers_payload.get("help_centers", [])
            if isinstance(help_centers_payload, dict)
            else []
        ):
            item_id = str(item.get("id", "")).strip()
            name = str(item.get("name", "")).strip()
            if item_id and name:
                catalogs["help_centers"].append(
                    {"object_type": "help_center", "id": item_id, "name": name}
                )

        categories_payload = await _safe_get("/api/v2/help_center/categories.json?per_page=100")
        for item in (
            categories_payload.get("categories", []) if isinstance(categories_payload, dict) else []
        ):
            item_id = str(item.get("id", "")).strip()
            name = str(item.get("name", "")).strip()
            if item_id and name:
                catalogs["categories"].append(
                    {"object_type": "category", "id": item_id, "name": name}
                )

        sections_payload = await _safe_get("/api/v2/help_center/sections.json?per_page=100")
        for item in (
            sections_payload.get("sections", []) if isinstance(sections_payload, dict) else []
        ):
            item_id = str(item.get("id", "")).strip()
            name = str(item.get("name", "")).strip()
            if item_id and name:
                catalogs["sections"].append(
                    {"object_type": "section", "id": item_id, "name": name}
                )

    fetched_total = sum(len(v) for v in catalogs.values())
    if fetched_total == 0:
        return {
            "ok": False,
            "detail": "No Zendesk catalog entries were returned. Check Guide/Support permissions.",
            "base_url": base_url,
            "catalogs": catalogs,
            "fetched_at": _now_iso(),
            "warnings": warnings,
        }

    return {
        "ok": True,
        "detail": "Zendesk catalogs fetched.",
        "base_url": base_url,
        "catalogs": catalogs,
        "fetched_at": _now_iso(),
        "warnings": warnings,
    }


def _build_trigger_payload(record: dict) -> dict:
    conditions = record.get("conditions", []) or []
    actions = record.get("actions", []) or []
    return {
        "trigger": {
            "title": str(record.get("title", "Untitled trigger")).strip() or "Untitled trigger",
            "active": True,
            "conditions": {
                "all": [_normalize_condition(item) for item in conditions if item.get("field")],
                "any": [],
            },
            "actions": [_normalize_action(item) for item in actions if item.get("field")],
        }
    }


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


async def deploy_records_to_zendesk(
    *,
    subdomain: str,
    email: str,
    api_token: str,
    records: list[dict],
    dry_run: bool = False,
) -> dict:
    base_url = _build_base_url(subdomain)
    auth_user = f"{email}/token"

    results: list[dict] = []
    attempted = 0
    deployed = 0
    failed = 0
    skipped = 0
    group_lookup_cache: dict[str, str] | None = None

    async with httpx.AsyncClient(timeout=30) as client:
        async def resolve_group_id(raw_value) -> str | None:
            nonlocal group_lookup_cache
            if raw_value is None:
                return None
            raw = str(raw_value).strip()
            if not raw:
                return None
            if raw.isdigit():
                return raw

            if group_lookup_cache is None:
                group_lookup_cache = {}
                try:
                    groups_response = await client.get(
                        f"{base_url}/api/v2/groups.json",
                        auth=(auth_user, api_token),
                        headers={"Content-Type": "application/json"},
                    )
                    if groups_response.is_success:
                        groups_payload = groups_response.json() if groups_response.text else {}
                        groups = (
                            groups_payload.get("groups", [])
                            if isinstance(groups_payload, dict)
                            else []
                        )
                        for group in groups:
                            name = str(group.get("name", "")).strip().lower()
                            group_id = str(group.get("id", "")).strip()
                            if name and group_id:
                                group_lookup_cache[name] = group_id
                except Exception:
                    group_lookup_cache = {}

            return group_lookup_cache.get(raw.lower())

        for record in records:
            record_id = str(record.get("record_id", "")).strip()
            object_type = str(record.get("object_type", "")).strip().lower()
            title = str(record.get("title", "")).strip()
            executed_at = _now_iso()
            decision = str(record.get("import_decision", "")).strip().lower()
            deployable = bool(record.get("deployable", False))

            if decision != "approved" or not deployable:
                skipped += 1
                results.append(
                    {
                        "record_id": record_id,
                        "object_type": object_type or "unknown",
                        "title": title,
                        "deployment_status": "skipped",
                        "zendesk_object_id": None,
                        "execution_message": "Skipped (not approved or blocked by validation).",
                        "executed_at": executed_at,
                    }
                )
                continue

            attempted += 1

            if object_type not in {"trigger", "triggers"}:
                skipped += 1
                results.append(
                    {
                        "record_id": record_id,
                        "object_type": object_type or "unknown",
                        "title": title,
                        "deployment_status": "skipped",
                        "zendesk_object_id": None,
                        "execution_message": "Object type deploy not implemented yet; trigger deploy is supported.",
                        "executed_at": executed_at,
                    }
                )
                continue

            payload = _build_trigger_payload(record)
            actions = payload.get("trigger", {}).get("actions", [])
            if not actions:
                failed += 1
                results.append(
                    {
                        "record_id": record_id,
                        "object_type": object_type,
                        "title": title,
                        "deployment_status": "failed",
                        "zendesk_object_id": None,
                        "execution_message": "Trigger has no valid actions.",
                        "executed_at": executed_at,
                    }
                )
                continue

            unresolved_group = None
            for action in actions:
                if action.get("field") != "group_id":
                    continue
                resolved_group_id = await resolve_group_id(action.get("value"))
                if resolved_group_id:
                    action["value"] = resolved_group_id
                    continue
                raw_group = str(action.get("value", "")).strip()
                if raw_group and not raw_group.isdigit():
                    unresolved_group = raw_group
                    break

            if unresolved_group:
                failed += 1
                results.append(
                    {
                        "record_id": record_id,
                        "object_type": object_type,
                        "title": title,
                        "deployment_status": "failed",
                        "zendesk_object_id": None,
                        "execution_message": (
                            f"Could not resolve group '{unresolved_group}' to a Zendesk group_id. "
                            "Use a numeric group_id or an existing exact group name."
                        ),
                        "executed_at": executed_at,
                    }
                )
                continue

            if dry_run:
                deployed += 1
                results.append(
                    {
                        "record_id": record_id,
                        "object_type": object_type,
                        "title": title,
                        "deployment_status": "deployed",
                        "zendesk_object_id": "DRY-RUN",
                        "execution_message": "Dry run success. Payload validated locally.",
                        "executed_at": executed_at,
                    }
                )
                continue

            try:
                response = await client.post(
                    f"{base_url}/api/v2/triggers.json",
                    auth=(auth_user, api_token),
                    headers={"Content-Type": "application/json"},
                    json=payload,
                )
            except httpx.HTTPError as exc:
                failed += 1
                results.append(
                    {
                        "record_id": record_id,
                        "object_type": object_type,
                        "title": title,
                        "deployment_status": "failed",
                        "zendesk_object_id": None,
                        "execution_message": f"Zendesk request failed: {exc}",
                        "executed_at": executed_at,
                    }
                )
                continue

            response_payload: dict = {}
            try:
                response_payload = response.json()
            except ValueError:
                response_payload = {}

            if response.is_success:
                trigger_id = (
                    str(response_payload.get("trigger", {}).get("id"))
                    if isinstance(response_payload, dict)
                    else None
                )
                deployed += 1
                results.append(
                    {
                        "record_id": record_id,
                        "object_type": object_type,
                        "title": title,
                        "deployment_status": "deployed",
                        "zendesk_object_id": trigger_id,
                        "execution_message": "Trigger created successfully.",
                        "executed_at": executed_at,
                    }
                )
                continue

            failed += 1
            detail = "Zendesk deployment failed."
            if isinstance(response_payload, dict):
                error = response_payload.get("error")
                description = response_payload.get("description")
                detail = f"Zendesk deployment failed ({response.status_code})"
                if error:
                    detail += f": {error}"
                if description:
                    detail += f" - {description}"
            results.append(
                {
                    "record_id": record_id,
                    "object_type": object_type,
                    "title": title,
                    "deployment_status": "failed",
                    "zendesk_object_id": None,
                    "execution_message": detail,
                    "executed_at": executed_at,
                }
            )

    return {
        "summary": {
            "attempted": attempted,
            "deployed": deployed,
            "failed": failed,
            "skipped": skipped,
        },
        "results": results,
        "base_url": base_url,
    }
