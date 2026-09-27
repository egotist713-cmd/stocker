"""
Профили Creative Review (паспорт §3A.10, docs/STOCK_READINESS_CONTRACT.md §4.3).

Ядро Creative Review не знает тематик: аудитория покупателей и ориентиры
оценки приходят из профиля. Новый профиль — новая запись здесь, без изменения
ядра, формулы и событий. Версия профиля входит в prompt_version, поэтому
оценки разных профилей и версий различимы в истории.
"""

import os
from dataclasses import dataclass

ACTIVE = "active"
PLANNED = "planned"


@dataclass(frozen=True)
class CreativeProfile:
    name: str
    version: str
    status: str
    audience: str = ""
    anchors: tuple[str, ...] = ()


INDUSTRIAL_STOCK = CreativeProfile(
    name="industrial_stock",
    version="1",
    status=ACTIVE,
    audience=(
        "The photographer sells INDUSTRIAL and TECHNICAL stock: construction, elevators, electrical "
        "installations, machinery, engineering, building infrastructure, industrial textures. Buyers are B2B: "
        "engineering companies, trade publications, presentations, manuals."
    ),
    anchors=(
        "Personal, family, vacation, pet, selfie or shadow photos, casual snapshots of people or children: "
        "demand low, uniqueness low, recommendation skip_suggested (or attention if technically excellent).",
        "Clear, well-lit, deliberate photos of industrial equipment, installations or processes: "
        "demand medium or high; recommendation proceed.",
        "Plain textures and backgrounds: demand medium, uniqueness low.",
    ),
)

# Архитектура рассчитана на разные классы контента; эти профили ещё не описаны.
PLANNED_PROFILES = ("nature_stock", "commercial_product", "editorial", "ai_content", "personal_archive")

PROFILES = {INDUSTRIAL_STOCK.name: INDUSTRIAL_STOCK} | {
    name: CreativeProfile(name=name, version="0", status=PLANNED) for name in PLANNED_PROFILES
}

# Общие для всех профилей ориентиры — не тематика, а свойства любого стокового кадра.
UNIVERSAL_ANCHORS = (
    "Recognizable people or children: stock sites need a model release; recommendation attention at best.",
    "Out of focus, dark, cluttered or accidental frames: composition weak, recommendation skip_suggested.",
)

DEFAULT_PROFILE = INDUSTRIAL_STOCK.name


class UnknownProfileError(ValueError):
    def __init__(self, name: str):
        status = PROFILES[name].status if name in PROFILES else "unknown"
        active = ", ".join(sorted(n for n, p in PROFILES.items() if p.status == ACTIVE))
        super().__init__(f"Creative profile '{name}' is {status}; active profiles: {active}")
        self.name = name


def get_profile(name: str | None = None) -> CreativeProfile:
    """Профиль по имени; по умолчанию — STOCKER_CREATIVE_PROFILE или industrial_stock."""
    name = name or os.getenv("STOCKER_CREATIVE_PROFILE") or DEFAULT_PROFILE
    profile = PROFILES.get(name)
    if profile is None or profile.status != ACTIVE:
        raise UnknownProfileError(name)
    return profile
