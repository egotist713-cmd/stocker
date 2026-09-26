"""
Интеграционный тест Enhancement Advisor на реальном LM Studio.

По умолчанию пропускается. Запуск:

    $env:STOCKER_LMSTUDIO_TESTS = "1"; python -m pytest -m lmstudio
"""

import os

import pytest

from app import enhancement as en
from app.ai.enhancement_advisor import LMStudioEnhancementAdvisor

from tests.test_enhancement import jpeg, scene

pytestmark = [
    pytest.mark.lmstudio,
    pytest.mark.skipif(
        os.getenv("STOCKER_LMSTUDIO_TESTS") != "1",
        reason="requires LM Studio; set STOCKER_LMSTUDIO_TESTS=1",
    ),
]


def test_real_advisor_returns_valid_repeatable_advice(tmp_path):
    path = tmp_path / "scene.jpg"
    jpeg(scene(size=(2400, 1800)), 45).save(path, "JPEG", quality=95)
    assessment = en.assess(metrics := en.read_metrics(path), "hash")
    assert metrics["megapixels"] > 4

    advisor = LMStudioEnhancementAdvisor()
    first = advisor.advise(path, assessment)
    second = advisor.advise(path, assessment)

    assert first.decision in (en.NOT_NEEDED, en.RECOMMENDED, en.RISKY)
    assert 0 <= first.confidence <= 1
    assert first.decision == second.decision  # temperature=0
