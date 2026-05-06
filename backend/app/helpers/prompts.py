import json


def build_planner_messages(prompt: str) -> list[dict]:
    system_prompt = (
        "You are a Grok planning assistant for Zendesk admins. "
        "Return strict JSON only with keys: object_type, intent, confidence. "
        "confidence must be between 0 and 1."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]


def build_generator_messages(plan: dict) -> list[dict]:
    system_prompt = (
        "You are a Grok generator for Zendesk configuration records. "
        "Return strict JSON only. Output either an array of records or "
        '{"records":[...]}. Each record must include "title", "conditions", and "actions".'
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(plan)},
    ]
