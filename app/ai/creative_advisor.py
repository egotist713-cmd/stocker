"""
Creative Review Advisor: оценка коммерческой ценности кадра для стоков
(docs/STOCK_READINESS_CONTRACT.md §4.3).

Модель видит кадр и описание Vision и возвращает первичные признаки
(композиция, уникальность, спрос, сценарии использования, замечания о
качестве). Итоговый score считает Python. Только рекомендация: экспорт не
блокирует, решений не принимает.
"""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageOps
from pydantic import ValidationError

from app.ai.analyzer import AIResponseError
from app.ai.enhancement_advisor import _jpeg_data_url
from app.ai.schema import AIAnalysis, CreativeReview
from app.ai.structured import json_schema_response_format, strict_json_schema
from app.creative_profiles import UNIVERSAL_ANCHORS, CreativeProfile, get_profile

load_dotenv()

# Своя конфигурация роли; значения по умолчанию совпадают с Vision (одна модель, §3A.6).
DEFAULT_BASE_URL = "http://192.168.1.104:1234/v1"
DEFAULT_MODEL = "qwen3-vl-8b-instruct"
DEFAULT_API_KEY = "lm-studio"
DEFAULT_TIMEOUT = 180.0

OVERVIEW_EDGE = 1536
USE_CASES_MAX = 5

INPUTS = ["image_overview", "vision_summary"]


def response_schema() -> dict:
    return strict_json_schema(CreativeReview, {"commercial_use_cases": {"maxItems": USE_CASES_MAX}})


class CreativeAdvisor:
    """Интерфейс провайдера; провайдер и профиль заменяемы конфигурацией (§4.4, §3A.10)."""

    provider: str = "unknown"
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    profile: Optional[CreativeProfile] = None

    def review(self, image_path: Path, vision: AIAnalysis) -> CreativeReview:
        raise NotImplementedError("Creative advisor is not configured.")


def overview(image_path: Path) -> str:
    with Image.open(image_path) as source:
        image = ImageOps.exif_transpose(source)
        image.thumbnail((OVERVIEW_EDGE, OVERVIEW_EDGE), Image.Resampling.LANCZOS)
        return _jpeg_data_url(image)


# Шаблон не знает тематик: аудитория и ориентиры — из профиля (app/creative_profiles.py).
# История: v1 без ориентиров не различал слабые и сильные кадры (медиана 63 против 65);
# v2 с ориентирами ниши — 36 против 90; v3 — те же ориентиры, вынесенные в профиль.
TEMPLATE_VERSION = "creative-review-v3"

PROMPT = """You are a strict stock photo editor. {audience}

Judge this photograph as a product for those buyers. Use these anchors:
{anchors}

What a vision model saw:
- subject: {subject}
- title: {title}
- description: {description}

Return:
- composition: good / acceptable / weak; composition_notes: one short sentence.
- uniqueness: high / medium / low compared with similar images already on stock sites.
- demand: high / medium / low for the buyers above.
- commercial_use_cases: 0 to 5 REALISTIC uses for those buyers; list fewer or none when the image is weak.
- quality_notes: visible quality problems, empty if none.
- recommendation: proceed / attention / skip_suggested.
- confidence between 0 and 1.
"""


def prompt_for(vision: AIAnalysis, profile: CreativeProfile) -> str:
    anchors = "\n".join(f"- {anchor}" for anchor in (*profile.anchors, *UNIVERSAL_ANCHORS))
    return PROMPT.format(
        audience=profile.audience, anchors=anchors,
        subject=vision.subject, title=vision.title, description=vision.description,
    )


def prompt_version_for(profile: CreativeProfile) -> str:
    """Версия шаблона + профиль и его версия: смена любого — новая оценка."""
    return f"{TEMPLATE_VERSION}/{profile.name}-v{profile.version}"


class LMStudioCreativeAdvisor(CreativeAdvisor):
    """Creative Review через LM Studio (OpenAI-compatible), та же локальная модель."""

    provider = "lmstudio"

    def __init__(
        self,
        profile: CreativeProfile | None = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.profile = profile or get_profile()
        # Меняется при любом изменении шаблона, профиля, входов или формата ответа.
        self.prompt_version = prompt_version_for(self.profile)

        self.base_url = base_url or os.getenv("CREATIVE_BASE_URL") or DEFAULT_BASE_URL
        self.model = model or os.getenv("CREATIVE_MODEL") or DEFAULT_MODEL
        self.timeout = timeout or float(os.getenv("CREATIVE_TIMEOUT") or DEFAULT_TIMEOUT)

        # Без скрытых повторов: сбой фиксируется событием CREATIVE_REVIEW/FAILED.
        self.client = OpenAI(
            api_key=api_key or os.getenv("CREATIVE_API_KEY") or DEFAULT_API_KEY,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
        )

    def review(self, image_path: Path, vision: AIAnalysis) -> CreativeReview:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_for(vision, self.profile)},
                    {"type": "image_url", "image_url": {"url": overview(Path(image_path))}},
                ],
            }],
            response_format=json_schema_response_format("CreativeReview", response_schema()),
            temperature=0,  # повторяемость, как у Enhancement Advisor
        )
        result_text = response.choices[0].message.content or ""
        try:
            return CreativeReview.model_validate_json(result_text)
        except ValidationError as exc:
            raise AIResponseError(
                f"Model output is not a valid CreativeReview: {exc.error_count()} error(s)",
                raw_output=result_text,
            ) from exc
