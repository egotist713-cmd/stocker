from pathlib import Path

from app.ai.schema import AIAnalysis


class AIAnalyzer:
    """
    Базовый интерфейс AI-анализа.

    Конкретный провайдер (OpenAI, Claude или локальная VLM)
    будет подключаться отдельно.
    """

    def analyze(self, image_path: Path) -> AIAnalysis:
        raise NotImplementedError(
            "AI provider is not configured yet."
        )
