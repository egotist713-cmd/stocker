"""
Интеграционные тесты Metadata AI на реальном LM Studio.

По умолчанию пропускаются. Запуск:

    $env:STOCKER_LMSTUDIO_TESTS = "1"; python -m pytest -m lmstudio
"""

import os

import pytest

from app.ai.analyzer import AIResponseError
from app.ai.metadata_analyzer import LMStudioMetadataAnalyzer
from app.ai.schema import AIAnalysis

pytestmark = [
    pytest.mark.lmstudio,
    pytest.mark.skipif(
        os.getenv("STOCKER_LMSTUDIO_TESTS") != "1",
        reason="requires LM Studio; set STOCKER_LMSTUDIO_TESTS=1",
    ),
]

# Vision-результат asset 5 (IMG_20260911_130437.jpg, local-v2).
VISION = AIAnalysis(
    title="Mechanical Door Locking Mechanism",
    description=(
        "Close-up of a mechanical door locking mechanism with metal hardware, "
        "cables and pulleys mounted on a brick wall, with electrical wiring."
    ),
    keywords=[
        "door lock",
        "mechanical",
        "industrial equipment",
        "metal hardware",
        "cable system",
        "brick wall",
        "electrical wiring",
        "security mechanism",
    ],
    subject="Mechanical door locking system",
    confidence=0.98,
)


def test_real_suggestion_is_valid_and_enriched():
    suggestion = LMStudioMetadataAnalyzer().suggest(VISION)

    assert suggestion.title
    assert suggestion.description
    assert 25 <= len(suggestion.keywords) <= 49
    assert len(suggestion.keywords) > len(VISION.keywords)


def test_real_truncated_response_keeps_raw_output():
    analyzer = LMStudioMetadataAnalyzer()
    create = analyzer.client.chat.completions.create

    # Ограничение токенов обрывает JSON посреди ответа: реальный невалидный вывод модели.
    analyzer.client.chat.completions.create = lambda **kwargs: create(**kwargs, max_tokens=12)

    with pytest.raises(AIResponseError) as info:
        analyzer.suggest(VISION)

    assert info.value.raw_output
    assert info.value.raw_output.lstrip().startswith("{")


def test_unreachable_endpoint_raises_quickly():
    analyzer = LMStudioMetadataAnalyzer(base_url="http://127.0.0.1:9/v1", timeout=5)

    with pytest.raises(Exception) as info:
        analyzer.suggest(VISION)

    assert not isinstance(info.value, AIResponseError)
