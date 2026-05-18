import json
import re
from ast import literal_eval


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
        repaired = _repair_json_like_text(cleaned)
        if repaired is not None:
            return repaired
        raise ValueError("Model did not return valid JSON.") from exc


def _repair_json_like_text(value: str):
    candidate = (value or "").strip()
    if not candidate:
        return None

    # Common LLM output issues: trailing commas, smart quotes, and JS-style keys/booleans.
    transforms = [
        lambda text: text.replace("\ufeff", ""),
        lambda text: text.replace("“", '"').replace("”", '"').replace("’", "'"),
        lambda text: re.sub(r",(\s*[}\]])", r"\1", text),
        lambda text: re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)", r'\1"\2"\3', text),
    ]

    repaired = candidate
    for transform in transforms:
        repaired = transform(repaired)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    python_like = repaired
    python_like = re.sub(r"\bnull\b", "None", python_like, flags=re.IGNORECASE)
    python_like = re.sub(r"\btrue\b", "True", python_like, flags=re.IGNORECASE)
    python_like = re.sub(r"\bfalse\b", "False", python_like, flags=re.IGNORECASE)
    try:
        parsed = literal_eval(python_like)
    except Exception:
        return None
    return parsed
