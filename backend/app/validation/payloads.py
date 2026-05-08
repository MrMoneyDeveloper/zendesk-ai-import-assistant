from app.models.schemas import GeneratedRecord


def ensure_prompt_is_valid(prompt: str) -> str:
    cleaned = prompt.strip()
    if len(cleaned) < 5:
        raise ValueError("Prompt must be at least 5 characters long.")
    return cleaned


def normalize_generated_rows(rows: list[dict]) -> list[dict]:
    action_field_aliases = {
        "assign": "group_id",
        "group": "group_id",
        "group_name": "group_id",
        "assignee": "assignee_id",
    }
    condition_field_aliases = {
        "group": "group_id",
        "assignee": "assignee_id",
    }

    normalized: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        conditions = row.get("conditions", [])
        actions = row.get("actions", [])
        if isinstance(conditions, dict):
            conditions = [conditions]
        if isinstance(actions, dict):
            actions = [actions]

        remapped_conditions = []
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            raw_field = str(condition.get("field", "")).strip().lower()
            condition["field"] = condition_field_aliases.get(raw_field, raw_field)
            remapped_conditions.append(condition)

        remapped_actions = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            raw_field = str(action.get("field", "")).strip().lower()
            action["field"] = action_field_aliases.get(raw_field, raw_field)
            remapped_actions.append(action)

        record = GeneratedRecord.model_validate(
            {
                "object_type": row.get("object_type", "triggers"),
                "title": row.get("title", "Untitled Rule"),
                "conditions": remapped_conditions,
                "actions": remapped_actions,
            }
        )
        normalized_row = record.model_dump()
        dependency_notes = row.get("dependency_notes")
        if isinstance(dependency_notes, str) and dependency_notes.strip():
            normalized_row["dependency_notes"] = [dependency_notes.strip()]
        elif isinstance(dependency_notes, list):
            normalized_row["dependency_notes"] = [
                str(item).strip() for item in dependency_notes if str(item).strip()
            ]
        normalized.append(normalized_row)

    if not normalized:
        raise ValueError("No valid records were produced by the model.")

    return normalized
