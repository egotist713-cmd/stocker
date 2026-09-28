import hashlib
import json

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

    def analyze(self, view) -> AIAnalysis:
        """view — AnalysisView (app/analysis_view.py): провайдер файл сам не открывает."""
        raise NotImplementedError(
            "AI provider is not configured yet."
        )

    def identity(self) -> dict:
        """Входы, от которых зависит ответ, кроме изображения. Провайдеры уточняют."""
        return {"provider": getattr(self, "provider", type(self).__name__),
                "model": getattr(self, "model", None),
                "prompt_version": getattr(self, "prompt_version", None)}


def vision_inputs(view_fingerprint: str, identity: dict) -> dict:
    return {"view": view_fingerprint, **identity}


def input_fingerprint(view_fingerprint: str, identity: dict) -> str:
    """Отпечаток входов Vision: AnalysisView + провайдер, модель, промпт, схема, кодирование, параметры."""
    payload = json.dumps(vision_inputs(view_fingerprint, identity), sort_keys=True, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
