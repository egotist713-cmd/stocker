from pathlib import Path
import json
import sqlite3

import numpy as np
from PIL import Image

from app.ingest import source_file


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "db" / "stocker.db"


MIN_WIDTH = 4000
MIN_HEIGHT = 3000
MIN_FILE_SIZE = 100 * 1024
MAX_FILE_SIZE = 45 * 1024 * 1024


def get_connection():
    conn = sqlite3.connect(DB_PATH)
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
    preview.thumbnail((1600, 1600))

    rgb = np.asarray(preview.convert("RGB"), dtype=np.uint8)

    dark = np.all(rgb <= 5, axis=2)
    bright = np.all(rgb >= 250, axis=2)

    total = rgb.shape[0] * rgb.shape[1]

    dark_ratio = float(dark.sum() / total)
    bright_ratio = float(bright.sum() / total)

    return dark_ratio, bright_ratio


def check_asset(asset):
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

    try:
        with Image.open(source_path) as image:
            image.verify()

        with Image.open(source_path) as image:
            width, height = image.size
            image_format = image.format

            result["metrics"]["width"] = width
            result["metrics"]["height"] = height
            result["metrics"]["format"] = image_format

            if width < MIN_WIDTH or height < MIN_HEIGHT:
                result["passed"] = False
                result["errors"].append("RESOLUTION_TOO_LOW")

            sharpness = calculate_sharpness(image)
            dark_ratio, bright_ratio = calculate_extreme_pixels(image)

            result["metrics"]["sharpness"] = sharpness
            result["metrics"]["dark_ratio"] = dark_ratio
            result["metrics"]["bright_ratio"] = bright_ratio

            # Пока это только предупреждения.
            # Не отклоняем фотографию автоматически по этим двум метрикам.
            if dark_ratio > 0.20:
                result["warnings"].append("HIGH_DARK_PIXEL_RATIO")

            if bright_ratio > 0.20:
                result["warnings"].append("HIGH_BRIGHT_PIXEL_RATIO")

    except Exception as exc:
        result["passed"] = False
        result["errors"].append(f"IMAGE_READ_ERROR: {exc}")

    return result


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

        conn.execute(
            """
            INSERT INTO processing_events
            (asset_id, stage, status, message)
            VALUES (?, ?, ?, ?)
            """,
            (
                asset_id,
                "QC",
                status,
                json.dumps(result, ensure_ascii=False),
            ),
        )

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
    run_qc()