"""
Export preparation export-v1 (docs/EXPORT_PREPARATION_CONTRACT.md, паспорт §35ZZW).

Готовит финальный файл площадки из уже принятых решений — ничего не улучшает и не
решает, стоит ли кадр экспорта:

    source (только чтение) → AnalysisView.full (ориентация, цвет → sRGB) → файл площадки

Девять шагов §3: вход (SHA256 source, актуальные Readiness ready и Publication approved) →
пиксели через AnalysisView (единственный вход пикселей: основной кадр, ориентация, цвет) →
размер (только уменьшение сверх максимума) → JPEG (качество профиля, fit_file_size) →
встроенные данные отсутствуют по построению (новый файл из пикселей) → metadata собираются
заново по белому списку → повторный разбор готового файла (inventory ⊆ whitelist, цвет,
размер, title / keywords) → учёт: файл атомарно, затем DERIVATIVE/CREATED одной транзакцией.

Metadata источника в файл не переносится ни одно поле: файл строится из пикселей, а ICC,
XMP (dc:title, dc:subject) пишет профиль площадки. Title / keywords — только из снимка
export_plan актуального Readiness (не из ai_result и не из metadata_json напрямую).
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import PIL
from PIL import Image, features

from app import analysis_view, ingest, normalizer, source_facts
from app import readiness as rd
from app.database import db
from app.database.db import get_asset, get_connection, insert_event, transaction

EXPORT_VERSION = "export-v1"

STAGE = "EXPORT"
DERIVATIVE_STAGE = "DERIVATIVE"

# Исходы операции.
CREATED = "CREATED"
UNCHANGED = "UNCHANGED"
FAILED = "FAILED"

# Отказы (EXPORT/FAILED, §3 шаг 1, §4.4, §5).
SOURCE_CHANGED = "SOURCE_CHANGED"
NOT_READY = "NOT_READY"
STALE = "STALE"
COLOR_SPACE_UNDECLARED = "COLOR_SPACE_UNDECLARED"
METADATA_NOT_CLEAN = "METADATA_NOT_CLEAN"
TITLE_TOO_LONG_FOR_PROFILE = "TITLE_TOO_LONG_FOR_PROFILE"              # <FIELD>_TOO_LONG_FOR_PROFILE
DESCRIPTION_TOO_LONG_FOR_PROFILE = "DESCRIPTION_TOO_LONG_FOR_PROFILE"
TITLE_EMPTY = "TITLE_EMPTY"
KEYWORDS_OUT_OF_RANGE = "KEYWORDS_OUT_OF_RANGE"
RESOLUTION_TOO_LOW = "RESOLUTION_TOO_LOW"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
VIEW_UNAVAILABLE = "VIEW_UNAVAILABLE"
VERIFY_FAILED = "VERIFY_FAILED"

# Статусы для export.get / asset_state (§2).
READY_FOR_EXPORT = "ready_for_export"
EXPORT_STALE = "stale"
NOT_PREPARED = "not_prepared"

_PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
# sRGB назначается профилем площадки (не копируется из source). Файл зафиксирован: профиль
# LittleCMS с датой в заголовке — сгенерированный заново, он давал бы другой hash экспорта.
SRGB_ICC = (_PROFILES_DIR / "srgb.icc").read_bytes()
SRGB_ICC_SHA256 = "551b26472636a62b293e5855bfc8a85f74dbd973b0d0c8889e74d3e7bece4bcc"
if hashlib.sha256(SRGB_ICC).hexdigest() != SRGB_ICC_SHA256:  # pragma: no cover — повреждённый профиль
    raise RuntimeError("app/profiles/srgb.icc changed: export would not be reproducible")


class ExportError(Exception):
    """Операция невозможна (не отказ экспорта): неизвестный asset / площадка."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ExportRefused(Exception):
    """Отказ экспорта — пишется событием EXPORT/FAILED с кодом."""

    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code = code
        self.details = details


# --- Профиль площадки (§5): тот же, что у Readiness, плюс параметры файла -----------------

@dataclass(frozen=True)
class ExportProfile:
    platform: str
    version: str                   # = версия профиля Readiness (один профиль на Readiness и Export)
    min_mp: float
    max_mp: float | None
    max_file_size: int
    format: str
    extension: str
    color_space: str
    jpeg_quality: int
    jpeg_quality_min: int          # нижняя граница fit_file_size
    jpeg_subsampling: str
    text_field: str                # текстовое поле площадки в файле: title (Adobe) / description (Shutterstock)
    title_max: int                 # жёсткий лимит текстового поля → <FIELD>_TOO_LONG_FOR_PROFILE
    title_recommended: int | None  # рекомендация → предупреждение <FIELD>_LONG
    keywords_min: int
    keywords_max: int
    filename_max: int              # имя файла целиком, с расширением
    metadata_whitelist: frozenset
    sources: tuple = field(default=())
    # Параметры второй площадки; по умолчанию — поведение Adobe. В spec() входят, только если
    # отличаются от умолчания: отпечатки существующих экспортов Adobe не меняются.
    xmp_text: str = "dc:title"                 # свойство XMP текстового поля
    text_no_commas_warning: bool = True        # Adobe CSV: title без запятых
    text_min_words: int | None = None          # меньше слов -> предупреждение <FIELD>_SHORT
    text_recommended_code: str | None = None   # код предупреждения о рекомендации (по умолчанию <FIELD>_LONG)

    @property
    def name(self) -> str:
        return self.version

    def spec(self) -> dict:
        """Всё, от чего зависит файл, — входит в inputs_fingerprint."""
        return {
            "version": self.version, "min_mp": self.min_mp, "max_mp": self.max_mp,
            "max_file_size": self.max_file_size, "format": self.format, "color_space": self.color_space,
            "jpeg_quality": self.jpeg_quality, "jpeg_quality_min": self.jpeg_quality_min,
            "jpeg_subsampling": self.jpeg_subsampling, "text_field": self.text_field, "title_max": self.title_max,
            "keywords_min": self.keywords_min, "keywords_max": self.keywords_max,
            "filename_max": self.filename_max, "metadata_whitelist": sorted(self.metadata_whitelist),
            "icc_sha256": SRGB_ICC_SHA256,
            **{key: value for key, value, default in (
                ("xmp_text", self.xmp_text, "dc:title"),
                ("text_no_commas_warning", self.text_no_commas_warning, True),
                ("text_min_words", self.text_min_words, None),
                ("text_recommended_code", self.text_recommended_code, None),
            ) if value != default},
        }


_READINESS_ADOBE = rd.PROFILES["adobe"]

ADOBE = ExportProfile(
    platform="adobe",
    version=_READINESS_ADOBE.version,              # adobe-2026-09
    min_mp=_READINESS_ADOBE.min_mp,                # 4 MP
    max_mp=_READINESS_ADOBE.max_mp,                # 100 MP
    max_file_size=_READINESS_ADOBE.max_file_size,  # 45 MB
    format="JPEG",
    extension=".jpg",
    color_space="srgb",
    jpeg_quality=95,
    jpeg_quality_min=90,
    jpeg_subsampling="4:4:4",
    text_field="title",                            # description Adobe-профиль в файл не пишет
    title_max=200,                                 # жёсткий лимит поля title в Contributor Portal
    title_recommended=70,                          # «ideally under 70 characters»; CSV: до 70, без запятых
    keywords_min=7,                                # как Gate (TOO_FEW_KEYWORDS)
    keywords_max=49,                               # «up to 49» (CSV допускает 50 — берётся строже)
    filename_max=30,                               # CSV: Filename 30 characters maximum, с расширением
    metadata_whitelist=frozenset({"segment:APP2:ICC", "segment:APP1:XMP", "xmp:dc:title", "xmp:dc:subject"}),
    sources=(
        {"what": "4–100 MP, ≤ 45 MB, JPEG with sRGB color profile",
         "url": "https://helpx.adobe.com/stock/contributor/submit-your-content/submit-photos/technical-legal-requirements-photo-submission.html",
         "checked_at": "2026-09-30"},
        {"what": "title ideally under 70 characters; up to 49 keywords",
         "url": "https://helpx.adobe.com/stock/contributor/content-policies-guidelines/metadata/tips-effective-titles-keywords.html",
         "checked_at": "2026-09-30"},
        {"what": "CSV: Filename ≤ 30 characters incl. extension; Title ≤ 70, no commas; Keywords ≤ 50",
         "url": "https://helpx.adobe.com/stock/contributor/manage-your-portfolio/csv-requirements-content.html",
         "checked_at": "2026-09-30"},
        {"what": "title hard limit 200 characters (Contributor Portal); not stated on current helpx pages — "
                 "secondary sources; equals the Stocker / Gate limit",
         "url": None, "checked_at": "2026-09-30"},
    ),
)

_READINESS_SHUTTERSTOCK = rd.PROFILES["shutterstock"]

SHUTTERSTOCK = ExportProfile(
    platform="shutterstock",
    version=_READINESS_SHUTTERSTOCK.version,              # shutterstock-2026-09
    min_mp=_READINESS_SHUTTERSTOCK.min_mp,                # 4 MP
    max_mp=_READINESS_SHUTTERSTOCK.max_mp,                # None: для фото верхнего предела нет (только EPS ≤ 25 MP)
    max_file_size=_READINESS_SHUTTERSTOCK.max_file_size,  # 50 MB (JPEG, веб и FTPS)
    format="JPEG",                                        # TIFF допускается, но JPEG рекомендован
    extension=".jpg",
    color_space="srgb",
    jpeg_quality=95,
    jpeg_quality_min=90,
    jpeg_subsampling="4:4:4",
    text_field="description",                             # «Description» — единственное текстовое поле
    title_max=2048,                                       # поле Description в портале: лимит 2048 (пробная загрузка 30.09.2026)
    title_recommended=150,                                # help center: «150 character limit» — рекомендация, не отказ
    keywords_min=7,                                       # «7-50 keywords»
    keywords_max=50,
    filename_max=30,                                      # лимит Shutterstock не найден — как у Adobe (одно имя на площадки)
    metadata_whitelist=frozenset({"segment:APP2:ICC", "segment:APP1:XMP", "xmp:dc:description", "xmp:dc:subject"}),
    xmp_text="dc:description",
    text_no_commas_warning=False,                         # правило CSV Adobe, не Shutterstock
    text_min_words=_READINESS_SHUTTERSTOCK.text_min_words,  # 5 — портал: минимум 5 слов; как Readiness (предупреждение)
    text_recommended_code="DESCRIPTION_LONG_FOR_RECOMMENDATION",
    sources=(
        {"what": "JPEG (TIFF accepted), sRGB recommended, >= 4 MP, JPEG <= 50 MB (web / FTPS)",
         "url": "https://submit.shutterstock.com/help/en/articles/10617390-what-are-the-technical-requirements-for-images",
         "checked_at": "2026-09-30"},
        {"what": "Contributor Portal UI (trial upload of prod #13): Description limit 2048 characters, minimum 5 words; "
                 "keywords 28/50, minimum 7; XMP dc:description and dc:subject were picked up from the file",
         "url": None, "checked_at": "2026-09-30"},
        {"what": "Help center: Description 150 character limit (recommendation; the portal accepts 2048); "
                 "at least 7 keywords; 1 category required, 2nd optional",
         "url": "https://submit.shutterstock.com/help/en/articles/10617414-portfolio-preparing-your-uploaded-content-for-submission",
         "checked_at": "2026-09-30"},
        {"what": "7-50 keywords; JPEG / TIFF >= 4 MP; files under 50 MB via web upload",
         "url": "https://submit.shutterstock.com/help/en/articles/10594645-how-do-i-submit-photos-for-review",
         "checked_at": "2026-09-30"},
        {"what": "embedded titles and keywords (Adobe Bridge, Lightroom, Photo Mechanic) are supported; which XMP / "
                 "IPTC fields are read is not stated on help pages — confirmed by the trial upload (XMP dc:description / dc:subject)",
         "url": "https://submit.shutterstock.com/help/en/articles/10594594-can-i-sell-my-work-on-sites-other-than-shutterstock",
         "checked_at": "2026-09-30"},
    ),
)

PROFILES = {profile.platform: profile for profile in (ADOBE, SHUTTERSTOCK)}


# --- Инвентарь JPEG (§4.4): всё, кроме пикселей --------------------------------------------
# Разбор файла — зона source_facts (архитектурная граница: декодеры — только normalizer,
# source_facts, ingest). Экспорт своих разборов не держит.
inventory = source_facts.metadata_inventory


# --- Metadata файла (§4.2): собираются заново, только белый список --------------------------

def build_xmp(title: str, keywords: list[str], text_property: str = "dc:title") -> bytes:
    """XMP только с текстовым полем профиля (dc:title / dc:description) и dc:subject (IPTC Core).
    Детерминированно: без дат и идентификаторов."""
    items = "".join(f"<rdf:li>{escape(k)}</rdf:li>" for k in keywords)
    packet = (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<{text_property}><rdf:Alt><rdf:li xml:lang="x-default">{escape(title)}</rdf:li></rdf:Alt></{text_property}>'
        f"<dc:subject><rdf:Bag>{items}</rdf:Bag></dc:subject>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
        '<?xpacket end="r"?>'
    )
    return packet.encode("utf-8")


def read_xmp_fields(path: Path) -> dict:
    """dc:title / dc:description (что есть) и dc:subject готового файла — для сверки со снимком (шаг 8)."""
    from xml.etree import ElementTree

    ns = {"rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#", "dc": "http://purl.org/dc/elements/1.1/"}
    packets = source_facts.xmp_packets(path)
    if len(packets) != 1:
        return {"title": None, "keywords": [], "packets": len(packets)}
    xml = re.sub(r"<\?xpacket[^>]*\?>", "", packets[0].decode("utf-8"))
    root = ElementTree.fromstring(xml)
    fields = {}
    for name in ("title", "description"):
        node = root.find(f".//dc:{name}/rdf:Alt/rdf:li", ns)
        if node is not None:
            fields[name] = node.text
    if not fields:
        fields["title"] = None
    return {**fields, "keywords": [li.text for li in root.findall(".//dc:subject/rdf:Bag/rdf:li", ns)]}


# --- Имя файла (§3.1) ---------------------------------------------------------------------

_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
     "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"],
))
# Стоп-слова и запрещённые в имени метки (§3.1): камеры / шаблоны телефонов, программы, рабочие пометки.
STOP_WORDS = frozenset({
    "photo", "photos", "image", "images", "picture", "pic", "stock", "jpg", "jpeg",
    # служебные слова английского title: в 30 символах имени места для них нет
    "a", "an", "the", "of", "with", "and", "in", "on", "at", "for", "to", "by", "from",
})
FORBIDDEN_TOKENS = frozenset({
    "img", "pxl", "dsc", "dscn", "dcim", "mvimg", "mv", "topaz", "photoshop", "ps", "lightroom", "lr",
    "gigapixel", "ai", "edited", "edit", "final", "copy", "standard", "stocker",
})
# Версии (v2), final2 и чисто цифровые токены: нумерация шаблонов (IMG_001), даты, серийные номера.
_FORBIDDEN_PATTERN = re.compile(r"^(v\d+|final\d+|\d+)$")


def _allowed_token(token: str) -> bool:
    return token not in STOP_WORDS and token not in FORBIDDEN_TOKENS and not _FORBIDDEN_PATTERN.match(token)


def slug(text: str) -> list[str]:
    folded = "".join(_TRANSLIT.get(c, c) for c in text.lower())
    folded = unicodedata.normalize("NFKD", folded).encode("ascii", "ignore").decode("ascii")
    return [t for t in re.split(r"[^a-z0-9]+", folded) if t and _allowed_token(t)]


def export_filename(title: str, asset_id: int, profile: ExportProfile) -> str:
    """<slug title>_<asset_id><ext>; суффикс сохраняется всегда, обрезается только slug (по словам)."""
    suffix = f"_{asset_id}{profile.extension}"
    limit = profile.filename_max - len(suffix)
    base = ""
    for token in slug(title):
        candidate = f"{base}_{token}" if base else token
        if len(candidate) > limit:
            break
        base = candidate
    if not base:
        first = next(iter(slug(title)), "untitled")
        base = first[:limit] if limit > 0 else ""
    return f"{base}{suffix}" if base else f"{asset_id}{profile.extension}"


def filename_problems(name: str, asset_id: int, profile: ExportProfile) -> list[str]:
    problems = []
    if len(name) > profile.filename_max:
        problems.append(f"filename longer than {profile.filename_max}")
    if not name.endswith(f"_{asset_id}{profile.extension}") and name != f"{asset_id}{profile.extension}":
        problems.append("asset_id suffix missing")
    stem = name[: -len(profile.extension)]
    tokens = stem.split("_")[:-1]
    bad = [t for t in tokens if not re.fullmatch(r"[a-z0-9]+", t) or not _allowed_token(t)]
    if bad:
        problems.append(f"forbidden tokens: {bad}")
    return problems


# --- Пиксели и кодирование (§3 шаги 2–5) -----------------------------------------------------

def _fit_resolution(image: Image.Image, profile: ExportProfile) -> tuple[Image.Image, list[str]]:
    width, height = image.size
    megapixels = width * height / 1_000_000
    if megapixels < profile.min_mp:
        raise ExportRefused(RESOLUTION_TOO_LOW, f"{megapixels:.2f} MP < {profile.min_mp:g} MP (no upscaling)")
    if profile.max_mp and megapixels > profile.max_mp:
        scale = math.sqrt(profile.max_mp * 1_000_000 / (width * height))
        size = (max(1, math.floor(width * scale)), max(1, math.floor(height * scale)))
        while size[0] * size[1] > profile.max_mp * 1_000_000:
            size = (size[0] - 1, size[1] - 1)
        return image.resize(size, Image.Resampling.LANCZOS), [f"downscale_to_mp:{profile.max_mp:g}"]
    return image, []


_SUBSAMPLING = {"4:4:4": 0, "4:2:2": 1, "4:2:0": 2}


def _encode(image: Image.Image, profile: ExportProfile, xmp: bytes) -> tuple[bytes, int]:
    """JPEG с ICC и XMP профиля; fit_file_size — ступени качества до минимума профиля."""
    for quality in range(profile.jpeg_quality, profile.jpeg_quality_min - 1, -1):
        buffer = io.BytesIO()
        # optimize=False: при optimize Pillow ограничивает буфер w×h байт — детальный кадр в 4:4:4 его
        # превышает («Suspension not allowed»). Потоковое кодирование — без ограничения, те же пиксели.
        image.save(buffer, "JPEG", quality=quality, subsampling=_SUBSAMPLING[profile.jpeg_subsampling],
                   optimize=False, progressive=False, icc_profile=SRGB_ICC, xmp=xmp)
        data = buffer.getvalue()
        if len(data) <= profile.max_file_size:
            return data, quality
    raise ExportRefused(FILE_TOO_LARGE, f"{len(data)} bytes at quality {profile.jpeg_quality_min} > {profile.max_file_size}")


def render(image: Image.Image, profile: ExportProfile, title: str, keywords: list[str],
           color_converted: bool = True) -> tuple[bytes, dict]:
    """Пиксели view (RGB, sRGB, ориентированы) → байты файла площадки. Без ввода-вывода.

    color_converted — были ли пиксели переведены в sRGB (view): иначе source уже sRGB и
    ICC профиля только назначается (assign_icc), пиксели не меняются.
    """
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.info = {}  # никаких данных source: только пиксели
    sized, operations = _fit_resolution(image, profile)
    data, quality = _encode(sized, profile, build_xmp(title, keywords, profile.xmp_text))
    color = f"to_{profile.color_space}" if color_converted else f"assign_icc:{profile.color_space}"
    operations = [color, *operations, f"encode_jpeg:q{quality}", "strip_embedded", "strip_metadata", "write_xmp"]
    return data, {"width": sized.size[0], "height": sized.size[1], "quality": quality, "operations": operations}


# --- Проверка готового файла (§3 шаг 8, §4.4) -------------------------------------------------

def verify(path: Path, profile: ExportProfile, title: str, keywords: list[str], expected_size: tuple[int, int]) -> dict:
    """Повторный разбор файла: формат, размер, цвет, ориентация, metadata против белого списка."""
    found = inventory(path)
    extra = sorted(found - profile.metadata_whitelist)
    missing = sorted(profile.metadata_whitelist - found)
    audit = {"inventory": sorted(found), "whitelist": sorted(profile.metadata_whitelist),
             "not_allowed": extra, "missing": missing}
    if extra:
        raise ExportRefused(METADATA_NOT_CLEAN, f"fields outside the {profile.version} whitelist: {extra}", metadata_audit=audit)
    fields = read_xmp_fields(path)
    field_name = profile.xmp_text.split(":", 1)[1]
    if fields.get(field_name) != title or fields["keywords"] != list(keywords):
        raise ExportRefused(METADATA_NOT_CLEAN, f"embedded {field_name} / keywords differ from the export plan snapshot",
                            metadata_audit=audit)
    facts = source_facts.read_facts(Path(path))  # файл разбирается заново — теми же фактами, что и source
    icc = facts["color_profile"].get("icc") or {}
    problems = []
    if facts["format"] != profile.format:
        problems.append(f"format {facts['format']}")
    if (facts["color_mode"], facts["bit_depth"], facts["has_alpha"]) != ("RGB", 8, False):
        problems.append(f"mode {facts['color_mode']} / {facts['bit_depth']} bit / alpha {facts['has_alpha']}")
    if (facts["width"], facts["height"]) != tuple(expected_size):
        problems.append(f"size {facts['width']}x{facts['height']} != {tuple(expected_size)}")
    if facts["orientation_raw"] not in (None, 1):
        problems.append(f"orientation tag {facts['orientation_raw']} (must be applied to pixels)")
    if icc.get("sha256") != SRGB_ICC_SHA256 or facts["color_profile"]["kind"] != profile.color_space:
        problems.append("ICC is not the profile's sRGB")
    if facts["embedded"]["motion_video"] or facts["embedded"]["auxiliary"] or facts["mpf"] or facts["frames"]["readable"] != 1:
        problems.append("embedded images / video present")
    size_bytes = Path(path).stat().st_size
    megapixels = expected_size[0] * expected_size[1] / 1_000_000
    if size_bytes > profile.max_file_size:
        problems.append(f"{size_bytes} bytes > {profile.max_file_size}")
    if megapixels < profile.min_mp or (profile.max_mp and megapixels > profile.max_mp):
        problems.append(f"{megapixels:.2f} MP outside {profile.min_mp:g}–{profile.max_mp or 'no limit'}")
    if problems or missing:
        raise ExportRefused(VERIFY_FAILED, "; ".join(problems + [f"missing {missing}"] * bool(missing)), metadata_audit=audit)
    audit["xmp"] = {profile.xmp_text: {"length": len(title)}, "dc:subject": {"count": len(keywords)}}
    return audit


def prepare_file(source: Path, profile: ExportProfile, out_dir: Path | None = None, title: str = "Test export title",
                 keywords: list[str] | None = None) -> Path:
    """Файл площадки из незарегистрированного файла (тесты / dry-run): те же view, render и verify."""
    keywords = keywords or [f"keyword{i}" for i in range(profile.keywords_min)]
    view = analysis_view.view_of_file(source)
    if view.color_space != profile.color_space:
        raise ExportRefused(COLOR_SPACE_UNDECLARED, f"view color space is {view.color_space}")
    data, info = render(view.full, profile, title, keywords, view.color_converted)
    target = Path(out_dir or Path(source).parent) / f"export_{profile.platform}_{Path(source).stem}.jpg"
    target.write_bytes(data)
    verify(target, profile, title, keywords, (info["width"], info["height"]))
    return target


# --- Входы из БД ---------------------------------------------------------------------------

def _json(value):
    try:
        return json.loads(value) if value else None
    except (json.JSONDecodeError, TypeError):
        return None


def _events(asset_id: int) -> list[dict]:
    connection = get_connection()
    try:
        rows = connection.execute(
            "SELECT id, stage, status, message FROM processing_events WHERE asset_id = ? ORDER BY id", (asset_id,)
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _last(events: list[dict], stage: str, status: str, **match) -> dict | None:
    for event in reversed(events):
        if event["stage"] == stage and event["status"] == status:
            message = _json(event["message"]) or {}
            if all(message.get(k) == v for k, v in match.items()):
                return event
    return None


def _profile(platform: str) -> ExportProfile:
    if platform not in PROFILES:
        raise ExportError("UNKNOWN_PLATFORM", f"No export profile for platform '{platform}' (available: {sorted(PROFILES)})")
    return PROFILES[platform]


def _snapshot(plan: dict, profile: ExportProfile) -> dict:
    categories = plan["category"] if "category" in plan else plan.get("categories")
    return {"text": plan.get(profile.text_field) or "", "keywords": list(plan.get("keywords") or []),
            "category": categories}


def inputs(asset: dict, readiness_fp: str | None, snapshot: dict, profile: ExportProfile) -> dict:
    """Входы inputs_fingerprint (§6) — только данные БД, без чтения файла."""
    return {
        "export_version": EXPORT_VERSION,
        "profile": profile.spec(),
        "readiness_fingerprint": readiness_fp,
        "view": normalizer.view_fingerprint(asset["file_hash"]),  # source SHA256 + нормализатор + views
        "normalizer_version": normalizer.NORMALIZER_VERSION,
        "snapshot": {profile.text_field: snapshot["text"], "keywords": snapshot["keywords"]},  # Adobe: «title»
    }


def fingerprint(inputs_: dict) -> str:
    payload = json.dumps(inputs_, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _readiness_plan(events: list[dict], platform: str) -> tuple[str | None, dict | None]:
    event = _last(events, "READINESS", "EVALUATED")
    stored = _json(event["message"]) if event else None
    if not stored:
        return None, None
    platform_result = (stored.get("platforms") or {}).get(platform) or {}
    return stored.get("fingerprint"), platform_result.get("export_plan") if platform_result.get("status") == rd.READY else None


def _current_derivative(asset: dict, events: list[dict], profile: ExportProfile, stages: dict) -> dict:
    """Статус файла площадки по БД и stat (без хеширования): ready_for_export / stale / not_prepared."""
    event = _last(events, DERIVATIVE_STAGE, "CREATED", purpose="export", platform=profile.platform)
    if event is None:
        return {"status": NOT_PREPARED, "reason": "NOT_PREPARED"}
    created = _json(event["message"])
    base = {"event_id": event["id"], "path": created["path"], "sha256": created["sha256"]}
    readiness_fp, plan = _readiness_plan(events, profile.platform)
    publication = stages["publication"]
    if stages["readiness"]["status"] != "current" or plan is None or publication["status"] != "current" \
            or profile.platform not in (publication.get("approved_for") or []):
        return {**base, "status": EXPORT_STALE, "reason": "NOT_READY_OR_NOT_APPROVED"}
    current = fingerprint(inputs(asset, readiness_fp, _snapshot(plan, profile), profile))
    if created.get("inputs_fingerprint") != current:
        return {**base, "status": EXPORT_STALE, "reason": "FINGERPRINT_CHANGED"}
    path = ingest.ROOT / created["path"]
    if not path.exists() or path.stat().st_size != created["size_bytes"]:
        return {**base, "status": EXPORT_STALE, "reason": "FILE_MISSING_OR_CHANGED"}
    return {**base, "status": READY_FOR_EXPORT, "reason": None}


def stage_status(asset: dict, events: list[dict], stages: dict) -> dict:
    """Стадия export для asset_state: применима только к одобренным Publication площадкам."""
    publication = stages["publication"]
    approved = publication.get("approved_for") or [] if publication["status"] == "current" else []
    has_result = _last(events, DERIVATIVE_STAGE, "CREATED", purpose="export") is not None
    targets = [p for p in approved if p in PROFILES]
    if not targets:
        return {"status": "not_applicable", "reason": "PUBLICATION_NOT_APPROVED", "has_result": has_result}
    platforms = {p: _current_derivative(asset, events, PROFILES[p], stages) for p in targets}
    ready = [p for p, s in platforms.items() if s["status"] == READY_FOR_EXPORT]
    stale = [p for p, s in platforms.items() if s["status"] == EXPORT_STALE]
    if stale:
        return {"status": "stale", "reason": f"FILE_STALE:{','.join(stale)}", "platforms": platforms, "ready_for_export": ready}
    if not ready:
        return {"status": "missing", "reason": "NOT_PREPARED", "platforms": platforms, "ready_for_export": []}
    return {"status": "current", "reason": None, "platforms": platforms, "ready_for_export": ready}


# --- Операции -----------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tools() -> dict:
    return {"export": EXPORT_VERSION, "pillow": PIL.__version__, "libjpeg": features.version_codec("jpg"),
            "normalizer": normalizer.NORMALIZER_VERSION, "view": normalizer.VIEW_VERSION}


def _refuse(asset_id: int, platform: str, profile_version: str, exc: ExportRefused) -> dict:
    message = {"platform": platform, "profile": profile_version, "code": exc.code, "error": str(exc)[:500],
               **exc.details, "export_version": EXPORT_VERSION}
    with transaction() as connection:
        insert_event(connection, asset_id, STAGE, "FAILED", json.dumps(message, ensure_ascii=False))
    return {"asset_id": asset_id, "platform": platform, "outcome": FAILED, "refused": {"code": exc.code, "error": str(exc)},
            "export": None}


def _check_inputs(asset_id: int, profile: ExportProfile) -> tuple[dict, dict, str, dict]:
    """Шаг 1: SHA256 source, актуальные Readiness ready и Publication approved для площадки."""
    from app import asset_state

    state = asset_state.get(asset_id, verify_source=True)
    stages = state["stages"]
    if stages["source"]["status"] != "ok":
        raise ExportRefused(SOURCE_CHANGED, f"Source is '{stages['source']['status']}' ({stages['source']['reason']})")
    for name in ("readiness", "publication"):
        if stages[name]["status"] == "stale":
            raise ExportRefused(STALE, f"{name} is stale ({stages[name]['reason']})")
    readiness = stages["readiness"]
    if readiness["status"] != "current" or profile.platform not in (readiness.get("ready_for") or []):
        raise ExportRefused(NOT_READY, f"Readiness is not ready for {profile.platform} "
                                       f"({readiness['status']}: {readiness.get('reason')})")
    publication = stages["publication"]
    if publication["status"] != "current" or profile.platform not in (publication.get("approved_for") or []):
        raise ExportRefused(NOT_READY, f"Publication Gate has not approved {profile.platform} "
                                       f"({publication['status']}: {publication.get('reason')})")
    events = _events(asset_id)
    readiness_fp, plan = _readiness_plan(events, profile.platform)
    if plan is None:
        raise ExportRefused(NOT_READY, f"No export plan for {profile.platform} in the current Readiness result")
    return state, plan, readiness_fp, {"events": events}


def _check_snapshot(snapshot: dict, profile: ExportProfile) -> list[dict]:
    """Title / keywords снимка против профиля. Жёсткое — отказ; рекомендации — предупреждения. Ничего не меняет."""
    text, keywords = snapshot["text"], snapshot["keywords"]
    field_code = profile.text_field.upper()  # TITLE (Adobe) / DESCRIPTION (Shutterstock)
    if not text.strip():
        raise ExportRefused(f"{field_code}_EMPTY", f"{profile.text_field} is empty")
    if len(text) > profile.title_max:
        raise ExportRefused(f"{field_code}_TOO_LONG_FOR_PROFILE",
                            f"{profile.text_field} is {len(text)} characters (max {profile.title_max}); not truncated")
    if not profile.keywords_min <= len(keywords) <= profile.keywords_max:
        raise ExportRefused(KEYWORDS_OUT_OF_RANGE, f"{len(keywords)} keywords ({profile.keywords_min}–{profile.keywords_max})")
    warnings = []
    if profile.title_recommended and len(text) > profile.title_recommended:
        warnings.append({"code": profile.text_recommended_code or f"{field_code}_LONG",
                         "message": f"{profile.text_field} is {len(text)} characters (recommended <= {profile.title_recommended})"})
    if profile.text_no_commas_warning and "," in text:
        warnings.append({"code": f"{field_code}_HAS_COMMA",
                         "message": f"{profile.text_field} contains a comma (not allowed in Adobe CSV)"})
    if profile.text_min_words and len(text.split()) < profile.text_min_words:
        warnings.append({"code": f"{field_code}_SHORT",
                         "message": f"{profile.text_field} has {len(text.split())} words (recommended >= {profile.text_min_words})"})
    return warnings


def _write_verified(target: Path, data: bytes, check) -> dict:
    """Временный файл → fsync → проверка шага 8 → атомарная замена. Непроверенный файл не остаётся."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.tmp"
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        audit = check(temporary)
        os.replace(temporary, target)
        return audit
    finally:
        temporary.unlink(missing_ok=True)


def prepare(asset_id: int, platform: str = "adobe") -> dict:
    """export.prepare: один объект, одна площадка. Идемпотентно по inputs_fingerprint (UNCHANGED)."""
    profile = _profile(platform)
    asset = get_asset(asset_id)
    if asset is None:
        raise ExportError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")

    try:
        state, plan, readiness_fp, loaded = _check_inputs(asset_id, profile)
        snapshot = _snapshot(plan, profile)
        warnings = _check_snapshot(snapshot, profile)
        inputs_ = inputs(asset, readiness_fp, snapshot, profile)
        current = fingerprint(inputs_)

        last = _last(loaded["events"], DERIVATIVE_STAGE, "CREATED", purpose="export", platform=platform)
        created = _json(last["message"]) if last else None
        if created and created.get("inputs_fingerprint") == current:
            path = ingest.ROOT / created["path"]
            if path.exists() and ingest.sha256_file(path) == created["sha256"]:
                return {"asset_id": asset_id, "platform": platform, "outcome": UNCHANGED, "export": created}

        try:
            view = analysis_view.open_asset_view(asset_id)
        except analysis_view.ViewUnavailable as exc:
            code = STALE if exc.code == analysis_view.NORMALIZE_STALE else VIEW_UNAVAILABLE
            raise ExportRefused(code, f"{exc.code}: {exc}") from exc
        if view.color_space != profile.color_space:
            raise ExportRefused(COLOR_SPACE_UNDECLARED, f"source color is {view.color_space}; {profile.color_space} is not assumed")

        data, info = render(view.full, profile, snapshot["text"], snapshot["keywords"], view.color_converted)
        name = export_filename(snapshot["text"], asset_id, profile)
        bad_name = filename_problems(name, asset_id, profile)
        if bad_name:
            raise ExportRefused(METADATA_NOT_CLEAN, f"export filename {name!r}: {bad_name}")
        target = db.export_dir() / platform / str(asset_id) / name
        audit = _write_verified(target, data, lambda path: verify(
            path, profile, snapshot["text"], snapshot["keywords"], (info["width"], info["height"])))
    except ExportRefused as exc:
        return _refuse(asset_id, platform, profile.version, exc)

    source_facts = (_json((_last(loaded["events"], "NORMALIZE", "EVALUATED") or {}).get("message")) or {}).get("facts") or {}
    message = {
        "purpose": "export",
        "platform": platform,
        "profile": profile.version,
        "export_version": EXPORT_VERSION,
        "status": READY_FOR_EXPORT,
        "path": target.relative_to(ingest.ROOT).as_posix(),
        "filename": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "width": info["width"],
        "height": info["height"],
        "megapixels": round(info["width"] * info["height"] / 1_000_000, 2),
        "jpeg_quality": info["quality"],
        "source_sha256": asset["file_hash"],
        "source_derivative_id": None,
        "operations": info["operations"],
        "inputs_fingerprint": current,
        "inputs": {k: v for k, v in inputs_.items() if k != "profile"} | {"profile": profile.version},
        "metadata": {profile.text_field: snapshot["text"], "keywords": snapshot["keywords"], "category": snapshot["category"],
                     f"{profile.text_field}_length": len(snapshot["text"]), "words": len(snapshot["text"].split()),
                     "keywords_count": len(snapshot["keywords"])},
        "warnings": warnings,
        "tools": tools(),
        "metadata_audit": audit,
        "source_view": {"color_space": view.color_space, "color_converted": view.color_converted,
                        "size": list(view.size), "source_format": source_facts.get("format")},
        "created_at": _now(),
    }
    with transaction() as connection:
        insert_event(connection, asset_id, DERIVATIVE_STAGE, "CREATED", json.dumps(message, ensure_ascii=False))
    return {"asset_id": asset_id, "platform": platform, "outcome": CREATED, "export": message}


def get(asset_id: int, platform: str = "adobe") -> dict:
    """export.get: статус ready_for_export / stale / not_prepared, файл, metadata_audit; последний отказ."""
    from app import asset_state

    profile = _profile(platform)
    asset = get_asset(asset_id)
    if asset is None:
        raise ExportError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")
    events = _events(asset_id)
    stages = asset_state.get(asset_id)["stages"]
    status = _current_derivative(asset, events, profile, stages)
    event = _last(events, DERIVATIVE_STAGE, "CREATED", purpose="export", platform=platform)
    failed = _last(events, STAGE, "FAILED", platform=platform)
    last_failure = None
    if failed and (event is None or failed["id"] > event["id"]):
        stored = _json(failed["message"]) or {}
        last_failure = {"event_id": failed["id"], "code": stored.get("code"), "error": stored.get("error")}
    created = _json(event["message"]) if event else None
    current = status["status"] == READY_FOR_EXPORT
    from app import outbox  # партия к загрузке (export.collect), если текущий файл уже собран

    return {
        "asset_id": asset_id,
        "platform": platform,
        "profile": profile.version,
        "status": status["status"],
        "reason": status.get("reason"),
        "export": {**created, "event_id": event["id"]} if current else None,
        **({"last_export": {**created, "current": False}} if created and not current else {}),
        "last_failure": last_failure,
        "collected": outbox.collected(asset_id, platform, created["sha256"]) if current else None,
    }
