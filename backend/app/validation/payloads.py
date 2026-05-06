from app.models.schemas import GeneratedRecord


def ensure_prompt_is_valid(prompt: str) -> str:
    cleaned = prompt.strip()
    if len(cleaned) < 5:
        raise ValueError("Prompt must be at least 5 characters long.")
    return cleaned


def normalize_generated_rows(rows: list[dict]) -> list[dict]:
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

        record = GeneratedRecord.model_validate(
            {
                "title": row.get("title", "Untitled Rule"),
                "conditions": conditions,
                "actions": actions,
            }
        )
        normalized.append(record.model_dump())

    if not normalized:
        raise ValueError("No valid records were produced by the model.")

    return normalized
