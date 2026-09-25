from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from app.ai import local_analyzer
from app.ai.analyzer import AIResponseError
from app.ai.local_analyzer import LocalAnalyzer

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


def _stub_response(analyzer: LocalAnalyzer, content: str) -> None:
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    analyzer.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response))
    )


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
