from pydantic import BaseModel, Field


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
