"""
Stock Readiness readiness-v1: готов ли объект к экспорту на площадку.

Контракт: docs/STOCK_READINESS_CONTRACT.md. Детерминированные правила без БД
и без AI: одинаковые входы дают одинаковый результат. Ничего не меняет —
ни metadata, ни файл. Единственное чтение файла — read_file_facts().
"""

import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app import metadata_builder as mb
from app import review_gate as rg
from app.ai.schema import AIAnalysis
from app.textnorm import normalize_text

READINESS_VERSION = "readiness-v1"

READY_METADATA_STATES = (rg.AUTO_APPROVED, mb.APPROVED)

NOT_EVALUATED = "not_evaluated"
READY = "ready"
BLOCKED = "blocked"
STALE = "stale"

BLOCKER = "blocker"
DERIVATIVE = "derivative"
WARNING = "warning"

MB = 1024 * 1024


# --- Профили площадок (§3.2) -------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    platform: str
    version: str
    min_mp: float
    max_mp: float | None
    max_file_size: int
    formats: tuple[str, ...]
    text_field: str               # поле площадки: title (Adobe) / description (Shutterstock)
    text_max: int
    text_recommended: int | None  # мягкий лимит → warning
    text_min_words: int | None    # законченное предложение → warning
    keywords_min: int
    keywords_max: int
    category_required: bool
    max_categories: int
    verified_at: str


ADOBE = Profile(
    platform="adobe",
    version="adobe-2026-09",
    min_mp=4.0,
    max_mp=100.0,
    max_file_size=45 * MB,
    formats=("JPEG",),
    text_field="title",
    text_max=mb.TITLE_MAX,  # Adobe максимум не указывает — лимит Stocker
    text_recommended=70,
    text_min_words=None,
    keywords_min=1,
    keywords_max=49,
    category_required=False,  # Adobe сам предлагает категорию при загрузке
    max_categories=1,
    verified_at="2026-09-26",
)

SHUTTERSTOCK = Profile(
    platform="shutterstock",
    version="shutterstock-2026-09",
    min_mp=4.0,
    max_mp=None,
    max_file_size=50 * MB,
    formats=("JPEG", "TIFF"),
    text_field="description",
    text_max=2048,
    text_recommended=None,
    text_min_words=5,
    keywords_min=7,
    keywords_max=50,
    category_required=True,
    max_categories=2,
    verified_at="2026-09-26",
)

PROFILES = {profile.platform: profile for profile in (ADOBE, SHUTTERSTOCK)}


# --- Сведения о файле --------------------------------------------------------------

SRGB = "srgb"
OTHER = "other"
MISSING = "missing"

_PRIMARY_TOLERANCE = 0.005


def _primaries_xy(profile) -> list[tuple[float, float]] | None:
    try:
        return [tuple(round(v, 4) for v in primary[1][:2]) for primary in
                (profile.red_primary, profile.green_primary, profile.blue_primary)]
    except (AttributeError, TypeError, IndexError):
        return None


def color_profile_kind(icc_bytes: bytes | None) -> tuple[str, str | None]:
    """sRGB определяется по xy основных цветов, а не по описанию профиля.

    Display P3 телефонов описан как "sRGB EOTF with DCI-P3 Color Gamut" — это не sRGB.
    """
    if not icc_bytes:
        return MISSING, None

    from PIL import ImageCms

    try:
        profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes)).profile
    except (OSError, ImageCms.PyCMSError):
        return OTHER, None

    description = (profile.profile_description or "").strip() or None
    reference = _primaries_xy(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).profile)
    actual = _primaries_xy(profile)
    if actual is None or reference is None:
        return OTHER, description

    same = all(abs(a - r) <= _PRIMARY_TOLERANCE for pa, pr in zip(actual, reference) for a, r in zip(pa, pr))
    return (SRGB if same else OTHER), description


def read_file_facts(path: Path) -> dict:
    """Формат, размеры, размер файла и цветовой профиль. Файл только читается."""
    from PIL import Image

    with Image.open(path) as image:
        kind, description = color_profile_kind(image.info.get("icc_profile"))
        return {
            "format": image.format,
            "width": image.width,
            "height": image.height,
            "file_size": path.stat().st_size,
            "color_profile": kind,
            "color_profile_description": description,
        }


# --- Бренды (§3.3a) ----------------------------------------------------------------

DOMINANCE_WORDS = ("logo", "logotype", "brand", "branding", "signage", "trademark")


def _contains(text: str, phrase: str) -> bool:
    return bool(phrase) and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def _fold(text: str) -> str:
    return normalize_text(text).casefold()


def brand_presence(vision: AIAnalysis) -> list[dict]:
    """Бренд-термины Vision и надписи brand_or_legal с уровнем dominant / minor."""
    terms = [(name, "brand") for name in vision.brands] + [(name, "logo") for name in vision.logos]
    terms += [
        (item["text"], f"text:{item['rule']}")
        for item in (rg.classify_text_item(text, vision) for text in vision.text_visible)
        if item["category"] == "brand_or_legal"
    ]

    main = _fold(f"{vision.subject} . {vision.title}")
    subject = _fold(vision.subject)
    subject_is_brand = any(_contains(subject, word) for word in DOMINANCE_WORDS)

    result, seen = [], set()
    for term, source in terms:
        folded = _fold(term)
        if not folded or folded in seen:
            continue
        seen.add(folded)
        dominant = subject_is_brand or _contains(main, folded)
        result.append({"term": normalize_text(term), "source": source, "prominence": "dominant" if dominant else "minor"})
    return result


def brands_in_metadata(fields: dict, brands: list[dict]) -> list[str]:
    text = _fold(" . ".join([fields.get("title", ""), fields.get("description", ""), *fields.get("keywords", [])]))
    return [brand["term"] for brand in brands if _contains(text, _fold(brand["term"]))]


# --- Категории (§3.5a) -------------------------------------------------------------

CATEGORY_RULES = (
    ("industry", "Industry", "Industrial", (
        "industrial", "industry", "factory", "manufacturing", "machinery", "machine", "electrical", "wiring",
        "wire", "wires", "cable", "cables", "elevator", "lift", "shaft", "pipe", "pipes", "valve", "mechanism",
        "equipment", "construction", "power supply", "control panel", "junction box", "terminal block",
        "infrastructure", "engineering", "maintenance",
    )),
    ("buildings", "Buildings and architecture", "Buildings/Landmarks", (
        "building", "architecture", "architectural", "facade", "interior", "staircase", "room", "wall", "house",
    )),
    ("technology", "Technology", "Technology", (
        "computer", "electronics", "electronic", "circuit", "server", "device", "smartphone", "technology",
    )),
    ("transport", "Transport", "Transportation", (
        "car", "truck", "train", "vehicle", "road", "highway", "transport", "transportation",
    )),
    ("leisure", "Hobbies and leisure", "Parks/Outdoor", (
        "playground", "park", "recreation", "recreational", "garden",
    )),
    ("nature", "Plants and flowers", "Nature", (
        "tree", "trees", "forest", "plant", "plants", "flower", "flowers", "nature",
    )),
    ("textures", "Graphic resources", "Backgrounds/Textures", (
        "texture", "background", "pattern", "surface",
    )),
)

_PLATFORM_COLUMN = {"adobe": 1, "shutterstock": 2}


def category_scores(vision: AIAnalysis, fields: dict) -> list[dict]:
    """Вес групп категорий: термины в subject/title/technical_subjects/categories — 3, в keywords — 1."""
    main = _fold(" . ".join([vision.subject, vision.title, *vision.technical_subjects, *vision.categories]))
    keywords = [_fold(keyword) for keyword in fields.get("keywords", [])]

    scores = []
    for order, (group, *_names, terms) in enumerate(CATEGORY_RULES):
        score = sum(3 for term in terms if _contains(main, term))
        score += sum(1 for keyword in keywords for term in terms if _contains(keyword, term))
        if score:
            scores.append({"group": group, "score": score, "order": order})
    return sorted(scores, key=lambda s: (-s["score"], s["order"]))


SECOND_CATEGORY_MIN_SCORE = 2
SECOND_CATEGORY_MIN_SHARE = 0.25  # доля веса первой категории


def map_categories(profile: Profile, scores: list[dict]) -> list[str]:
    """Первая категория — наибольший вес; вторая — только если она существенна."""
    column = _PLATFORM_COLUMN[profile.platform]
    by_group = {rule[0]: rule for rule in CATEGORY_RULES}
    chosen = []
    for index, score in enumerate(scores[: profile.max_categories]):
        if index > 0 and (score["score"] < SECOND_CATEGORY_MIN_SCORE
                          or score["score"] < scores[0]["score"] * SECOND_CATEGORY_MIN_SHARE):
            break
        chosen.append(by_group[score["group"]][column])
    return chosen


# --- Проверки (§3.3) ---------------------------------------------------------------

def _check(code: str, level: str, message: str) -> dict:
    return {"code": code, "level": level, "message": message}


def file_checks(profile: Profile, facts: dict) -> tuple[list[dict], list[str]]:
    """Проверки файла и операции derivative (оригинал не меняется, §3.5b)."""
    checks, operations = [], []

    if facts.get("source") != "ok":
        checks.append(_check("SOURCE_NOT_OK", BLOCKER, f"Source file is '{facts.get('source')}'"))
    if not facts.get("qc_passed"):
        checks.append(_check("QC_NOT_PASSED", BLOCKER, "QC has not passed"))

    megapixels = facts["width"] * facts["height"] / 1_000_000
    if megapixels < profile.min_mp:
        checks.append(_check("RESOLUTION_TOO_LOW", BLOCKER, f"{megapixels:.1f} MP < {profile.min_mp:g} MP"))
    if profile.max_mp and megapixels > profile.max_mp:
        checks.append(_check("RESOLUTION_TOO_HIGH", DERIVATIVE, f"{megapixels:.1f} MP > {profile.max_mp:g} MP"))
        operations.append(f"downscale_to_mp:{profile.max_mp:g}")

    if facts["format"] not in profile.formats:
        checks.append(_check("FORMAT_CONVERSION", DERIVATIVE, f"{facts['format']} is not accepted; JPEG derivative"))
        operations.append("to_jpeg")

    if facts["color_profile"] == OTHER:
        described = facts.get("color_profile_description") or "unknown"
        checks.append(_check("COLOR_PROFILE_CONVERSION", DERIVATIVE, f"Color profile '{described}' is not sRGB"))
        operations.append("to_srgb")
    elif facts["color_profile"] == MISSING:
        checks.append(_check("COLOR_PROFILE_MISSING", WARNING, "No ICC profile; assumed sRGB"))

    if facts["file_size"] > profile.max_file_size:
        checks.append(_check("FILE_TOO_LARGE", DERIVATIVE,
                             f"{facts['file_size'] / MB:.1f} MB > {profile.max_file_size / MB:g} MB"))
        operations.append(f"fit_file_size:{profile.max_file_size}")

    return checks, operations


def platform_text(profile: Profile, fields: dict) -> str:
    if profile.text_field == "description":
        return fields.get("description") or fields.get("title", "")
    return fields.get("title", "")


def metadata_checks(profile: Profile, metadata: dict, brands: list[dict], categories: list[str]) -> list[dict]:
    checks = []
    fields = metadata["fields"]

    if metadata.get("completeness") == mb.PARTIAL:
        checks.append(_check("METADATA_PARTIAL", BLOCKER, "Metadata is partial"))

    text = platform_text(profile, fields)
    if not text:
        checks.append(_check("TEXT_EMPTY", BLOCKER, f"{profile.text_field} is empty"))
    elif len(text) > profile.text_max:
        checks.append(_check("TEXT_TOO_LONG", BLOCKER, f"{profile.text_field} is {len(text)} characters (max {profile.text_max})"))
    elif profile.text_recommended and len(text) > profile.text_recommended:
        checks.append(_check("TITLE_LONG", WARNING, f"{profile.text_field} is {len(text)} characters (recommended <= {profile.text_recommended})"))
    if text and profile.text_min_words and len(text.split()) < profile.text_min_words:
        checks.append(_check("DESCRIPTION_NOT_SENTENCE", WARNING, f"{profile.text_field} has fewer than {profile.text_min_words} words"))

    count = len(fields.get("keywords", []))
    if count < profile.keywords_min:
        checks.append(_check("KEYWORDS_TOO_FEW", BLOCKER, f"{count} keywords (min {profile.keywords_min})"))
    if count > profile.keywords_max:
        checks.append(_check("KEYWORDS_TOO_MANY", BLOCKER, f"{count} keywords (max {profile.keywords_max})"))

    in_metadata = brands_in_metadata(fields, brands)
    if in_metadata:
        checks.append(_check("TRADEMARK_IN_METADATA", BLOCKER, f"Brand in metadata: {', '.join(in_metadata)}"))

    if not categories:
        level = BLOCKER if profile.category_required else WARNING
        checks.append(_check("CATEGORY_UNMAPPED", level, "No category matched; a human chooses one"))

    return checks


def rights_checks(vision: AIAnalysis, brands: list[dict]) -> list[dict]:
    checks = []

    people = rg.people_risk(vision)
    if people["level"] == "recognizable":
        checks.append(_check("MODEL_RELEASE_REQUIRED", BLOCKER, f"Recognizable people ({', '.join(people['markers'])}); no model release"))
    elif people["level"] in ("partial", "unclear"):
        checks.append(_check("PEOPLE_NOT_RECOGNIZABLE", WARNING, f"People present but not recognizable ({people['level']})"))

    dominant = [b["term"] for b in brands if b["prominence"] == "dominant"]
    minor = [b["term"] for b in brands if b["prominence"] == "minor"]
    if dominant:
        checks.append(_check("DOMINANT_BRAND", BLOCKER, f"Brand is the main subject: {', '.join(dominant)}"))
    if minor:
        checks.append(_check("MINOR_BRAND_PRESENCE", WARNING, f"Secondary brand marking: {', '.join(minor)}"))

    if vision.editorial_risk:
        checks.append(_check("EDITORIAL_ONLY", BLOCKER, f"Editorial risk: {', '.join(vision.editorial_risk)}"))
    if vision.ai_generated:
        checks.append(_check("AI_GENERATED", BLOCKER, "Vision reports AI-generated content"))

    return checks


# --- Оценка -------------------------------------------------------------------------

def fingerprint(facts: dict, metadata: dict | None, platforms: list[str]) -> str:
    """Отпечаток входов (§3.6): изменение любого из них делает результат stale."""
    metadata = metadata or {}
    payload = {
        "readiness_version": READINESS_VERSION,
        "profiles": [PROFILES[p].version for p in sorted(platforms)],
        "file_hash": facts.get("file_hash"),
        "source": facts.get("source"),
        "qc_passed": facts.get("qc_passed"),
        "state": metadata.get("state"),
        "completeness": metadata.get("completeness"),
        "fields": metadata.get("fields"),
        "vision_event_id": ((metadata.get("sources") or {}).get("vision") or {}).get("event_id"),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def evaluate_platform(profile: Profile, facts: dict, metadata: dict, vision: AIAnalysis) -> dict:
    brands = brand_presence(vision)
    categories = map_categories(profile, category_scores(vision, metadata["fields"]))

    file_list, operations = file_checks(profile, facts)
    checks = file_list + metadata_checks(profile, metadata, brands, categories) + rights_checks(vision, brands)

    blocked = any(check["level"] == BLOCKER for check in checks)
    plan = None
    if not blocked:
        fields = metadata["fields"]
        plan = {profile.text_field: platform_text(profile, fields), "keywords": list(fields["keywords"])}
        if profile.max_categories == 1:
            plan["category"] = categories[0] if categories else None
        else:
            plan["categories"] = categories
        plan.update({
            "file": {"from": "original", "operations": operations},
            "releases": [],
            "editorial": False,
            "ai_generated": False,
        })

    return {
        "profile": profile.version,
        "status": BLOCKED if blocked else READY,
        "checks": checks,
        "export_plan": plan,
    }


def evaluate(facts: dict, metadata: dict | None, vision: AIAnalysis | None, platforms: list[str] | None = None) -> dict:
    """Результат §3.7. facts: file_hash, source, qc_passed, format, width, height, file_size, color_profile."""
    platforms = sorted(platforms or PROFILES)
    unknown = [p for p in platforms if p not in PROFILES]
    if unknown:
        raise ValueError(f"Unknown platform(s): {', '.join(unknown)}")

    result = {
        "readiness_version": READINESS_VERSION,
        "evaluated_at": _now(),
        "fingerprint": fingerprint(facts, metadata, platforms),
        "platforms": {},
        "ready_for": [],
    }

    state = (metadata or {}).get("state")
    if vision is None or state not in READY_METADATA_STATES:
        reason = "Vision analysis is missing" if vision is None else f"Metadata state is '{state}'"
        for platform in platforms:
            result["platforms"][platform] = {
                "profile": PROFILES[platform].version,
                "status": NOT_EVALUATED,
                "checks": [_check("METADATA_NOT_READY", BLOCKER, reason)],
                "export_plan": None,
            }
        return result

    for platform in platforms:
        result["platforms"][platform] = evaluate_platform(PROFILES[platform], facts, metadata, vision)
    result["ready_for"] = [p for p in platforms if result["platforms"][p]["status"] == READY]
    return result


def current_status(result: dict | None, current_fingerprint: str) -> dict:
    """Статусы для представления: stale, если входы изменились после оценки."""
    if not result:
        return {"evaluated": False, "stale": False, "platforms": {}, "ready_for": []}
    stale = result["fingerprint"] != current_fingerprint
    platforms = {
        name: (STALE if stale else data["status"]) for name, data in result["platforms"].items()
    }
    return {
        "evaluated": True,
        "stale": stale,
        "platforms": platforms,
        "ready_for": [] if stale else list(result["ready_for"]),
    }
