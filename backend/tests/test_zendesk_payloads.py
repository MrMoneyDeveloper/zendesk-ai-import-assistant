from app.services.zendesk import _build_ticket_field_payload, _build_ticket_form_payload


def test_build_ticket_field_payload_accepts_aliases_and_sets_defaults():
    record = {
        "title": "Test",
        "conditions": [],
        "actions": [
            {"field": "type", "value": "dropdown"},
            {"field": "options", "value": "tested, not tested"},
        ],
    }

    payload = _build_ticket_field_payload(record)
    ticket_field = payload["ticket_field"]
    assert ticket_field["title"] == "Test"
    assert ticket_field["type"] == "tagger"
    assert ticket_field["custom_field_options"] == [
        {"name": "tested", "value": "tested"},
        {"name": "not tested", "value": "not_tested"},
    ]
    assert ticket_field["agent_can_edit"] is True
    assert ticket_field["visible_in_portal"] is True
    assert ticket_field["editable_in_portal"] is False
    assert ticket_field["required"] is False
    assert ticket_field["required_in_portal"] is False


def test_build_ticket_form_payload_collects_numeric_ids_and_named_refs():
    record = {
        "title": "Claims Form",
        "conditions": [],
        "actions": [
            {"field": "ticket_fields", "value": ["123", "Claim Number", 456]},
            {"field": "ticket_field_names", "value": "Policy Number"},
        ],
    }
    payload, names = _build_ticket_form_payload(record)

    assert payload["ticket_form"]["name"] == "Claims Form"
    assert payload["ticket_form"]["ticket_field_ids"] == [123, 456]
    assert names == ["Claim Number", "Policy Number"]
