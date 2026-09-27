import io
import json
import sqlite3

import pytest
from PIL import Image, ImageCms

from app import normalization, worker
from app.ingest import ingest_file, sha256_file
from app.service import dispatch
from app.service.mcp_server import tools
from app.source_facts import detect_container, read_facts

from tests.conftest import FakeAnalyzer, events, make_image

N8N = "workflow:n8n"
SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def jpeg_bytes(image: Image.Image, **kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=92, **kwargs)
    return buffer.getvalue()


def write(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --- Факты об исходнике -------------------------------------------------------------


def test_jpeg_facts_with_orientation_icc_and_quality(tmp_path):
    exif = Image.Exif()
    exif[0x0112] = 6  # повернуть на 90°: ширина и высота меняются местами
    path = write(tmp_path, "IMG_1.jpg", jpeg_bytes(Image.new("RGB", (400, 300), "gray"), exif=exif, icc_profile=SRGB_ICC))

    facts = read_facts(path, "IMG_1.jpg")

    assert facts["original_filename"] == "IMG_1.jpg"
    assert (facts["container"], facts["format"]) == ("JPEG", "JPEG")
    assert (facts["width"], facts["height"], facts["orientation"]) == (300, 400, 6)
    assert (facts["color_mode"], facts["bit_depth"], facts["has_alpha"]) == ("RGB", 8, False)
    assert facts["color_profile"]["kind"] == "srgb"
    assert facts["jpeg_quality"] == 92
    assert facts["hdr"] == "none" and facts["embedded"]["motion_video"] is None
    assert facts["file_size"] == facts["image_payload_size"] == path.stat().st_size


def _png_with_chunk(image: Image.Image, chunk_type: bytes, payload: bytes) -> bytes:
    """PNG с дополнительным блоком перед IDAT (для cICP и т.п.)."""
    import struct
    import zlib

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    data = buffer.getvalue()
    idat = data.find(b"IDAT") - 4
    chunk = struct.pack(">I", len(payload)) + chunk_type + payload
    chunk += struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    return data[:idat] + chunk + data[idat:]


def test_png_srgb_chunk_is_a_declared_profile(tmp_path):
    # Реальный случай (новая сотня): PNG Topaz Gigapixel — sRGB задан блоком, ICC нет.
    path = write(tmp_path, "s.png", _png_with_chunk(Image.new("RGB", (40, 30), "gray"), b"sRGB", b"\x00"))
    color = read_facts(path)["color_profile"]
    assert (color["kind"], color["source"]) == ("srgb", "png_srgb_chunk")


def test_png_gamma_only_is_not_assumed_srgb(tmp_path):
    path = write(tmp_path, "g.png", _png_with_chunk(Image.new("RGB", (40, 30), "gray"), b"gAMA", (45455).to_bytes(4, "big")))
    color = read_facts(path)["color_profile"]
    assert (color["kind"], color["source"]) == ("other", "png_gama_chrm")


def test_png_cicp_pq_is_hdr(tmp_path):
    path = write(tmp_path, "pq.png", _png_with_chunk(Image.new("RGB", (40, 30), "gray"), b"cICP", bytes([9, 16, 0, 1])))
    facts = read_facts(path)
    assert facts["hdr"] == "pq" and facts["color_profile"]["source"] == "png_cicp"


def test_png_16_bit_depth_from_header(tmp_path):
    import numpy as np

    path = tmp_path / "gray16.png"
    pixels = np.full((30, 40, 3), 40000, dtype=np.uint16)
    Image.fromarray(pixels[:, :, 0]).save(path)  # 16-бит оттенки серого — поддерживаемая запись Pillow
    assert read_facts(path)["bit_depth"] == 16


def test_alpha_used_only_when_transparent(tmp_path):
    opaque, transparent = tmp_path / "o.png", tmp_path / "t.png"
    Image.new("RGBA", (40, 30), (10, 20, 30, 255)).save(opaque)
    image = Image.new("RGBA", (40, 30), (10, 20, 30, 255))
    image.putpixel((0, 0), (0, 0, 0, 0))
    image.save(transparent)
    assert read_facts(opaque)["has_alpha"] and read_facts(opaque)["alpha_used"] is False
    assert read_facts(transparent)["alpha_used"] is True


def test_avif_color_and_depth_from_boxes(tmp_path):
    path = tmp_path / "a.avif"
    try:
        Image.new("RGB", (64, 48), "gray").save(path, "AVIF")
    except (KeyError, OSError, ValueError):
        pytest.skip("AVIF encoder is not available")
    facts = read_facts(path)
    assert facts["container"] == "AVIF" and facts["bit_depth"] == 8
    assert facts["hdr"] in ("none", None)
    if facts["color_profile"]["source"] == "nclx":
        assert facts["color_profile"]["nclx"]["transfer"] != 16


@pytest.mark.parametrize("color_space,expected", [(1, "srgb"), (65535, "uncalibrated")])
def test_exif_color_space_without_icc(tmp_path, color_space, expected):
    # Реальные случаи: 7 снимков первого набора — ColorSpace=1 без ICC; 1 файл новой сотни — 65535.
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0xA001] = color_space
    path = write(tmp_path, "cs.jpg", jpeg_bytes(Image.new("RGB", (200, 100), "gray"), exif=exif))
    color = read_facts(path)["color_profile"]
    assert (color["kind"], color["source"]) == (expected, "exif_colorspace")


def test_absent_interop_ifd_is_not_a_crash_or_damage(tmp_path):
    # Реальный случай (28 файлов новой сотни): Interop IFD нет, Pillow на get_ifd бросает KeyError.
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0xA001] = 65535  # Uncalibrated — Interop IFD нужен для проверки DCF R03
    path = write(tmp_path, "nointerop.jpg", jpeg_bytes(Image.new("RGB", (200, 100), "gray"), exif=exif))
    facts = read_facts(path)
    assert facts["metadata_present"]["exif_damaged"] is False
    assert facts["color_profile"]["kind"] == "uncalibrated"


def test_invalid_orientation_is_recorded_not_applied(tmp_path):
    # Реальный случай (оба набора): EXIF Orientation = 0 — вне диапазона 1–8.
    exif = Image.Exif()
    exif[0x0112] = 0
    path = write(tmp_path, "o0.jpg", jpeg_bytes(Image.new("RGB", (400, 300), "gray"), exif=exif))
    facts = read_facts(path)
    assert (facts["orientation"], facts["orientation_raw"], facts["width"]) == (None, 0, 400)


def test_digital_source_type_provenance(tmp_path):
    # Реальный случай (57 из новой сотни): Topaz пишет IPTC digitalSourceType.
    xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><Iptc4xmpExt:DigitalSourceType>'
           b"http://cv.iptc.org/newscodes/digitalsourcetype/compositeWithTrainedAlgorithmicMedia"
           b"</Iptc4xmpExt:DigitalSourceType></x:xmpmeta>")
    path = write(tmp_path, "t.jpg", jpeg_bytes(Image.new("RGB", (200, 100), "gray"), xmp=xmp))
    assert read_facts(path)["provenance"] == {"digital_source_type": ["compositeWithTrainedAlgorithmicMedia"], "c2pa": False}


def test_frames_declared_and_readable(tmp_path):
    path = write(tmp_path, "one.jpg", jpeg_bytes(Image.new("RGB", (40, 30), "gray")))
    assert read_facts(path)["frames"] == {"declared": 1, "readable": 1}


def test_png_alpha_and_16_bit(tmp_path):
    rgba = write(tmp_path, "a.png", b"")
    Image.new("RGBA", (50, 40), (10, 20, 30, 128)).save(rgba, "PNG")
    gray16 = tmp_path / "g.png"
    Image.new("I;16", (50, 40), 30000).save(gray16, "PNG")

    alpha = read_facts(rgba)
    deep = read_facts(gray16)

    assert (alpha["color_mode"], alpha["has_alpha"], alpha["jpeg_quality"]) == ("RGB", True, None)
    assert alpha["alpha_used"] is True
    assert alpha["color_profile"]["kind"] == "missing"
    assert (deep["color_mode"], deep["bit_depth"]) == ("L", 16)


def test_tiff_bit_depth_from_tag(tmp_path):
    # TIFF нет ни в одном реальном наборе — покрытие только синтетическое.
    rgb, gray16 = tmp_path / "rgb.tif", tmp_path / "g16.tif"
    Image.new("RGB", (40, 30), "gray").save(rgb, compression="tiff_lzw")
    Image.new("I;16", (40, 30), 30000).save(gray16)
    assert (read_facts(rgb)["container"], read_facts(rgb)["bit_depth"]) == ("TIFF", 8)
    assert read_facts(gray16)["bit_depth"] == 16


def test_cmyk_jpeg(tmp_path):
    path = write(tmp_path, "c.jpg", jpeg_bytes(Image.new("CMYK", (40, 30), (0, 50, 100, 0))))
    assert read_facts(path)["color_mode"] == "CMYK"


def test_motion_photo_video_is_detected_by_real_mp4_box(tmp_path):
    image = jpeg_bytes(Image.new("RGB", (400, 300), "gray"))
    mp4 = (24).to_bytes(4, "big") + b"ftypmp42" + b"\x00" * 12 + b"\x00\x00\x00\x08moov" + b"x" * 5000
    path = write(tmp_path, "PXL_1.MV.jpg", image + mp4)

    facts = read_facts(path)

    assert facts["embedded"]["motion_video"] == {"offset": len(image), "size": len(mp4)}
    assert facts["image_payload_size"] == len(image) and facts["file_size"] == len(image) + len(mp4)


def test_xmp_claim_without_video_is_not_trusted(tmp_path):
    # Как asset 3: редактор сохранил XMP «MotionPhoto», а видео в файле нет.
    xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><GCamera:MotionPhoto>1</GCamera:MotionPhoto></x:xmpmeta>'
    path = write(tmp_path, "edited.jpg", jpeg_bytes(Image.new("RGB", (200, 100), "gray"), xmp=xmp))
    facts = read_facts(path)
    assert facts["embedded"]["motion_video"] is None and facts["metadata_present"]["xmp"] is True


def test_ultra_hdr_gain_map_marker(tmp_path):
    xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/" xmlns:hdrgm="http://ns.adobe.com/hdr-gain-map/1.0/" hdrgm:Version="1.0"/>'
    path = write(tmp_path, "hdr.jpg", jpeg_bytes(Image.new("RGB", (200, 100), "gray"), xmp=xmp))
    facts = read_facts(path)
    assert facts["hdr"] == "gain_map" and "gain_map" in facts["embedded"]["auxiliary"]


def test_gps_and_device_only_presence_is_stored(tmp_path):
    exif = Image.Exif()
    exif[0x010F] = "Google"
    exif[0x0110] = "Pixel 9"
    exif[0x0131] = "HDR+ 1.0"
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2] = "N", (53.0, 20.0, 12.5)
    path = write(tmp_path, "gps.jpg", jpeg_bytes(Image.new("RGB", (200, 100), "gray"), exif=exif))

    facts = read_facts(path)
    present = facts["metadata_present"]

    assert (present["gps"], present["device"], present["software"]) == (True, True, True)
    stored = json.dumps(facts)
    assert "Pixel 9" not in stored and "53.0" not in stored  # GPS и устройство — только признак
    assert facts["software"] == "HDR+ 1.0"  # программа — внутренняя история инструментов (§3A.13)


@pytest.mark.parametrize("head,expected", [
    (b"\xff\xd8\xff\xe0", "JPEG"), (b"\x89PNG\r\n\x1a\n", "PNG"), (b"II*\x00", "TIFF"),
    (b"\x00\x00\x00\x18ftypheic", "HEIF"), (b"\x00\x00\x00\x1cftypavif", "AVIF"), (b"hello", None),
])
def test_container_signatures(head, expected):
    assert detect_container(head + b"\x00" * 8) == expected


# --- События и операции ---------------------------------------------------------------


@pytest.fixture
def asset_id(stocker_root) -> int:
    return ingest_file(make_image(stocker_root))


def _norm_events(root, asset_id):
    return [(st, json.loads(m)) for s, st, m in events(root, asset_id) if s == "NORMALIZE"]


def test_evaluate_writes_event_and_is_idempotent(stocker_root, asset_id):
    envelope = dispatch("normalize.evaluate", {"asset_id": asset_id}, actor=N8N)

    assert envelope["ok"] and envelope["outcome"] == "EVALUATED"
    assert envelope["data"]["facts"]["format"] == "JPEG"
    ((status, message),) = _norm_events(stocker_root, asset_id)
    assert status == "EVALUATED" and message["actor"] == N8N and message["facts_version"] == "normalize-facts-v2"

    assert dispatch("normalize.evaluate", {"asset_id": asset_id})["outcome"] == "UNCHANGED"
    assert len(_norm_events(stocker_root, asset_id)) == 1


def test_file_is_not_modified(stocker_root, asset_id):
    path = stocker_root / "data" / "incoming" / "photo.jpg"
    before = sha256_file(path)
    dispatch("normalize.evaluate", {"asset_id": asset_id})
    assert sha256_file(path) == before


def test_get_and_asset_view(stocker_root, asset_id):
    assert dispatch("normalize.get", {"asset_id": asset_id})["data"]["evaluated"] is False
    dispatch("normalize.evaluate", {"asset_id": asset_id})

    data = dispatch("normalize.get", {"asset_id": asset_id}, actor="agent:openclaw")["data"]
    assert data["evaluated"] and data["container"] == "JPEG" and data["facts"]["width"] == 64
    view = dispatch("asset.get", {"asset_id": asset_id})["data"]
    assert view["pipeline"]["normalize"]["event_id"] == data["event_id"]


def test_heif_without_codec_fails_without_guessing(stocker_root, asset_id):
    path = stocker_root / "data" / "incoming" / "photo.jpg"
    path.write_bytes(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64)
    with sqlite3.connect(stocker_root / "data" / "db" / "stocker.db") as connection:
        connection.execute("UPDATE assets SET file_hash = ? WHERE id = ?", (sha256_file(path), asset_id))

    envelope = dispatch("normalize.evaluate", {"asset_id": asset_id})

    assert envelope["ok"] is False and envelope["outcome"] == "NORMALIZE_FAILED"
    assert envelope["data"]["error"]["error_type"] == "MISSING_CODEC" and envelope["data"]["error"]["container"] == "HEIF"
    assert dispatch("normalize.get", {"asset_id": asset_id})["data"]["failed"] == "MISSING_CODEC"


def test_changed_source_fails(stocker_root, asset_id):
    make_image(stocker_root, seed=3)
    envelope = dispatch("normalize.evaluate", {"asset_id": asset_id})
    assert envelope["data"]["error"]["error_type"] == "SOURCE_CHANGED"


def test_unknown_asset_and_tools(stocker_root):
    assert dispatch("normalize.evaluate", {"asset_id": 999})["error"]["code"] == "ASSET_NOT_FOUND"
    assert {"normalize_evaluate", "normalize_get"} <= {tool.name for tool in tools()}


def test_worker_records_facts_before_qc(stocker_root):
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())
    stages = [(s, st) for s, st, _ in events(stocker_root, asset_id)]
    assert stages.index(("NORMALIZE", "EVALUATED")) < stages.index(("QC", "PASSED"))


def test_worker_continues_when_facts_crash(stocker_root, monkeypatch):
    def broken(asset_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(normalization, "evaluate_asset", broken)
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())
    assert ("AI", "PASSED") in [(s, st) for s, st, _ in events(stocker_root, asset_id)]
