import httpx
import asyncio
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
        "triggers": [],
        "automations": [],
        "macros": [],
        "views": [],
        "ticket_fields": [],
        "articles": [],
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

        endpoint_map = {
            "brands": "/api/v2/brands.json",
            "groups": "/api/v2/groups.json",
            "ticket_forms": "/api/v2/ticket_forms.json",
            "triggers": "/api/v2/triggers.json",
            "automations": "/api/v2/automations.json",
            "macros": "/api/v2/macros.json",
            "views": "/api/v2/views.json",
            "ticket_fields": "/api/v2/ticket_fields.json",
            "articles": "/api/v2/help_center/articles.json?per_page=100",
            "help_centers": "/api/v2/help_center/help_centers.json",
            "categories": "/api/v2/help_center/categories.json?per_page=100",
            "sections": "/api/v2/help_center/sections.json?per_page=100",
        }
        payloads = await asyncio.gather(
            *[_safe_get(path) for path in endpoint_map.values()]
        )
        payload_by_key = dict(zip(endpoint_map.keys(), payloads))

        def _add(
            *,
            target_key: str,
            object_type: str,
            entries: list,
            name_field: str = "name",
            description_builder=None,
        ) -> None:
            for item in entries:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id", "")).strip()
                name = str(item.get(name_field, "")).strip()
                if not item_id or not name:
                    continue
                payload = {"object_type": object_type, "id": item_id, "name": name}
                if callable(description_builder):
                    description = description_builder(item)
                    if description:
                        payload["description"] = description
                catalogs[target_key].append(payload)

        def _rule_summary(item: dict) -> str:
            conditions = item.get("conditions", {}) if isinstance(item, dict) else {}
            all_conditions = conditions.get("all", []) if isinstance(conditions, dict) else []
            actions = item.get("actions", []) if isinstance(item, dict) else []
            cond_count = len(all_conditions) if isinstance(all_conditions, list) else 0
            action_count = len(actions) if isinstance(actions, list) else 0
            return f"conditions={cond_count}; actions={action_count}"

        def _macro_summary(item: dict) -> str:
            actions = item.get("actions", []) if isinstance(item, dict) else []
            action_count = len(actions) if isinstance(actions, list) else 0
            return f"actions={action_count}"

        def _view_summary(item: dict) -> str:
            all_conditions = item.get("all", []) if isinstance(item, dict) else []
            any_conditions = item.get("any", []) if isinstance(item, dict) else []
            return f"all={len(all_conditions) if isinstance(all_conditions, list) else 0}; any={len(any_conditions) if isinstance(any_conditions, list) else 0}"

        def _ticket_field_summary(item: dict) -> str:
            field_type = str(item.get("type", "")).strip().lower()
            tag = str(item.get("tag", "")).strip()
            if tag:
                return f"type={field_type}; tag={tag}"
            return f"type={field_type}"

        def _article_summary(item: dict) -> str:
            label_names = item.get("label_names", []) if isinstance(item, dict) else []
            section_id = str(item.get("section_id", "")).strip()
            tags = ", ".join(label_names[:3]) if isinstance(label_names, list) and label_names else ""
            parts = []
            if section_id:
                parts.append(f"section_id={section_id}")
            if tags:
                parts.append(f"labels={tags}")
            return "; ".join(parts)

        _add(
            target_key="brands",
            object_type="brand",
            entries=payload_by_key.get("brands", {}).get("brands", []) if isinstance(payload_by_key.get("brands"), dict) else [],
        )
        _add(
            target_key="groups",
            object_type="group",
            entries=payload_by_key.get("groups", {}).get("groups", []) if isinstance(payload_by_key.get("groups"), dict) else [],
        )
        _add(
            target_key="ticket_forms",
            object_type="ticket_form",
            entries=payload_by_key.get("ticket_forms", {}).get("ticket_forms", []) if isinstance(payload_by_key.get("ticket_forms"), dict) else [],
        )
        _add(
            target_key="triggers",
            object_type="trigger",
            entries=payload_by_key.get("triggers", {}).get("triggers", []) if isinstance(payload_by_key.get("triggers"), dict) else [],
            name_field="title",
            description_builder=_rule_summary,
        )
        _add(
            target_key="automations",
            object_type="automation",
            entries=payload_by_key.get("automations", {}).get("automations", []) if isinstance(payload_by_key.get("automations"), dict) else [],
            name_field="title",
            description_builder=_rule_summary,
        )
        _add(
            target_key="macros",
            object_type="macro",
            entries=payload_by_key.get("macros", {}).get("macros", []) if isinstance(payload_by_key.get("macros"), dict) else [],
            name_field="title",
            description_builder=_macro_summary,
        )
        _add(
            target_key="views",
            object_type="view",
            entries=payload_by_key.get("views", {}).get("views", []) if isinstance(payload_by_key.get("views"), dict) else [],
            name_field="title",
            description_builder=_view_summary,
        )
        _add(
            target_key="ticket_fields",
            object_type="ticket_field",
            entries=payload_by_key.get("ticket_fields", {}).get("ticket_fields", []) if isinstance(payload_by_key.get("ticket_fields"), dict) else [],
            name_field="title",
            description_builder=_ticket_field_summary,
        )
        _add(
            target_key="articles",
            object_type="article",
            entries=payload_by_key.get("articles", {}).get("articles", []) if isinstance(payload_by_key.get("articles"), dict) else [],
            name_field="title",
            description_builder=_article_summary,
        )
        _add(
            target_key="help_centers",
            object_type="help_center",
            entries=payload_by_key.get("help_centers", {}).get("help_centers", []) if isinstance(payload_by_key.get("help_centers"), dict) else [],
        )
        _add(
            target_key="categories",
            object_type="category",
            entries=payload_by_key.get("categories", {}).get("categories", []) if isinstance(payload_by_key.get("categories"), dict) else [],
        )
        _add(
            target_key="sections",
            object_type="section",
            entries=payload_by_key.get("sections", {}).get("sections", []) if isinstance(payload_by_key.get("sections"), dict) else [],
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


def _build_rule_payload(record: dict, root_key: str) -> dict:
    conditions = record.get("conditions", []) or []
    actions = record.get("actions", []) or []
    all_conditions = [_normalize_condition(item) for item in conditions if item.get("field")]
    if not all_conditions:
        all_conditions = [{"field": "status", "operator": "less_than", "value": "solved"}]
    return {
        root_key: {
            "title": str(record.get("title", "Untitled trigger")).strip() or "Untitled trigger",
            "active": True,
            "conditions": {
                "all": all_conditions,
                "any": [],
            },
            "actions": [_normalize_action(item) for item in actions if item.get("field")],
        }
    }


def _build_trigger_payload(record: dict) -> dict:
    return _build_rule_payload(record, "trigger")


def _build_automation_payload(record: dict) -> dict:
    return _build_rule_payload(record, "automation")


def _find_action_values(record: dict, target_field: str) -> list:
    actions = record.get("actions", []) or []
    results = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        field = str(action.get("field", "")).strip().lower()
        if field == target_field:
            results.append(action.get("value"))
    return results


def _find_first_value(record: dict, target_field: str):
    values = _find_action_values(record, target_field)
    if values:
        return values[0]
    for condition in record.get("conditions", []) or []:
        if not isinstance(condition, dict):
            continue
        field = str(condition.get("field", "")).strip().lower()
        if field == target_field:
            return condition.get("value")
    return None


def _build_macro_payload(record: dict) -> dict:
    actions = [_normalize_action(item) for item in (record.get("actions", []) or []) if item.get("field")]
    return {
        "macro": {
            "title": str(record.get("title", "Untitled macro")).strip() or "Untitled macro",
            "active": True,
            "actions": actions,
        }
    }


def _build_view_payload(record: dict) -> dict:
    conditions = record.get("conditions", []) or []
    all_conditions = [_normalize_condition(item) for item in conditions if item.get("field")]
    if not all_conditions:
        all_conditions = [{"field": "status", "operator": "less_than", "value": "solved"}]

    raw_columns = _find_first_value(record, "output_columns")
    if isinstance(raw_columns, list):
        output_columns = [str(item).strip() for item in raw_columns if str(item).strip()]
    elif isinstance(raw_columns, str):
        output_columns = [part.strip() for part in raw_columns.split(",") if part.strip()]
    else:
        output_columns = ["status", "updated", "subject"]

    return {
        "view": {
            "title": str(record.get("title", "Untitled view")).strip() or "Untitled view",
            "active": True,
            "all": all_conditions,
            "any": [],
            "output": {"columns": output_columns},
        }
    }


def _build_group_payload(record: dict) -> dict:
    name = str(record.get("title", "")).strip() or "Untitled group"
    description_value = _find_first_value(record, "description")
    payload = {"group": {"name": name}}
    if description_value:
        payload["group"]["description"] = str(description_value)
    return payload


def _build_ticket_form_payload(record: dict) -> dict:
    payload = {"ticket_form": {"name": str(record.get("title", "Untitled form")).strip() or "Untitled form"}}
    raw_field_ids = _find_first_value(record, "ticket_field_ids")
    if isinstance(raw_field_ids, list):
        ids = [int(str(item)) for item in raw_field_ids if str(item).isdigit()]
        if ids:
            payload["ticket_form"]["ticket_field_ids"] = ids
    elif isinstance(raw_field_ids, str):
        ids = [int(part.strip()) for part in raw_field_ids.split(",") if part.strip().isdigit()]
        if ids:
            payload["ticket_form"]["ticket_field_ids"] = ids
    return payload


def _build_ticket_field_payload(record: dict) -> dict:
    title = str(record.get("title", "Untitled field")).strip() or "Untitled field"
    field_type = str(_find_first_value(record, "field_type") or "text").strip().lower()
    payload = {"ticket_field": {"title": title, "type": field_type}}
    tag_value = _find_first_value(record, "tag")
    if tag_value:
        payload["ticket_field"]["tag"] = str(tag_value).strip()
    title_portal = _find_first_value(record, "title_in_portal")
    if title_portal:
        payload["ticket_field"]["title_in_portal"] = str(title_portal).strip()
    options = _find_first_value(record, "custom_field_options")
    if isinstance(options, list):
        parsed_options = []
        for item in options:
            if isinstance(item, dict) and item.get("name") and item.get("value"):
                parsed_options.append({"name": str(item["name"]), "value": str(item["value"])})
        if parsed_options:
            payload["ticket_field"]["custom_field_options"] = parsed_options
    return payload


def _build_article_payload(record: dict) -> tuple[dict, str | None]:
    title = str(record.get("title", "Untitled article")).strip() or "Untitled article"
    body = str(_find_first_value(record, "body") or "").strip()
    if not body:
        body = f"<p>{title}</p>"
    locale = str(_find_first_value(record, "locale") or "en-us").strip().lower()
    section_id = _find_first_value(record, "section_id")
    if not section_id:
        return {}, "Article requires a section_id in conditions/actions."
    section_id_text = str(section_id).strip()
    if not section_id_text.isdigit():
        return {}, "Article section_id must be numeric."
    payload = {
        "article": {
            "title": title,
            "body": body,
            "locale": locale,
            "draft": False,
        },
        "notify_subscribers": False,
    }
    return payload, section_id_text


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
    on_existing: str = "create_new",
) -> dict:
    base_url = _build_base_url(_normalize_subdomain(subdomain))
    auth_user = f"{email}/token"

    results: list[dict] = []
    attempted = 0
    deployed = 0
    failed = 0
    skipped = 0
    group_lookup_cache: dict[str, str] | None = None
    existing_cache: dict[str, dict[str, str]] = {}

    object_mappings = {
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
        "ticket_form": "ticket_forms",
        "ticket_forms": "ticket_forms",
        "ticket_field": "ticket_fields",
        "ticket_fields": "ticket_fields",
        "article": "articles",
        "articles": "articles",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        async def _safe_list(path: str) -> dict:
            try:
                response = await client.get(
                    f"{base_url}{path}",
                    auth=(auth_user, api_token),
                    headers={"Content-Type": "application/json"},
                )
                if not response.is_success:
                    return {}
                return response.json() if response.text else {}
            except Exception:
                return {}

        async def _load_existing_map(object_type: str) -> dict[str, str]:
            if object_type in existing_cache:
                return existing_cache[object_type]

            endpoint_by_type = {
                "triggers": "/api/v2/triggers.json",
                "automations": "/api/v2/automations.json",
                "macros": "/api/v2/macros.json",
                "views": "/api/v2/views.json",
                "groups": "/api/v2/groups.json",
                "ticket_forms": "/api/v2/ticket_forms.json",
                "ticket_fields": "/api/v2/ticket_fields.json",
                "articles": "/api/v2/help_center/articles.json?per_page=100",
            }
            items_key_by_type = {
                "triggers": "triggers",
                "automations": "automations",
                "macros": "macros",
                "views": "views",
                "groups": "groups",
                "ticket_forms": "ticket_forms",
                "ticket_fields": "ticket_fields",
                "articles": "articles",
            }

            path = endpoint_by_type.get(object_type)
            if not path:
                existing_cache[object_type] = {}
                return {}

            payload = await _safe_list(path)
            items_key = items_key_by_type.get(object_type, object_type)
            entries = payload.get(items_key, []) if isinstance(payload, dict) else []
            match_map: dict[str, str] = {}
            for item in entries:
                if not isinstance(item, dict):
                    continue
                item_id = str(item.get("id", "")).strip()
                if not item_id:
                    continue
                if object_type in {"groups", "ticket_forms"}:
                    name = str(item.get("name", "")).strip().lower()
                else:
                    name = str(item.get("title", item.get("name", ""))).strip().lower()
                if name:
                    match_map[name] = item_id
            existing_cache[object_type] = match_map
            return match_map

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
            raw_object_type = str(record.get("object_type", "")).strip().lower()
            object_type = object_mappings.get(raw_object_type, raw_object_type)
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

            supported_types = {
                "triggers",
                "automations",
                "macros",
                "views",
                "groups",
                "ticket_forms",
                "ticket_fields",
                "articles",
            }
            if object_type not in supported_types:
                skipped += 1
                results.append(
                    {
                        "record_id": record_id,
                        "object_type": object_type or "unknown",
                        "title": title,
                        "deployment_status": "skipped",
                        "zendesk_object_id": None,
                        "execution_message": f"Object type '{object_type}' deploy is not implemented yet.",
                        "executed_at": executed_at,
                    }
                )
                continue

            payload = {}
            create_path = ""
            update_path_template = ""
            response_root = ""

            if object_type == "triggers":
                payload = _build_trigger_payload(record)
                create_path = "/api/v2/triggers.json"
                update_path_template = "/api/v2/triggers/{id}.json"
                response_root = "trigger"
            elif object_type == "automations":
                payload = _build_automation_payload(record)
                create_path = "/api/v2/automations.json"
                update_path_template = "/api/v2/automations/{id}.json"
                response_root = "automation"
            elif object_type == "macros":
                payload = _build_macro_payload(record)
                create_path = "/api/v2/macros.json"
                update_path_template = "/api/v2/macros/{id}.json"
                response_root = "macro"
            elif object_type == "views":
                payload = _build_view_payload(record)
                create_path = "/api/v2/views.json"
                update_path_template = "/api/v2/views/{id}.json"
                response_root = "view"
            elif object_type == "groups":
                payload = _build_group_payload(record)
                create_path = "/api/v2/groups.json"
                update_path_template = "/api/v2/groups/{id}.json"
                response_root = "group"
            elif object_type == "ticket_forms":
                payload = _build_ticket_form_payload(record)
                create_path = "/api/v2/ticket_forms.json"
                update_path_template = "/api/v2/ticket_forms/{id}.json"
                response_root = "ticket_form"
            elif object_type == "ticket_fields":
                payload = _build_ticket_field_payload(record)
                create_path = "/api/v2/ticket_fields.json"
                update_path_template = "/api/v2/ticket_fields/{id}.json"
                response_root = "ticket_field"
            elif object_type == "articles":
                payload, section_id_or_error = _build_article_payload(record)
                if not payload:
                    failed += 1
                    results.append(
                        {
                            "record_id": record_id,
                            "object_type": object_type,
                            "title": title,
                            "deployment_status": "failed",
                            "zendesk_object_id": None,
                            "execution_message": str(section_id_or_error),
                            "executed_at": executed_at,
                        }
                    )
                    continue
                create_path = f"/api/v2/help_center/sections/{section_id_or_error}/articles.json"
                update_path_template = "/api/v2/help_center/articles/{id}.json"
                response_root = "article"

            if object_type in {"triggers", "automations"}:
                rule_key = "trigger" if object_type == "triggers" else "automation"
                actions = payload.get(rule_key, {}).get("actions", [])
                if not actions:
                    failed += 1
                    results.append(
                        {
                            "record_id": record_id,
                            "object_type": object_type,
                            "title": title,
                            "deployment_status": "failed",
                            "zendesk_object_id": None,
                            "execution_message": "Record has no valid actions.",
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

            existing_id = None
            if on_existing in {"overwrite_existing", "skip_existing"} and title:
                existing_map = await _load_existing_map(object_type)
                existing_id = existing_map.get(title.strip().lower())

            if existing_id and on_existing == "skip_existing":
                skipped += 1
                results.append(
                    {
                        "record_id": record_id,
                        "object_type": object_type,
                        "title": title,
                        "deployment_status": "skipped",
                        "zendesk_object_id": existing_id,
                        "execution_message": "Skipped because object already exists (on_existing=skip_existing).",
                        "executed_at": executed_at,
                    }
                )
                continue

            method = "POST"
            request_path = create_path
            success_text = "created"
            if existing_id and on_existing == "overwrite_existing":
                method = "PUT"
                request_path = update_path_template.format(id=existing_id)
                success_text = "updated"

            if dry_run:
                deployed += 1
                results.append(
                    {
                        "record_id": record_id,
                        "object_type": object_type,
                        "title": title,
                        "deployment_status": "deployed",
                        "zendesk_object_id": existing_id or "DRY-RUN",
                        "execution_message": f"Dry run success. Payload validated locally ({success_text}).",
                        "executed_at": executed_at,
                    }
                )
                continue

            try:
                if method == "POST":
                    response = await client.post(
                        f"{base_url}{request_path}",
                        auth=(auth_user, api_token),
                        headers={"Content-Type": "application/json"},
                        json=payload,
                    )
                else:
                    response = await client.put(
                        f"{base_url}{request_path}",
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
                        "zendesk_object_id": existing_id,
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
                response_object = response_payload.get(response_root, {}) if isinstance(response_payload, dict) else {}
                object_id = str(response_object.get("id")) if isinstance(response_object, dict) and response_object.get("id") is not None else (existing_id or None)
                deployed += 1
                results.append(
                    {
                        "record_id": record_id,
                        "object_type": object_type,
                        "title": title,
                        "deployment_status": "deployed",
                        "zendesk_object_id": object_id,
                        "execution_message": f"{object_type.rstrip('s').title()} {success_text} successfully.",
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
                    "zendesk_object_id": existing_id,
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
