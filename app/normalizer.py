"""
Internal Normalization Engine (docs/INTERNAL_IMAGE_REPRESENTATION_CONTRACT.md, normalize-v1).

Три части:
- plan(facts)            — чистое решение по фактам: representation = source | derivative
                           или отказ с причиной (§3.1, §7); ничего не читает и не пишет;
- build_derivative(...)  — внутренний lossless derivative (AVIF / HEIF): только декодирование,
                           поворот по EXIF, перенос в PNG без потерь, ICC source без изменений;
                           запись атомарна: временный файл → fsync → адресное имя (§8);
- open_view(...)         — analysis view в памяти (§4): уменьшение, 8 бит, альфа на белом,
                           цвет в sRGB только если он объявлен; undeclared — без конвертации.
                           Стадии получают view через app/analysis_view.py, а не отсюда.

ICC (§4a): ICC источника — факт (facts.color_profile.icc). Representation без
конвертации пикселей несёт ICC источника байт в байт; конвертированные пиксели
обязаны нести профиль своего фактического пространства (check_color). View в
памяти ICC не несёт: его пространство — AnalysisView.color_space.

Никаких улучшений (§5): ни sharpening, ни denoise, ни upscaling, ни AI. Файлы source не
меняются. События пишет app/normalization.py.
"""

import hashlib
import io
import json
import os
from pathlib import Path

import PIL
from PIL import Image, ImageCms, ImageOps

NORMALIZER_VERSION = "normalize-v1"
# Версия кода analysis views (§4): входит в fingerprint стадий, читающих пиксели.
VIEW_VERSION = "analysis-view-v1"

PARAMS = {
    "representation": {"source": ["JPEG", "PNG", "TIFF"], "derivative": ["AVIF", "HEIF"]},
    "derivative_format": "png",
    "views": {"full": None, "preview": 2048, "overview": 1536},
    "resample": "lanczos",
    "alpha_background": [255, 255, 255],
    "rendering_intent": "perceptual",
    # 2 (28.09.2026): манифест описывает цвет representation (§4a).
    "manifest_version": 2,
}
PARAMS_HASH = "sha256:" + hashlib.sha256(json.dumps(PARAMS, sort_keys=True).encode()).hexdigest()

SOURCE = "source"
DERIVATIVE = "derivative"

# Отказы (§7): коды NORMALIZE/FAILED.
MISSING_CODEC = "MISSING_CODEC"
UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
UNSUPPORTED_HDR = "UNSUPPORTED_HDR"
UNSUPPORTED_BIT_DEPTH = "UNSUPPORTED_BIT_DEPTH"
MULTI_FRAME_UNSUPPORTED = "MULTI_FRAME_UNSUPPORTED"
COLOR_SPACE_UNDECLARED = "COLOR_SPACE_UNDECLARED"
COLOR_CONVERSION_UNSUPPORTED = "COLOR_CONVERSION_UNSUPPORTED"
DECODE_ERROR = "DECODE_ERROR"

_DECODABLE = {"JPEG", "PNG", "TIFF", "AVIF"}  # HEIF — нет кодека в runtime (решение 28.09.2026)


class NormalizationRefused(Exception):
    """Безопасная нормализация невозможна; code — причина NORMALIZE/FAILED (§7)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def fingerprint(source_sha256: str) -> str:
    payload = json.dumps({"source": source_sha256, "version": NORMALIZER_VERSION, "params": PARAMS_HASH}, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def view_fingerprint(source_sha256: str | None) -> str:
    """Отпечаток analysis view без чтения файла: representation + версия кода views."""
    payload = json.dumps({"normalizer": fingerprint(source_sha256), "view": VIEW_VERSION}, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def plan(facts: dict) -> dict:
    """Решение по фактам — без чтения файла. NormalizationRefused — отказ с причиной."""
    container = facts["container"]
    if container not in PARAMS["representation"]["source"] + PARAMS["representation"]["derivative"]:
        raise NormalizationRefused(UNSUPPORTED_FORMAT, f"container {container} is not supported")
    if container not in _DECODABLE:
        raise NormalizationRefused(MISSING_CODEC, f"no decoder for {container} in runtime")
    if facts["hdr"] in ("pq", "hlg"):
        raise NormalizationRefused(UNSUPPORTED_HDR, f"HDR transfer {facts['hdr']}: tone mapping for views is not specified")
    color = facts["color_profile"]
    if facts["color_mode"] == "CMYK" and not color["declared"]:
        raise NormalizationRefused(COLOR_SPACE_UNDECLARED, "CMYK without ICC: RGB view is impossible without a guessed profile")
    if color["declared"] and color["kind"] != "srgb":
        icc = color.get("icc")
        if not icc:
            # DCF Adobe RGB, nclx / cICP, gAMA/cHRM без ICC: профиль для конвертации не подбирается.
            raise NormalizationRefused(COLOR_CONVERSION_UNSUPPORTED, f"{color['kind']} declared by {color['source']} without ICC")
        if not icc.get("color_space"):
            raise NormalizationRefused(COLOR_CONVERSION_UNSUPPORTED, "ICC profile is not readable")
    frames = facts["frames"]
    if frames["readable"] > 1 and container != "JPEG":
        raise NormalizationRefused(MULTI_FRAME_UNSUPPORTED, f"{frames['readable']} readable frames: the primary photo is not known")

    representation = SOURCE if container in PARAMS["representation"]["source"] else DERIVATIVE
    if representation == DERIVATIVE and facts["bit_depth"] not in (8, None):
        # Декодер Pillow отдаёт 8 бит: производный файл потерял бы точность — не молча.
        raise NormalizationRefused(UNSUPPORTED_BIT_DEPTH, f"{facts['bit_depth']}-bit {container}: lossless derivative is not possible yet")
    return {
        "representation": representation,
        "transforms": [] if representation == SOURCE else [f"decode:{container.lower()}", "orient", "encode:png-lossless"],
        "preserved": {
            "orientation_raw": facts["orientation_raw"],
            "color": {k: color.get(k) for k in ("kind", "source", "declared", "icc")},
            "bit_depth": facts["bit_depth"],
            "has_alpha": facts["has_alpha"],
            "alpha_used": facts["alpha_used"],
            "frames": facts["frames"],
            "references": {"motion_video": facts["embedded"]["motion_video"], "mpf": facts.get("mpf"),
                           "auxiliary": facts["embedded"]["auxiliary"]},
        },
    }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_derivative(source: Path, target_dir: Path) -> dict:
    """
    Lossless derivative: декодирование, поворот по EXIF, PNG без потерь. Пиксели не
    конвертируются — поэтому ICC source переносится байт в байт и описывает derivative.
    Атомарно: временный файл → fsync → адресное имя <sha256>.png.
    """
    with Image.open(source) as image:
        icc = image.info.get("icc_profile")
        oriented = ImageOps.exif_transpose(image)
        mode = oriented.mode
        buffer = io.BytesIO()
        oriented.save(buffer, "PNG", icc_profile=icc) if icc else oriented.save(buffer, "PNG")
    data = buffer.getvalue()
    digest = _sha256(data)
    target_dir.mkdir(parents=True, exist_ok=True)
    final = target_dir / f"{digest}.png"
    if not final.exists():
        temporary = target_dir / f".{digest}.tmp"
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, final)
    with Image.open(final) as check:
        width, height = check.size
        embedded = check.info.get("icc_profile")
    return {"path": final, "sha256": digest, "size_bytes": len(data), "width": width, "height": height,
            "mode": mode, "icc_embedded": embedded is not None,
            "icc_sha256": _sha256(embedded) if embedded else None, "pixels_converted": False}


def representation_color(source_color: dict, derivative: dict | None) -> dict:
    """Цвет representation (§4a): что описывает сам файл, которым пользуются стадии."""
    source_icc = (source_color.get("icc") or {}).get("sha256")
    if derivative is None:
        return {"space": source_color["kind"], "declared": source_color["declared"], "icc_sha256": source_icc,
                "icc_is_source": source_icc is not None, "pixels_converted": False}
    return {"space": source_color["kind"], "declared": source_color["declared"], "icc_sha256": derivative["icc_sha256"],
            "icc_is_source": derivative["icc_sha256"] is not None and derivative["icc_sha256"] == source_icc,
            "pixels_converted": derivative["pixels_converted"]}


def check_color(source_color: dict, color: dict) -> None:
    """
    Инвариант §4a: нельзя одновременно считать ICC источника сохранённым байт в байт и
    пиксели — уже конвертированными. Нарушение — ошибка кода, события PASSED не будет.
    """
    source_icc = (source_color.get("icc") or {}).get("sha256")
    if color["pixels_converted"]:
        if color["icc_is_source"] or color["icc_sha256"] == source_icc:
            raise ValueError("converted pixels must not carry the source ICC")
        if color["space"] == source_color["kind"]:
            raise ValueError("converted pixels must declare their own color space")
    else:
        if color["icc_sha256"] != source_icc:
            raise ValueError("unconverted pixels must carry the source ICC unchanged (or none)")
        if color["space"] != source_color["kind"]:
            raise ValueError("unconverted pixels keep the source color space")


def decoder_versions() -> dict:
    return {"pillow": PIL.__version__}


# --- Analysis views (§4) -------------------------------------------------------------

_SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))


_ICC_MODES = {"RGB": "RGB", "CMYK": "CMYK", "GRAY": "L"}


def open_view(path: Path, variant: str, color: dict, representation: str) -> tuple[Image.Image, dict]:
    """
    View в памяти: ориентация (для source), цвет в sRGB только при объявленном цвете,
    8 бит, альфа на белом, только уменьшение. Никаких улучшений. ICC view не несёт.
    """
    if variant not in PARAMS["views"]:
        raise ValueError(f"unknown view {variant}")
    info = {"variant": variant, "color_converted": False, "color_space": "undeclared",
            "alpha_composited": False, "downscaled": False}
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened) if representation == SOURCE else opened.copy()
        icc = opened.info.get("icc_profile")

    if image.mode == "CMYK" and not color.get("declared"):
        # Как plan(): без профиля CMYK → RGB — догадка (защита для прямых вызовов).
        raise NormalizationRefused(COLOR_SPACE_UNDECLARED, "CMYK without ICC: RGB view is impossible without a guessed profile")
    if image.mode in ("I;16", "I;16B", "I;16L", "I"):
        image = image.point(lambda v: v / 256).convert("L")  # масштабирование, не обрезка

    if "A" in image.getbands() or image.mode == "P" and "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, tuple(PARAMS["alpha_background"]) + (255,))
        image = Image.alpha_composite(background, rgba).convert("RGB")
        info["alpha_composited"] = True

    if color.get("declared"):
        if color.get("kind") != "srgb" and not icc:
            # Объявлено не-sRGB (DCF Adobe RGB, nclx P3), но профиля для конвертации нет — не угадываем.
            raise NormalizationRefused(COLOR_CONVERSION_UNSUPPORTED, f"{color.get('kind')} declared by {color.get('source')} without ICC")
        info["color_space"] = "srgb"
        if icc and color.get("kind") != "srgb":
            source_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            space = source_profile.profile.xcolor_space.strip()
            if space not in _ICC_MODES:
                raise NormalizationRefused(COLOR_CONVERSION_UNSUPPORTED, f"ICC color space {space!r} is not supported")
            # Пиксели — в пространстве профиля (CMYK / L / RGB), затем в sRGB.
            image = ImageCms.profileToProfile(image.convert(_ICC_MODES[space]), source_profile, _SRGB,
                                              renderingIntent=ImageCms.Intent.PERCEPTUAL, outputMode="RGB")
            info["color_converted"] = True
    image = image.convert("RGB")
    image.info = {}  # ни ICC, ни EXIF источника: пространство view — info["color_space"]

    limit = PARAMS["views"][variant]
    if limit and max(image.size) > limit:
        image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
        info["downscaled"] = True
    return image, info
