from pathlib import Path

from app.ai.schema import AIAnalysis


class AIResponseError(ValueError):
    """
    Модель ответила, но ответ не прошёл валидацию AIAnalysis.

    Сырой ответ сохраняется, чтобы его можно было записать в историю событий.
    """

    def __init__(self, message: str, raw_output: str):
        super().__init__(message)
        self.raw_output = raw_output


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
