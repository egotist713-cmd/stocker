import json
from types import SimpleNamespace

import pytest

from app.ai import metadata_analyzer
from app.ai.analyzer import AIAnalyzer, AIResponseError
from app.ai.metadata_analyzer import (
    LMStudioMetadataAnalyzer,
    MetadataAnalyzer,
    VISION_JSON_INPUT,
    response_schema,
)
from app.ai.schema import AIAnalysis, MetadataSuggestion

ENV_VARS = (
    "METADATA_BASE_URL",
    "METADATA_MODEL",
    "METADATA_API_KEY",
    "METADATA_TIMEOUT",
    "LMSTUDIO_BASE_URL",
    "LMSTUDIO_MODEL",
    "LMSTUDIO_API_KEY",
    "LMSTUDIO_TIMEOUT",
)

VISION = AIAnalysis(
    title="Elevator Shaft Interior View",
    description="An elevator shaft with concrete walls and metal rails.",
    keywords=["elevator shaft", "concrete wall"],
    brands=["Otis"],
)

VALID_OUTPUT = json.dumps(
    {
        "suggestion_version": "1.0",
        "title": "Elevator shaft interior",
        "description": "Concrete elevator shaft with steel guide rails.",
        "keywords": ["elevator shaft", "concrete wall", "guide rail"],
    }
)


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _stub(analyzer, content: str) -> dict:
    """Подменить LM Studio; вернуть dict, в который попадут аргументы запроса."""
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return response

    analyzer.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return captured


def test_metadata_provider_is_separate_from_vision():
    assert not issubclass(MetadataAnalyzer, AIAnalyzer)
    assert not issubclass(LMStudioMetadataAnalyzer, AIAnalyzer)


def test_base_interface_is_not_implemented():
    with pytest.raises(NotImplementedError):
        MetadataAnalyzer().suggest(VISION)


def test_defaults(clean_env):
    analyzer = LMStudioMetadataAnalyzer()

    assert analyzer.provider == "lmstudio"
    assert analyzer.prompt_version == "metadata-v1"
    assert analyzer.base_url == metadata_analyzer.DEFAULT_BASE_URL
    assert analyzer.model == "qwen3-vl-8b-instruct"
    assert analyzer.timeout == metadata_analyzer.DEFAULT_TIMEOUT
    assert analyzer.client.max_retries == 0


def test_settings_come_from_metadata_env(clean_env, monkeypatch):
    monkeypatch.setenv("METADATA_BASE_URL", "http://brain:1234/v1")
    monkeypatch.setenv("METADATA_MODEL", "qwen3.8-9b-distill")
    monkeypatch.setenv("METADATA_TIMEOUT", "30")

    analyzer = LMStudioMetadataAnalyzer()

    assert analyzer.base_url == "http://brain:1234/v1"
    assert analyzer.model == "qwen3.8-9b-distill"
    assert analyzer.timeout == 30.0


def test_vision_settings_do_not_leak_into_metadata(clean_env, monkeypatch):
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://vision:1234/v1")
    monkeypatch.setenv("LMSTUDIO_MODEL", "vision-only-model")

    analyzer = LMStudioMetadataAnalyzer()

    assert analyzer.base_url == metadata_analyzer.DEFAULT_BASE_URL
    assert analyzer.model == metadata_analyzer.DEFAULT_MODEL


def test_request_is_text_only_with_vision_json(clean_env, tmp_path):
    analyzer = LMStudioMetadataAnalyzer()
    captured = _stub(analyzer, VALID_OUTPUT)

    analyzer.suggest(VISION, image_path=tmp_path / "ignored.jpg")

    (message,) = captured["messages"]
    assert isinstance(message["content"], str)  # никакого image_url
    assert VISION.model_dump_json(indent=2) in message["content"]
    assert captured["model"] == analyzer.model


def test_inputs_report_only_vision_json_in_v1(clean_env, tmp_path):
    analyzer = LMStudioMetadataAnalyzer()

    assert analyzer.inputs() == [VISION_JSON_INPUT]
    assert analyzer.inputs(tmp_path / "photo.jpg") == [VISION_JSON_INPUT]


def test_request_uses_strict_schema_with_keyword_limits(clean_env):
    analyzer = LMStudioMetadataAnalyzer()
    captured = _stub(analyzer, VALID_OUTPUT)

    analyzer.suggest(VISION)

    response_format = captured["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "MetadataSuggestion"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema == response_schema()
    assert set(schema["required"]) == set(MetadataSuggestion.model_fields)
    assert schema["properties"]["keywords"]["minItems"] == 25
    assert schema["properties"]["keywords"]["maxItems"] == 49


def test_valid_output_is_parsed(clean_env):
    analyzer = LMStudioMetadataAnalyzer()
    _stub(analyzer, VALID_OUTPUT)

    suggestion = analyzer.suggest(VISION)

    assert suggestion.title == "Elevator shaft interior"
    assert suggestion.keywords == ["elevator shaft", "concrete wall", "guide rail"]


def test_output_over_limits_is_kept_for_python_validation(clean_env):
    analyzer = LMStudioMetadataAnalyzer()
    keywords = [f"k{i}" for i in range(60)]
    _stub(analyzer, json.dumps({"title": "t", "description": "d", "keywords": keywords}))

    assert analyzer.suggest(VISION).keywords == keywords


@pytest.mark.parametrize(
    "raw",
    [
        '{"title": "Elevator shaft", "keywo',  # обрезанный ответ
        "```json\n{}\n```",                    # markdown-обёртка
        "",                                    # пустой ответ
        '{"title": 42}',                       # неверный тип
    ],
)
def test_invalid_output_raises_with_raw_text(clean_env, raw):
    analyzer = LMStudioMetadataAnalyzer()
    _stub(analyzer, raw)

    with pytest.raises(AIResponseError) as info:
        analyzer.suggest(VISION)

    assert info.value.raw_output == raw


def test_none_content_is_reported_as_empty_raw_output(clean_env):
    analyzer = LMStudioMetadataAnalyzer()
    _stub(analyzer, None)

    with pytest.raises(AIResponseError) as info:
        analyzer.suggest(VISION)

    assert info.value.raw_output == ""
