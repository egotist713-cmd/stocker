import base64
from io import BytesIO
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from app.ai import local_analyzer
from app.ai.analyzer import AIResponseError
from app.ai.local_analyzer import LocalAnalyzer, response_schema
from app.ai.schema import AIAnalysis

ENV_VARS = ("LMSTUDIO_BASE_URL", "LMSTUDIO_MODEL", "LMSTUDIO_API_KEY", "LMSTUDIO_TIMEOUT")


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults_match_production_values(clean_env):
    analyzer = LocalAnalyzer()

    assert analyzer.base_url == "http://192.168.1.104:1234/v1"
    assert analyzer.model == "qwen3-vl-8b-instruct"
    assert analyzer.timeout == local_analyzer.DEFAULT_TIMEOUT
    assert analyzer.client.max_retries == 0
    assert analyzer.client.timeout == local_analyzer.DEFAULT_TIMEOUT


def test_settings_come_from_env(clean_env, monkeypatch):
    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://localhost:9999/v1")
    monkeypatch.setenv("LMSTUDIO_MODEL", "other-model")
    monkeypatch.setenv("LMSTUDIO_TIMEOUT", "42")

    analyzer = LocalAnalyzer()

    assert analyzer.base_url == "http://localhost:9999/v1"
    assert analyzer.model == "other-model"
    assert analyzer.timeout == 42.0


def test_explicit_arguments_override_env(clean_env, monkeypatch):
    monkeypatch.setenv("LMSTUDIO_MODEL", "env-model")

    assert LocalAnalyzer(model="arg-model").model == "arg-model"


def test_prepare_image_applies_exif_orientation(tmp_path):
    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: при показе кадр поворачивается на 90 градусов
    Image.new("RGB", (60, 40), "white").save(path, exif=exif)

    data, _ = LocalAnalyzer._prepare_image(path, "image/jpeg")

    assert Image.open(BytesIO(data)).size == (40, 60)


def test_prepare_image_limits_long_edge(tmp_path):
    path = tmp_path / "big.jpg"
    Image.new("RGB", (4096, 3072), "white").save(path)

    data, _ = LocalAnalyzer._prepare_image(path, "image/jpeg")

    assert Image.open(BytesIO(data)).size == (2048, 1536)


def _stub_response(analyzer: LocalAnalyzer, content: str) -> dict:
    """Подменить LM Studio; вернуть dict, в который попадут аргументы запроса."""
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return response

    analyzer.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return captured


def test_invalid_output_raises_with_raw_text(tmp_path, clean_env):
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (32, 32), "white").save(path)
    analyzer = LocalAnalyzer()
    _stub_response(analyzer, "```json\n{not valid")

    with pytest.raises(AIResponseError) as info:
        analyzer.analyze(path)

    assert info.value.raw_output == "```json\n{not valid"


def test_valid_output_is_parsed(tmp_path, clean_env):
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (32, 32), "white").save(path)
    analyzer = LocalAnalyzer()
    _stub_response(analyzer, '{"title": "White square", "confidence": 0.9}')

    assert analyzer.analyze(path).title == "White square"


def test_response_schema_requires_every_field():
    schema = response_schema()

    assert set(schema["required"]) == set(AIAnalysis.model_fields)
    assert schema["additionalProperties"] is False
    people = schema["$defs"]["PeopleInfo"]
    assert set(people["required"]) == {"present", "count"}
    assert people["additionalProperties"] is False


def test_response_schema_does_not_modify_aianalysis():
    response_schema()

    assert "required" not in AIAnalysis.model_json_schema()


def test_request_uses_strict_json_schema(tmp_path, clean_env):
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (32, 32), "white").save(path)
    analyzer = LocalAnalyzer()
    captured = _stub_response(analyzer, '{"title": "x"}')

    analyzer.analyze(path)

    response_format = captured["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"] == response_schema()


@pytest.mark.parametrize("suffix", [".tif", ".tiff"])
def test_tiff_is_sent_to_model_as_jpeg(tmp_path, clean_env, suffix):
    path = tmp_path / f"photo{suffix}"
    Image.new("CMYK", (40, 30), (0, 50, 100, 0)).save(path, compression="tiff_lzw")
    analyzer = LocalAnalyzer()
    captured = _stub_response(analyzer, '{"title": "x"}')

    analyzer.analyze(path)

    url = captured["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1]))).format == "JPEG"


def test_16bit_tiff_is_scaled_not_clipped(tmp_path):
    path = tmp_path / "gray16.tif"
    Image.fromarray(np.full((30, 40), 40000, dtype=np.uint16)).save(path)

    data, _ = LocalAnalyzer._prepare_image(path, "image/jpeg")

    red, green, blue = Image.open(BytesIO(data)).getpixel((5, 5))
    assert abs(red - 40000 // 256) <= 2
