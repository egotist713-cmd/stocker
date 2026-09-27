"""
Факты об исходном файле (docs/INTERNAL_IMAGE_REPRESENTATION_CONTRACT.md §3,
docs/FORMAT_CONTRACT.md §3.1; normalize-facts-v4).

Первый шаг Normalization: файл только читается и описывается. Ничего не
меняется и не конвертируется.

Правило «не угадывать»: признак берётся из самого файла — заголовков и блоков
контейнера (PNG IHDR / sRGB / cICP, AVIF pixi / colr, JPEG SOF, MPF, EXIF,
XMP), а не из того, как библиотека декодировала пиксели. Что нельзя
установить — null. Значения GPS, модели и серийных номеров устройства не
сохраняются — только признак наличия. Название программы (Software) —
сохраняется: это внутренняя история инструментов (паспорт §3A.13).

v2 (валидация на независимой сотне 27.09.2026, паспорт §35ZP): битность из
заголовков, цвет PNG из sRGB / gAMA / cHRM / cICP и AVIF из nclx, HDR по
передаточной функции, реальное использование альфы, заявленные и читаемые
кадры (MPF), сырая ориентация, метки происхождения (IPTC digitalSourceType,
C2PA).
"""

import hashlib
import io
import re
import struct
from pathlib import Path

from PIL import Image, ImageCms

from app import readiness as rd
from app.enhancement import jpeg_quality

# v3 (28.09.2026): необъявленный цвет — kind "undeclared" (не "missing"), признак declared.
# v4 (28.09.2026): ICC источника как факт — sha256, размер, цветовое пространство профиля
#   (ICC derivative / view сверяются с ним; паспорт §35ZR).
FACTS_VERSION = "normalize-facts-v4"

_SIGNATURES = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
)
_HEIF_BRANDS = {b"heic": "HEIF", b"heix": "HEIF", b"hevc": "HEIF", b"mif1": "HEIF", b"msf1": "HEIF", b"avif": "AVIF", b"avis": "AVIF"}
_MP4_BRANDS = (b"mp41", b"mp42", b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"qt  ", b"avc1", b"MSNV", b"dash")

# Цветовая модель по режиму Pillow (битность — из заголовков файла, не отсюда).
_MODES = {
    "RGB": ("RGB", False), "RGBA": ("RGB", True), "RGBX": ("RGB", False), "YCbCr": ("RGB", False),
    "L": ("L", False), "LA": ("L", True), "1": ("L", False), "I;16": ("L", False), "I;16B": ("L", False),
    "I;16L": ("L", False), "I": ("L", False), "F": ("L", False), "P": ("P", False), "PA": ("P", True),
    "CMYK": ("CMYK", False),
}

# Основные цвета (xy, адаптация D50) Display P3 телефонов — измерены на реальном файле.
_P3_RED_GREEN = ((0.682, 0.319), (0.285, 0.675))
_PRIMARY_TOLERANCE = 0.005

# ITU-T H.273: передаточные функции HDR в nclx / cICP.
_TRANSFER_HDR = {16: "pq", 18: "hlg"}

_TAG_MAKE, _TAG_MODEL, _TAG_SOFTWARE, _TAG_ORIENTATION = 0x010F, 0x0110, 0x0131, 0x0112
_IFD_EXIF, _IFD_GPS, _IFD_INTEROP = 0x8769, 0x8825, 0xA005
_TAG_COLOR_SPACE = 0xA001


def _safe_ifd(exif, tag: int) -> tuple[dict, bool]:
    """Вложенный IFD EXIF; (содержимое, повреждён ли указатель). Повреждённый EXIF — факт, не сбой."""
    try:
        return exif.get_ifd(tag), False
    except (KeyError, ValueError, TypeError, struct.error, OSError):
        return {}, True
_TAG_MAKERNOTE, _TAG_BODY_SERIAL, _TAG_LENS_SERIAL = 0x927C, 0xA431, 0xA435

_DIGITAL_SOURCE_TYPE = re.compile(rb"digitalsourcetype/([A-Za-z]+)")


def detect_container(head: bytes) -> str | None:
    for signature, name in _SIGNATURES:
        if head.startswith(signature):
            return name
    if head[4:8] == b"ftyp":
        return _HEIF_BRANDS.get(head[8:12])
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    return None


# --- Разбор контейнеров (только чтение байтов) ---------------------------------------

def _png_chunks(data: bytes) -> dict[str, bytes]:
    """Первое вхождение каждого блока PNG (без IDAT)."""
    chunks, pos = {}, 8
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8].decode("latin1")
        if kind != "IDAT":
            chunks.setdefault(kind, data[pos + 8:pos + 8 + length])
        pos += 12 + length
        if kind == "IEND":
            break
    return chunks


def _isobmff_payload(data: bytes, box: bytes) -> bytes | None:
    """Содержимое первого блока ISOBMFF с данным типом (по заголовку «размер + тип»)."""
    pos = data.find(box)
    while pos >= 4:
        size = int.from_bytes(data[pos - 4:pos], "big")
        if 8 <= size <= 1 << 20:
            return data[pos + 4:pos - 4 + size]
        pos = data.find(box, pos + 4)
    return None


def _isobmff_payloads(data: bytes, box: bytes) -> list[bytes]:
    """Содержимое всех блоков ISOBMFF с данным типом (у AVIF бывает два colr: prof и nclx)."""
    payloads, pos = [], data.find(box)
    while pos >= 4:
        size = int.from_bytes(data[pos - 4:pos], "big")
        if 8 <= size <= 1 << 20:
            payloads.append(data[pos + 4:pos - 4 + size])
        pos = data.find(box, pos + 4)
    return payloads


def _nclx(payload: bytes | None) -> dict | None:
    if not payload or payload[:4] != b"nclx" or len(payload) < 11:
        return None
    primaries, transfer, matrix = struct.unpack(">HHH", payload[4:10])
    return {"primaries": primaries, "transfer": transfer, "matrix": matrix, "full_range": bool(payload[10] & 0x80)}


_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def _jpeg_segments(data: bytes) -> list[tuple[int, int, bytes]]:
    """Сегменты JPEG по маркерам от SOI до SOS: (маркер, смещение, содержимое). Без поиска по байтам."""
    segments, pos = [], 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker == 0xFF:  # заполнитель
            pos += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        segments.append((marker, pos, data[pos + 4:pos + 2 + length]))
        if marker == 0xDA:  # SOS — дальше сжатые данные
            break
        pos += 2 + length
    return segments


def _jpeg_precision(segments) -> int | None:
    """Точность выборки из первого маркера SOF (8 или 12 бит)."""
    return next((payload[0] for marker, _, payload in segments if marker in _SOF_MARKERS and payload), None)


def _jpeg_mpf(segments, size: int) -> dict | None:
    """
    Multi-Picture Format (APP2 «MPF»): сколько изображений заявлено в индексе MP и
    сколько из них целиком лежат внутри файла. Смещения — от начала заголовка MPF.
    """
    for marker, offset, payload in segments:
        if marker != 0xE2 or not payload.startswith(b"MPF\x00"):
            continue
        tiff = payload[4:]
        base = offset + 8  # начало заголовка TIFF внутри файла
        endian = "<" if tiff[:2] == b"II" else ">"
        try:
            ifd = struct.unpack(endian + "I", tiff[4:8])[0]
            count = struct.unpack(endian + "H", tiff[ifd:ifd + 2])[0]
            entries = None
            for i in range(count):
                tag, _type, n, value = struct.unpack(endian + "HHII", tiff[ifd + 2 + 12 * i: ifd + 14 + 12 * i])
                if tag == 0xB002:
                    entries = (value, n // 16)
            if not entries:
                return {"declared": 0, "within_file": 0}
            start, number = entries
            within = 0
            for i in range(number):
                _attr, image_size, image_offset = struct.unpack(endian + "III", tiff[start + 16 * i: start + 16 * i + 12])
                absolute = 0 if i == 0 else base + image_offset
                within += int(absolute + image_size <= size)
            return {"declared": number, "within_file": within}
        except struct.error:
            return {"declared": None, "within_file": None}
    return None


def _embedded_video(data: bytes) -> dict | None:
    """Motion Photo: реальный MP4-блок после изображения (ftyp с корректным заголовком)."""
    start = 16
    while True:
        pos = data.find(b"ftyp", start)
        if pos < 4:
            return None
        box_size = int.from_bytes(data[pos - 4:pos], "big")
        if 8 <= box_size <= 256 and data[pos + 4:pos + 8] in _MP4_BRANDS and pos > 1024:
            return {"offset": pos - 4, "size": len(data) - (pos - 4)}
        start = pos + 4


# --- Цвет ----------------------------------------------------------------------------

def _icc_kind(icc: bytes) -> tuple[str, str | None]:
    kind, description = rd.color_profile_kind(icc)
    if kind == rd.OTHER:
        try:
            profile = ImageCms.ImageCmsProfile(io.BytesIO(icc)).profile
            red, green = tuple(profile.red_primary[1][:2]), tuple(profile.green_primary[1][:2])
            if all(abs(a - b) <= _PRIMARY_TOLERANCE for pa, pb in zip((red, green), _P3_RED_GREEN) for a, b in zip(pa, pb)):
                kind = "display_p3"
        except (OSError, ImageCms.PyCMSError, AttributeError, TypeError, IndexError):
            pass
    return kind, description


def icc_fact(icc: bytes | None) -> dict | None:
    """ICC как факт: хеш байтов, размер и пространство профиля (RGB / CMYK / GRAY)."""
    if not icc:
        return None
    try:
        space = ImageCms.ImageCmsProfile(io.BytesIO(icc)).profile.xcolor_space.strip() or None
    except (OSError, ImageCms.PyCMSError, AttributeError):
        space = None
    return {"sha256": hashlib.sha256(icc).hexdigest(), "size": len(icc), "color_space": space}


def _color(image: Image.Image, container: str | None, png: dict, nclx: dict | None) -> dict:
    """Цвет из того, чем он объявлен в файле: ICC, блоки PNG, nclx / cICP. Ничего не предполагается."""
    color = _declared_color(image, container, png, nclx)
    color["icc"] = icc_fact(image.info.get("icc_profile"))
    return color


def _declared_color(image: Image.Image, container: str | None, png: dict, nclx: dict | None) -> dict:
    icc = image.info.get("icc_profile")
    if icc:
        kind, description = _icc_kind(icc)
        return {"kind": kind, "description": description, "source": "icc", "nclx": nclx}
    if container == "PNG" and "sRGB" in png:
        return {"kind": "srgb", "description": "PNG sRGB chunk", "source": "png_srgb_chunk", "nclx": None}
    if container == "PNG" and "cICP" in png:
        cicp = png["cICP"]
        return {"kind": "other", "description": "PNG cICP chunk", "source": "png_cicp",
                "nclx": {"primaries": cicp[0], "transfer": cicp[1], "matrix": cicp[2], "full_range": bool(cicp[3])}}
    if container == "PNG" and ("gAMA" in png or "cHRM" in png):
        return {"kind": "other", "description": "PNG gAMA/cHRM without sRGB or ICC", "source": "png_gama_chrm", "nclx": None}
    if nclx:
        kind = "srgb" if (nclx["primaries"], nclx["transfer"]) == (1, 13) else "display_p3" if nclx["primaries"] == 12 else "other"
        return {"kind": kind, "description": f"nclx {nclx['primaries']}/{nclx['transfer']}/{nclx['matrix']}", "source": "nclx", "nclx": nclx}
    # EXIF ColorSpace: 1 — sRGB; 65535 (Uncalibrated) — Adobe RGB только при индексе DCF R03,
    # иначе цветовое пространство не объявлено (не предполагается sRGB).
    exif = image.getexif()
    color_space = _safe_ifd(exif, _IFD_EXIF)[0].get(_TAG_COLOR_SPACE) if exif else None
    if color_space == 1:
        return {"kind": "srgb", "description": "EXIF ColorSpace sRGB", "source": "exif_colorspace", "nclx": None}
    if color_space == 65535:
        interop = _safe_ifd(exif, _IFD_INTEROP)[0].get(1)
        if interop == "R03":
            return {"kind": "adobe_rgb", "description": "EXIF Uncalibrated + DCF R03", "source": "exif_dcf", "nclx": None}
        return {"kind": "uncalibrated", "description": "EXIF ColorSpace Uncalibrated, no ICC", "source": "exif_colorspace", "nclx": None}
    # Цвет не объявлен ничем — это факт, а не sRGB (решение пользователя 28.09.2026).
    return {"kind": "undeclared", "description": None, "source": None, "nclx": None}


UNDECLARED_KINDS = ("undeclared", "uncalibrated")


# --- Факты ---------------------------------------------------------------------------

def read_facts(path: Path, original_filename: str | None = None) -> dict:
    """Факты об исходнике. Файл только читается. Исключение — файл нельзя прочитать."""
    data = path.read_bytes()
    container = detect_container(data[:16])
    png = _png_chunks(data) if container == "PNG" else {}
    segments = _jpeg_segments(data) if container == "JPEG" else []
    nclx = None
    if container in ("AVIF", "HEIF"):
        nclx = next((_nclx(p) for p in _isobmff_payloads(data, b"colr") if p[:4] == b"nclx"), None)
    elif "cICP" in png:
        cicp = png["cICP"]
        nclx = {"primaries": cicp[0], "transfer": cicp[1], "matrix": cicp[2], "full_range": bool(cicp[3])}

    with Image.open(io.BytesIO(data)) as image:
        exif = image.getexif()
        orientation_raw = exif.get(_TAG_ORIENTATION) if exif else None
        orientation = orientation_raw if orientation_raw in range(1, 9) else None
        width, height = image.size
        if orientation in (5, 6, 7, 8):
            width, height = height, width

        # Битность — только из заголовков контейнера.
        bit_depth = None
        if container == "PNG" and "IHDR" in png:
            bit_depth = png["IHDR"][8]
        elif container == "JPEG":
            bit_depth = _jpeg_precision(segments)
        elif container == "TIFF":
            bits = getattr(image, "tag_v2", {}).get(258)
            bit_depth = int(bits[0] if isinstance(bits, tuple) else bits) if bits else None
        elif container in ("AVIF", "HEIF"):
            pixi = _isobmff_payload(data, b"pixi")
            bit_depth = pixi[5] if pixi and len(pixi) > 5 and pixi[4] > 0 else None

        color_mode, alpha = _MODES.get(image.mode, (image.mode, False))
        alpha = alpha or "transparency" in image.info
        alpha_used = None
        if alpha and "A" in image.getbands():
            alpha_used = image.getchannel("A").getextrema()[0] < 255

        color = _color(image, container, png, nclx)
        # Операции, которым нужно цветовое пространство, при declared=False не принимают sRGB молча.
        color["declared"] = color["kind"] not in UNDECLARED_KINDS
        head = data[:512 * 1024]
        gain_map = b"hdrgm" in head
        if gain_map:
            hdr = "gain_map"
        elif nclx:
            hdr = _TRANSFER_HDR.get(nclx["transfer"], "none")
        elif container in ("JPEG", "PNG", "TIFF"):
            hdr = "none"
        else:
            hdr = None  # AVIF / HEIF без nclx — HDR не установить

        video = _embedded_video(data)
        mpf = _jpeg_mpf(segments, len(data)) if container == "JPEG" else None
        auxiliary = []
        if gain_map:
            auxiliary.append("gain_map")
        if b"GDepth" in head or b"depthmap" in head.lower():
            auxiliary.append("depth_map")
        if mpf:
            auxiliary.append("mpf_images")

        frames_declared = getattr(image, "n_frames", 1)
        frames_readable = 1
        for index in range(1, frames_declared):
            try:
                image.seek(index)
                frames_readable += 1
            except (ValueError, EOFError, OSError):
                pass

        present = _metadata_presence(image, data)
        software = exif.get(_TAG_SOFTWARE) if exif else None
        if not software and container == "PNG":
            software = image.info.get("Software")
        source_types = sorted({m.decode() for m in _DIGITAL_SOURCE_TYPE.findall(data[:2 << 20])})

        return {
            "facts_version": FACTS_VERSION,
            "original_filename": original_filename or path.name,
            "container": container or image.format,
            "format": image.format,
            "width": width,
            "height": height,
            "megapixels": round(width * height / 1_000_000, 2),
            "orientation": orientation,
            "orientation_raw": orientation_raw,
            "color_mode": color_mode,
            "bit_depth": bit_depth,
            "has_alpha": alpha,
            "alpha_used": alpha_used,
            "color_profile": color,
            "hdr": hdr,
            "frames": {"declared": frames_declared, "readable": frames_readable},
            "mpf": mpf,
            "embedded": {"motion_video": video, "auxiliary": auxiliary},
            "metadata_present": present,
            "software": str(software).strip()[:100] if software else None,
            "provenance": {
                "digital_source_type": source_types,
                "c2pa": _c2pa(container, segments, png, data),
            },
            "jpeg_quality": jpeg_quality(image),
            "file_size": len(data),
            "image_payload_size": video["offset"] if video else len(data),
        }


def _c2pa(container: str | None, segments, png: dict, data: bytes) -> bool:
    """Content Credentials: JUMBF-сегмент APP11 с меткой c2pa (JPEG), блок caBX (PNG)."""
    if container == "JPEG":
        return any(marker == 0xEB and payload.startswith(b"JP") and b"c2pa" in payload for marker, _, payload in segments)
    if container == "PNG":
        return "caBX" in png
    if container in ("AVIF", "HEIF"):
        return _isobmff_payload(data, b"jumb") is not None and b"c2pa" in data
    return False


def _metadata_presence(image: Image.Image, data: bytes) -> dict:
    exif = image.getexif()
    exif_ifd, broken_exif = _safe_ifd(exif, _IFD_EXIF) if exif else ({}, False)
    gps_ifd, broken_gps = _safe_ifd(exif, _IFD_GPS) if exif else ({}, False)
    broken_interop = _safe_ifd(exif, _IFD_INTEROP)[1] if exif and _IFD_INTEROP in exif_ifd else False
    xmp = image.info.get("xmp") or b""
    if isinstance(xmp, str):
        xmp = xmp.encode("utf-8", "ignore")
    return {
        "exif": bool(exif),
        "exif_damaged": broken_exif or broken_gps or broken_interop,
        "gps": bool(gps_ifd),
        "device": bool(exif.get(_TAG_MAKE) or exif.get(_TAG_MODEL)) if exif else False,
        "serial_numbers": any(tag in exif_ifd for tag in (_TAG_BODY_SERIAL, _TAG_LENS_SERIAL)),
        "makernote": _TAG_MAKERNOTE in exif_ifd,
        "software": bool(exif.get(_TAG_SOFTWARE)) if exif else bool(image.info.get("Software")),
        "xmp": bool(xmp) or b"<x:xmpmeta" in data[:512 * 1024],
        "iptc": b"Photoshop 3.0\x008BIM" in data[:512 * 1024],
        "icc": bool(image.info.get("icc_profile")),
    }
