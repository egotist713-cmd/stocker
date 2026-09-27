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

PARAMS = {
    "representation": {"source": ["JPEG", "PNG", "TIFF"], "derivative": ["AVIF", "HEIF"]},
    "derivative_format": "png",
    "views": {"full": None, "preview": 2048, "overview": 1536},
    "resample": "lanczos",
    "alpha_background": [255, 255, 255],
    "rendering_intent": "perceptual",
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


def plan(facts: dict) -> dict:
    """Решение по фактам — без чтения файла. NormalizationRefused — отказ с причиной."""
    container = facts["container"]
    if container not in PARAMS["representation"]["source"] + PARAMS["representation"]["derivative"]:
        raise NormalizationRefused(UNSUPPORTED_FORMAT, f"container {container} is not supported")
    if container not in _DECODABLE:
        raise NormalizationRefused(MISSING_CODEC, f"no decoder for {container} in runtime")
    if facts["hdr"] in ("pq", "hlg"):
        raise NormalizationRefused(UNSUPPORTED_HDR, f"HDR transfer {facts['hdr']}: tone mapping for views is not specified")
    if facts["color_mode"] == "CMYK" and not facts["color_profile"]["declared"]:
        raise NormalizationRefused(COLOR_SPACE_UNDECLARED, "CMYK without ICC: RGB view is impossible without a guessed profile")
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
            "color": {k: facts["color_profile"][k] for k in ("kind", "source", "declared")},
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
    Lossless derivative: декодирование, поворот по EXIF, PNG без потерь, ICC source без
    изменений. Атомарно: временный файл → fsync → адресное имя <sha256>.png.
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
    return {"path": final, "sha256": digest, "size_bytes": len(data), "width": width, "height": height,
            "mode": mode, "icc_embedded": icc is not None}


def decoder_versions() -> dict:
    return {"pillow": PIL.__version__}


# --- Analysis views (§4) -------------------------------------------------------------

_SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))


def open_view(path: Path, variant: str, color: dict, representation: str) -> tuple[Image.Image, dict]:
    """
    View в памяти: ориентация (для source), цвет в sRGB только при объявленном цвете,
    8 бит, альфа на белом, только уменьшение. Никаких улучшений.
    """
    if variant not in PARAMS["views"]:
        raise ValueError(f"unknown view {variant}")
    info = {"variant": variant, "color_converted": False, "color_space": "undeclared",
            "alpha_composited": False, "downscaled": False}
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened) if representation == SOURCE else opened.copy()
        icc = opened.info.get("icc_profile")

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
            image = ImageCms.profileToProfile(image.convert("RGB"), source_profile, _SRGB,
                                              renderingIntent=ImageCms.Intent.PERCEPTUAL, outputMode="RGB")
            info["color_converted"] = True
    image = image.convert("RGB")

    limit = PARAMS["views"][variant]
    if limit and max(image.size) > limit:
        image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
        info["downscaled"] = True
    return image, info
