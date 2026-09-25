import base64
from io import BytesIO
from pathlib import Path
from typing import Optional

from openai import OpenAI
from dotenv import load_dotenv
from PIL import Image

from app.ai.schema import AIAnalysis
from app.ai.analyzer import AIAnalyzer

load_dotenv()


class LocalAnalyzer(AIAnalyzer):
    """
    Локальный провайдер для анализа изображений через LM Studio.
    """

    def __init__(self, api_key: Optional[str] = None, base_url: str = "http://192.168.1.104:1234/v1", model: str = "qwen3-vl-8b-instruct"): 
        self.client = OpenAI(api_key=api_key or "lm-studio", base_url=base_url)
        self.model = model

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
        )

        result_text = response.choices[0].message.content or ""

        return AIAnalysis.model_validate_json(result_text)

    @staticmethod
    def _prepare_image(image_path: Path, mime_type: str) -> tuple[bytes, str]:
        """Resize large images before sending them to the local vision model."""
        max_edge = 2048
        with Image.open(image_path) as image:
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)

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
