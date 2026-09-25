import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import ValidationError

from app.ai.analyzer import AIResponseError
from app.ai.schema import AIAnalysis, MetadataSuggestion
from app.ai.structured import json_schema_response_format, strict_json_schema

load_dotenv()


# Конфигурация Metadata AI независима от Vision (LMSTUDIO_*): это разные роли.
# Значения по умолчанию совпадают с Vision только из-за ограничений VRAM.
DEFAULT_BASE_URL = "http://192.168.1.104:1234/v1"
DEFAULT_MODEL = "qwen3-vl-8b-instruct"
DEFAULT_API_KEY = "lm-studio"
DEFAULT_TIMEOUT = 180.0

# Действуют только на ответ модели; сохранённый результат проверяет Python-слой.
KEYWORDS_MIN_ITEMS = 25
KEYWORDS_MAX_ITEMS = 49

VISION_JSON_INPUT = "vision_json"


def response_schema() -> dict:
    """JSON schema ответа Metadata AI (docs/METADATA_CONTRACT.md, §3.3)."""
    return strict_json_schema(
        MetadataSuggestion,
        {"keywords": {"minItems": KEYWORDS_MIN_ITEMS, "maxItems": KEYWORDS_MAX_ITEMS}},
    )


class MetadataAnalyzer:
    """
    Базовый интерфейс Metadata AI: как подготовить изображение к продаже.

    Отдельная роль от Vision (AIAnalyzer): на входе результат Vision, а не
    только изображение. image_path предусмотрен для будущих версий; какие
    входы провайдер фактически использовал, сообщает inputs().
    """

    provider: str = "unknown"
    model: Optional[str] = None
    prompt_version: Optional[str] = None

    def suggest(self, analysis: AIAnalysis, image_path: Path | None = None) -> MetadataSuggestion:
        raise NotImplementedError("Metadata provider is not configured yet.")

    def inputs(self, image_path: Path | None = None) -> list[str]:
        """Входы, которые провайдер реально передаёт модели (для provenance)."""
        return [VISION_JSON_INPUT]


PROMPT = """You prepare metadata for selling a photograph on stock photo sites.

You do not see the photograph. Below is a JSON analysis of it produced by a
vision model. Treat it as the only source of facts.

Write:
- title: a concise, descriptive English title, at most 70 characters, no trailing period.
- description: one or two factual English sentences, at most 200 characters.
- keywords: 30 to 49 English keywords or short phrases in lowercase, ordered by
  relevance (most important first). Start with all relevant keywords from the
  analysis, then add related concepts a buyer would search for: subject,
  setting, industry, use cases, mood, composition.

Rules:
- Use only facts from the analysis. Do not invent locations, people, dates,
  equipment models or specifications.
- Never mention brands, logos, trademarks or company names.
- Do not use generic words such as "photo", "image", "picture" or "stock".
- Do not repeat keywords.

Vision analysis:
"""


class LMStudioMetadataAnalyzer(MetadataAnalyzer):
    """Metadata AI через LM Studio (OpenAI-compatible), только текстовый вход."""

    provider = "lmstudio"

    # Увеличивать при любом изменении промпта, входов или формата ответа.
    prompt_version = "metadata-v1"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.base_url = base_url or os.getenv("METADATA_BASE_URL") or DEFAULT_BASE_URL
        self.model = model or os.getenv("METADATA_MODEL") or DEFAULT_MODEL
        self.timeout = timeout or float(os.getenv("METADATA_TIMEOUT") or DEFAULT_TIMEOUT)

        # Как и у Vision: без скрытых повторов, сбой фиксируется событием.
        self.client = OpenAI(
            api_key=api_key or os.getenv("METADATA_API_KEY") or DEFAULT_API_KEY,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
        )

    def suggest(self, analysis: AIAnalysis, image_path: Path | None = None) -> MetadataSuggestion:
        # v1 работает только на JSON Vision; image_path не передаётся модели,
        # и inputs() честно это отражает.
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": PROMPT + analysis.model_dump_json(indent=2),
                }
            ],
            response_format=json_schema_response_format("MetadataSuggestion", response_schema()),
        )

        result_text = response.choices[0].message.content or ""

        try:
            return MetadataSuggestion.model_validate_json(result_text)
        except ValidationError as exc:
            raise AIResponseError(
                f"Model output is not a valid MetadataSuggestion: {exc.error_count()} error(s)",
                raw_output=result_text,
            ) from exc
