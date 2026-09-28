import base64
import hashlib
import json
import os
from typing import Optional

from openai import OpenAI
from dotenv import load_dotenv
from pydantic import ValidationError

from app.ai.schema import AIAnalysis
from app.ai.analyzer import AIAnalyzer, AIResponseError
from app.ai.structured import json_schema_response_format, strict_json_schema

load_dotenv()


DEFAULT_BASE_URL = "http://192.168.1.104:1234/v1"
DEFAULT_MODEL = "qwen3-vl-8b-instruct"
DEFAULT_API_KEY = "lm-studio"
DEFAULT_TIMEOUT = 180.0


def response_schema() -> dict:
    """JSON schema ответа Vision: AIAnalysis со всеми полями обязательными (см. structured.py)."""
    return strict_json_schema(AIAnalysis)


# Текст промпта Vision. Его SHA256 входит в отпечаток входов (правка без смены
# prompt_version тоже делает результаты STALE).
PROMPT = """Analyze this stock photograph.

Return ONLY valid JSON matching this structure:

{
  "analysis_version": "1.0",
  "description": "",
  "title": "",
  "keywords": [],
  "categories": [],
  "subject": "",
  "commercial_context": "",
  "technical_subjects": [],
  "people": {
    "present": false,
    "count": 0
  },
  "brands": [],
  "logos": [],
  "text_visible": [],
  "editorial_risk": [],
  "ai_generated": false,
  "confidence": 0.0
}

Important:
- Describe only what is actually visible.
- Do not invent brands, equipment specifications, locations, or facts.
- Keywords must describe visible or directly inferable stock concepts.
- Do not assume that the image was AI-generated.
- confidence must be between 0 and 1."""

# Какие байты получает модель: вариант AnalysisView и кодирование (ASSET_STATE §2.1a).
IMAGE = {"variant": "preview", "format": "jpeg", "quality": 90}

# Параметры запроса, влияющие на ответ. Сейчас не задаются — умолчание сервера (фиксируется
# явно: если параметр появится, отпечаток изменится).
REQUEST_PARAMS = {"temperature": None, "max_tokens": None}


def _sha256(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LocalAnalyzer(AIAnalyzer):
    """
    Локальный провайдер для анализа изображений через LM Studio.
    """

    provider = "lmstudio"

    # Увеличивать при любом изменении текста промпта или формата ответа:
    # версия пишется в историю событий.
    # local-v1: промпт + свободный текстовый ответ.
    # local-v2: тот же промпт + strict json_schema response_format.
    prompt_version = "local-v2"

    @classmethod
    def _identity(cls, model: str) -> dict:
        """Всё, от чего зависит ответ, кроме view (контракт ASSET_STATE §2.1a)."""
        return {
            "provider": cls.provider,
            "model": model,
            "prompt_version": cls.prompt_version,
            "prompt_sha256": _sha256(PROMPT),
            "schema_sha256": _sha256(response_schema()),
            "image": dict(IMAGE),
            "params": dict(REQUEST_PARAMS),
        }

    def identity(self) -> dict:
        return self._identity(self.model)

    @classmethod
    def current_identity(cls) -> dict:
        """Идентичность Vision, которую pipeline использовал бы сейчас (конфигурация окружения)."""
        return cls._identity(os.getenv("LMSTUDIO_MODEL") or DEFAULT_MODEL)

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.base_url = base_url or os.getenv("LMSTUDIO_BASE_URL") or DEFAULT_BASE_URL
        self.model = model or os.getenv("LMSTUDIO_MODEL") or DEFAULT_MODEL
        self.timeout = timeout or float(os.getenv("LMSTUDIO_TIMEOUT") or DEFAULT_TIMEOUT)

        # Скрытые повторы SDK отключены: сбой фиксируется как AI/FAILED,
        # повтор выполняется явно через worker --asset-id.
        self.client = OpenAI(
            api_key=api_key or os.getenv("LMSTUDIO_API_KEY") or DEFAULT_API_KEY,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
        )

    def analyze(self, view) -> AIAnalysis:
        """Анализирует AnalysisView через LM Studio и возвращает структуру AIAnalysis."""
        image_bytes, mime_type = self._prepare_image(view)
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": PROMPT,
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{image_base64}"
                            },
                        },
                    ],
                }
            ],
            response_format=json_schema_response_format("AIAnalysis", response_schema()),
        )

        result_text = response.choices[0].message.content or ""

        try:
            return AIAnalysis.model_validate_json(result_text)
        except ValidationError as exc:
            raise AIResponseError(
                f"Model output is not a valid AIAnalysis: {exc.error_count()} error(s)",
                raw_output=result_text,
            ) from exc

    @staticmethod
    def _prepare_image(view) -> tuple[bytes, str]:
        """
        Вариант view "preview" (длинная сторона 2048, ориентирован, 8 бит, sRGB или
        undeclared без конвертации) в JPEG. Ориентацию и цвет решает AnalysisView.
        """
        return view.jpeg(IMAGE["variant"], quality=IMAGE["quality"]), "image/jpeg"
