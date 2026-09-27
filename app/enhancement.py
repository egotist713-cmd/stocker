"""
Enhancement decision: детерминированные метрики качества и решение правилами
(docs/STOCK_READINESS_CONTRACT.md §4.2, enhancement-rules-v2).

v2 (калибровка на 108 отобранных фото, 27.09.2026): v1 давал 26 % ложных
рекомендаций — путал малую глубину резкости, фактуру и обычный JPEG телефона
с дефектами. Теперь резкость — по самым резким участкам кадра (объект, а не фон),
артефакты — по фактическому качеству JPEG из таблиц квантования.

Модель здесь не вызывается: явные случаи решают правила, спорные помечаются
disputed — рекомендацию для них даёт локальная модель отдельно. Topaz не
запускается. Файл только читается.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

RULES_VERSION = "enhancement-rules-v2"

NOT_NEEDED = "enhancement_not_needed"
RECOMMENDED = "enhancement_recommended"
RISKY = "enhancement_risky"

OK = "ok"
BORDERLINE = "borderline"
ISSUE = "issue"
SEVERE = "severe"

ANALYSIS_SIDE = 2048
CROP = 1024
FLAT_PERCENTILE = 30
SOFT_DETAIL_RATIO = 0.2
TILE = 256                  # плитка на копии 2048 px
SHARP_TILE = 150.0          # плитка «резкая» (для доли резкой площади)
PEAK_SHARE = 0.10           # peak — среднее по 10 % самых резких плиток (не меньше 3)
ISOLATED_SUBJECT_RATIO = 0.15

# Стандартная таблица квантования яркости (JPEG Annex K): по ней оценивается качество файла.
_STD_LUMA = np.array([
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55, 14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62, 18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99,
], dtype=np.float64)


# --- Метрики -----------------------------------------------------------------------

def _gray(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("L"), dtype=np.float32)


def _laplacian(a: np.ndarray) -> np.ndarray:
    return 4 * a[1:-1, 1:-1] - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:]


def _crops(image: Image.Image) -> list[Image.Image]:
    """Центр и четыре точки по сетке 1/5 — 3/5; выравнивание по блокам 8×8 JPEG."""
    size = min(CROP, image.width, image.height) // 8 * 8
    points = [((image.width - size) // 2, (image.height - size) // 2)]
    points += [(x, y) for x in (image.width // 5, 3 * image.width // 5) for y in (image.height // 5, 3 * image.height // 5)]
    crops = []
    for x, y in points:
        x, y = min(x, image.width - size) // 8 * 8, min(y, image.height - size) // 8 * 8
        crops.append(image.crop((x, y, x + size, y + size)))
    return crops


def _noise_sigma(a: np.ndarray) -> float:
    """Immerkær: σ по свёртке с ядром второй производной, только «плоские» пиксели."""
    residual = (
        a[:-2, :-2] - 2 * a[:-2, 1:-1] + a[:-2, 2:]
        - 2 * a[1:-1, :-2] + 4 * a[1:-1, 1:-1] - 2 * a[1:-1, 2:]
        + a[2:, :-2] - 2 * a[2:, 1:-1] + a[2:, 2:]
    )
    gradient = np.abs(a[1:-1, 2:] - a[1:-1, :-2]) + np.abs(a[2:, 1:-1] - a[:-2, 1:-1])
    flat = gradient <= np.percentile(gradient, FLAT_PERCENTILE)
    return float(np.sqrt(np.pi / 2) / 6 * np.mean(np.abs(residual[flat])))


def _blockiness(a: np.ndarray) -> float:
    """Перепад на границе блока 8×8 (индекс 7) к середине блока (индекс 3, та же чётность)."""
    dx = np.abs(np.diff(a, axis=1)).mean(axis=0)
    dy = np.abs(np.diff(a, axis=0)).mean(axis=1)
    profile = np.array([dx[i::8].mean() + dy[i::8].mean() for i in range(8)])
    return float(profile[7] / (profile[3] + 1e-6))


def _detail_ratio(crop: Image.Image, a: np.ndarray) -> float:
    """Энергия лапласиана в исходном разрешении к половинному: мало — мягко на 100 %."""
    half = _gray(crop.resize((crop.width // 2, crop.height // 2), Image.LANCZOS))
    return float(np.var(_laplacian(a)) / (np.var(_laplacian(half)) + 1e-6))


def jpeg_quality(image: Image.Image) -> int | None:
    """Качество JPEG (1–100) по таблице квантования яркости; None — не JPEG."""
    tables = getattr(image, "quantization", None)
    if not tables or 0 not in tables or len(tables[0]) != 64:
        return None
    scale = float(np.mean(np.asarray(tables[0], dtype=np.float64) / _STD_LUMA)) * 100
    quality = (200 - scale) / 2 if scale <= 100 else 5000 / scale
    return int(round(min(100.0, max(1.0, quality))))


def _tile_sharpness(analysis: Image.Image, scale: float) -> dict:
    """Резкость по плиткам: peak — самые резкие участки (объект), доля резкой площади, где они."""
    a = _gray(analysis)
    size = min(TILE, a.shape[0], a.shape[1])
    tiles = []
    for y in range(0, a.shape[0] - size + 1, size):
        for x in range(0, a.shape[1] - size + 1, size):
            tiles.append((float(np.var(_laplacian(a[y:y + size, x:x + size]))), x, y))
    tiles.sort(reverse=True)
    top = tiles[: max(3, int(len(tiles) * PEAK_SHARE))]
    best_x, best_y = top[0][1], top[0][2]
    return {
        "sharpness_peak": round(float(np.mean([t[0] for t in top])), 1),
        "sharp_tile_ratio": round(sum(t[0] >= SHARP_TILE for t in tiles) / len(tiles), 2),
        # центр самой резкой плитки в координатах исходного кадра (фрагмент для советника)
        "sharpest_point": [round((best_x + size / 2) / scale), round((best_y + size / 2) / scale)],
    }


def measure(image: Image.Image) -> dict:
    quality = jpeg_quality(image)
    image = image.convert("RGB")
    scale = min(1.0, ANALYSIS_SIDE / max(image.size))
    analysis = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS) if scale < 1 else image

    noise, blocks, details = [], [], []
    for crop in _crops(image):
        a = _gray(crop)
        noise.append(_noise_sigma(a))
        blocks.append(_blockiness(a))
        details.append(_detail_ratio(crop, a))

    return {
        "megapixels": round(image.width * image.height / 1_000_000, 2),
        "sharpness": round(float(np.var(_laplacian(_gray(analysis)))), 1),
        "noise_sigma": round(float(np.median(noise)), 2),
        "blockiness": round(float(np.median(blocks)), 3),
        "detail_ratio": round(float(np.median(details)), 3),
        **_tile_sharpness(analysis, scale),
        "jpeg_quality": quality,
    }


def read_metrics(path: Path) -> dict:
    with Image.open(path) as image:
        return measure(image)


# --- Правила (пороги v2, калибровка — контракт §4.2) ---------------------------------

# (metric, reason, higher_is_better, (ok, borderline, issue) пороги, operation)
THRESHOLDS = (
    ("sharpness_peak", "sharpness", True, (120.0, 40.0, 20.0), "sharpen"),
    ("noise_sigma", "noise", False, (3.0, 5.0, 12.0), "denoise"),
    ("blockiness", "artifacts", False, (1.2, 1.5, 3.0), "remove_compression_artifacts"),
    ("megapixels", "resolution", True, (6.0, 4.0, 1.0), "upscale"),
)

# Для JPEG артефакты оцениваются по фактическому качеству файла: блочность 1.2–1.5
# у JPEG телефона с качеством 93+ — это фактура и обработка, а не артефакты сжатия.
JPEG_QUALITY_BOUNDS = (80.0, 60.0, 30.0)


def _level(value: float, higher_is_better: bool, bounds: tuple[float, float, float]) -> str:
    ok, borderline, issue = bounds
    if higher_is_better:
        if value >= ok:
            return OK
        if value >= borderline:
            return BORDERLINE
        return ISSUE if value >= issue else SEVERE
    if value < ok:
        return OK
    if value < borderline:
        return BORDERLINE
    return ISSUE if value < issue else SEVERE


def findings(metrics: dict) -> list[dict]:
    result = []
    for metric, reason, higher, bounds, operation in THRESHOLDS:
        if reason == "artifacts" and metrics.get("jpeg_quality") is not None:
            metric, higher, bounds = "jpeg_quality", True, JPEG_QUALITY_BOUNDS
        value = metrics[metric]
        result.append({"reason": reason, "metric": metric, "value": value,
                       "level": _level(value, higher, bounds), "operation": operation})
    return result


def notes(metrics: dict) -> list[dict]:
    """Замечания без действия: ничего не меняется автоматически (решение — по статистике отказов)."""
    result = []
    if metrics["detail_ratio"] < SOFT_DETAIL_RATIO:
        result.append({
            "code": "SOFT_AT_NATIVE_RESOLUTION",
            "level": "warning",
            "detail": f"detail_ratio {metrics['detail_ratio']} < {SOFT_DETAIL_RATIO}: "
                      f"effective detail about {metrics['megapixels'] / 4:.1f} MP of {metrics['megapixels']} MP",
        })
    # Тип снимка: резкий объект на малой доле кадра — малая глубина резкости, фон размыт намеренно.
    if metrics["sharp_tile_ratio"] < ISOLATED_SUBJECT_RATIO and metrics["sharpness_peak"] >= THRESHOLDS[0][3][0]:
        result.append({
            "code": "ISOLATED_SUBJECT",
            "level": "info",
            "detail": f"sharp area {metrics['sharp_tile_ratio']:.0%} of the frame: shallow depth of field, "
                      "background blur is not a defect",
        })
    return result


def decide(found: list[dict]) -> dict:
    """Явный случай — решение правилами; только borderline — disputed (решает модель)."""
    by_level = {level: [f for f in found if f["level"] == level] for level in (SEVERE, ISSUE, BORDERLINE)}

    def reasons(items):
        return [{"reason": f["reason"], "level": f["level"], "detail": f"{f['metric']}={f['value']}"} for f in items]

    if by_level[SEVERE]:
        return {"decision": RISKY, "disputed": False, "reasons": reasons(by_level[SEVERE] + by_level[ISSUE]), "operations": []}
    if by_level[ISSUE]:
        return {"decision": RECOMMENDED, "disputed": False, "reasons": reasons(by_level[ISSUE]),
                "operations": [f["operation"] for f in by_level[ISSUE]]}
    if by_level[BORDERLINE]:
        return {"decision": None, "disputed": True, "reasons": reasons(by_level[BORDERLINE]), "operations": []}
    return {"decision": NOT_NEEDED, "disputed": False, "reasons": [], "operations": []}


def fingerprint(file_hash: str | None) -> str:
    payload = json.dumps({"rules_version": RULES_VERSION, "file_hash": file_hash}, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assess(metrics: dict, file_hash: str | None) -> dict:
    found = findings(metrics)
    return {
        "rules_version": RULES_VERSION,
        "assessed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fingerprint": fingerprint(file_hash),
        "provider": "rules",
        "metrics": metrics,
        "findings": found,
        **decide(found),
        "notes": notes(metrics),
    }
