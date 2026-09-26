"""
Enhancement Advisor: рекомендация модели для спорных случаев Enhancement decision
(docs/STOCK_READINESS_CONTRACT.md §4.2).

Вызывается только когда детерминированные правила не приняли решение
(disputed). Модель видит кадр целиком и фрагмент в масштабе 100 % (шум и
артефакты на уменьшенной копии не видны) и метрики правил. Её ответ —
рекомендация: правила QC и Enhancement она не отменяет, Topaz не запускает.
"""

import base64
import os
from io import BytesIO
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageOps
from pydantic import ValidationError

from app.ai.analyzer import AIResponseError
from app.ai.schema import EnhancementAdvice
from app.ai.structured import json_schema_response_format, strict_json_schema

load_dotenv()

# Своя конфигурация роли; значения по умолчанию совпадают с Vision (одна модель, §3A.6).
DEFAULT_BASE_URL = "http://192.168.1.104:1234/v1"
DEFAULT_MODEL = "qwen3-vl-8b-instruct"
DEFAULT_API_KEY = "lm-studio"
DEFAULT_TIMEOUT = 180.0

OVERVIEW_EDGE = 1536
DETAIL_CROP = 1024

INPUTS = ["image_overview", "image_crop_100", "rules_metrics"]


def response_schema() -> dict:
    return strict_json_schema(EnhancementAdvice)


class EnhancementAdvisor:
    """Интерфейс провайдера рекомендаций; провайдер заменяем конфигурацией (§4.4)."""

    provider: str = "unknown"
    model: Optional[str] = None
    prompt_version: Optional[str] = None

    def advise(self, image_path: Path, assessment: dict) -> EnhancementAdvice:
        raise NotImplementedError("Enhancement advisor is not configured.")


def _jpeg_data_url(image: Image.Image) -> str:
    output = BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def prepare_images(image_path: Path) -> tuple[str, str]:
    """Обзор кадра (длинная сторона 1536 px) и центральный фрагмент 1024 px в масштабе 100 %."""
    with Image.open(image_path) as source:
        image = ImageOps.exif_transpose(source)
        size = min(DETAIL_CROP, image.width, image.height)
        left, top = (image.width - size) // 2, (image.height - size) // 2
        crop = image.crop((left, top, left + size, top + size))
        overview = image.copy()
        overview.thumbnail((OVERVIEW_EDGE, OVERVIEW_EDGE), Image.Resampling.LANCZOS)
        return _jpeg_data_url(overview), _jpeg_data_url(crop)


PROMPT = """You check the technical image quality of a stock photograph before it is sent to stock sites.

Image 1 is the whole frame (downscaled). Image 2 is a crop from the center at 100% scale:
use it to judge noise, sharpness and compression artifacts at pixel level.

Deterministic measurements found only borderline values, so the rules could not decide.
Measurements and levels (ok / borderline / issue / severe):
{findings}

Decide whether an enhancement tool (denoise, sharpen, compression artifact removal, upscale)
would clearly improve acceptance on stock sites:
- enhancement_not_needed: quality is acceptable as is at 100% scale.
- enhancement_recommended: a visible defect that an enhancement tool can fix without inventing details.
  List the defects in reasons and the tools in operations.
- enhancement_risky: enhancement would likely invent details or damage the image
  (strong blur, small text, faces, fine repeating patterns). List why in reasons.

Rules:
- reasons use only: noise, sharpness, artifacts, resolution, other ("other" needs a detail).
- operations only for enhancement_recommended: sharpen, denoise, remove_compression_artifacts, upscale.
- Judge only what you see. If unsure, prefer enhancement_not_needed with lower confidence.
- confidence is between 0 and 1.
"""


def prompt_for(assessment: dict) -> str:
    lines = [f"- {f['reason']}: {f['metric']}={f['value']} ({f['level']})" for f in assessment.get("findings", [])]
    lines += [f"- note: {n['code']} — {n['detail']}" for n in assessment.get("notes", [])]
    return PROMPT.format(findings="\n".join(lines))


class LMStudioEnhancementAdvisor(EnhancementAdvisor):
    """Рекомендация через LM Studio (OpenAI-compatible), та же локальная модель."""

    provider = "lmstudio"

    # Увеличивать при любом изменении промпта, входов или формата ответа.
    prompt_version = "enhancement-advice-v1"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.base_url = base_url or os.getenv("ENHANCEMENT_BASE_URL") or DEFAULT_BASE_URL
        self.model = model or os.getenv("ENHANCEMENT_MODEL") or DEFAULT_MODEL
        self.timeout = timeout or float(os.getenv("ENHANCEMENT_TIMEOUT") or DEFAULT_TIMEOUT)

        # Без скрытых повторов: сбой фиксируется событием ENHANCEMENT/FAILED.
        self.client = OpenAI(
            api_key=api_key or os.getenv("ENHANCEMENT_API_KEY") or DEFAULT_API_KEY,
            base_url=self.base_url,
            timeout=self.timeout,
            max_retries=0,
        )

    def advise(self, image_path: Path, assessment: dict) -> EnhancementAdvice:
        overview, crop = prepare_images(Path(image_path))
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_for(assessment)},
                    {"type": "image_url", "image_url": {"url": overview}},
                    {"type": "image_url", "image_url": {"url": crop}},
                ],
            }],
            response_format=json_schema_response_format("EnhancementAdvice", response_schema()),
            # Пограничные случаи: без температуры один и тот же кадр получал разные ответы.
            temperature=0,
        )
        result_text = response.choices[0].message.content or ""
        try:
            return EnhancementAdvice.model_validate_json(result_text)
        except ValidationError as exc:
            raise AIResponseError(
                f"Model output is not a valid EnhancementAdvice: {exc.error_count()} error(s)",
                raw_output=result_text,
            ) from exc


def advisor_enabled() -> bool:
    """Аварийный выключатель советника: STOCKER_ENHANCEMENT_ADVISOR=0."""
    return os.getenv("STOCKER_ENHANCEMENT_ADVISOR", "1") != "0"
