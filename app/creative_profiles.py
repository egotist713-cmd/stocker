"""
Профили Creative Review (паспорт §3A.10, docs/STOCK_READINESS_CONTRACT.md §4.3).

Ядро Creative Review не знает тематик: аудитория покупателей и ориентиры
оценки приходят из профиля. Новый профиль — новая запись здесь, без изменения
ядра, формулы и событий. Версия профиля входит в prompt_version, поэтому
оценки разных профилей и версий различимы в истории.

Выбор профиля для кадра — явно (параметр profile) или автоматически
(profile="auto"): детерминированно по терминам описания Vision (route_profile).
"""

import os
import re
from dataclasses import dataclass

from app.textnorm import normalize_text

ACTIVE = "active"
PLANNED = "planned"
AUTO = "auto"


@dataclass(frozen=True)
class CreativeProfile:
    name: str
    version: str
    status: str
    audience: str = ""
    anchors: tuple[str, ...] = ()
    # Термины описания Vision, по которым профиль выбирается автоматически.
    route_terms: tuple[str, ...] = ()


INDUSTRIAL_STOCK = CreativeProfile(
    name="industrial_stock",
    # v2 (калибровка 27.09.2026): промышленные текстуры — законный фоновый сток,
    # v1 занижал их («uniqueness low») при сознательном отборе пользователем.
    version="2",
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
        "Industrial textures and surfaces (concrete, metal, rust, asphalt) shot cleanly and evenly lit are "
        "legitimate background stock: demand medium; recommendation proceed unless blurred or cluttered.",
    ),
    route_terms=(
        "industrial", "industry", "factory", "machinery", "machine", "equipment", "electrical", "wiring", "wire",
        "cable", "cables", "elevator", "lift", "shaft", "construction", "concrete", "metal", "steel", "pipe",
        "valve", "mechanism", "tool", "drill", "hammer", "installation", "engineering", "panel", "circuit",
        "breaker", "motor", "demolition", "bolt", "rust", "asphalt", "texture", "surface", "duct", "ductwork",
    ),
)

ARCHITECTURE_STOCK = CreativeProfile(
    name="architecture_stock",
    version="1",
    status=ACTIVE,
    audience=(
        "The photographer sells ARCHITECTURE and URBAN stock: buildings, facades, residential districts, "
        "city views and skylines, rooftops, streets, interiors. Buyers: real estate, urban planning and "
        "construction publications, city guides, news and presentations."
    ),
    anchors=(
        "Personal, family, vacation or selfie photos, casual snapshots of people or children: "
        "demand low, recommendation skip_suggested.",
        "Clear views of buildings, streets, skylines or rooftops with good light and a deliberate composition: "
        "demand medium; recommendation proceed.",
        "Ordinary residential districts are common on stock sites (uniqueness low to medium) but remain usable "
        "for real estate and urban topics; do not skip them for being ordinary.",
    ),
    route_terms=(
        "building", "buildings", "architecture", "architectural", "facade", "skyline", "cityscape", "city",
        "urban", "rooftop", "roof", "residential", "apartment", "street", "tower", "bridge", "district",
        "neighborhood", "housing",
    ),
)

NATURE_STOCK = CreativeProfile(
    name="nature_stock",
    version="1",
    status=ACTIVE,
    audience=(
        "The photographer sells NATURE and LANDSCAPE stock: mountains, forests, water, sky, seasons, plants, "
        "natural details. Buyers: travel and tourism, environment and outdoor brands, editorial, backgrounds "
        "and wallpapers."
    ),
    anchors=(
        "Personal, family, vacation or selfie photos, casual snapshots of people or children: "
        "demand low, recommendation skip_suggested.",
        "Scenic landscapes with good light and a clear composition: demand medium or high; recommendation proceed.",
        "Plain nature details and natural textures: demand medium, uniqueness low; proceed when clean.",
    ),
    route_terms=(
        "landscape", "mountain", "mountains", "forest", "tree", "trees", "sea", "ocean", "beach", "lake", "river",
        "sky", "sunset", "sunrise", "snow", "nature", "natural", "park", "garden", "flower", "flowers", "plant",
        "plants", "hills", "valley", "rock", "rocks", "field", "meadow",
    ),
)

# Архитектура рассчитана на разные классы контента; эти профили ещё не описаны.
PLANNED_PROFILES = ("travel_stock", "product_stock", "lifestyle_stock", "ai_content", "personal_archive")

PROFILES = {p.name: p for p in (INDUSTRIAL_STOCK, ARCHITECTURE_STOCK, NATURE_STOCK)} | {
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
        super().__init__(f"Creative profile '{name}' is {status}; active profiles: {active} (or '{AUTO}')")
        self.name = name


def get_profile(name: str | None = None) -> CreativeProfile:
    """Профиль по имени; по умолчанию — STOCKER_CREATIVE_PROFILE или industrial_stock."""
    name = name or os.getenv("STOCKER_CREATIVE_PROFILE") or DEFAULT_PROFILE
    profile = PROFILES.get(name)
    if profile is None or profile.status != ACTIVE:
        raise UnknownProfileError(name)
    return profile


def _count(text: str, terms: tuple[str, ...]) -> int:
    return sum(1 for term in terms if re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text))


def route_profile(subject: str, title: str, keywords: list[str]) -> tuple[CreativeProfile, dict]:
    """
    Автоматический выбор активного профиля по описанию Vision: термины в
    subject/title — вес 3, в keywords — 1. Нет совпадений или ничья с профилем по
    умолчанию — профиль по умолчанию. Возвращает (профиль, объяснение выбора).
    """
    main = normalize_text(f"{subject} . {title}").casefold()
    words = normalize_text(" . ".join(keywords)).casefold()
    scores = {
        p.name: 3 * _count(main, p.route_terms) + _count(words, p.route_terms)
        for p in PROFILES.values() if p.status == ACTIVE
    }
    default = get_profile()
    best = max(scores, key=lambda name: (scores[name], name == default.name))
    chosen = PROFILES[best] if scores[best] > 0 else default
    return chosen, {"mode": AUTO, "scores": scores, "chosen": chosen.name}
