import httpx
import asyncio
import hashlib
import json
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

HELP_CENTER_ENABLEMENT_DOC_URL = (
    "https://support.zendesk.com/hc/en-us/articles/5702269234330"
)

_HELP_CENTER_FALLBACK_404_COOLDOWN: dict[str, float] = {}

RULE_ACTION_ALLOWLIST = {
    "group_id",
    "assignee_id",
    "current_tags",
    "remove_tags",
    "set_tags",
    "status",
    "priority",
    "comment_value",
    "comment_mode_is_public",
    "notification_user",
    "notification_group",
}
VIEW_CONDITION_ALLOWLIST = {
    "status",
    "group_id",
    "assignee_id",
    "priority",
    "ticket_form_id",
    "brand_id",
    "tags",
    "current_tags",
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
        "add_tags": "current_tags",
        "add_tag": "current_tags",
        "tag": "current_tags",
        "tags": "current_tags",
        "request_type": "current_tags",
        "add_note": "comment_value",
        "comment": "comment_value",
        "comment_body": "comment_value",
        "comment_text": "comment_value",
    }
    normalized_field = field_aliases.get(raw_field, raw_field)
    raw_value = action.get("value")
    if normalized_field in {"current_tags", "remove_tags", "set_tags"}:
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


def _action_signature(action: dict) -> str:
    return json.dumps(
        {
            "field": str(action.get("field", "")).strip().lower(),
            "value": action.get("value"),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _trusted_update_actions(record: dict) -> list[dict]:
    if str(record.get("operation_mode", "")).strip().lower() != "update":
        return []
    before = record.get("before_configuration", {})
    if not isinstance(before, dict):
        return []
    actions = before.get("actions", [])
    return [dict(item) for item in actions if isinstance(item, dict)] if isinstance(actions, list) else []


def _sanitize_rule_actions(
    actions: list[dict],
    *,
    trusted_actions: list[dict] | None = None,
) -> list[dict]:
    sanitized: list[dict] = []
    trusted_signatures = {
        _action_signature(item)
        for item in list(trusted_actions or [])
        if isinstance(item, dict)
    }
    for item in actions or []:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_action(item)
        field = str(normalized.get("field", "")).strip().lower()
        value = normalized.get("value")
        trusted_unchanged_action = _action_signature(item) in trusted_signatures
        if not field or (field not in RULE_ACTION_ALLOWLIST and not trusted_unchanged_action):
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
    page_counts: dict[str, int] = {}
    endpoint_complete: dict[str, bool] = {}
    endpoint_specs = {
        "brands": ("/api/v2/brands.json?per_page=100", "brands", "brand", ("name",), True),
        "groups": ("/api/v2/groups.json?per_page=100", "groups", "group", ("name",), True),
        "ticket_forms": ("/api/v2/ticket_forms.json?per_page=100", "ticket_forms", "ticket_form", ("name",), True),
        "triggers": ("/api/v2/triggers.json?per_page=100", "triggers", "trigger", ("title", "name"), True),
        "automations": ("/api/v2/automations.json?per_page=100", "automations", "automation", ("title", "name"), True),
        "macros": ("/api/v2/macros.json?per_page=100", "macros", "macro", ("title", "name"), True),
        "views": ("/api/v2/views.json?per_page=100", "views", "view", ("title", "name"), True),
        "ticket_fields": ("/api/v2/ticket_fields.json?per_page=100", "ticket_fields", "ticket_field", ("title", "name"), True),
        "articles": ("/api/v2/help_center/articles.json?per_page=100", "articles", "article", ("title", "name"), True),
        "categories": ("/api/v2/help_center/categories.json?per_page=100", "categories", "category", ("name", "title"), True),
        "sections": ("/api/v2/help_center/sections.json?per_page=100", "sections", "section", ("name", "title"), True),
        "sla_policies": ("/api/v2/slas/policies.json?per_page=100", "sla_policies", "sla_policy", ("title", "name"), False),
        "schedules": ("/api/v2/business_hours/schedules.json?per_page=100", "schedules", "schedule", ("name", "title"), False),
        "user_fields": ("/api/v2/user_fields.json?per_page=100", "user_fields", "user_field", ("title", "name"), False),
        "organization_fields": ("/api/v2/organization_fields.json?per_page=100", "organization_fields", "organization_field", ("title", "name"), False),
        "custom_objects": ("/api/v2/custom_objects", "custom_objects", "custom_object", ("title", "name", "key"), False),
    }
    catalogs: dict[str, list[dict]] = {key: [] for key in endpoint_specs}
    catalogs["help_centers"] = []
    raw_entries_by_key: dict[str, list[dict]] = {key: [] for key in endpoint_specs}

    async with httpx.AsyncClient(timeout=25) as client:
        async def _safe_get(path_or_url: str) -> tuple[dict, int | None]:
            path_or_url = str(path_or_url or "").strip()
            request_url = path_or_url if path_or_url.startswith(("http://", "https://")) else f"{base_url}{path_or_url}"
            parsed_url = urlparse(request_url)
            if parsed_url.netloc and parsed_url.netloc != urlparse(base_url).netloc:
                warnings.append(f"{_sanitize_path(path_or_url)}: rejected cross-host pagination link.")
                return {}, None
            started = time.perf_counter()
            try:
                response = await client.get(
                    request_url,
                    auth=(auth_user, api_token),
                    headers={"Content-Type": "application/json"},
                )
            except httpx.HTTPError as exc:
                _emit_zendesk_http_event(
                    operation="reference_catalog_fetch",
                    method="GET",
                    url_or_path=path_or_url,
                    status_code=None,
                    duration_ms=(time.perf_counter() - started) * 1000.0,
                    success=False,
                    error=str(exc),
                )
                warnings.append(f"{_sanitize_path(path_or_url)}: request failed ({exc})")
                return {}, None
            _emit_zendesk_http_event(
                operation="reference_catalog_fetch",
                method="GET",
                url_or_path=path_or_url,
                status_code=response.status_code,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                success=response.is_success,
                error=None if response.is_success else f"HTTP {response.status_code}",
            )
            if not response.is_success:
                warnings.append(f"{_sanitize_path(path_or_url)}: HTTP {response.status_code}")
                return {}, response.status_code
            try:
                return (response.json() if response.text else {}), response.status_code
            except ValueError:
                warnings.append(f"{_sanitize_path(path_or_url)}: response was not valid JSON.")
                return {}, response.status_code

        async def _fetch_all(key: str) -> tuple[str, list[dict], int, bool]:
            path, root_key, _object_type, _name_fields, _editable = endpoint_specs[key]
            entries: list[dict] = []
            next_ref: str | None = path
            seen: set[str] = set()
            pages = 0
            complete = True
            while next_ref and pages < 50:
                if next_ref in seen:
                    warnings.append(f"{key}: repeated pagination link; stopped to avoid a loop.")
                    complete = False
                    next_ref = None
                    break
                seen.add(next_ref)
                payload, status_code = await _safe_get(next_ref)
                pages += 1
                if status_code is None or not (200 <= status_code < 300):
                    complete = False
                    next_ref = None
                    break
                raw_items = payload.get(root_key, []) if isinstance(payload, dict) else []
                if isinstance(raw_items, list):
                    entries.extend(item for item in raw_items if isinstance(item, dict))
                next_value = payload.get("next_page") if isinstance(payload, dict) else None
                links = payload.get("links", {}) if isinstance(payload, dict) else {}
                if not next_value and isinstance(links, dict):
                    next_value = links.get("next")
                next_ref = str(next_value).strip() if next_value else None
            if next_ref:
                warnings.append(f"{key}: pagination stopped at the 50-page safety cap.")
                complete = False
            return key, entries, pages, complete

        fetched = await asyncio.gather(*[_fetch_all(key) for key in endpoint_specs])
        for key, entries, pages, complete in fetched:
            raw_entries_by_key[key] = entries
            page_counts[key] = pages
            endpoint_complete[key] = complete

        def _description(item: dict, key: str) -> str:
            actions = item.get("actions", [])
            raw_conditions = item.get("conditions", {})
            if isinstance(raw_conditions, dict):
                all_count = len(raw_conditions.get("all", []) or [])
                any_count = len(raw_conditions.get("any", []) or [])
            else:
                all_count = len(item.get("all", []) or [])
                any_count = len(item.get("any", []) or [])
            parts: list[str] = []
            if key in {"triggers", "automations", "views"}:
                parts.append(f"conditions={all_count + any_count}")
            if isinstance(actions, list) and actions:
                parts.append(f"actions={len(actions)}")
            if item.get("type"):
                parts.append(f"type={item.get('type')}")
            if item.get("section_id"):
                parts.append(f"section_id={item.get('section_id')}")
            if key == "sla_policies":
                parts.append(f"metrics={len(item.get('policy_metrics', []) or [])}")
            if key == "schedules":
                parts.append(f"time_zone={item.get('time_zone', '')}")
            if item.get("active") is not None:
                parts.append(f"active={bool(item.get('active'))}")
            return "; ".join(part for part in parts if not part.endswith("="))[:1200]

        for key, spec in endpoint_specs.items():
            _path, _root, object_type, name_fields, editable = spec
            for item in raw_entries_by_key.get(key, []):
                item_id = str(item.get("id") or item.get("key") or "").strip()
                name = next(
                    (str(item.get(field, "")).strip() for field in name_fields if str(item.get(field, "")).strip()),
                    "",
                )
                if not item_id or not name:
                    continue
                snapshot = json.loads(json.dumps(item, default=str))
                serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
                catalogs[key].append(
                    {
                        "object_type": object_type,
                        "id": item_id,
                        "name": name,
                        "description": _description(item, key) or None,
                        "catalog_key": key,
                        "updated_at": str(item.get("updated_at") or "").strip() or None,
                        "snapshot_hash": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                        "snapshot": snapshot,
                        "editable": bool(editable),
                    }
                )

        guide_content_exists = any(
            raw_entries_by_key.get(key)
            for key in ("categories", "sections", "articles")
        )
        raw_brands = raw_entries_by_key.get("brands", [])
        help_center_brands = [
            item
            for item in raw_brands
            if item.get("has_help_center") is True
            or str(item.get("help_center_state") or "").strip().lower()
            in {"active", "enabled", "live"}
        ]
        if not help_center_brands and guide_content_exists:
            default_brands = [item for item in raw_brands if item.get("default") is True]
            if len(default_brands) == 1:
                help_center_brands = default_brands
            elif len(raw_brands) == 1:
                help_center_brands = list(raw_brands)

        for brand in help_center_brands:
            brand_id = str(brand.get("id") or "").strip()
            brand_name = str(brand.get("name") or "").strip()
            if not brand_id or not brand_name:
                continue
            host = (
                str(brand.get("host_mapping") or "").strip()
                or str(urlparse(str(brand.get("brand_url") or "")).hostname or "").strip()
                or (
                    f"{str(brand.get('subdomain') or '').strip()}.zendesk.com"
                    if str(brand.get("subdomain") or "").strip()
                    else urlparse(base_url).hostname or ""
                )
            )
            snapshot = {
                "brand_id": brand_id,
                "brand_name": brand_name,
                "has_help_center": brand.get("has_help_center"),
                "help_center_state": brand.get("help_center_state"),
                "help_center_url": f"https://{host}/hc/en-us" if host else "",
                "locale": "en-us",
            }
            serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
            catalogs["help_centers"].append(
                {
                    "object_type": "help_center",
                    "id": brand_id,
                    "name": f"{brand_name} Help Center",
                    "description": f"Help Center for Zendesk brand {brand_name}.",
                    "catalog_key": "help_centers",
                    "updated_at": str(brand.get("updated_at") or "").strip() or None,
                    "snapshot_hash": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                    "snapshot": snapshot,
                    "editable": False,
                }
            )
        page_counts["help_centers"] = 0
        endpoint_complete["help_centers"] = bool(catalogs["help_centers"] or guide_content_exists)

        if not catalogs["help_centers"]:
            if guide_content_exists:
                warnings.append(
                    "Zendesk Guide content is readable, but it could not be mapped to one brand. "
                    "Select the target brand and verify its Help Center URL before deployment."
                )
            else:
                warnings.append(
                    "No Help Center was confirmed from Zendesk brand or Guide data. Verify the target "
                    "brand before attempting category, section, or article deployment."
                )

    fetched_total = sum(len(v) for v in catalogs.values())
    catalog_counts = {key: len(values) for key, values in catalogs.items()}
    sync_material = json.dumps(
        {
            key: [(item.get("id"), item.get("snapshot_hash")) for item in values]
            for key, values in catalogs.items()
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    sync_id = f"SYNC-{hashlib.sha256(sync_material.encode('utf-8')).hexdigest()[:20].upper()}"
    required_catalogs = {
        "brands",
        "groups",
        "ticket_forms",
        "triggers",
        "automations",
        "macros",
        "views",
        "ticket_fields",
    }
    complete = all(endpoint_complete.get(key, False) for key in required_catalogs)
    if fetched_total == 0:
        return {
            "ok": False,
            "detail": "No Zendesk catalog entries were returned. Check Guide/Support permissions.",
            "base_url": base_url,
            "catalogs": catalogs,
            "fetched_at": _now_iso(),
            "warnings": warnings,
            "sync_id": sync_id,
            "complete": complete,
            "catalog_counts": catalog_counts,
            "page_counts": page_counts,
        }

    return {
        "ok": True,
        "detail": "Zendesk catalogs fetched.",
        "base_url": base_url,
        "catalogs": catalogs,
        "fetched_at": _now_iso(),
        "warnings": warnings,
        "sync_id": sync_id,
        "complete": complete,
        "catalog_counts": catalog_counts,
        "page_counts": page_counts,
    }


def _build_rule_payload(record: dict, root_key: str) -> dict:
    conditions = record.get("conditions", []) or []
    actions = record.get("actions", []) or []
    all_conditions: list[dict] = []
    any_conditions: list[dict] = []
    for item in conditions:
        if not isinstance(item, dict) or not item.get("field"):
            continue
        scope = str(item.get("scope") or item.get("condition_scope") or "all").strip().lower()
        target = any_conditions if scope == "any" else all_conditions
        target.append(_normalize_condition(item))
    if not all_conditions and not any_conditions:
        all_conditions = [{"field": "status", "operator": "less_than", "value": "solved"}]
    normalized_actions = _sanitize_rule_actions(
        actions,
        trusted_actions=_trusted_update_actions(record),
    )
    active = _coerce_bool(record.get("active"))
    return {
        root_key: {
            "title": str(record.get("title", "Untitled trigger")).strip() or "Untitled trigger",
            "active": True if active is None else active,
            "conditions": {
                "all": all_conditions,
                "any": any_conditions,
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
    actions = _sanitize_rule_actions(
        record.get("actions", []) or [],
        trusted_actions=_trusted_update_actions(record),
    )
    active = _coerce_bool(record.get("active"))
    return {
        "macro": {
            "title": str(record.get("title", "Untitled macro")).strip() or "Untitled macro",
            "active": True if active is None else active,
            "actions": actions,
        }
    }


def _build_view_payload(record: dict) -> dict:
    conditions = record.get("conditions", []) or []
    all_conditions: list[dict] = []
    any_conditions: list[dict] = []
    for item in conditions:
        if (
            not isinstance(item, dict)
            or not item.get("field")
            or str(item.get("field", "")).strip().lower() not in VIEW_CONDITION_ALLOWLIST
        ):
            continue
        scope = str(item.get("scope") or item.get("condition_scope") or "all").strip().lower()
        target = any_conditions if scope == "any" else all_conditions
        target.append(_normalize_condition(item))
    if not all_conditions and not any_conditions:
        all_conditions = [{"field": "status", "operator": "less_than", "value": "solved"}]

    raw_columns = _find_first_value(record, "output_columns")
    if isinstance(raw_columns, list):
        output_columns = [str(item).strip() for item in raw_columns if str(item).strip()]
    elif isinstance(raw_columns, str):
        output_columns = [part.strip() for part in raw_columns.split(",") if part.strip()]
    else:
        output_columns = ["status", "updated", "subject"]

    active = _coerce_bool(record.get("active"))
    return {
        "view": {
            "title": str(record.get("title", "Untitled view")).strip() or "Untitled view",
            "active": True if active is None else active,
            "all": all_conditions,
            "any": any_conditions,
            "output": {
                "columns": output_columns,
                **(
                    {"sort_by": str(_find_first_value(record, "sort_by")).strip()}
                    if _find_first_value(record, "sort_by")
                    else {}
                ),
                **(
                    {"sort_order": str(_find_first_value(record, "sort_order")).strip().lower()}
                    if str(_find_first_value(record, "sort_order") or "").strip().lower()
                    in {"asc", "desc"}
                    else {}
                ),
            },
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
    category_value = _find_first_value_by_aliases(record, {"category_id", "category_name", "category"})
    payload = {"section": {"name": name, "locale": locale}}
    description_value = _find_first_value(record, "description")
    if description_value:
        payload["section"]["description"] = str(description_value).strip()
    if category_value is None:
        return payload, None
    return payload, str(category_value).strip()


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


def _build_article_payload(record: dict, *, article_mode: str = "draft") -> tuple[dict, str | None]:
    title = str(record.get("title", "Untitled article")).strip() or "Untitled article"
    body = str(_find_first_value(record, "body") or "").strip()
    if not body:
        body = f"<p>{title}</p>"
    locale = str(_find_first_value(record, "locale") or "en-us").strip().lower()
    section_reference = _find_first_value_by_aliases(
        record,
        {"section_id", "section_name", "section"},
    )
    if not section_reference:
        return {}, "Article requires a section_id or section_name in conditions/actions."
    section_reference_text = str(section_reference).strip()
    payload = {
        "article": {
            "title": title,
            "body": body,
            "locale": locale,
            "draft": str(article_mode).strip().lower() != "publish",
        },
        "notify_subscribers": False,
    }
    return payload, section_reference_text


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


def _normalize_help_center_target(
    *,
    subdomain: str,
    help_center_url: str | None,
    locale: str | None,
) -> tuple[str, str, str | None]:
    requested_locale = str(locale or "").strip().lower()
    raw_url = str(help_center_url or "").strip()
    if not raw_url:
        effective_locale = requested_locale or "en-us"
        return f"https://{subdomain}.zendesk.com/hc/{effective_locale}", effective_locale, None

    if "://" not in raw_url:
        raw_url = f"https://{raw_url}"
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return raw_url, requested_locale or "en-us", "Provide a valid Help Center URL."

    locale_match = re.search(r"/hc/([a-z]{2}(?:-[a-z0-9]{2,8})?)", parsed.path, flags=re.IGNORECASE)
    if not locale_match and not requested_locale:
        return (
            raw_url.rstrip("/"),
            "en-us",
            "Help Center URL must include a locale path such as /hc/en-us.",
        )
    effective_locale = requested_locale or str(locale_match.group(1)).lower()
    normalized_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    return normalized_url, effective_locale, None


def _help_center_manual_instructions(*, brand_name: str, locale: str) -> list[str]:
    return [
        "Sign in to Zendesk as the account owner or an admin with Guide permissions.",
        f"Open Knowledge/Guide, select the '{brand_name}' brand, and choose Get started or Enable Help Center.",
        f"Activate the Help Center and make sure the '{locale}' locale is enabled.",
        "Return here, paste the brand Help Center URL, and run Verify again.",
    ]


async def check_zendesk_help_center_readiness(
    *,
    subdomain: str,
    email: str,
    api_token: str,
    help_center_url: str | None = None,
    brand_id: str | None = None,
    locale: str | None = None,
) -> dict:
    normalized_subdomain = _normalize_subdomain(subdomain)
    base_url = _build_base_url(normalized_subdomain)
    help_center_api_base_url = base_url
    target_url, effective_locale, url_error = _normalize_help_center_target(
        subdomain=normalized_subdomain,
        help_center_url=help_center_url,
        locale=locale,
    )
    checks: list[dict] = []
    available_brands: list[dict] = []
    default_brand_name = normalized_subdomain

    def _response(
        *,
        ready: bool,
        state: str,
        detail: str,
        brand: dict | None = None,
        instructions: list[str] | None = None,
    ) -> dict:
        return {
            "ready": ready,
            "state": state,
            "detail": detail,
            "base_url": base_url,
            "help_center_api_base_url": help_center_api_base_url,
            "help_center_url": target_url,
            "locale": effective_locale,
            "brand": brand,
            "available_brands": available_brands,
            "checks": checks,
            "instructions": instructions or [],
            "documentation_url": HELP_CENTER_ENABLEMENT_DOC_URL,
            "can_create_structure": ready,
            "can_create_articles": ready,
        }

    if url_error:
        checks.append(
            {
                "name": "help_center_url",
                "status": "failed",
                "detail": url_error,
                "expected": "https://brand.example.com/hc/en-us",
                "actual": str(help_center_url or ""),
            }
        )
        return _response(
            ready=False,
            state="invalid_url",
            detail=url_error,
        )

    auth_user = f"{email}/token"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            brands_started = time.perf_counter()
            brands_response = await client.get(
                f"{base_url}/api/v2/brands.json",
                auth=(auth_user, api_token),
                headers={"Content-Type": "application/json"},
            )
            _emit_zendesk_http_event(
                operation="help_center_readiness_brands",
                method="GET",
                url_or_path="/api/v2/brands.json",
                status_code=brands_response.status_code,
                duration_ms=(time.perf_counter() - brands_started) * 1000.0,
                success=brands_response.is_success,
                error=None if brands_response.is_success else f"HTTP {brands_response.status_code}",
            )
            checks.append(
                {
                    "name": "brands_api",
                    "status": "passed" if brands_response.is_success else "failed",
                    "detail": (
                        "Authenticated brand catalog is available."
                        if brands_response.is_success
                        else f"Zendesk returned HTTP {brands_response.status_code}."
                    ),
                    "http_status": brands_response.status_code,
                }
            )
            if brands_response.status_code == 401:
                return _response(
                    ready=False,
                    state="invalid_credentials",
                    detail="Zendesk rejected the session credentials while checking brands.",
                )
            if brands_response.status_code == 403:
                return _response(
                    ready=False,
                    state="permission_denied",
                    detail="The authenticated user cannot read Zendesk brands.",
                    instructions=["Use an admin account with access to Brands and Guide, then verify again."],
                )
            if not brands_response.is_success:
                return _response(
                    ready=False,
                    state="unavailable",
                    detail=f"Zendesk brand readiness check failed (HTTP {brands_response.status_code}).",
                    instructions=["Wait briefly and run Verify again. No Help Center records were changed."],
                )

            try:
                brands_payload = brands_response.json() if brands_response.text else {}
            except ValueError:
                brands_payload = {}
            raw_brands = brands_payload.get("brands", []) if isinstance(brands_payload, dict) else []
            target_host = str(urlparse(target_url).hostname or "").strip().lower()
            selected_brand: dict | None = None

            for raw_brand in raw_brands if isinstance(raw_brands, list) else []:
                if not isinstance(raw_brand, dict):
                    continue
                raw_brand_id = str(raw_brand.get("id", "")).strip()
                name = str(raw_brand.get("name", "")).strip() or f"Brand {raw_brand_id}"
                brand_url = str(raw_brand.get("brand_url", "")).strip()
                host_mapping = str(raw_brand.get("host_mapping", "")).strip().lower()
                brand_subdomain = str(raw_brand.get("subdomain", "")).strip().lower()
                brand_hosts = {
                    host
                    for host in [
                        host_mapping,
                        str(urlparse(brand_url).hostname or "").strip().lower(),
                        f"{brand_subdomain}.zendesk.com" if brand_subdomain else "",
                    ]
                    if host
                }
                normalized_brand = {
                    "id": raw_brand_id,
                    "name": name,
                    "has_help_center": raw_brand.get("has_help_center"),
                    "help_center_state": str(raw_brand.get("help_center_state", "")).strip().lower() or None,
                    "brand_url": brand_url or None,
                    "host_mapping": host_mapping or None,
                    "subdomain": brand_subdomain or None,
                    "default": bool(raw_brand.get("default", False)),
                }
                available_brands.append(normalized_brand)
                if brand_id and raw_brand_id == str(brand_id).strip():
                    selected_brand = normalized_brand
                elif not brand_id and target_host and target_host in brand_hosts:
                    selected_brand = normalized_brand

            if not selected_brand and not brand_id and len(available_brands) == 1:
                selected_brand = available_brands[0]
            if not selected_brand and not brand_id:
                default_matches = [item for item in available_brands if item.get("default")]
                if len(default_matches) == 1 and target_host == f"{normalized_subdomain}.zendesk.com":
                    selected_brand = default_matches[0]

            if not selected_brand:
                checks.append(
                    {
                        "name": "brand_selection",
                        "status": "failed",
                        "detail": "The URL could not be matched to one Zendesk brand.",
                        "actual": target_host,
                    }
                )
                return _response(
                    ready=False,
                    state="brand_selection_required",
                    detail="Select the target brand or provide that brand's exact Help Center URL.",
                    instructions=[
                        "Open the target brand in Zendesk Admin Center and copy its Help Center URL.",
                        "Paste the URL here and verify again.",
                    ],
                )

            default_brand_name = str(selected_brand.get("name") or default_brand_name)
            selected_brand_subdomain = str(selected_brand.get("subdomain") or "").strip().lower()
            if selected_brand_subdomain:
                help_center_api_base_url = _build_base_url(selected_brand_subdomain)
            else:
                help_center_api_base_url = base_url
            checks.append(
                {
                    "name": "brand_selection",
                    "status": "passed",
                    "detail": f"Matched Help Center target to brand '{default_brand_name}'.",
                    "brand_id": selected_brand.get("id"),
                }
            )

            has_help_center = selected_brand.get("has_help_center")
            help_center_state = str(selected_brand.get("help_center_state") or "").lower()
            explicitly_disabled = has_help_center is False or help_center_state in {
                "disabled",
                "not_enabled",
                "none",
            }
            if explicitly_disabled:
                checks.append(
                    {
                        "name": "brand_help_center",
                        "status": "failed",
                        "detail": "Zendesk reports that Help Center is not enabled for this brand.",
                        "expected": "has_help_center=true",
                        "actual": f"has_help_center={has_help_center}; state={help_center_state or 'unknown'}",
                    }
                )
                return _response(
                    ready=False,
                    state="manual_enablement_required",
                    detail=f"Enable and activate Help Center for '{default_brand_name}' before deploying content.",
                    brand=selected_brand,
                    instructions=_help_center_manual_instructions(
                        brand_name=default_brand_name,
                        locale=effective_locale,
                    ),
                )

            guide_path = f"/api/v2/help_center/{effective_locale}/categories.json?per_page=1"
            guide_started = time.perf_counter()
            guide_response = await client.get(
                f"{help_center_api_base_url}{guide_path}",
                auth=(auth_user, api_token),
                headers={"Content-Type": "application/json"},
            )
            _emit_zendesk_http_event(
                operation="help_center_readiness_guide",
                method="GET",
                url_or_path=guide_path,
                status_code=guide_response.status_code,
                duration_ms=(time.perf_counter() - guide_started) * 1000.0,
                success=guide_response.is_success,
                error=None if guide_response.is_success else f"HTTP {guide_response.status_code}",
            )
            checks.append(
                {
                    "name": "guide_api",
                    "status": "passed" if guide_response.is_success else "failed",
                    "detail": (
                        "Authenticated Help Center category API is available."
                        if guide_response.is_success
                        else f"Zendesk Guide returned HTTP {guide_response.status_code}."
                    ),
                    "http_status": guide_response.status_code,
                }
            )
            if guide_response.is_success:
                return _response(
                    ready=True,
                    state="ready",
                    detail=(
                        f"Help Center is ready for '{default_brand_name}'. Categories and sections can be "
                        "created before article drafts."
                    ),
                    brand=selected_brand,
                )
            if guide_response.status_code in {401, 403}:
                return _response(
                    ready=False,
                    state="permission_denied" if guide_response.status_code == 403 else "invalid_credentials",
                    detail=(
                        "The current Zendesk user does not have Guide access for this brand."
                        if guide_response.status_code == 403
                        else "Zendesk rejected the session credentials while checking Guide."
                    ),
                    brand=selected_brand,
                    instructions=[
                        "Use an account owner, Support admin, or Guide admin with publishing permissions, then verify again."
                    ],
                )
            if guide_response.status_code == 404:
                return _response(
                    ready=False,
                    state="manual_enablement_required",
                    detail=f"Zendesk Guide is not active for '{default_brand_name}'.",
                    brand=selected_brand,
                    instructions=_help_center_manual_instructions(
                        brand_name=default_brand_name,
                        locale=effective_locale,
                    ),
                )
            return _response(
                ready=False,
                state="unavailable",
                detail=f"Help Center verification failed (HTTP {guide_response.status_code}).",
                brand=selected_brand,
                instructions=["Retry verification. No Help Center records were changed."],
            )
    except httpx.HTTPError as exc:
        checks.append(
            {
                "name": "zendesk_connection",
                "status": "failed",
                "detail": str(exc),
            }
        )
        return _response(
            ready=False,
            state="unavailable",
            detail=f"Zendesk readiness request failed: {exc}",
            instructions=["Check connectivity and run Verify again. No Help Center records were changed."],
        )


async def deploy_records_to_zendesk(
    *,
    subdomain: str,
    email: str,
    api_token: str,
    records: list[dict],
    dry_run: bool = False,
    on_existing: str = "create_new",
    article_mode: str = "draft",
    help_center_base_url: str | None = None,
) -> dict:
    base_url = _build_base_url(_normalize_subdomain(subdomain))
    parsed_help_center_base = urlparse(str(help_center_base_url or "").strip())
    guide_base_url = (
        f"{parsed_help_center_base.scheme}://{parsed_help_center_base.netloc}"
        if parsed_help_center_base.scheme in {"http", "https"}
        and parsed_help_center_base.netloc
        else base_url
    )
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
        "articles": 10,
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

    def _api_base_for_type(object_type: str) -> str:
        return guide_base_url if object_type in {"categories", "sections", "articles"} else base_url

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
        async def _safe_list(path: str, *, object_type: str) -> dict:
            started = time.perf_counter()
            try:
                response = await client.get(
                    f"{_api_base_for_type(object_type)}{path}",
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

            payload = await _safe_list(path, object_type=object_type)
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
                    f"{_api_base_for_type(object_type)}{endpoint}",
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
            allow_create: bool = True,
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

            if not allow_create:
                return None, (
                    f"Dependency '{raw}' was not found in the synchronized Zendesk instance. "
                    "Exact update mode never auto-creates dependencies."
                )

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
            exact_update = str(record.get("operation_mode", "create")).strip().lower() == "update"
            target_object_id = str(record.get("target_object_id") or "").strip()
            raw_target_type = str(record.get("target_object_type") or "").strip().lower()
            target_object_type = object_mappings.get(raw_target_type, raw_target_type)

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
                payload, category_reference = _build_section_payload(record)
                resolved_category_id: str | None = None
                category_resolution_error: str | None = None
                if category_reference:
                    resolved_category_id, category_resolution_error = await _ensure_dependency_id(
                        object_type="categories",
                        raw_value=category_reference,
                        allow_create=not exact_update,
                    )
                else:
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
                                "Section requires a resolvable category_id or exact category_name. "
                                + (category_resolution_error or "Include its category in this Help Center phase.")
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
                            allow_create=not exact_update,
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
                payload, section_reference = _build_article_payload(
                    record,
                    article_mode=article_mode,
                )
                if not payload:
                    failed += 1
                    results.append(
                        {
                            "record_id": record_id,
                            "object_type": object_type,
                            "title": title,
                            "deployment_status": "failed",
                            "zendesk_object_id": None,
                            "execution_message": str(section_reference),
                            "executed_at": executed_at,
                        }
                    )
                    continue
                resolved_section_id, section_resolution_error = await _ensure_dependency_id(
                    object_type="sections",
                    raw_value=section_reference,
                    allow_create=not exact_update,
                )
                if not resolved_section_id:
                    failed += 1
                    results.append(
                        {
                            "record_id": record_id,
                            "object_type": object_type,
                            "title": title,
                            "deployment_status": "failed",
                            "zendesk_object_id": None,
                            "execution_message": (
                                "Article requires a resolvable section_id or exact section_name. "
                                + str(section_resolution_error or "Include its section in this Help Center phase.")
                            ),
                            "executed_at": executed_at,
                        }
                    )
                    continue
                create_path = f"/api/v2/help_center/sections/{resolved_section_id}/articles.json"
                update_path_template = "/api/v2/help_center/articles/{id}.json"
                response_root = "article"

            if object_type in {"triggers", "automations", "macros", "views"}:
                raw_actions_count = len(list(record.get("actions", []) or []))
                if object_type == "triggers":
                    entry_actions = payload.get("trigger", {}).get("actions", [])
                    trigger_conditions = payload.get("trigger", {}).get("conditions", {})
                    entry_conditions = [
                        *list(trigger_conditions.get("all", []) or []),
                        *list(trigger_conditions.get("any", []) or []),
                    ]
                elif object_type == "automations":
                    entry_actions = payload.get("automation", {}).get("actions", [])
                    automation_conditions = payload.get("automation", {}).get("conditions", {})
                    entry_conditions = [
                        *list(automation_conditions.get("all", []) or []),
                        *list(automation_conditions.get("any", []) or []),
                    ]
                elif object_type == "macros":
                    entry_actions = payload.get("macro", {}).get("actions", [])
                    entry_conditions = []
                else:  # views
                    entry_actions = []
                    view_payload = payload.get("view", {})
                    entry_conditions = [
                        *list(view_payload.get("all", []) or []),
                        *list(view_payload.get("any", []) or []),
                    ]

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
                        allow_create=not exact_update,
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

            existing_id = target_object_id if exact_update else None
            if not exact_update and on_existing in {"overwrite_existing", "skip_existing"} and title:
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

            exact_update_error = ""
            if exact_update and not target_object_id:
                exact_update_error = "Exact update is missing its synchronized target ID."
            elif exact_update and not target_object_id.isdigit():
                exact_update_error = "Exact update target ID must be a numeric Zendesk ID."
            elif exact_update and target_object_type and target_object_type != object_type:
                exact_update_error = "Exact update target type does not match the proposed object type."
            elif exact_update and on_existing != "overwrite_existing":
                exact_update_error = "Exact update requires on_existing=overwrite_existing."
            if exact_update_error:
                failed += 1
                results.append(
                    {
                        "record_id": record_id,
                        "object_type": object_type,
                        "title": title,
                        "deployment_status": "failed",
                        "zendesk_object_id": target_object_id or None,
                        "execution_message": exact_update_error,
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

            if exact_update and not dry_run:
                live_error = ""
                live_payload: dict = {}
                started = time.perf_counter()
                try:
                    live_response = await client.get(
                        f"{_api_base_for_type(object_type)}{request_path}",
                        auth=(auth_user, api_token),
                        headers={"Content-Type": "application/json"},
                    )
                    _emit_zendesk_http_event(
                        operation="deploy_exact_update_preflight",
                        method="GET",
                        url_or_path=request_path,
                        status_code=live_response.status_code,
                        duration_ms=(time.perf_counter() - started) * 1000.0,
                        success=live_response.is_success,
                        error=None if live_response.is_success else f"HTTP {live_response.status_code}",
                    )
                    if not live_response.is_success:
                        live_error = f"Exact update target could not be reloaded (HTTP {live_response.status_code})."
                    else:
                        live_payload = live_response.json() if live_response.text else {}
                except (httpx.HTTPError, ValueError) as exc:
                    live_error = f"Exact update target preflight failed: {exc}"

                live_object = live_payload.get(response_root, {}) if isinstance(live_payload, dict) else {}
                live_id = str(live_object.get("id") or "").strip() if isinstance(live_object, dict) else ""
                if not live_error and live_id != target_object_id:
                    live_error = "Exact update preflight returned a different Zendesk object."
                expected_updated_at = str(record.get("target_updated_at") or "").strip()
                live_updated_at = str(live_object.get("updated_at") or "").strip() if isinstance(live_object, dict) else ""
                if (
                    not live_error
                    and expected_updated_at
                    and live_updated_at
                    and live_updated_at != expected_updated_at
                ):
                    live_error = (
                        "The Zendesk object changed after synchronization. Sync the instance again "
                        "before applying this update."
                    )
                if live_error:
                    failed += 1
                    results.append(
                        {
                            "record_id": record_id,
                            "object_type": object_type,
                            "title": title,
                            "deployment_status": "failed",
                            "zendesk_object_id": target_object_id,
                            "execution_message": live_error,
                            "executed_at": executed_at,
                        }
                    )
                    continue

            if dry_run:
                if title:
                    existing_cache.setdefault(object_type, {})[title.strip().lower()] = (
                        existing_id or f"DRY-RUN-{record_id or object_type}"
                    )
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
                        f"{_api_base_for_type(object_type)}{request_path}",
                        auth=(auth_user, api_token),
                        headers={"Content-Type": "application/json"},
                        json=payload,
                    )
                else:
                    response = await client.put(
                        f"{_api_base_for_type(object_type)}{request_path}",
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
        "help_center_base_url": guide_base_url,
        "sanitization_stats": sanitization_stats,
        "dependency_auto_create": {
            "events": dependency_events,
            "created_count": len([item for item in dependency_events if item.get("status") in {"created", "simulated"}]),
        },
    }
