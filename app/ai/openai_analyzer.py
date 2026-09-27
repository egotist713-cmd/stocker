import base64
import os

from dotenv import load_dotenv
from openai import OpenAI

from app.ai.schema import AIAnalysis
from app.ai.analyzer import AIAnalyzer


load_dotenv()


# Изображение — вариант view "preview" (2048 px): лимит Responses API в 30 000 патчей не достигается.


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

    def analyze(self, view) -> AIAnalysis:
        """AnalysisView → JPEG (вариант preview). Файл провайдер сам не открывает."""
        image_bytes, mime_type = view.jpeg("preview", quality=95), "image/jpeg"
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
