import json


def _strip_code_fences(value: str) -> str:
    trimmed = value.strip()
    if not trimmed.startswith("```"):
        return trimmed

    lines = trimmed.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def extract_json_payload(raw_text: str):
    cleaned = _strip_code_fences(raw_text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError("Model did not return valid JSON.") from exc
