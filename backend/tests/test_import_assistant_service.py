from app.models.schemas import ValidationSummary
from app.services.import_assistant_service import (
    _annotate_duplicate_candidates,
    _build_clarification_questions,
    _build_existing_object_index,
    _build_preview_records,
)


def test_build_preview_records_sets_warnings_and_blocks():
    plan = {"object_type": "trigger"}
    generated_data = [
        {"title": "Route claims", "conditions": [], "actions": []},
        {"title": "", "conditions": [{"field": "group", "operator": "is", "value": "claims"}], "actions": []},
    ]

    records, summary = _build_preview_records(plan, generated_data)

    assert len(records) == 2
    assert isinstance(summary, ValidationSummary)
    assert summary.passed == 0
    assert summary.warnings == 1
    assert summary.blocked == 1
    assert records[0]["validation_status"] == "warning"
    assert records[1]["validation_status"] == "failed"


def test_duplicate_annotation_adds_dependency_note():
    catalog = {
        "triggers": [
            {"object_type": "trigger", "id": "123", "name": "Route claims", "description": "conditions=1; actions=2"}
        ]
    }
    index = _build_existing_object_index(catalog)
    rows = [
        {
            "object_type": "triggers",
            "title": "Route claims",
            "conditions": [{"field": "status", "operator": "is", "value": "new"}],
            "actions": [{"field": "set_tags", "value": "claims"}],
            "dependency_notes": [],
        }
    ]

    patched, duplicates = _annotate_duplicate_candidates(
        rows,
        existing_index=index,
        dependency_mode="match_existing_or_create_new",
    )

    assert len(patched) == 1
    assert len(duplicates) == 1
    notes = patched[0].get("dependency_notes", [])
    assert any("Duplicate candidate" in note for note in notes)


def test_clarification_questions_capped_to_one():
    plan = {
        "object_type": "macros",
        "confidence": 0.2,
        "clarification_questions": [
            "Question one?",
            "Question two?",
        ],
    }

    questions = _build_clarification_questions(
        "create a macro",
        plan,
        ambiguity_score=0.9,
        ambiguity_threshold=0.55,
    )

    assert len(questions) == 1
