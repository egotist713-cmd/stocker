"""
Enhancement decision: детерминированные метрики качества и решение правилами
(docs/STOCK_READINESS_CONTRACT.md §4.2, enhancement-rules-v1).

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

RULES_VERSION = "enhancement-rules-v1"

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


def measure(image: Image.Image) -> dict:
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
    }


def read_metrics(path: Path) -> dict:
    with Image.open(path) as image:
        return measure(image)


# --- Правила (пороги v1, калибровка — контракт §4.2) ---------------------------------

# (metric, reason, higher_is_better, (ok, borderline, issue) пороги, operation)
THRESHOLDS = (
    ("sharpness", "sharpness", True, (150.0, 50.0, 15.0), "sharpen"),
    ("noise_sigma", "noise", False, (3.0, 5.0, 12.0), "denoise"),
    ("blockiness", "artifacts", False, (1.2, 1.5, 3.0), "remove_compression_artifacts"),
    ("megapixels", "resolution", True, (6.0, 4.0, 1.0), "upscale"),
)


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
    return [
        {"reason": reason, "metric": metric, "value": metrics[metric],
         "level": _level(metrics[metric], higher, bounds), "operation": operation}
        for metric, reason, higher, bounds, operation in THRESHOLDS
    ]


def notes(metrics: dict) -> list[dict]:
    """Предупреждения без действия: ничего не уменьшается автоматически (решение — по статистике отказов)."""
    if metrics["detail_ratio"] >= SOFT_DETAIL_RATIO:
        return []
    return [{
        "code": "SOFT_AT_NATIVE_RESOLUTION",
        "level": "warning",
        "detail": f"detail_ratio {metrics['detail_ratio']} < {SOFT_DETAIL_RATIO}: "
                  f"effective detail about {metrics['megapixels'] / 4:.1f} MP of {metrics['megapixels']} MP",
    }]


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
