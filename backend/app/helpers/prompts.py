import json


def build_planner_messages(
    prompt: str,
    *,
    dependency_mode: str = "match_existing_or_create_new",
    related_objects: list[dict] | None = None,
    reference_catalog: dict | None = None,
    recent_batch_context: list[str] | None = None,
    context_notes: str | None = None,
) -> list[dict]:
    system_prompt = (
        "You are a Zendesk planning assistant for administrators. "
        "Return JSON only and follow the schema exactly. "
        "Choose the best object_type, summarize intent, score confidence and ambiguity, "
        "and include short clarification_questions when the request is under-specified."
    )
    user_payload = {
        "prompt": prompt,
        "dependency_mode": dependency_mode,
        "related_objects": related_objects or [],
        "reference_catalog": reference_catalog or {},
        "recent_batch_context": recent_batch_context or [],
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
    reference_catalog: dict | None = None,
    recent_batch_context: list[str] | None = None,
    context_notes: str | None = None,
) -> list[dict]:
    system_prompt = (
        "You are a Zendesk configuration generator. "
        "Return JSON only and follow the schema exactly. "
        'Output {"records":[...],"generation_notes":[...]}. '
        'Each record must include "object_type", "title", "conditions", "actions", and "dependency_notes". '
        'Supported object_type values include "triggers","automations","macros","views","groups","ticket_forms","ticket_fields","articles". '
        'For trigger or automation object_type, format conditions as [{"field","operator","value"}] and '
        'actions as Zendesk-compatible actions like {"field":"status","value":"open"}, '
        '{"field":"group_id","value":"123456"}, {"field":"priority","value":"high"}, '
        '{"field":"set_tags","value":"tag1 tag2"}. Do not use free-form fields like "assign". '
        'For macros, include valid "actions". For views, include filter conditions and optionally output columns in actions (field="output_columns"). '
        'For groups, set title to the group name. For articles, include "section_id" in conditions or actions when available. '
        "If related_objects include known IDs (groups/forms/brands/sections), prefer those IDs in output. "
        "If the request appears to modify existing setup, include an explicit dependency_notes string per record."
    )
    user_payload = {
        "plan": plan,
        "dependency_mode": dependency_mode,
        "related_objects": related_objects or [],
        "reference_catalog": reference_catalog or {},
        "recent_batch_context": recent_batch_context or [],
        "context_notes": context_notes or "",
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload)},
    ]
