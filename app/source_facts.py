"""
Факты об исходном файле (docs/FORMAT_CONTRACT.md §3.1, normalize-facts-v1).

Первый шаг Normalization: файл только читается и описывается — формат, цвет,
битность, HDR / Ultra HDR, Motion Photo, дополнительные потоки, размеры,
ориентация, исходное имя, наличие технических metadata. Ничего не меняется и
не конвертируется.

Правило «не угадывать»: признак фиксируется по фактическим данным файла
(например, видео Motion Photo — по реальному MP4-блоку, а не по пометке XMP);
то, что нельзя установить, — null. Значения GPS, серийных номеров и прочих
персональных данных не сохраняются — только признак наличия.
"""

import io
from pathlib import Path

from PIL import Image, ImageCms

from app import readiness as rd
from app.enhancement import jpeg_quality

FACTS_VERSION = "normalize-facts-v1"

# Первые байты контейнеров (для файлов, которые Pillow не открывает).
_SIGNATURES = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
)
_HEIF_BRANDS = {b"heic": "HEIF", b"heix": "HEIF", b"hevc": "HEIF", b"mif1": "HEIF", b"msf1": "HEIF", b"avif": "AVIF", b"avis": "AVIF"}
_MP4_BRANDS = (b"mp41", b"mp42", b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"qt  ", b"avc1", b"MSNV", b"dash")

_MODES = {
    "RGB": ("RGB", 8, False), "RGBA": ("RGB", 8, True), "RGBX": ("RGB", 8, False),
    "L": ("L", 8, False), "LA": ("L", 8, True), "1": ("L", 1, False),
    "P": ("P", 8, False), "PA": ("P", 8, True),
    "CMYK": ("CMYK", 8, False), "YCbCr": ("RGB", 8, False),
    "I;16": ("L", 16, False), "I;16B": ("L", 16, False), "I;16L": ("L", 16, False), "I": ("L", 32, False),
    "F": ("L", 32, False),
}

# Основные цвета (xy, после адаптации к D50) профиля Display P3 телефонов — измерены
# на реальном файле 26.09.2026; sRGB сравнивается с встроенным профилем Pillow.
_P3_RED_GREEN = ((0.682, 0.319), (0.285, 0.675))
_PRIMARY_TOLERANCE = 0.005

# EXIF: теги верхнего уровня и Exif IFD.
_TAG_MAKE, _TAG_MODEL, _TAG_SOFTWARE, _TAG_ORIENTATION = 0x010F, 0x0110, 0x0131, 0x0112
_IFD_EXIF, _IFD_GPS = 0x8769, 0x8825
_TAG_MAKERNOTE, _TAG_BODY_SERIAL, _TAG_LENS_SERIAL = 0x927C, 0xA431, 0xA435


def detect_container(head: bytes) -> str | None:
    for signature, name in _SIGNATURES:
        if head.startswith(signature):
            return name
    if head[4:8] == b"ftyp":
        return _HEIF_BRANDS.get(head[8:12])
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    return None


def _color_profile(icc: bytes | None) -> dict:
    kind, description = rd.color_profile_kind(icc)
    if kind == rd.OTHER and icc:
        try:
            profile = ImageCms.ImageCmsProfile(io.BytesIO(icc)).profile
            red, green = (tuple(profile.red_primary[1][:2]), tuple(profile.green_primary[1][:2]))
            if all(abs(a - b) <= _PRIMARY_TOLERANCE for pa, pb in zip((red, green), _P3_RED_GREEN) for a, b in zip(pa, pb)):
                kind = "display_p3"
        except (OSError, ImageCms.PyCMSError, AttributeError, TypeError, IndexError):
            pass
    return {"kind": kind, "description": description}


def _embedded_video(data: bytes) -> dict | None:
    """Motion Photo: реальный MP4-блок после изображения (ftyp с корректным заголовком)."""
    start = 16
    while True:
        pos = data.find(b"ftyp", start)
        if pos < 4:
            return None
        box_size = int.from_bytes(data[pos - 4:pos], "big")
        if 8 <= box_size <= 256 and data[pos + 4:pos + 8] in _MP4_BRANDS and pos > 1024:
            offset = pos - 4
            return {"offset": offset, "size": len(data) - offset}
        start = pos + 4


def _metadata_presence(image: Image.Image, data: bytes) -> dict:
    exif = image.getexif()
    exif_ifd = exif.get_ifd(_IFD_EXIF) if exif else {}
    xmp = image.info.get("xmp") or b""
    if isinstance(xmp, str):
        xmp = xmp.encode("utf-8", "ignore")
    return {
        "exif": bool(exif),
        "gps": bool(exif.get_ifd(_IFD_GPS)) if exif else False,
        "device": bool(exif.get(_TAG_MAKE) or exif.get(_TAG_MODEL)) if exif else False,
        "serial_numbers": any(tag in exif_ifd for tag in (_TAG_BODY_SERIAL, _TAG_LENS_SERIAL)),
        "makernote": _TAG_MAKERNOTE in exif_ifd,
        "software": bool(exif.get(_TAG_SOFTWARE)) if exif else False,
        "xmp": bool(xmp) or b"<x:xmpmeta" in data[:512 * 1024],
        "iptc": b"Photoshop 3.0\x008BIM" in data[:512 * 1024],
        "icc": bool(image.info.get("icc_profile")),
    }


def read_facts(path: Path, original_filename: str | None = None) -> dict:
    """Факты об исходнике. Файл только читается. OSError — файл нельзя прочитать."""
    data = path.read_bytes()
    container = detect_container(data[:16])

    with Image.open(io.BytesIO(data)) as image:
        exif = image.getexif()
        orientation = exif.get(_TAG_ORIENTATION) if exif else None
        width, height = image.size
        if orientation in (5, 6, 7, 8):
            width, height = height, width

        color_mode, bit_depth, alpha = _MODES.get(image.mode, (image.mode, None, False))
        if image.format == "TIFF":
            bits = getattr(image, "tag_v2", {}).get(258)
            if bits:
                bit_depth = int(bits[0] if isinstance(bits, tuple) else bits)
        alpha = alpha or "transparency" in image.info

        video = _embedded_video(data)
        head = data[:512 * 1024]
        gain_map = b"hdrgm" in head
        auxiliary = []
        if gain_map:
            auxiliary.append("gain_map")
        if b"GDepth" in head or b"depthmap" in head.lower():
            auxiliary.append("depth_map")
        if image.info.get("mp"):
            auxiliary.append("mpf_images")

        return {
            "facts_version": FACTS_VERSION,
            "original_filename": original_filename or path.name,
            "container": container or image.format,
            "format": image.format,
            "width": width,
            "height": height,
            "megapixels": round(width * height / 1_000_000, 2),
            "orientation": orientation,
            "color_mode": color_mode,
            "bit_depth": bit_depth,
            "has_alpha": alpha,
            "color_profile": _color_profile(image.info.get("icc_profile")),
            "hdr": "gain_map" if gain_map else "none" if image.format in ("JPEG", "PNG", "TIFF") else None,
            "frames": getattr(image, "n_frames", 1),
            "embedded": {"motion_video": video, "auxiliary": auxiliary},
            "metadata_present": _metadata_presence(image, data),
            "jpeg_quality": jpeg_quality(image),
            "file_size": len(data),
            "image_payload_size": video["offset"] if video else len(data),
        }
