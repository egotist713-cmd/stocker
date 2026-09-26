from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PeopleInfo(BaseModel):
    present: bool = False
    count: int = Field(default=0, ge=0)


class AIAnalysis(BaseModel):
    analysis_version: str = "1.0"

    description: str = ""
    title: str = ""

    keywords: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)

    subject: str = ""
    commercial_context: str = ""
    technical_subjects: list[str] = Field(default_factory=list)

    people: PeopleInfo = Field(default_factory=PeopleInfo)

    brands: list[str] = Field(default_factory=list)
    logos: list[str] = Field(default_factory=list)
    text_visible: list[str] = Field(default_factory=list)

    editorial_risk: list[str] = Field(default_factory=list)

    ai_generated: bool = False

    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class MetadataSuggestion(BaseModel):
    """
    Ответ Metadata AI: как подготовить изображение к продаже.

    Не содержит лимитов: minItems/maxItems для keywords задаются только в схеме
    запроса, а сохранённый результат проверяет Python metadata-слой
    (docs/METADATA_CONTRACT.md, §3.3 и §4.5).
    """

    suggestion_version: str = "1.0"

    title: str = ""
    description: str = ""

    keywords: list[str] = Field(default_factory=list)


class EnhancementReason(BaseModel):
    reason: Literal["noise", "sharpness", "artifacts", "resolution", "other"]
    detail: str = ""


class EnhancementAdvice(BaseModel):
    """
    Рекомендация модели по улучшению для спорного случая
    (docs/STOCK_READINESS_CONTRACT.md §4.2). Только рекомендация: решение правил
    она не отменяет и ничего не запускает. Версия — в provenance (prompt_version):
    служебных полей в ответе модели нет, иначе strict-схема заставляет их заполнять.
    """

    decision: Literal["enhancement_not_needed", "enhancement_recommended", "enhancement_risky"]
    reasons: list[EnhancementReason] = Field(default_factory=list)
    operations: list[Literal["sharpen", "denoise", "remove_compression_artifacts", "upscale"]] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _consistent(self):
        if self.decision != "enhancement_not_needed" and not self.reasons:
            raise ValueError(f"{self.decision} requires at least one reason")
        if any(r.reason == "other" and not r.detail.strip() for r in self.reasons):
            raise ValueError("reason 'other' requires detail")
        if self.operations and self.decision != "enhancement_recommended":
            raise ValueError("operations are allowed only for enhancement_recommended")
        return self
