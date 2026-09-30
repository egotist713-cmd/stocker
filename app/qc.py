from pathlib import Path
import hashlib
import json
import sqlite3

import numpy as np
from PIL import Image

from app import analysis_view
from app.database import db
from app.database.db import insert_event
from app.ingest import source_file


# Технический минимум — требования площадок (Adobe Stock и Shutterstock: 4 MP).
# Ниже рекомендации — только warning: дальше решают Enhancement и Readiness.
MIN_MEGAPIXELS = 4.0
RECOMMENDED_MEGAPIXELS = 12.0
MIN_FILE_SIZE = 100 * 1024
MAX_FILE_SIZE = 45 * 1024 * 1024

# Доли почти чёрных / почти белых пикселей (только warnings) и размер превью для них.
EXTREME_RATIO = 0.20
DARK_LEVEL = 5
BRIGHT_LEVEL = 250
EXTREME_PREVIEW = 1600

# Версия формул QC (резкость, доли тёмного / светлого, проверки). Значения порогов
# входят в отпечаток сами (rules()), поэтому их смена тоже делает результат STALE.
# v2 (28.09.2026): пиксели — из AnalysisView.
# v3 (30.09.2026): чёрные полосы по краям (EDGE_BORDER, фиксированные окна; паспорт §35ZZZB).
# v4 (30.09.2026): EDGE_BORDER — глубина по каждой колонке (полосы до 3 % кадра; §35ZZZC).
QC_RULES_VERSION = "qc-rules-v4"

# Чёрные полосы по краям (EDGE_BORDER). Для стороны — линии от края внутрь; по каждой колонке
# (строке): r — длина тёмного (<= DARK) прогона от края; outer = mean линий 0…min(r, 8),
# inner = mean линий r+2…r+9. Колонка полосная, если outer <= DARK и inner - outer >= DIFF
# (скачок сразу за прогоном — тень и градиент его не дают). Ширина стороны w = медиана r
# полосных колонок; согласованные — |r - w| <= max(3, w / 4); полоса, если их доля >= SHARE.
# Прогон шире MAX_WIDTH_RATIO размера кадра по этой оси — тёмный сюжет, не полоса.
# Не по максимуму линии: у реальных полос края кадра светлее (prod #9: max строки 110).
# Пороги: prod #4 / #5 / #9 / #10 найдены, 130 тестовых исходников — 0 срабатываний.
EDGE_DARK = 25
EDGE_DIFF = 20
EDGE_SHARE = 0.4
EDGE_OUTER_LINES = 8
EDGE_INNER_OFFSET = 2
EDGE_INNER_LINES = 8
EDGE_CONSISTENCY_PX = 3
EDGE_CONSISTENCY_RATIO = 0.25
EDGE_MAX_WIDTH_RATIO = 0.03


def rules() -> dict:
    """Всё, от чего зависит QC, кроме view (контракт ASSET_STATE §2.1a)."""
    return {
        "version": QC_RULES_VERSION,
        "min_megapixels": MIN_MEGAPIXELS,
        "recommended_megapixels": RECOMMENDED_MEGAPIXELS,
        "min_file_size": MIN_FILE_SIZE,
        "max_file_size": MAX_FILE_SIZE,
        "extreme_ratio": EXTREME_RATIO,
        "dark_level": DARK_LEVEL,
        "bright_level": BRIGHT_LEVEL,
        "extreme_preview": EXTREME_PREVIEW,
        "edge_dark": EDGE_DARK,
        "edge_diff": EDGE_DIFF,
        "edge_share": EDGE_SHARE,
        "edge_outer_lines": EDGE_OUTER_LINES,
        "edge_inner_offset": EDGE_INNER_OFFSET,
        "edge_inner_lines": EDGE_INNER_LINES,
        "edge_consistency_px": EDGE_CONSISTENCY_PX,
        "edge_consistency_ratio": EDGE_CONSISTENCY_RATIO,
        "edge_max_width_ratio": EDGE_MAX_WIDTH_RATIO,
    }


def inputs(view_fingerprint: str) -> dict:
    return {"view": view_fingerprint, "rules": rules()}


def fingerprint(view_fingerprint: str) -> str:
    """Отпечаток входов QC: view (source / normalization / views) + правила и пороги."""
    payload = json.dumps(inputs(view_fingerprint), sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_connection():
    conn = sqlite3.connect(db.db_path())  # путь — только из db (STOCKER_DATA_DIR)
    conn.row_factory = sqlite3.Row
    return conn


def calculate_sharpness(image: Image.Image) -> float:
    """
    Простая оценка резкости через дисперсию градиента.
    Чем выше значение, тем больше мелких контрастных деталей.
    """
    gray = np.asarray(image.convert("L"), dtype=np.float32)

    gx = np.diff(gray, axis=1)
    gy = np.diff(gray, axis=0)

    return float(np.var(gx) + np.var(gy))


def calculate_extreme_pixels(image: Image.Image):
    """
    Оцениваем долю почти чёрных и почти белых пикселей.
    Работаем с уменьшенной копией, чтобы не расходовать лишнюю память.
    """
    preview = image.copy()
    preview.thumbnail((EXTREME_PREVIEW, EXTREME_PREVIEW))

    rgb = np.asarray(preview.convert("RGB"), dtype=np.uint8)

    dark = np.all(rgb <= DARK_LEVEL, axis=2)
    bright = np.all(rgb >= BRIGHT_LEVEL, axis=2)

    total = rgb.shape[0] * rgb.shape[1]

    dark_ratio = float(dark.sum() / total)
    bright_ratio = float(bright.sum() / total)

    return dark_ratio, bright_ratio


def _side_lines(gray: np.ndarray, depth: int) -> dict:
    """Массивы (линия от края, позиция вдоль края) для каждой стороны, depth линий."""
    return {"top": gray[:depth, :], "bottom": gray[::-1][:depth, :],
            "left": gray[:, :depth].T, "right": gray[:, ::-1][:, :depth].T}


def _side_border(lines: np.ndarray, limit: int) -> dict | None:
    """Полоса одной стороны: lines — (линия от края, колонка); limit — наибольшая ширина полосы."""
    dark = lines[:limit] <= EDGE_DARK
    run = np.where(dark.all(axis=0), limit, np.argmin(dark, axis=0))  # r: тёмный прогон от края
    columns = np.arange(lines.shape[1])
    cumsum = np.vstack([np.zeros((1, lines.shape[1]), np.float32), np.cumsum(lines, axis=0)])
    n_outer = np.clip(np.minimum(run, EDGE_OUTER_LINES), 1, None)
    outer = cumsum[n_outer, columns] / n_outer
    lo = run + EDGE_INNER_OFFSET
    hi = np.minimum(lo + EDGE_INNER_LINES, lines.shape[0])
    inner = (cumsum[hi, columns] - cumsum[np.minimum(lo, hi), columns]) / np.clip(hi - lo, 1, None)
    banded = (run >= 1) & (run < limit) & (hi > lo) & (outer <= EDGE_DARK) & (inner - outer >= EDGE_DIFF)
    if not banded.any():
        return None
    width = int(np.median(run[banded]))
    consistent = banded & (np.abs(run - width) <= max(EDGE_CONSISTENCY_PX, EDGE_CONSISTENCY_RATIO * width))
    share = float(consistent.mean())
    return {"width": width, "share": round(share, 4)} if share >= EDGE_SHARE else None


def edge_border(image: Image.Image) -> list[dict]:
    """Стороны с чёрной полосой: [{side, width, share}]; пусто — полос нет."""
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    height, width = gray.shape
    found = []
    for side, axis in (("top", height), ("bottom", height), ("left", width), ("right", width)):
        limit = int(axis * EDGE_MAX_WIDTH_RATIO)  # шире — тёмный сюжет, не полоса
        if limit < 1:
            continue
        lines = _side_lines(gray, limit + EDGE_INNER_OFFSET + EDGE_INNER_LINES)[side]
        border = _side_border(lines, limit)
        if border:
            found.append({"side": side, **border})
    return found


def check_asset(asset, view=None):
    """
    QC по AnalysisView (пиксели: размеры, резкость, доли тёмного / светлого) и по файлу
    источника (размер файла). Файл изображения QC сам не декодирует: view — один на все
    стадии (worker передаёт его), иначе строится по манифесту NORMALIZE/PASSED.
    """
    source_path = source_file(asset)

    result = {
        "passed": True,
        "errors": [],
        "warnings": [],
        "metrics": {},
    }

    if not source_path.exists():
        result["passed"] = False
        result["errors"].append("FILE_NOT_FOUND")
        return result

    file_size = source_path.stat().st_size

    result["metrics"]["file_size"] = file_size

    if file_size < MIN_FILE_SIZE:
        result["passed"] = False
        result["errors"].append("FILE_TOO_SMALL")

    if file_size > MAX_FILE_SIZE:
        result["passed"] = False
        result["errors"].append("FILE_TOO_LARGE")

    if view is None:
        try:
            view = analysis_view.open_asset_view(asset["id"])
        except analysis_view.ViewUnavailable as exc:
            result["passed"] = False
            result["errors"].append(f"VIEW_UNAVAILABLE: {exc.code}")
            return result

    image = view.full
    width, height = view.size

    result["metrics"]["width"] = width
    result["metrics"]["height"] = height
    result["metrics"]["format"] = view.source_format
    result["metrics"]["view"] = view.identity()
    # Отпечаток входов: результат актуален, пока совпадает (контракт ASSET_STATE §2.1a).
    result["inputs"] = inputs(view.fingerprint)
    result["input_fingerprint"] = fingerprint(view.fingerprint)

    megapixels = width * height / 1_000_000
    if megapixels < MIN_MEGAPIXELS:
        result["passed"] = False
        result["errors"].append("RESOLUTION_TOO_LOW")
    elif megapixels < RECOMMENDED_MEGAPIXELS:
        result["warnings"].append("RESOLUTION_BELOW_RECOMMENDED")

    sharpness = calculate_sharpness(image)
    dark_ratio, bright_ratio = calculate_extreme_pixels(image)

    result["metrics"]["sharpness"] = sharpness
    result["metrics"]["dark_ratio"] = dark_ratio
    result["metrics"]["bright_ratio"] = bright_ratio

    # Пока это только предупреждения.
    # Не отклоняем фотографию автоматически по этим двум метрикам.
    if dark_ratio > EXTREME_RATIO:
        result["warnings"].append("HIGH_DARK_PIXEL_RATIO")

    if bright_ratio > EXTREME_RATIO:
        result["warnings"].append("HIGH_BRIGHT_PIXEL_RATIO")

    # Чёрные полосы по краям — предупреждение, не отказ: объект уходит к человеку
    # (эскалация metadata после QC, worker / reprocess), не reject и не обрезка.
    borders = edge_border(image)
    result["metrics"]["edge_border"] = borders
    if borders:
        result["warnings"].append("EDGE_BORDER")
        result["edge_border"] = borders

    return result


def edge_escalation_reason(qc_result: dict | None) -> str | None:
    """Причина эскалации по результату QC: «EDGE_BORDER: чёрные полосы — top 11 px»."""
    borders = (qc_result or {}).get("edge_border") or []
    if not borders:
        return None
    return "EDGE_BORDER: чёрные полосы — " + ", ".join(f"{b['side']} {b['width']} px" for b in borders)


def save_qc_result(asset_id, result):
    status = "PASSED" if result["passed"] else "FAILED"

    conn = get_connection()

    try:
        conn.execute(
            """
            UPDATE assets
            SET status = ?,
                qc_result = ?,
                rejection_reason = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                status,
                json.dumps(result, ensure_ascii=False),
                "; ".join(result["errors"]) if result["errors"] else None,
                asset_id,
            ),
        )

        insert_event(conn, asset_id, "QC", status, json.dumps(result, ensure_ascii=False))

        conn.commit()

    finally:
        conn.close()


def run_qc():
    conn = get_connection()

    try:
        assets = conn.execute(
            """
            SELECT *
            FROM assets
            WHERE status = 'NEW'
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()

    print(f"Assets for QC: {len(assets)}")

    passed = 0
    failed = 0

    for asset in assets:
        print()
        print(f"Checking: {asset['filename']}")

        result = check_asset(asset)

        save_qc_result(asset["id"], result)

        print(f"  Resolution: {result['metrics'].get('width')}x{result['metrics'].get('height')}")
        print(f"  Format:     {result['metrics'].get('format')}")
        print(f"  Size:       {result['metrics'].get('file_size')} bytes")
        print(f"  Sharpness:  {result['metrics'].get('sharpness')}")
        print(f"  Dark:       {result['metrics'].get('dark_ratio')}")
        print(f"  Bright:     {result['metrics'].get('bright_ratio')}")

        if result["errors"]:
            print(f"  Errors:     {result['errors']}")

        if result["warnings"]:
            print(f"  Warnings:   {result['warnings']}")

        if result["passed"]:
            print("  QC:         PASSED")
            passed += 1
        else:
            print("  QC:         FAILED")
            failed += 1

    print()
    print("=== QC COMPLETE ===")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")


if __name__ == "__main__":
    print(db.describe())
    run_qc()