import base64
import os
from io import BytesIO
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageOps

from app.ai.schema import AIAnalysis
from app.ai.analyzer import AIAnalyzer


load_dotenv()


# The Responses API rejects images that require more than 30,000 patches.
# 6,144 px keeps a 4:3 photograph at 27,648 patches, leaving safe headroom.
MAX_ANALYSIS_IMAGE_EDGE = 6_144


class OpenAIAnalyzer(AIAnalyzer):
    """
    OpenAI-провайдер для анализа изображений.
    """

    def __init__(self, model: str = "gpt-5.6"):
        api_key = os.getenv("OPENAI_API_KEY")

        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not configured."
            )

        self.client = OpenAI(api_key=api_key)
        self.model = model

    def analyze(self, image_path: Path) -> AIAnalysis:
        image_path = Path(image_path)

        if not image_path.exists():
            raise FileNotFoundError(
                f"Image not found: {image_path}"
            )

        suffix = image_path.suffix.lower()

        mime_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }

        mime_type = mime_types.get(suffix)

        if not mime_type:
            raise ValueError(
                f"Unsupported image format: {suffix}"
            )

        image_bytes, mime_type = self._prepare_image(
            image_path, mime_type
        )
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")

        response = self.client.responses.create(
            model=self.model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": """
Analyze this stock photograph.

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
- confidence must be between 0 and 1.
""",
                        },
                        {
                            "type": "input_image",
                            "image_url": (
                                f"data:{mime_type};base64,"
                                f"{image_base64}"
                            ),
                        },
                    ],
                }
            ],
        )

        result_text = response.output_text.strip()

        return AIAnalysis.model_validate_json(result_text)

    @staticmethod
    def _prepare_image(
        image_path: Path, mime_type: str
    ) -> tuple[bytes, str]:
        """Return the original image or a resized in-memory copy for OpenAI."""
        with Image.open(image_path) as source:
            image = ImageOps.exif_transpose(source)

            if max(image.size) <= MAX_ANALYSIS_IMAGE_EDGE:
                return image_path.read_bytes(), mime_type

            image.thumbnail(
                (MAX_ANALYSIS_IMAGE_EDGE, MAX_ANALYSIS_IMAGE_EDGE),
                Image.Resampling.LANCZOS,
            )

            output = BytesIO()
            if mime_type == "image/jpeg":
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                image.save(output, format="JPEG", quality=95, optimize=True)
            elif mime_type == "image/webp":
                image.save(output, format="WEBP", quality=95, method=6)
            else:
                image.save(output, format="PNG", optimize=True)

        return output.getvalue(), mime_type
