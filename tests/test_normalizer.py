"""
Internal Normalization Engine (INTERNAL_IMAGE_REPRESENTATION_CONTRACT, normalize-v1):
representation source / lossless derivative, отказы §7, analysis views §4, атомарность §8.
"""

import io
from pathlib import Path
import json

import numpy as np
import pytest
from PIL import Image, ImageCms

from app import normalization, normalizer, worker
from app.database.db import insert_asset, insert_event, transaction
from app.ingest import sha256_file
from app.service import dispatch
from app.source_facts import read_facts
from scripts.check_consistency import find_orphans, find_problems

from tests.conftest import FakeAnalyzer, events, make_image
from tests.fixtures.icc import matrix_profile

P3_ICC = matrix_profile()
SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def register(root, name: str, data: bytes) -> int:
    """Asset в обход ingest: AVIF / HEIF ingest пока не принимает (views — шаг 3)."""
    path = root / "data" / "incoming" / name
    path.write_bytes(data)
    with transaction() as connection:
        asset_id = insert_asset(connection, name, f"data/incoming/{name}", sha256_file(path), path.suffix.lower())
        insert_event(connection, asset_id, "INGEST", "DONE", "{}")
    return asset_id


def encoded(image: Image.Image, fmt: str, **kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, fmt, **kwargs)
    return buffer.getvalue()


def avif_bytes(orientation: int = 6, icc: bytes | None = P3_ICC) -> bytes:
    image = Image.new("RGBA", (40, 20), (200, 100, 50, 255))
    image.putpixel((0, 0), (0, 0, 255, 0))  # один прозрачный пиксель: alpha_used
    exif = Image.Exif()
    exif[0x0112] = orientation
    return encoded(image, "AVIF", exif=exif.tobytes(), **({"icc_profile": icc} if icc else {}))


def norm_events(root, asset_id):
    return [(st, json.loads(m)) for s, st, m in events(root, asset_id) if s == "NORMALIZE"]


def db_path(root):
    return root / "data" / "db" / "stocker.db"


# --- Pass-through ---------------------------------------------------------------------


def test_jpeg_is_used_as_is(stocker_root):
    asset_id = register(stocker_root, "a.jpg", encoded(Image.new("RGB", (40, 30), "gray"), "JPEG"))

    result = normalization.run_asset(asset_id)

    manifest = result["manifest"]
    assert result["outcome"] == normalization.NORMALIZED
    assert manifest["representation"] == "source" and manifest["derivative"] is None and manifest["transforms"] == []
    assert manifest["preserved"]["color"] == {"kind": "undeclared", "source": None, "declared": False, "icc": None}
    assert manifest["color"] == {"space": "undeclared", "declared": False, "icc_sha256": None, "icc_is_source": False, "pixels_converted": False}
    assert manifest["normalizer_version"] == "normalize-v1" and manifest["params_hash"] == normalizer.PARAMS_HASH
    assert not (stocker_root / "data" / "internal").exists()

    assert normalization.run_asset(asset_id)["outcome"] == normalization.UNCHANGED
    assert [st for st, _ in norm_events(stocker_root, asset_id)] == ["EVALUATED", "PASSED"]


# --- AVIF: lossless derivative ------------------------------------------------------


def test_avif_gets_lossless_derivative(stocker_root):
    data = avif_bytes()
    asset_id = register(stocker_root, "b.avif", data)
    source = stocker_root / "data" / "incoming" / "b.avif"

    result = normalization.run_asset(asset_id)

    manifest = result["manifest"]
    derivative = manifest["derivative"]
    path = stocker_root / derivative["path"]
    assert result["outcome"] == normalization.NORMALIZED and manifest["representation"] == "derivative"
    assert derivative["path"] == f"data/internal/{asset_id}/normalize-v1/{derivative['sha256']}.png"
    assert sha256_file(path) == derivative["sha256"]
    assert source.read_bytes() == data  # source неизменен

    with Image.open(path) as image, Image.open(source) as original:
        assert image.format == "PNG" and image.size == (20, 40)  # orientation 6 применена
        assert image.info["icc_profile"] == original.info["icc_profile"]  # ICC байт в байт
        assert image.mode == "RGBA" and image.getpixel((19, 0))[3] == 0  # альфа сохранена
        assert "exif" not in image.info  # ориентация не применится второй раз
    assert manifest["preserved"]["orientation_raw"] == 6 and manifest["preserved"]["color"]["kind"] == "display_p3"
    assert manifest["transforms"] == ["decode:avif", "orient", "encode:png-lossless"]

    # Идемпотентно: тот же файл, то же событие.
    assert normalization.run_asset(asset_id)["outcome"] == normalization.UNCHANGED
    assert len(list(path.parent.iterdir())) == 1
    assert not any(find_problems(db_path(stocker_root)).values()) and find_orphans(db_path(stocker_root)) == []


def test_lost_derivative_is_detected_and_rebuilt_identically(stocker_root):
    asset_id = register(stocker_root, "c.avif", avif_bytes())
    first = normalization.run_asset(asset_id)["manifest"]["derivative"]
    (stocker_root / first["path"]).unlink()

    assert find_problems(db_path(stocker_root))["NORMALIZE/PASSED derivative missing or changed"] == [asset_id]

    second = normalization.run_asset(asset_id)
    assert second["outcome"] == normalization.NORMALIZED and second["manifest"]["derivative"]["sha256"] == first["sha256"]
    assert not any(find_problems(db_path(stocker_root)).values())


def test_crash_between_file_and_event_leaves_orphan_without_event(stocker_root, monkeypatch):
    asset_id = register(stocker_root, "d.avif", avif_bytes())
    normalization.evaluate_asset(asset_id)

    def power_loss(*args, **kwargs):
        raise RuntimeError("power loss")

    with monkeypatch.context() as patch:
        patch.setattr(normalization, "insert_event", power_loss)
        with pytest.raises(RuntimeError):
            normalization.run_asset(asset_id)

    assert [st for st, _ in norm_events(stocker_root, asset_id)] == ["EVALUATED"]
    assert not any(find_problems(db_path(stocker_root)).values())
    (orphan,) = find_orphans(db_path(stocker_root))

    # Следующий прогон переиспользует тот же адресный файл — сирота исчезает.
    assert normalization.run_asset(asset_id)["manifest"]["derivative"]["path"] == orphan
    assert find_orphans(db_path(stocker_root)) == []


# --- Отказы §7 ----------------------------------------------------------------------


def test_heif_fails_with_missing_codec_and_stops_worker(stocker_root):
    asset_id = register(stocker_root, "e.heic", b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64)

    envelope = dispatch("normalize.run", {"asset_id": asset_id})
    assert envelope["ok"] is False and envelope["outcome"] == "NORMALIZE_FAILED"
    assert envelope["data"]["error"]["error_type"] == "MISSING_CODEC"

    assert worker.process_asset(asset_id, analyzer=FakeAnalyzer()) == worker.NORMALIZE_FAILED
    assert {s for s, _, _ in events(stocker_root, asset_id)} == {"INGEST", "NORMALIZE"}


def test_cmyk_without_icc_fails_once(stocker_root):
    asset_id = register(stocker_root, "f.jpg", encoded(Image.new("CMYK", (40, 30), (0, 50, 100, 0)), "JPEG"))

    result = normalization.run_asset(asset_id)
    assert result["outcome"] == normalization.NORMALIZE_FAILED
    assert result["error"]["error_type"] == "COLOR_SPACE_UNDECLARED" and result["error"]["stage"] == "engine"

    normalization.run_asset(asset_id)
    assert [st for st, _ in norm_events(stocker_root, asset_id)] == ["EVALUATED", "FAILED"]
    assert dispatch("normalize.get", {"asset_id": asset_id})["data"]["failed"] == "COLOR_SPACE_UNDECLARED"


def test_undeclared_rgb_is_not_a_failure(stocker_root):
    # Решение 28.09: undeclared — факт, не автоматический FAILED и не sRGB.
    asset_id = register(stocker_root, "g.jpg", encoded(Image.new("RGB", (40, 30), "gray"), "JPEG"))
    manifest = normalization.run_asset(asset_id)["manifest"]
    assert manifest["preserved"]["color"]["kind"] == "undeclared"


def _facts(tmp_path, image=None, fmt="PNG", **kwargs):
    path = tmp_path / f"x.{fmt.lower()}"
    (image or Image.new("RGB", (40, 30), "gray")).save(path, fmt, **kwargs)
    return read_facts(path)


def test_plan_refuses_hdr(tmp_path):
    facts = {**_facts(tmp_path), "hdr": "pq"}
    with pytest.raises(normalizer.NormalizationRefused) as refused:
        normalizer.plan(facts)
    assert refused.value.code == "UNSUPPORTED_HDR"


def test_plan_refuses_animation(tmp_path):
    frames = [Image.new("RGB", (40, 30), color) for color in ("red", "blue")]
    facts = _facts(tmp_path, frames[0], save_all=True, append_images=frames[1:])
    assert facts["frames"]["readable"] == 2
    with pytest.raises(normalizer.NormalizationRefused) as refused:
        normalizer.plan(facts)
    assert refused.value.code == "MULTI_FRAME_UNSUPPORTED"


def test_plan_refuses_high_bit_avif_and_unknown_container(tmp_path):
    facts = _facts(tmp_path)
    with pytest.raises(normalizer.NormalizationRefused) as refused:
        normalizer.plan({**facts, "container": "AVIF", "bit_depth": 10})
    assert refused.value.code == "UNSUPPORTED_BIT_DEPTH"
    with pytest.raises(normalizer.NormalizationRefused) as refused:
        normalizer.plan({**facts, "container": "WEBP"})
    assert refused.value.code == "UNSUPPORTED_FORMAT"


def test_version_is_part_of_fingerprint(monkeypatch):
    before = normalizer.fingerprint("abc")
    monkeypatch.setattr(normalizer, "NORMALIZER_VERSION", "normalize-v2")
    assert normalizer.fingerprint("abc") != before


# --- Analysis views §4 --------------------------------------------------------------


def _view(tmp_path, image, variant="full", fmt="PNG", **kwargs):
    path = tmp_path / f"v.{fmt.lower()}"
    image.save(path, fmt, **kwargs)
    color = read_facts(path)["color_profile"]
    return normalizer.open_view(path, variant, color, normalizer.SOURCE)


def test_view_does_not_convert_undeclared(tmp_path):
    image, info = _view(tmp_path, Image.new("RGB", (40, 30), (200, 100, 50)))
    assert info["color_space"] == "undeclared" and not info["color_converted"]
    assert image.getpixel((0, 0)) == (200, 100, 50)


def test_view_converts_declared_p3_to_srgb(tmp_path):
    image, info = _view(tmp_path, Image.new("RGB", (40, 30), (200, 100, 50)), icc_profile=P3_ICC)
    assert info["color_space"] == "srgb" and info["color_converted"]
    assert image.getpixel((0, 0)) != (200, 100, 50)


def test_view_keeps_srgb_pixels(tmp_path):
    image, info = _view(tmp_path, Image.new("RGB", (40, 30), (200, 100, 50)), icc_profile=SRGB_ICC)
    assert info["color_space"] == "srgb" and not info["color_converted"]
    assert image.getpixel((0, 0)) == (200, 100, 50)


def test_view_composites_alpha_on_white_and_never_upscales(tmp_path):
    rgba = Image.new("RGBA", (40, 30), (0, 0, 0, 0))
    image, info = _view(tmp_path, rgba, variant="preview")
    assert info["alpha_composited"] and image.mode == "RGB" and image.getpixel((0, 0)) == (255, 255, 255)
    assert image.size == (40, 30) and not info["downscaled"]


def test_view_downscales_to_limit(tmp_path):
    pixels = np.zeros((1000, 3000, 3), dtype=np.uint8)
    image, info = _view(tmp_path, Image.fromarray(pixels), variant="overview")
    assert info["downscaled"] and max(image.size) == 1536


def test_view_refuses_declared_non_srgb_without_icc(tmp_path):
    path = tmp_path / "v.png"
    Image.new("RGB", (40, 30), "gray").save(path)
    with pytest.raises(normalizer.NormalizationRefused) as refused:
        normalizer.open_view(path, "full", {"kind": "display_p3", "source": "nclx", "declared": True}, normalizer.SOURCE)
    assert refused.value.code == "COLOR_CONVERSION_UNSUPPORTED"


def test_worker_pipeline_records_representation(stocker_root):
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())
    data = dispatch("normalize.get", {"asset_id": asset_id})["data"]
    assert data["normalized"] and data["representation"] == "source" and not data["representation_stale"]
    assert data["manifest"]["source_sha256"] == sha256_file(stocker_root / "data" / "incoming" / "photo.jpg")


# --- ICC: источник / representation / view — разные вещи (§4a) ------------------------


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def test_source_icc_is_a_fact(tmp_path):
    path = tmp_path / "p3.jpg"
    Image.new("RGB", (40, 30), (200, 100, 50)).save(path, icc_profile=P3_ICC)
    icc = read_facts(path)["color_profile"]["icc"]
    assert icc == {"sha256": _sha(P3_ICC), "size": len(P3_ICC), "color_space": "RGB"}


def test_p3_derivative_keeps_p3_pixels_and_p3_icc(stocker_root):
    """ICC источника байт в байт ⇔ пиксели не конвертированы: derivative остаётся P3."""
    asset_id = register(stocker_root, "p3.avif", avif_bytes(orientation=1))
    manifest = normalization.run_asset(asset_id)["manifest"]
    color = manifest["color"]
    assert color == {"space": "display_p3", "declared": True, "icc_sha256": _sha(P3_ICC),
                     "icc_is_source": True, "pixels_converted": False}
    assert manifest["preserved"]["color"]["icc"]["sha256"] == _sha(P3_ICC)

    derivative = stocker_root / manifest["derivative"]["path"]
    with Image.open(derivative) as image, Image.open(stocker_root / "data/incoming/p3.avif") as source:
        assert _sha(image.info["icc_profile"]) == color["icc_sha256"]
        # Пиксели derivative — те же декодированные пиксели source (без конвертации в sRGB).
        assert image.tobytes() == source.convert(image.mode).tobytes()


def test_view_of_p3_is_srgb_and_carries_no_source_icc(stocker_root):
    asset_id = register(stocker_root, "p3v.avif", avif_bytes(orientation=1))
    manifest = normalization.run_asset(asset_id)["manifest"]
    path = stocker_root / manifest["derivative"]["path"]

    image, info = normalizer.open_view(path, "full", manifest["preserved"]["color"], manifest["representation"])

    assert info["color_space"] == "srgb" and info["color_converted"]
    assert "icc_profile" not in image.info  # view не выдаёт себя за P3
    with Image.open(path) as derivative:
        rgb = derivative.convert("RGB")
        expected = ImageCms.profileToProfile(rgb, ImageCms.ImageCmsProfile(io.BytesIO(P3_ICC)),
                                             ImageCms.createProfile("sRGB"), renderingIntent=0, outputMode="RGB")
    # Альфа (один прозрачный пиксель) на белом; остальные пиксели — ровно конвертация P3 → sRGB.
    assert image.getpixel((5, 5)) == expected.getpixel((5, 5)) != (200, 100, 50)


def test_srgb_view_drops_source_icc_too(tmp_path):
    image, info = _view(tmp_path, Image.new("RGB", (40, 30), (200, 100, 50)), icc_profile=SRGB_ICC)
    assert info["color_space"] == "srgb" and "icc_profile" not in image.info


@pytest.mark.parametrize("record, error", [
    # ICC источника при конвертированных пикселях — то самое противоречие.
    ({"space": "srgb", "icc_sha256": "SRC", "icc_is_source": True, "pixels_converted": True}, "must not carry the source ICC"),
    # Конвертировали, но назвали пространство источника.
    ({"space": "display_p3", "icc_sha256": "NEW", "icc_is_source": False, "pixels_converted": True}, "own color space"),
    # Не конвертировали, но ICC уже другой (например, подменён на sRGB).
    ({"space": "display_p3", "icc_sha256": "NEW", "icc_is_source": False, "pixels_converted": False}, "source ICC unchanged"),
    # Не конвертировали, но называем sRGB.
    ({"space": "srgb", "icc_sha256": "SRC", "icc_is_source": True, "pixels_converted": False}, "keep the source color space"),
])
def test_color_record_contradictions_are_rejected(record, error):
    source = {"kind": "display_p3", "declared": True, "icc": {"sha256": "SRC"}}
    with pytest.raises(ValueError, match=error):
        normalizer.check_color(source, {"declared": True, **record})


def test_contradictory_color_record_writes_no_event(stocker_root, monkeypatch):
    asset_id = register(stocker_root, "bad.avif", avif_bytes(orientation=1))
    normalization.evaluate_asset(asset_id)
    original = normalizer.build_derivative

    def claims_conversion(source, target):
        return {**original(source, target), "pixels_converted": True}  # ICC источника + «конвертировано»

    monkeypatch.setattr(normalizer, "build_derivative", claims_conversion)
    with pytest.raises(ValueError):
        normalization.run_asset(asset_id)
    assert [st for st, _ in norm_events(stocker_root, asset_id)] == ["EVALUATED"]


def test_declared_non_srgb_without_icc_is_refused_at_plan(tmp_path):
    facts = _facts(tmp_path)
    facts = {**facts, "color_profile": {"kind": "adobe_rgb", "source": "exif_dcf", "declared": True, "icc": None}}
    with pytest.raises(normalizer.NormalizationRefused) as refused:
        normalizer.plan(facts)
    assert refused.value.code == "COLOR_CONVERSION_UNSUPPORTED"


def test_cmyk_with_icc_converts_in_cmyk_space(tmp_path):
    cmyk_icc = next(iter(sorted((Path("C:/Windows/System32/spool/drivers/color")).glob("RSWOP.icm"))), None)
    if cmyk_icc is None:
        pytest.skip("no CMYK ICC on this system")
    image, info = _view(tmp_path, Image.new("CMYK", (40, 30), (0, 0, 0, 0)), fmt="JPEG", icc_profile=cmyk_icc.read_bytes())
    assert info["color_converted"] and info["color_space"] == "srgb"
    assert min(image.getpixel((0, 0))) > 200  # «без краски» — почти белый, а не инверсия
