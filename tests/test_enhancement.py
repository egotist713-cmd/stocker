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
    data = {"megapixels": 12.6, "sharpness": 800.0, "noise_sigma": 1.0, "blockiness": 1.0, "detail_ratio": 0.7}
    return {**data, **overrides}


# --- Метрики: направление изменений -----------------------------------------------


@pytest.fixture(scope="module")
def clean():
    return en.measure(scene())


def test_measure_returns_all_metrics(clean):
    assert set(clean) == {"megapixels", "sharpness", "noise_sigma", "blockiness", "detail_ratio"}
    assert clean["megapixels"] == 1.08


def test_blur_lowers_sharpness(clean):
    blurred = en.measure(scene().filter(ImageFilter.GaussianBlur(3)))
    assert blurred["sharpness"] < clean["sharpness"] / 5


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


def test_small_image_uses_smaller_crops():
    result = en.measure(scene(size=(400, 300)))
    assert result["megapixels"] == 0.12


# --- Правила --------------------------------------------------------------------


def test_all_ok_is_not_needed_without_model():
    result = en.decide(en.findings(metrics()))
    assert result == {"decision": en.NOT_NEEDED, "disputed": False, "reasons": [], "operations": []}


@pytest.mark.parametrize("override,reason,operation", [
    ({"sharpness": 40.0}, "sharpness", "sharpen"),
    ({"noise_sigma": 8.0}, "noise", "denoise"),
    ({"blockiness": 2.0}, "artifacts", "remove_compression_artifacts"),
    ({"megapixels": 3.0}, "resolution", "upscale"),
])
def test_clear_issue_is_recommended_with_reason(override, reason, operation):
    result = en.decide(en.findings(metrics(**override)))
    assert result["decision"] == en.RECOMMENDED and not result["disputed"]
    assert [r["reason"] for r in result["reasons"]] == [reason]
    assert result["operations"] == [operation]


@pytest.mark.parametrize("override", [{"sharpness": 5.0}, {"noise_sigma": 15.0}, {"blockiness": 4.0}, {"megapixels": 0.5}])
def test_severe_is_risky_without_operations(override):
    result = en.decide(en.findings(metrics(**override)))
    assert result["decision"] == en.RISKY
    assert result["reasons"] and result["operations"] == []


def test_severe_wins_over_issue_and_lists_both():
    result = en.decide(en.findings(metrics(sharpness=5.0, noise_sigma=8.0)))
    assert result["decision"] == en.RISKY
    assert {r["reason"] for r in result["reasons"]} == {"sharpness", "noise"}


def test_borderline_only_is_disputed_for_model():
    result = en.decide(en.findings(metrics(noise_sigma=4.0)))
    assert result["decision"] is None and result["disputed"] is True
    assert result["reasons"] == [{"reason": "noise", "level": "borderline", "detail": "noise_sigma=4.0"}]


def test_issue_with_borderline_is_decided_by_rules():
    result = en.decide(en.findings(metrics(noise_sigma=4.0, blockiness=2.0)))
    assert result["decision"] == en.RECOMMENDED and not result["disputed"]


@pytest.mark.parametrize("value,level", [(150.0, en.OK), (149.9, en.BORDERLINE), (50.0, en.BORDERLINE), (49.9, en.ISSUE), (15.0, en.ISSUE), (14.9, en.SEVERE)])
def test_sharpness_boundaries(value, level):
    (sharpness,) = [f for f in en.findings(metrics(sharpness=value)) if f["reason"] == "sharpness"]
    assert sharpness["level"] == level


def test_soft_at_native_is_note_not_topaz():
    result = en.assess(metrics(megapixels=50.3, detail_ratio=0.12), "hash")
    assert result["decision"] == en.NOT_NEEDED
    assert [n["code"] for n in result["notes"]] == ["SOFT_AT_NATIVE_RESOLUTION"]
    assert "12.6 MP" in result["notes"][0]["detail"]


def test_assess_structure_and_fingerprint():
    result = en.assess(metrics(), "hash-a")
    assert result["rules_version"] == "enhancement-rules-v1" and result["provider"] == "rules"
    assert result["fingerprint"] == en.fingerprint("hash-a") != en.fingerprint("hash-b")
    assert [f["reason"] for f in result["findings"]] == ["sharpness", "noise", "artifacts", "resolution"]


_PHONE_PHOTO = ROOT / "data" / "incoming" / "IMG_20260911_130437.jpg"


@pytest.mark.skipif(not _PHONE_PHOTO.exists(), reason="real phone photo is not available")
def test_real_50mp_phone_photo_needs_no_topaz_but_is_soft():
    result = en.assess(en.read_metrics(_PHONE_PHOTO), "hash")
    assert result["decision"] == en.NOT_NEEDED
    assert [n["code"] for n in result["notes"]] == ["SOFT_AT_NATIVE_RESOLUTION"]
