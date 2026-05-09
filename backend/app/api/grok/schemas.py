OBJECT_TYPE_ENUM = [
    "triggers",
    "automations",
    "macros",
    "views",
    "groups",
    "ticket_fields",
    "ticket_forms",
    "articles",
]


PLANNER_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "object_type": {"type": "string", "enum": OBJECT_TYPE_ENUM},
        "intent": {"type": "string", "minLength": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "ambiguity_score": {"type": "number", "minimum": 0, "maximum": 1},
        "ambiguity_reasons": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 12,
        },
        "dependency_notes": {"type": "string"},
        "clarification_questions": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 6,
        },
    },
    "required": [
        "object_type",
        "intent",
        "confidence",
        "ambiguity_score",
        "ambiguity_reasons",
        "dependency_notes",
        "clarification_questions",
    ],
    "additionalProperties": False,
}


GENERATOR_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "object_type": {"type": "string", "enum": OBJECT_TYPE_ENUM},
                    "title": {"type": "string", "minLength": 1},
                    "conditions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string"},
                                "operator": {"type": "string"},
                                "value": {
                                    "type": ["string", "number", "boolean", "null", "array", "object"]
                                },
                            },
                            "required": ["field"],
                            "additionalProperties": True,
                        },
                    },
                    "actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string"},
                                "value": {
                                    "type": ["string", "number", "boolean", "null", "array", "object"]
                                },
                            },
                            "required": ["field"],
                            "additionalProperties": True,
                        },
                    },
                    "dependency_notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["object_type", "title", "conditions", "actions", "dependency_notes"],
                "additionalProperties": False,
            },
            "minItems": 1,
        },
        "generation_notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["records", "generation_notes"],
    "additionalProperties": False,
}
