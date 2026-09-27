import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from app import enhancement as en

ROOT = Path(__file__).resolve().parents[1]


def scene(size=(1200, 900), seed=0) -> Image.Image:
    """Синтетическая «промышленная» сцена: градиент, фигуры, линии, мелкий узор."""
    rng = np.random.default_rng(seed)
    w, h = size
    gradient = np.linspace(60, 190, w, dtype=np.float32)[None, :].repeat(h, axis=0)
    image = Image.fromarray(np.stack([gradient] * 3, axis=2).astype(np.uint8))
    draw = ImageDraw.Draw(image)
    for _ in range(60):
        x, y = int(rng.integers(0, w - 80)), int(rng.integers(0, h - 80))
        color = tuple(int(c) for c in rng.integers(0, 256, 3))
        draw.rectangle((x, y, x + int(rng.integers(10, 80)), y + int(rng.integers(10, 80))), outline=color, width=2)
        draw.line((x, y, x + int(rng.integers(-60, 60)), y + int(rng.integers(-60, 60))), fill=color, width=1)
    for x in range(0, w, 7):
        draw.line((x, h - 120, x, h - 60), fill=(20, 20, 20), width=1)
    return image


def jpeg(image: Image.Image, quality: int) -> Image.Image:
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return Image.open(io.BytesIO(buffer.getvalue())).convert("RGB")


def with_noise(image: Image.Image, sigma: float) -> Image.Image:
    pixels = np.asarray(image, np.float32)
    noise = np.random.default_rng(1).normal(0, sigma, (image.height, image.width, 1))
    return Image.fromarray(np.clip(pixels + noise, 0, 255).astype(np.uint8))


def metrics(**overrides) -> dict:
    data = {"megapixels": 12.6, "sharpness": 800.0, "noise_sigma": 1.0, "blockiness": 1.0, "detail_ratio": 0.7,
            "sharpness_peak": 1500.0, "sharp_tile_ratio": 0.6, "sharpest_point": [100, 100], "jpeg_quality": None}
    return {**data, **overrides}


# --- Метрики: направление изменений -----------------------------------------------


@pytest.fixture(scope="module")
def clean():
    return en.measure(scene())


def test_measure_returns_all_metrics(clean):
    assert set(clean) == {"megapixels", "sharpness", "noise_sigma", "blockiness", "detail_ratio",
                          "sharpness_peak", "sharp_tile_ratio", "sharpest_point", "jpeg_quality"}
    assert clean["jpeg_quality"] is None  # не JPEG
    assert clean["megapixels"] == 1.08


def test_blur_lowers_sharpness(clean):
    blurred = en.measure(scene().filter(ImageFilter.GaussianBlur(3)))
    assert blurred["sharpness"] < clean["sharpness"] / 5
    assert blurred["sharpness_peak"] < clean["sharpness_peak"] / 5


def test_noise_raises_sigma(clean):
    noisy = en.measure(with_noise(scene(), 10))
    assert noisy["noise_sigma"] > 5 > clean["noise_sigma"]


def test_heavy_jpeg_raises_blockiness(clean):
    assert en.measure(jpeg(scene(), 5))["blockiness"] > 1.5 > clean["blockiness"]


def test_upscale_lowers_detail_ratio(clean):
    image = scene()
    upscaled = image.resize((image.width // 2, image.height // 2), Image.LANCZOS).resize(image.size, Image.BICUBIC)
    assert en.measure(upscaled)["detail_ratio"] < clean["detail_ratio"]


def test_upscale_is_not_mistaken_for_jpeg_blocks():
    image = scene()
    upscaled = image.resize((image.width // 2, image.height // 2), Image.LANCZOS).resize(image.size, Image.BICUBIC)
    assert en.measure(upscaled)["blockiness"] < 1.2


def test_jpeg_quality_from_quantization_tables():
    for quality in (95, 30):
        buffer = io.BytesIO()
        scene().save(buffer, "JPEG", quality=quality)
        with Image.open(io.BytesIO(buffer.getvalue())) as encoded:  # без convert: таблицы квантования на месте
            assert en.measure(encoded)["jpeg_quality"] == quality


def test_background_blur_keeps_peak_sharpness(clean):
    # Малая глубина резкости: резкий объект в углу, остальной кадр размыт.
    image = scene()
    blurred = image.filter(ImageFilter.GaussianBlur(4))
    blurred.paste(image.crop((0, 0, 540, 540)), (0, 0))  # резкий объект — 4 плитки из 12
    result = en.measure(blurred)
    assert result["sharpness"] < clean["sharpness"] / 3          # средняя резкость падает
    assert result["sharpness_peak"] > clean["sharpness_peak"] / 2  # резкий объект виден
    assert result["sharp_tile_ratio"] <= 0.4
    assert result["sharpest_point"][0] < 540 and result["sharpest_point"][1] < 540


def test_small_image_uses_smaller_crops():
    result = en.measure(scene(size=(400, 300)))
    assert result["megapixels"] == 0.12


# --- Правила --------------------------------------------------------------------


def test_all_ok_is_not_needed_without_model():
    result = en.decide(en.findings(metrics()))
    assert result == {"decision": en.NOT_NEEDED, "disputed": False, "reasons": [], "operations": []}


@pytest.mark.parametrize("override,reason,operation", [
    ({"sharpness_peak": 30.0}, "sharpness", "sharpen"),
    ({"noise_sigma": 8.0}, "noise", "denoise"),
    ({"blockiness": 2.0}, "artifacts", "remove_compression_artifacts"),       # не JPEG
    ({"jpeg_quality": 45}, "artifacts", "remove_compression_artifacts"),
    ({"megapixels": 3.0}, "resolution", "upscale"),
])
def test_clear_issue_is_recommended_with_reason(override, reason, operation):
    result = en.decide(en.findings(metrics(**override)))
    assert result["decision"] == en.RECOMMENDED and not result["disputed"]
    assert [r["reason"] for r in result["reasons"]] == [reason]
    assert result["operations"] == [operation]


@pytest.mark.parametrize("override", [{"sharpness_peak": 5.0}, {"noise_sigma": 15.0}, {"blockiness": 4.0}, {"jpeg_quality": 12}, {"megapixels": 0.5}])
def test_severe_is_risky_without_operations(override):
    result = en.decide(en.findings(metrics(**override)))
    assert result["decision"] == en.RISKY
    assert result["reasons"] and result["operations"] == []


def test_severe_wins_over_issue_and_lists_both():
    result = en.decide(en.findings(metrics(sharpness_peak=5.0, noise_sigma=8.0)))
    assert result["decision"] == en.RISKY
    assert {r["reason"] for r in result["reasons"]} == {"sharpness", "noise"}


def test_borderline_only_is_disputed_for_model():
    result = en.decide(en.findings(metrics(noise_sigma=4.0)))
    assert result["decision"] is None and result["disputed"] is True
    assert result["reasons"] == [{"reason": "noise", "level": "borderline", "detail": "noise_sigma=4.0"}]


def test_issue_with_borderline_is_decided_by_rules():
    result = en.decide(en.findings(metrics(noise_sigma=4.0, blockiness=2.0)))
    assert result["decision"] == en.RECOMMENDED and not result["disputed"]


@pytest.mark.parametrize("value,level", [(120.0, en.OK), (119.9, en.BORDERLINE), (40.0, en.BORDERLINE), (39.9, en.ISSUE), (20.0, en.ISSUE), (19.9, en.SEVERE)])
def test_sharpness_boundaries(value, level):
    (sharpness,) = [f for f in en.findings(metrics(sharpness_peak=value)) if f["reason"] == "sharpness"]
    assert sharpness["level"] == level
    assert sharpness["metric"] == "sharpness_peak"


def test_low_global_sharpness_with_sharp_subject_is_not_a_defect():
    # v1: глобальная резкость 33 (#24) → issue; v2 — решает резкость объекта.
    result = en.assess(metrics(sharpness=33.3, sharpness_peak=203.4, sharp_tile_ratio=0.04), "h")
    assert result["decision"] == en.NOT_NEEDED
    assert [n["code"] for n in result["notes"]] == ["ISOLATED_SUBJECT"]


@pytest.mark.parametrize("quality,blockiness,level", [(93, 1.45, en.OK), (80, 1.0, en.OK), (70, 1.0, en.BORDERLINE),
                                                      (45, 1.0, en.ISSUE), (12, 1.0, en.SEVERE)])
def test_jpeg_artifacts_by_quality_not_blockiness(quality, blockiness, level):
    # Блочность 1.45 у JPEG телефона с качеством 93 — фактура, а не артефакты.
    (artifacts,) = [f for f in en.findings(metrics(jpeg_quality=quality, blockiness=blockiness)) if f["reason"] == "artifacts"]
    assert artifacts["level"] == level and artifacts["metric"] == "jpeg_quality"


def test_non_jpeg_artifacts_fall_back_to_blockiness():
    (artifacts,) = [f for f in en.findings(metrics(jpeg_quality=None, blockiness=1.3)) if f["reason"] == "artifacts"]
    assert artifacts["metric"] == "blockiness" and artifacts["level"] == en.BORDERLINE


def test_soft_at_native_is_note_not_topaz():
    result = en.assess(metrics(megapixels=50.3, detail_ratio=0.12), "hash")
    assert result["decision"] == en.NOT_NEEDED
    assert [(n["code"], n["level"]) for n in result["notes"]] == [("SOFT_AT_NATIVE_RESOLUTION", "warning")]
    assert "12.6 MP" in result["notes"][0]["detail"]
    assert result["operations"] == []  # ничего не уменьшается автоматически


def test_assess_structure_and_fingerprint():
    result = en.assess(metrics(), "hash-a")
    assert result["rules_version"] == "enhancement-rules-v2" and result["provider"] == "rules"
    assert result["fingerprint"] == en.fingerprint("hash-a") != en.fingerprint("hash-b")
    assert [f["reason"] for f in result["findings"]] == ["sharpness", "noise", "artifacts", "resolution"]


_PHONE_PHOTO = ROOT / "data" / "incoming" / "IMG_20260911_130437.jpg"


@pytest.mark.skipif(not _PHONE_PHOTO.exists(), reason="real phone photo is not available")
def test_real_50mp_phone_photo_needs_no_topaz_but_is_soft():
    result = en.assess(en.read_metrics(_PHONE_PHOTO), "hash")
    assert result["decision"] == en.NOT_NEEDED
    assert [n["code"] for n in result["notes"]] == ["SOFT_AT_NATIVE_RESOLUTION"]
