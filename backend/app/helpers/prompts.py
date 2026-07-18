import json


def build_planner_messages(
    prompt: str,
    *,
    dependency_mode: str = "match_existing_or_create_new",
    focus_object_types: list[str] | None = None,
    related_objects: list[dict] | None = None,
    reference_catalog: dict | None = None,
    recent_batch_context: list[str] | None = None,
    context_notes: str | None = None,
) -> list[dict]:
    system_prompt = (
        "You are a Zendesk planning assistant for administrators. "
        "Return JSON only and follow the schema exactly. "
        "Choose the best object_type, summarize intent, score confidence and ambiguity, "
        "and include at most one short clarification_question when the request is under-specified. "
        "If focus_object_types is provided, keep object_type inside that set. "
        "Use reference_catalog and recent_batch_context to avoid duplicate creation patterns."
    )
    user_payload = {
        "prompt": prompt,
        "dependency_mode": dependency_mode,
        "focus_object_types": focus_object_types or [],
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
    focus_object_types: list[str] | None = None,
    related_objects: list[dict] | None = None,
    reference_catalog: dict | None = None,
    recent_batch_context: list[str] | None = None,
    context_notes: str | None = None,
    chunk_instruction: str | None = None,
    chunk_target_count: int | None = None,
    chunk_index: int | None = None,
    chunk_total: int | None = None,
    existing_titles: list[str] | None = None,
) -> list[dict]:
    selected_related_objects = list(related_objects or [])
    update_target = next(
        (
            item
            for item in selected_related_objects
            if isinstance(item, dict) and item.get("update_target") is True
        ),
        None,
    )
    supporting_related_objects = [
        item
        for item in selected_related_objects
        if not (isinstance(item, dict) and item.get("update_target") is True)
    ]
    system_prompt = (
        "You are a Zendesk configuration generator. "
        "Return JSON only and follow the schema exactly. "
        'Output {"records":[...],"generation_notes":[...]}. '
        'Each record must include "object_type", "title", "conditions", "actions", and "dependency_notes". '
        'Supported object_type values include "brands","categories","sections","triggers","automations",'
        '"macros","views","groups","ticket_forms","ticket_fields","articles". '
        'For trigger or automation object_type, format conditions as [{"field","operator","value"}] and '
        'actions as Zendesk-compatible actions like {"field":"status","value":"open"}, '
        '{"field":"group_id","value":"123456"}, {"field":"priority","value":"high"}, '
        '{"field":"current_tags","value":"tag1 tag2"}. Use current_tags to add tags without '
        'removing existing ticket tags; use set_tags only when the user explicitly asks to replace all tags. '
        'Do not use free-form fields like "assign". '
        'For macros, include valid "actions". For views, include filter conditions and optionally output columns in actions (field="output_columns"). '
        'For groups, set title to the group name. For articles, include "section_id" in conditions or actions when available. '
        "If related_objects include known IDs (groups/forms/brands/sections), prefer those IDs in output. "
        "If the request appears to modify existing setup, include an explicit dependency_notes string per record. "
        "Before proposing a new object, check reference_catalog for same/similar titles and prefer reuse/update notes. "
        "If focus_object_types is provided, every record.object_type must be inside that set. "
        "When update_target is present, its snapshot is the authoritative current configuration and starting point. "
        "Return exactly one complete final-state record for that same object type. Preserve every condition, action, "
        "setting, title, and dependency the user did not explicitly ask to change. Never invent or replace its ID."
    )
    user_payload = {
        "plan": plan,
        "operation_mode": "update" if update_target else "create",
        "update_target": update_target,
        "dependency_mode": dependency_mode,
        "focus_object_types": focus_object_types or [],
        "related_objects": supporting_related_objects,
        "reference_catalog": reference_catalog or {},
        "recent_batch_context": recent_batch_context or [],
        "context_notes": context_notes or "",
        "chunking": {
            "instruction": chunk_instruction or "",
            "target_count": chunk_target_count,
            "chunk_index": chunk_index,
            "chunk_total": chunk_total,
            "existing_titles": existing_titles or [],
        },
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload)},
    ]
