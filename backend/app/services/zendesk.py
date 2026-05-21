import httpx
import asyncio
import time
import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from app.core.settings import get_settings
from app.services.perf_capture import emit_perf_event

TICKET_FIELD_TYPE_ALIASES = {
    "dropdown": "tagger",
    "drop-down": "tagger",
    "drop_down": "tagger",
    "single-select": "tagger",
    "single_select": "tagger",
    "single select": "tagger",
    "select": "tagger",
    "tagger": "tagger",
    "multi-select": "multiselect",
    "multi_select": "multiselect",
    "multi select": "multiselect",
    "multiselect": "multiselect",
}

TICKET_FIELD_TYPE_FIELDS = {"field_type", "fieldtype", "field_type_name", "type"}
TICKET_FIELD_OPTIONS_FIELDS = {"custom_field_options", "options", "values", "field_values", "choices"}
TICKET_FIELD_PERMISSION_FIELDS = {
    "agent_can_edit": {"agent_can_edit", "agents_can_edit", "agent_editable"},
    "visible_in_portal": {"visible_in_portal", "customers_can_view", "customer_can_view"},
    "editable_in_portal": {"editable_in_portal", "customers_can_edit", "customer_can_edit"},
    "required": {"required", "required_to_solve", "required_for_agents"},
    "required_in_portal": {"required_in_portal", "required_to_submit", "required_for_customers"},
}
TICKET_FORM_REFERENCE_FIELDS = {
    "ticket_field_ids",
    "ticket_fields",
    "field_ids",
    "field_names",
    "fields",
    "ticket_field_names",
}

_HELP_CENTER_FALLBACK_404_COOLDOWN: dict[str, float] = {}

RULE_ACTION_ALLOWLIST = {
    "group_id",
    "assignee_id",
    "set_tags",
    "status",
    "priority",
    "comment_value",
    "notification_user",
    "notification_group",
}
VIEW_CONDITION_ALLOWLIST = {
    "status",
    "group_id",
    "priority",
    "ticket_form_id",
    "brand_id",
    "tags",
}


def _build_base_url(subdomain: str) -> str:
    return f"https://{subdomain}.zendesk.com"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _fallback_probe_allowed(path: str, cooldown_seconds: int) -> bool:
    if cooldown_seconds <= 0:
        return True
    now = time.monotonic()
    blocked_until = float(_HELP_CENTER_FALLBACK_404_COOLDOWN.get(path, 0.0) or 0.0)
    return blocked_until <= now


def _mark_fallback_404(path: str, cooldown_seconds: int) -> None:
    if cooldown_seconds <= 0:
        return
    _HELP_CENTER_FALLBACK_404_COOLDOWN[path] = time.monotonic() + float(cooldown_seconds)


def _sanitize_path(url_or_path: str) -> str:
    text = str(url_or_path or "").strip()
    if not text:
        return "/"
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urlparse(text)
        return parsed.path or "/"
    return text


def _emit_zendesk_http_event(
    *,
    operation: str,
    method: str,
    url_or_path: str,
    status_code: int | None,
    duration_ms: float,
    success: bool,
    error: str | None = None,
) -> None:
    emit_perf_event(
        "zendesk_http",
        {
            "operation": operation,
            "method": method.upper(),
            "path": _sanitize_path(url_or_path),
            "http_status": status_code,
            "success": success,
            "duration_ms": round(duration_ms, 2),
            "error": error or "",
        },
    )


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
        "team": "group_id",
        "add_tags": "set_tags",
        "add_tag": "set_tags",
        "tag": "set_tags",
        "tags": "set_tags",
        "request_type": "set_tags",
        "add_note": "comment_value",
        "comment": "comment_value",
        "comment_body": "comment_value",
        "comment_text": "comment_value",
    }
    normalized_field = field_aliases.get(raw_field, raw_field)
    raw_value = action.get("value")
    if normalized_field == "set_tags":
        if isinstance(raw_value, list):
            normalized_tokens = [str(item).strip() for item in raw_value if str(item).strip()]
            raw_value = " ".join(normalized_tokens)
        else:
            raw_text = str(raw_value or "").strip()
            if "," in raw_text or "|" in raw_text or ";" in raw_text:
                raw_text = " ".join(
                    part.strip()
                    for part in re.split(r"[,|;]", raw_text)
                    if part.strip()
                )
            raw_value = raw_text
    return {
        "field": normalized_field,
        "value": raw_value,
    }


def _sanitize_rule_actions(actions: list[dict]) -> list[dict]:
    sanitized: list[dict] = []
    for item in actions or []:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_action(item)
        field = str(normalized.get("field", "")).strip().lower()
        value = normalized.get("value")
        if not field or field not in RULE_ACTION_ALLOWLIST:
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        sanitized.append({"field": field, "value": value})
    return sanitized


def _normalize_ticket_field_type(raw: object) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return "text"
    direct = TICKET_FIELD_TYPE_ALIASES.get(text)
    if direct:
        return direct
    compact = text.replace(" ", "_")
    return TICKET_FIELD_TYPE_ALIASES.get(compact, text)


def _slugify_option_value(value: str) -> str:
    import re

    normalized = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    return normalized[:255] if normalized else "option"


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return None


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
        async def _safe_get(path: str) -> tuple[dict, int | None]:
            started = time.perf_counter()
            try:
                response = await client.get(
                    f"{base_url}{path}",
                    auth=(auth_user, api_token),
                    headers={"Content-Type": "application/json"},
                )
            except httpx.HTTPError as exc:
                _emit_zendesk_http_event(
                    operation="reference_catalog_fetch",
                    method="GET",
                    url_or_path=path,
                    status_code=None,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    success=False,
                    error=str(exc),
                )
                warnings.append(f"{path}: request failed ({exc})")
                return {}, None
            _emit_zendesk_http_event(
                operation="reference_catalog_fetch",
                method="GET",
                url_or_path=path,
                status_code=response.status_code,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                success=response.is_success,
                error=None if response.is_success else f"HTTP {response.status_code}",
            )
            if not response.is_success:
                warnings.append(f"{path}: HTTP {response.status_code}")
                return {}, response.status_code
            try:
                return (response.json() if response.text else {}), response.status_code
            except ValueError:
                warnings.append(f"{path}: response was not valid JSON.")
                return {}, response.status_code

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
        payload_pairs = await asyncio.gather(
            *[_safe_get(path) for path in endpoint_map.values()]
        )
        payload_by_key: dict[str, dict] = {}
        for key, pair in zip(endpoint_map.keys(), payload_pairs):
            payload, _status_code = pair
            payload_by_key[key] = payload

        help_center_entries = []
        help_center_payload = payload_by_key.get("help_centers")
        if isinstance(help_center_payload, dict):
            raw_list = help_center_payload.get("help_centers", [])
            if isinstance(raw_list, list):
                help_center_entries = raw_list

        if not help_center_entries:
            fallback_paths = [
                "/api/v2/help_center/help_center.json",
                "/api/v2/help_center.json",
            ]
            settings = get_settings()
            cooldown_seconds = max(int(settings.zendesk_fallback_404_cooldown_seconds), 0)
            for fallback_path in fallback_paths:
                if not _fallback_probe_allowed(fallback_path, cooldown_seconds):
                    warnings.append(
                        f"{fallback_path}: skipped due to recent 404 (cooldown active)."
                    )
                    continue
                fallback_payload, fallback_status = await _safe_get(fallback_path)
                if fallback_status == 404:
                    _mark_fallback_404(fallback_path, cooldown_seconds)
                if not isinstance(fallback_payload, dict):
                    continue
                raw_list = fallback_payload.get("help_centers")
                if isinstance(raw_list, list) and raw_list:
                    payload_by_key["help_centers"] = {"help_centers": raw_list}
                    help_center_entries = raw_list
                    break
                raw_single = fallback_payload.get("help_center")
                if isinstance(raw_single, dict) and raw_single.get("id") and raw_single.get("name"):
                    payload_by_key["help_centers"] = {"help_centers": [raw_single]}
                    help_center_entries = [raw_single]
                    break

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

        if not catalogs["help_centers"]:
            warnings.append(
                "No help centers returned by Zendesk Guide endpoints. Article creation still works via section_id."
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
    all_conditions = [
        _normalize_condition(item)
        for item in conditions
        if item.get("field")
    ]
    if not all_conditions:
        all_conditions = [{"field": "status", "operator": "less_than", "value": "solved"}]
    normalized_actions = _sanitize_rule_actions(actions)
    return {
        root_key: {
            "title": str(record.get("title", "Untitled trigger")).strip() or "Untitled trigger",
            "active": True,
            "conditions": {
                "all": all_conditions,
                "any": [],
            },
            "actions": normalized_actions,
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


def _find_first_value_by_aliases(record: dict, aliases: set[str]):
    for bucket_name in ("actions", "conditions"):
        bucket = record.get(bucket_name, []) or []
        for entry in bucket:
            if not isinstance(entry, dict):
                continue
            field = str(entry.get("field", "")).strip().lower()
            if field in aliases:
                return entry.get("value")
    return None


def _find_all_values_by_aliases(record: dict, aliases: set[str]) -> list:
    values = []
    for bucket_name in ("actions", "conditions"):
        bucket = record.get(bucket_name, []) or []
        for entry in bucket:
            if not isinstance(entry, dict):
                continue
            field = str(entry.get("field", "")).strip().lower()
            if field in aliases:
                values.append(entry.get("value"))
    return values


def _parse_custom_field_options(raw: object) -> list[dict]:
    options: list[dict[str, str]] = []

    def _clean_option_name(name: str) -> str:
        cleaned = str(name or "").strip().strip("\"'")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        cleaned = re.sub(
            r"\s*-\s*(?:a\s+)?(?:drop[\s-]?down|single[\s-]?select|multi[\s-]?select|multiselect|text|textarea|number|integer|decimal|date|checkbox)\b.*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(
            r"\s*\b(?:with|having)\s+(?:options?|values?)\s*:.*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        return cleaned

    def _append(name: str, value: str | None = None) -> None:
        cleaned = _clean_option_name(name)
        if not cleaned:
            return
        raw_value = str(value or "").strip()
        if re.search(r"(dropdown|drop_down|drop-down|called|with_options|with-values)", raw_value, flags=re.IGNORECASE):
            raw_value = ""
        normalized_value = _slugify_option_value(raw_value or cleaned)
        if any(item["value"] == normalized_value for item in options):
            return
        options.append({"name": cleaned[:255], "value": normalized_value})

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                raw_name = str(item.get("name") or item.get("label") or item.get("value") or "").strip()
                raw_value = str(item.get("value") or "").strip() or None
                if raw_name:
                    _append(raw_name, raw_value)
            else:
                _append(str(item))
    elif isinstance(raw, str):
        for token in [part.strip() for part in re.split(r"[\n,|;]", raw) if part.strip()]:
            _append(token)
    elif raw is not None:
        _append(str(raw))
    return options


def _extract_ticket_form_field_references(record: dict) -> list[str]:
    raw_values = _find_all_values_by_aliases(record, TICKET_FORM_REFERENCE_FIELDS)
    refs: list[str] = []
    for raw in raw_values:
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    candidate = item.get("id") or item.get("name") or item.get("title") or item.get("value")
                    if candidate is not None:
                        refs.append(str(candidate).strip())
                else:
                    refs.append(str(item).strip())
        elif isinstance(raw, dict):
            candidate = raw.get("id") or raw.get("name") or raw.get("title") or raw.get("value")
            if candidate is not None:
                refs.append(str(candidate).strip())
        elif raw is not None:
            text = str(raw).strip()
            if not text:
                continue
            if "," in text or "|" in text or ";" in text:
                import re

                refs.extend([part.strip() for part in re.split(r"[,|;]", text) if part.strip()])
            else:
                refs.append(text)

    deduped: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        key = ref.lower()
        if not ref or key in seen:
            continue
        seen.add(key)
        deduped.append(ref)
    return deduped


def _build_macro_payload(record: dict) -> dict:
    actions = _sanitize_rule_actions(record.get("actions", []) or [])
    return {
        "macro": {
            "title": str(record.get("title", "Untitled macro")).strip() or "Untitled macro",
            "active": True,
            "actions": actions,
        }
    }


def _build_view_payload(record: dict) -> dict:
    conditions = record.get("conditions", []) or []
    all_conditions = [
        _normalize_condition(item)
        for item in conditions
        if item.get("field")
        and str(item.get("field", "")).strip().lower() in VIEW_CONDITION_ALLOWLIST
    ]
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


def _slugify_brand_subdomain(value: str) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", str(value or "").strip().lower())
    base = re.sub(r"-{2,}", "-", base).strip("-")
    return (base or "brand")[:40]


def _build_brand_payload(record: dict) -> dict:
    name = str(record.get("title", "")).strip() or "Untitled brand"
    subdomain_value = _find_first_value(record, "subdomain")
    subdomain = _slugify_brand_subdomain(str(subdomain_value or name))
    payload = {"brand": {"name": name, "subdomain": subdomain}}
    active_value = _find_first_value(record, "active")
    active_bool = _coerce_bool(active_value)
    if active_bool is not None:
        payload["brand"]["active"] = active_bool
    return payload


def _build_category_payload(record: dict) -> dict:
    name = str(record.get("title", "")).strip() or "Untitled category"
    locale = str(_find_first_value(record, "locale") or "en-us").strip().lower()
    payload = {"category": {"name": name, "locale": locale}}
    description_value = _find_first_value(record, "description")
    if description_value:
        payload["category"]["description"] = str(description_value).strip()
    return payload


def _build_section_payload(record: dict) -> tuple[dict, str | None]:
    name = str(record.get("title", "")).strip() or "Untitled section"
    locale = str(_find_first_value(record, "locale") or "en-us").strip().lower()
    category_value = _find_first_value(record, "category_id")
    payload = {"section": {"name": name, "locale": locale}}
    description_value = _find_first_value(record, "description")
    if description_value:
        payload["section"]["description"] = str(description_value).strip()
    if category_value is None:
        return payload, None
    category_id_text = str(category_value).strip()
    if not category_id_text.isdigit():
        return payload, "Section category_id must be numeric."
    return payload, category_id_text


def _build_ticket_form_payload(record: dict) -> tuple[dict, list[str]]:
    payload = {"ticket_form": {"name": str(record.get("title", "Untitled form")).strip() or "Untitled form"}}
    references = _extract_ticket_form_field_references(record)
    ids: list[int] = []
    names: list[str] = []
    for ref in references:
        if ref.isdigit():
            ids.append(int(ref))
        else:
            names.append(ref)
    if ids:
        payload["ticket_form"]["ticket_field_ids"] = ids
    return payload, names


def _build_ticket_field_payload(record: dict) -> dict:
    title = str(record.get("title", "Untitled field")).strip() or "Untitled field"
    field_type_raw = _find_first_value_by_aliases(record, TICKET_FIELD_TYPE_FIELDS)
    field_type = _normalize_ticket_field_type(field_type_raw)
    payload = {"ticket_field": {"title": title, "type": field_type}}
    tag_value = _find_first_value(record, "tag")
    if tag_value:
        payload["ticket_field"]["tag"] = str(tag_value).strip()
    title_portal = _find_first_value(record, "title_in_portal")
    if title_portal:
        payload["ticket_field"]["title_in_portal"] = str(title_portal).strip()
    options_raw = _find_first_value_by_aliases(record, TICKET_FIELD_OPTIONS_FIELDS)
    parsed_options = _parse_custom_field_options(options_raw)
    if parsed_options:
        payload["ticket_field"]["custom_field_options"] = parsed_options

    default_permissions = {
        "agent_can_edit": True,
        "visible_in_portal": True,
        "editable_in_portal": False,
        "required": False,
        "required_in_portal": False,
    }
    for permission_field, aliases in TICKET_FIELD_PERMISSION_FIELDS.items():
        raw_permission = _find_first_value_by_aliases(record, aliases)
        parsed_permission = _coerce_bool(raw_permission)
        if parsed_permission is None:
            parsed_permission = default_permissions[permission_field]
        payload["ticket_field"][permission_field] = parsed_permission

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
    started = time.perf_counter()

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                url,
                auth=(auth_user, api_token),
                headers={"Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        _emit_zendesk_http_event(
            operation="validate_credentials",
            method="GET",
            url_or_path=url,
            status_code=None,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            success=False,
            error=str(exc),
        )
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
    _emit_zendesk_http_event(
        operation="validate_credentials",
        method="GET",
        url_or_path=url,
        status_code=response.status_code,
        duration_ms=(time.perf_counter() - started) * 1000.0,
        success=response.is_success,
        error=None if response.is_success else f"HTTP {response.status_code}",
    )

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
    existing_cache: dict[str, dict[str, str]] = {}
    dependency_events: list[dict] = []
    sanitization_stats = {
        "records_checked": 0,
        "actions_dropped": 0,
        "empty_rule_actions_blocked": 0,
    }

    object_mappings = {
        "brand": "brands",
        "brands": "brands",
        "category": "categories",
        "categories": "categories",
        "section": "sections",
        "sections": "sections",
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
    deploy_priority_by_type = {
        "brands": 5,
        "categories": 8,
        "sections": 9,
        "groups": 10,
        "ticket_fields": 20,
        "ticket_forms": 30,
    }
    reference_field_to_object_type = {
        "group_id": "groups",
        "ticket_form_id": "ticket_forms",
        "form_id": "ticket_forms",
        "ticket_field_id": "ticket_fields",
        "brand_id": "brands",
        "section_id": "sections",
        "category_id": "categories",
        "help_center_id": "help_centers",
    }
    creatable_dependency_types = {
        "brands",
        "categories",
        "sections",
        "groups",
        "ticket_fields",
        "ticket_forms",
    }

    def _record_deploy_priority(record: dict) -> tuple[int, str]:
        raw = str(record.get("object_type", "")).strip().lower()
        normalized = object_mappings.get(raw, raw)
        return deploy_priority_by_type.get(normalized, 100), normalized

    def _apply_payload_title_suffix(
        *,
        payload: dict,
        object_type: str,
        new_title: str,
    ) -> dict:
        root_by_type = {
            "triggers": ("trigger", "title"),
            "automations": ("automation", "title"),
            "macros": ("macro", "title"),
            "views": ("view", "title"),
            "groups": ("group", "name"),
            "ticket_forms": ("ticket_form", "name"),
            "ticket_fields": ("ticket_field", "title"),
            "articles": ("article", "title"),
            "brands": ("brand", "name"),
            "categories": ("category", "name"),
            "sections": ("section", "name"),
        }
        root_key, field_key = root_by_type.get(object_type, ("", ""))
        if not root_key or not field_key:
            return payload
        root = payload.get(root_key, {}) if isinstance(payload.get(root_key), dict) else {}
        root[field_key] = new_title
        payload[root_key] = root
        return payload

    async with httpx.AsyncClient(timeout=30) as client:
        async def _safe_list(path: str) -> dict:
            started = time.perf_counter()
            try:
                response = await client.get(
                    f"{base_url}{path}",
                    auth=(auth_user, api_token),
                    headers={"Content-Type": "application/json"},
                )
                _emit_zendesk_http_event(
                    operation="deploy_list_existing",
                    method="GET",
                    url_or_path=path,
                    status_code=response.status_code,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    success=response.is_success,
                    error=None if response.is_success else f"HTTP {response.status_code}",
                )
                if not response.is_success:
                    return {}
                return response.json() if response.text else {}
            except Exception as exc:
                _emit_zendesk_http_event(
                    operation="deploy_list_existing",
                    method="GET",
                    url_or_path=path,
                    status_code=None,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    success=False,
                    error=str(exc),
                )
                return {}

        async def _load_existing_map(object_type: str) -> dict[str, str]:
            if object_type in existing_cache:
                return existing_cache[object_type]

            endpoint_by_type = {
                "brands": "/api/v2/brands.json",
                "categories": "/api/v2/help_center/categories.json?per_page=100",
                "sections": "/api/v2/help_center/sections.json?per_page=100",
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
                "brands": "brands",
                "categories": "categories",
                "sections": "sections",
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
                if object_type in {"groups", "ticket_forms", "brands", "categories", "sections"}:
                    name = str(item.get("name", "")).strip().lower()
                else:
                    name = str(item.get("title", item.get("name", ""))).strip().lower()
                if name:
                    match_map[name] = item_id
            existing_cache[object_type] = match_map
            return match_map

        async def _create_dependency(
            *,
            object_type: str,
            title: str,
            category_id: str | None = None,
        ) -> tuple[str | None, str | None]:
            if dry_run:
                dry_id = "99999999"
                dependency_events.append(
                    {
                        "object_type": object_type,
                        "title": title,
                        "created_id": dry_id,
                        "status": "simulated",
                        "reason": "dry_run mode",
                    }
                )
                return dry_id, None

            payload_by_type = {
                "brands": {
                    "brand": {
                        "name": title,
                        "subdomain": _slugify_brand_subdomain(title),
                    }
                },
                "categories": {
                    "category": {
                        "name": title,
                        "locale": "en-us",
                    }
                },
                "groups": {"group": {"name": title}},
                "ticket_fields": {"ticket_field": {"title": title, "type": "text"}},
                "ticket_forms": {"ticket_form": {"name": title}},
            }
            endpoint_by_type = {
                "brands": "/api/v2/brands.json",
                "categories": "/api/v2/help_center/categories.json",
                "groups": "/api/v2/groups.json",
                "ticket_fields": "/api/v2/ticket_fields.json",
                "ticket_forms": "/api/v2/ticket_forms.json",
            }
            root_by_type = {
                "brands": "brand",
                "categories": "category",
                "groups": "group",
                "ticket_fields": "ticket_field",
                "ticket_forms": "ticket_form",
            }

            if object_type == "sections":
                if not (category_id and str(category_id).strip().isdigit()):
                    return None, (
                        "Auto-create for sections requires a category_id context. "
                        "Provide category_id or include one existing category in context."
                    )
                payload_by_type["sections"] = {
                    "section": {
                        "name": title,
                        "locale": "en-us",
                    }
                }
                endpoint_by_type["sections"] = (
                    f"/api/v2/help_center/categories/{str(category_id).strip()}/sections.json"
                )
                root_by_type["sections"] = "section"

            payload = payload_by_type.get(object_type)
            endpoint = endpoint_by_type.get(object_type)
            response_root = root_by_type.get(object_type)
            if not payload or not endpoint or not response_root:
                return None, f"Auto-create not supported for dependency type '{object_type}'."

            try:
                started = time.perf_counter()
                response = await client.post(
                    f"{base_url}{endpoint}",
                    auth=(auth_user, api_token),
                    headers={"Content-Type": "application/json"},
                    json=payload,
                )
                _emit_zendesk_http_event(
                    operation="deploy_auto_create_dependency",
                    method="POST",
                    url_or_path=endpoint,
                    status_code=response.status_code,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    success=response.is_success,
                    error=None if response.is_success else f"HTTP {response.status_code}",
                )
            except httpx.HTTPError as exc:
                _emit_zendesk_http_event(
                    operation="deploy_auto_create_dependency",
                    method="POST",
                    url_or_path=endpoint,
                    status_code=None,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    success=False,
                    error=str(exc),
                )
                return None, f"Dependency auto-create request failed: {exc}"

            response_payload: dict = {}
            try:
                response_payload = response.json()
            except ValueError:
                response_payload = {}

            if not response.is_success:
                error = response_payload.get("error") if isinstance(response_payload, dict) else None
                description = response_payload.get("description") if isinstance(response_payload, dict) else None
                detail = f"Dependency auto-create failed ({response.status_code})"
                if error:
                    detail += f": {error}"
                if description:
                    detail += f" - {description}"
                return None, detail

            response_object = response_payload.get(response_root, {}) if isinstance(response_payload, dict) else {}
            created_id = str(response_object.get("id", "")).strip() if isinstance(response_object, dict) else ""
            if not created_id:
                return None, "Dependency auto-create succeeded but no ID was returned."
            existing_cache.setdefault(object_type, {})[title.strip().lower()] = created_id
            dependency_events.append(
                {
                    "object_type": object_type,
                    "title": title,
                    "created_id": created_id,
                    "status": "created",
                }
            )
            return created_id, None

        async def _ensure_dependency_id(
            *,
            object_type: str,
            raw_value: object,
        ) -> tuple[str | None, str | None]:
            if raw_value is None:
                return None, "Missing dependency value."
            raw = str(raw_value).strip()
            if not raw:
                return None, "Missing dependency value."
            if raw.isdigit():
                return raw, None

            if object_type not in creatable_dependency_types:
                return None, (
                    f"Dependency '{raw}' requires object type '{object_type}', which cannot be auto-created. "
                    f"Provide an existing {object_type.rstrip('s')} ID or exact existing name."
                )

            existing_map = await _load_existing_map(object_type)
            existing_id = existing_map.get(raw.lower())
            if existing_id:
                return existing_id, None

            dependency_title = raw
            category_id_hint: str | None = None
            if object_type == "sections":
                parsed = re.split(r"\s*(?:>|/|::)\s*", raw, maxsplit=1)
                if len(parsed) == 2:
                    maybe_category = str(parsed[0] or "").strip()
                    maybe_section = str(parsed[1] or "").strip()
                    if maybe_section:
                        dependency_title = maybe_section
                    if maybe_category:
                        if maybe_category.isdigit():
                            category_id_hint = maybe_category
                        else:
                            category_map = await _load_existing_map("categories")
                            category_id_hint = category_map.get(maybe_category.lower())
                if not category_id_hint:
                    category_map = await _load_existing_map("categories")
                    if len(category_map) == 1:
                        category_id_hint = next(iter(category_map.values()))

                existing_section_id = existing_map.get(str(dependency_title).strip().lower())
                if existing_section_id:
                    return existing_section_id, None

            created_id, create_error = await _create_dependency(
                object_type=object_type,
                title=dependency_title,
                category_id=category_id_hint,
            )
            if create_error:
                return None, (
                    f"Missing dependency '{raw}' ({object_type}) cannot be auto-created. {create_error}"
                )
            return created_id, None

        ordered_records = sorted(
            enumerate(records),
            key=lambda item: (_record_deploy_priority(item[1])[0], item[0]),
        )
        for _, record in ordered_records:
            sanitization_stats["records_checked"] += 1
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
                "brands",
                "categories",
                "sections",
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
            elif object_type == "brands":
                payload = _build_brand_payload(record)
                create_path = "/api/v2/brands.json"
                update_path_template = "/api/v2/brands/{id}.json"
                response_root = "brand"
            elif object_type == "categories":
                payload = _build_category_payload(record)
                create_path = "/api/v2/help_center/categories.json"
                update_path_template = "/api/v2/help_center/categories/{id}.json"
                response_root = "category"
            elif object_type == "sections":
                payload, category_id_or_error = _build_section_payload(record)
                if not payload:
                    failed += 1
                    results.append(
                        {
                            "record_id": record_id,
                            "object_type": object_type,
                            "title": title,
                            "deployment_status": "failed",
                            "zendesk_object_id": None,
                            "execution_message": str(category_id_or_error),
                            "executed_at": executed_at,
                        }
                    )
                    continue
                resolved_category_id: str | None = None
                if category_id_or_error and str(category_id_or_error).strip().isdigit():
                    resolved_category_id = str(category_id_or_error).strip()
                elif category_id_or_error and "numeric" in str(category_id_or_error).lower():
                    failed += 1
                    results.append(
                        {
                            "record_id": record_id,
                            "object_type": object_type,
                            "title": title,
                            "deployment_status": "failed",
                            "zendesk_object_id": None,
                            "execution_message": str(category_id_or_error),
                            "executed_at": executed_at,
                        }
                    )
                    continue
                elif not category_id_or_error:
                    fallback_categories = await _load_existing_map("categories")
                    if len(fallback_categories) == 1:
                        resolved_category_id = next(iter(fallback_categories.values()))
                if not resolved_category_id:
                    failed += 1
                    results.append(
                        {
                            "record_id": record_id,
                            "object_type": object_type,
                            "title": title,
                            "deployment_status": "failed",
                            "zendesk_object_id": None,
                            "execution_message": (
                                "Section requires category_id. Provide category_id or include one existing "
                                "category in context for unambiguous auto-linking."
                            ),
                            "executed_at": executed_at,
                        }
                    )
                    continue
                create_path = f"/api/v2/help_center/categories/{resolved_category_id}/sections.json"
                update_path_template = "/api/v2/help_center/sections/{id}.json"
                response_root = "section"
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
                payload, unresolved_field_names = _build_ticket_form_payload(record)
                if unresolved_field_names:
                    resolved_ids: list[int] = []
                    for field_name in unresolved_field_names:
                        resolved_id, resolve_error = await _ensure_dependency_id(
                            object_type="ticket_fields",
                            raw_value=field_name,
                        )
                        if resolve_error:
                            failed += 1
                            results.append(
                                {
                                    "record_id": record_id,
                                    "object_type": object_type,
                                    "title": title,
                                    "deployment_status": "failed",
                                    "zendesk_object_id": None,
                                    "execution_message": (
                                        "Ticket form references unresolved ticket fields and auto-create failed: "
                                        + str(resolve_error)
                                    ),
                                    "executed_at": executed_at,
                                }
                            )
                            resolved_ids = []
                            break
                        if resolved_id and str(resolved_id).isdigit():
                            resolved_ids.append(int(str(resolved_id)))
                    if not resolved_ids and unresolved_field_names:
                        continue

                    existing_ids = payload.get("ticket_form", {}).get("ticket_field_ids", [])
                    merged_ids: list[int] = []
                    seen_ids: set[int] = set()
                    for raw_id in [*existing_ids, *resolved_ids]:
                        raw_text = str(raw_id).strip()
                        if not raw_text.isdigit():
                            continue
                        numeric_id = int(raw_text)
                        if numeric_id in seen_ids:
                            continue
                        seen_ids.add(numeric_id)
                        merged_ids.append(numeric_id)
                    if merged_ids:
                        payload.setdefault("ticket_form", {})["ticket_field_ids"] = merged_ids
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

            if object_type in {"triggers", "automations", "macros", "views"}:
                raw_actions_count = len(list(record.get("actions", []) or []))
                if object_type == "triggers":
                    entry_actions = payload.get("trigger", {}).get("actions", [])
                    entry_conditions = payload.get("trigger", {}).get("conditions", {}).get("all", [])
                elif object_type == "automations":
                    entry_actions = payload.get("automation", {}).get("actions", [])
                    entry_conditions = payload.get("automation", {}).get("conditions", {}).get("all", [])
                elif object_type == "macros":
                    entry_actions = payload.get("macro", {}).get("actions", [])
                    entry_conditions = []
                else:  # views
                    entry_actions = []
                    entry_conditions = payload.get("view", {}).get("all", [])

                if object_type in {"triggers", "automations", "macros"}:
                    sanitization_stats["actions_dropped"] += max(raw_actions_count - len(entry_actions), 0)

                if object_type in {"triggers", "automations", "macros"} and not entry_actions:
                    sanitization_stats["empty_rule_actions_blocked"] += 1
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

                unresolved_reference_error = None
                for entry in [*entry_conditions, *entry_actions]:
                    if not isinstance(entry, dict):
                        continue
                    field = str(entry.get("field", "")).strip().lower()
                    if field not in reference_field_to_object_type:
                        continue
                    expected_object_type = reference_field_to_object_type[field]
                    resolved_id, resolve_error = await _ensure_dependency_id(
                        object_type=expected_object_type,
                        raw_value=entry.get("value"),
                    )
                    if resolve_error:
                        unresolved_reference_error = (
                            f"{field}: {resolve_error}"
                        )
                        break
                    if resolved_id:
                        entry["value"] = resolved_id

                if unresolved_reference_error:
                    failed += 1
                    results.append(
                        {
                            "record_id": record_id,
                            "object_type": object_type,
                            "title": title,
                            "deployment_status": "failed",
                            "zendesk_object_id": None,
                            "execution_message": (
                                "Could not resolve dependency reference. "
                                f"{unresolved_reference_error}"
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

            if on_existing == "create_new" and title:
                existing_map = await _load_existing_map(object_type)
                if title.strip().lower() in existing_map:
                    suffix = 2
                    candidate_title = title
                    while True:
                        candidate_title = f"{title} ({suffix})"
                        if candidate_title.strip().lower() not in existing_map:
                            break
                        suffix += 1
                    payload = _apply_payload_title_suffix(
                        payload=payload,
                        object_type=object_type,
                        new_title=candidate_title,
                    )
                    title = candidate_title

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
                started = time.perf_counter()
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
                _emit_zendesk_http_event(
                    operation="deploy_record",
                    method=method,
                    url_or_path=request_path,
                    status_code=response.status_code,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    success=response.is_success,
                    error=None if response.is_success else f"HTTP {response.status_code}",
                )
            except httpx.HTTPError as exc:
                _emit_zendesk_http_event(
                    operation="deploy_record",
                    method=method,
                    url_or_path=request_path,
                    status_code=None,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    success=False,
                    error=str(exc),
                )
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
                if object_id and title:
                    existing_cache.setdefault(object_type, {})[title.strip().lower()] = object_id
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
                details = response_payload.get("details")
                detail = f"Zendesk deployment failed ({response.status_code})"
                if error:
                    detail += f": {error}"
                if description:
                    detail += f" - {description}"
                if isinstance(details, dict) and details:
                    detail += f" | details={details}"
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
        "sanitization_stats": sanitization_stats,
        "dependency_auto_create": {
            "events": dependency_events,
            "created_count": len([item for item in dependency_events if item.get("status") in {"created", "simulated"}]),
        },
    }
