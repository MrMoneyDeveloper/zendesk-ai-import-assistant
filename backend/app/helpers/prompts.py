import json


def build_planner_messages(
    prompt: str,
    *,
    dependency_mode: str = "match_existing_or_create_new",
    related_objects: list[dict] | None = None,
    context_notes: str | None = None,
) -> list[dict]:
    system_prompt = (
        "You are a Grok planning assistant for Zendesk admins. "
        "Return strict JSON only with keys: object_type, intent, confidence, dependency_notes. "
        "confidence must be between 0 and 1."
    )
    user_payload = {
        "prompt": prompt,
        "dependency_mode": dependency_mode,
        "related_objects": related_objects or [],
        "context_notes": context_notes or "",
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload)},
    ]


def build_generator_messages(
    plan: dict,
    *,
    dependency_mode: str = "match_existing_or_create_new",
    related_objects: list[dict] | None = None,
    context_notes: str | None = None,
) -> list[dict]:
    system_prompt = (
        "You are a Grok generator for Zendesk configuration records. "
        "Return strict JSON only. Output either an array of records or "
        '{"records":[...]}. Each record must include "title", "conditions", and "actions". '
        'For trigger object_type, format conditions as [{"field","operator","value"}] and '
        'actions as Zendesk-compatible actions like {"field":"status","value":"open"}, '
        '{"field":"group_id","value":"123456"}, {"field":"priority","value":"high"}, '
        '{"field":"set_tags","value":"tag1 tag2"}. Do not use free-form fields like "assign". '
        "If related_objects include known IDs (groups/forms/brands/sections), prefer those IDs in output. "
        "If the request appears to modify existing setup, include an explicit dependency_notes string per record."
    )
    user_payload = {
        "plan": plan,
        "dependency_mode": dependency_mode,
        "related_objects": related_objects or [],
        "context_notes": context_notes or "",
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload)},
    ]
