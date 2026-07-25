import asyncio
import json

from app.services import context_qa


def test_context_question_sends_only_relevant_name_index_to_model(monkeypatch):
    captured: dict = {}

    class FakeClient:
        async def chat(self, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return json.dumps(
                {
                    "answer": "Site Factory is the selected Zendesk brand.",
                    "findings": ["The synchronized brand ID is 42."],
                    "impact": [],
                    "risks": [],
                    "recommended_checks": [],
                    "cannot_verify": [],
                    "citations": [
                        {
                            "source": "zendesk",
                            "object_type": "brand",
                            "object_id": "42",
                            "name": "Site Factory",
                            "evidence": "Selected synchronized brand record.",
                        }
                    ],
                    "confidence": 0.95,
                }
            )

        def get_last_call_metrics(self, task):
            assert task == "context_qa"
            return {
                "provider": "Gemini",
                "model": "gemini-test",
                "fallback_used": False,
                "input_tokens": 100,
                "output_tokens": 40,
                "total_tokens": 140,
                "elapsed_ms": 25,
            }

    monkeypatch.setattr(context_qa, "GrokClient", FakeClient)
    brands = [
        {
            "id": str(index),
            "name": "Site Factory" if index == 42 else f"Brand {index:04d}",
            "object_type": "brand",
            "catalog_key": "brands",
            "snapshot": {"id": index, "name": f"Brand {index:04d}"},
            "editable": True,
        }
        for index in range(1, 2302)
    ]
    brands[41]["snapshot"]["name"] = "Site Factory"
    catalog_result = {
        "catalogs": {"brands": brands},
        "catalog_counts": {"brands": len(brands)},
        "complete": True,
        "sync_id": "SYNC-CONTEXT-TEST",
        "warnings": [],
    }

    response = asyncio.run(
        context_qa.answer_context_question(
            question="What is Site Factory?",
            question_mode="instance_question",
            catalog_result=catalog_result,
            selected_objects=[
                {
                    "object_type": "brand",
                    "id": "42",
                    "name": "Site Factory",
                    "catalog_key": "brands",
                }
            ],
        )
    )

    prompt_payload = json.loads(captured["messages"][1]["content"])
    relevant_ids = {
        row["object_id"] for row in prompt_payload["relevant_current_objects"]
    }
    indexed_ids = {
        row["object_id"] for row in prompt_payload["current_object_name_index"]
    }

    assert response["fallback_used"] is False
    assert response["scope"]["catalog_total"] == 2301
    assert response["scope"]["catalog_name_index_included"] <= 30
    assert indexed_ids == relevant_ids
    assert "42" in indexed_ids
