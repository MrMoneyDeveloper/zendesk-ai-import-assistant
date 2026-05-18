from app.helpers.json_parser import extract_json_payload


def test_extract_json_payload_repairs_trailing_commas_and_unquoted_keys():
    raw = '{records: [{"title":"Test",}], generation_notes: [],}'
    parsed = extract_json_payload(raw)
    assert isinstance(parsed, dict)
    assert "records" in parsed
    assert parsed["records"][0]["title"] == "Test"


def test_extract_json_payload_repairs_single_quotes_and_python_literals():
    raw = "{'records': [{'title': 'Test', 'active': true, 'meta': null}], 'generation_notes': []}"
    parsed = extract_json_payload(raw)
    assert isinstance(parsed, dict)
    assert parsed["records"][0]["active"] is True
    assert parsed["records"][0]["meta"] is None
