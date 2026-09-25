import base64
import copy
import os
from io import BytesIO
from pathlib import Path
from typing import Optional

import numpy as np
from openai import OpenAI
from dotenv import load_dotenv
from PIL import Image, ImageOps
from pydantic import ValidationError

from app.ai.schema import AIAnalysis
from app.ai.analyzer import AIAnalyzer, AIResponseError

load_dotenv()


DEFAULT_BASE_URL = "http://192.168.1.104:1234/v1"
DEFAULT_MODEL = "qwen3-vl-8b-instruct"
DEFAULT_API_KEY = "lm-studio"
DEFAULT_TIMEOUT = 180.0


def response_schema() -> dict:
    """
    JSON schema для strict structured output LM Studio.

    Строится из AIAnalysis без изменения самой модели. Все поля помечены
    обязательными, а лишние запрещены: иначе пустой {} формально валиден
    (у всех полей AIAnalysis есть значения по умолчанию) и модель может
    вернуть пустой анализ.
    """
    schema = copy.deepcopy(AIAnalysis.model_json_schema())

    def make_strict(node) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for value in node.values():
                make_strict(value)
        elif isinstance(node, list):
            for value in node:
                make_strict(value)

    make_strict(schema)
    return schema


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

    def analyze(self, image_path: Path) -> AIAnalysis:
        """Анализирует изображение через LM Studio и возвращает структуру AIAnalysis."""
        image_path = Path(image_path)

        if not image_path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        suffix = image_path.suffix.lower()

        mime_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            # TIFF модели не передаётся: _prepare_image перекодирует его в JPEG.
            ".tif": "image/jpeg",
            ".tiff": "image/jpeg",
        }

        mime_type = mime_types.get(suffix)

        if not mime_type:
            raise ValueError(f"Unsupported image format: {suffix}")

        image_bytes, mime_type = self._prepare_image(image_path, mime_type)
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": """Analyze this stock photograph.

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
- confidence must be between 0 and 1.""",
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
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "AIAnalysis",
                    "strict": True,
                    "schema": response_schema(),
                },
            },
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
    def _prepare_image(image_path: Path, mime_type: str) -> tuple[bytes, str]:
        """Resize large images before sending them to the local vision model."""
        max_edge = 2048
        with Image.open(image_path) as source:
            # Модель должна видеть кадр так же, как его видит человек.
            image = ImageOps.exif_transpose(source)
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

            # 16-bit grayscale (бывает в TIFF): convert("RGB") обрезает значения
            # до 255 и даёт белый кадр, поэтому сначала масштабируем в 8 бит.
            if image.mode in ("I", "I;16", "I;16L", "I;16B"):
                pixels = np.asarray(image, dtype=np.uint32) >> 8
                image = Image.fromarray(pixels.clip(0, 255).astype(np.uint8))

            output = BytesIO()
            if mime_type == "image/jpeg":
                image = image.convert("RGB")
                image.save(output, format="JPEG", quality=90, optimize=True)
            elif mime_type == "image/png":
                image.save(output, format="PNG", optimize=True)
            elif mime_type == "image/webp":
                image.save(output, format="WEBP", quality=90, method=6)
            else:
                raise ValueError(f"Unsupported image MIME type: {mime_type}")

            return output.getvalue(), mime_type
