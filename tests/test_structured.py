from app.ai.schema import AIAnalysis, MetadataSuggestion
from app.ai.structured import json_schema_response_format, strict_json_schema


def test_strict_schema_requires_all_fields_and_forbids_extras():
    schema = strict_json_schema(MetadataSuggestion)

    assert set(schema["required"]) == set(MetadataSuggestion.model_fields)
    assert schema["additionalProperties"] is False


def test_constraints_apply_only_to_request_schema():
    schema = strict_json_schema(MetadataSuggestion, {"keywords": {"minItems": 25, "maxItems": 49}})

    assert schema["properties"]["keywords"]["minItems"] == 25
    assert schema["properties"]["keywords"]["maxItems"] == 49
    assert "minItems" not in MetadataSuggestion.model_json_schema()["properties"]["keywords"]


def test_strict_schema_does_not_modify_model_schema():
    strict_json_schema(AIAnalysis)

    assert "required" not in AIAnalysis.model_json_schema()


def test_response_format_shape():
    response_format = json_schema_response_format("X", {"type": "object"})

    assert response_format == {
        "type": "json_schema",
        "json_schema": {"name": "X", "strict": True, "schema": {"type": "object"}},
    }


def test_metadata_suggestion_defaults():
    suggestion = MetadataSuggestion()

    assert suggestion.suggestion_version == "1.0"
    assert (suggestion.title, suggestion.description, suggestion.keywords) == ("", "", [])


def test_metadata_suggestion_does_not_enforce_limits():
    # Лимиты проверяет Python metadata-слой, а не Pydantic: превышение должно
    # сохраниться и попасть в validation, а не потеряться при парсинге.
    suggestion = MetadataSuggestion.model_validate_json(
        '{"title": "t", "description": "d", "keywords": [' + ",".join(f'"k{i}"' for i in range(60)) + "]}"
    )

    assert len(suggestion.keywords) == 60


def test_metadata_suggestion_ignores_vision_fields():
    suggestion = MetadataSuggestion.model_validate({"title": "t", "people": {"present": True}})

    assert not hasattr(suggestion, "people")
