"""
Проверка согласованности состояния и событий (только чтение; паспорт §35ZN).

    python scripts/check_consistency.py

Принцип: результат стадии и его событие пишутся одной транзакцией. Скрипт ищет
следы нарушений (например, после аварийного сброса ПК 27.09.2026 — asset 118):
результат без события и событие без результата. Ничего не исправляет.
"""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import ingest  # noqa: E402
from app.database import db  # noqa: E402

CHECKS = {
    "asset without INGEST/DONE": """
        SELECT a.id FROM assets a WHERE NOT EXISTS (
            SELECT 1 FROM processing_events e WHERE e.asset_id = a.id AND e.stage = 'INGEST' AND e.status = 'DONE')""",
    "qc_result without QC event": """
        SELECT a.id FROM assets a WHERE a.qc_result IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM processing_events e WHERE e.asset_id = a.id AND e.stage = 'QC')""",
    "ai_result without AI/PASSED": """
        SELECT a.id FROM assets a WHERE a.ai_result IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM processing_events e WHERE e.asset_id = a.id AND e.stage = 'AI' AND e.status = 'PASSED')""",
    "AI/PASSED without ai_result": """
        SELECT a.id FROM assets a WHERE a.ai_result IS NULL AND EXISTS (
            SELECT 1 FROM processing_events e WHERE e.asset_id = a.id AND e.stage = 'AI' AND e.status = 'PASSED')""",
    "metadata_json without METADATA event": """
        SELECT a.id FROM assets a WHERE a.metadata_json IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM processing_events e WHERE e.asset_id = a.id AND e.stage = 'METADATA')""",
    "METADATA_AI/PASSED without metadata_json": """
        SELECT a.id FROM assets a WHERE a.metadata_json IS NULL AND EXISTS (
            SELECT 1 FROM processing_events e WHERE e.asset_id = a.id AND e.stage = 'METADATA_AI' AND e.status = 'PASSED')""",
    "events of a missing asset": """
        SELECT DISTINCT e.asset_id FROM processing_events e WHERE NOT EXISTS (SELECT 1 FROM assets a WHERE a.id = e.asset_id)""",
}


def _latest_manifests(connection) -> dict[int, dict]:
    rows = connection.execute(
        """SELECT e.asset_id, e.message FROM processing_events e WHERE e.id IN (
               SELECT MAX(id) FROM processing_events WHERE stage = 'NORMALIZE' AND status = 'PASSED' GROUP BY asset_id)"""
    )
    return {asset_id: json.loads(message) for asset_id, message in rows}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _derivative_problems(connection, root: Path) -> list[int]:
    """NORMALIZE/PASSED ссылается на internal derivative, которого нет или который изменён."""
    broken = []
    for asset_id, manifest in _latest_manifests(connection).items():
        derivative = manifest.get("derivative")
        if derivative:
            path = root / derivative["path"]
            if not path.exists() or _sha256(path) != derivative["sha256"]:
                broken.append(asset_id)
    return sorted(broken)


def _export_problems(connection, root: Path) -> list[int]:
    """
    Последний DERIVATIVE/CREATED площадки ссылается на файл экспорта, которого нет или который изменён.
    У отклонённых объектов (metadata rejected) отсутствие файла — не нарушение: старые экспортные
    файлы удаляются вручную (паспорт §35ZZZF). Изменённый файл — нарушение всегда.
    """
    rejected = {asset_id for (asset_id,) in connection.execute(
        "SELECT id FROM assets WHERE json_extract(metadata_json, '$.state') = 'rejected'")}
    latest = {}
    for asset_id, message in connection.execute(
            "SELECT asset_id, message FROM processing_events WHERE stage = 'DERIVATIVE' AND status = 'CREATED' ORDER BY id"):
        created = json.loads(message)
        if created.get("purpose") == "export":
            latest[(asset_id, created.get("platform"))] = created
    broken = set()
    for (asset_id, _), created in latest.items():
        path = root / created["path"]
        if not path.exists():
            if asset_id not in rejected:
                broken.add(asset_id)
        elif _sha256(path) != created["sha256"]:
            broken.add(asset_id)
    return sorted(broken)


def find_problems(db_path: Path | None = None) -> dict[str, list[int]]:
    """{проверка: id объектов с нарушением} — только чтение. БД по умолчанию — из db (STOCKER_DATA_DIR)."""
    db_path = db_path or db.db_path()
    root = ingest.ROOT  # пути производных — относительно корня проекта
    connection = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        problems = {name: [row[0] for row in connection.execute(query)] for name, query in CHECKS.items()}
        problems["NORMALIZE/PASSED derivative missing or changed"] = _derivative_problems(connection, root)
        problems["DERIVATIVE/CREATED export file missing or changed"] = _export_problems(connection, root)
        return problems
    finally:
        connection.close()


def find_orphans(db_path: Path | None = None) -> list[str]:
    """
    Файлы в <DATA_DIR>/internal без события (сбой между записью файла и событием). Не ошибка
    согласованности: результата без события нет, следующий прогон переиспользует файл.
    """
    db_path = db_path or db.db_path()
    root = ingest.ROOT
    internal = db.internal_dir(db.data_dir_of(db_path))
    if not internal.exists():
        return []
    connection = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        referenced = {
            json.loads(message)["derivative"]["path"]
            for (message,) in connection.execute("SELECT message FROM processing_events WHERE stage = 'NORMALIZE' AND status = 'PASSED'")
            if json.loads(message).get("derivative")
        }
    finally:
        connection.close()
    files = (path.relative_to(root).as_posix() for path in internal.rglob("*") if path.is_file())
    return sorted(path for path in files if path not in referenced)


def find_export_orphans(db_path: Path | None = None) -> list[str]:
    """Файлы в <DATA_DIR>/export без DERIVATIVE/CREATED (сбой между записью файла и событием). Не ошибка."""
    db_path = db_path or db.db_path()
    root = ingest.ROOT
    export = db.export_dir_of(db.data_dir_of(db_path))
    if not export.exists():
        return []
    connection = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        referenced = {
            json.loads(message).get("path")
            for (message,) in connection.execute("SELECT message FROM processing_events WHERE stage = 'DERIVATIVE' AND status = 'CREATED'")
        }
    finally:
        connection.close()
    files = (path.relative_to(root).as_posix() for path in export.rglob("*") if path.is_file())
    return sorted(path for path in files if path not in referenced)


def main() -> int:
    print(db.describe())
    problems = find_problems()
    for name, ids in problems.items():
        print(f"{'OK  ' if not ids else 'FAIL'} {name}: {ids if ids else '-'}")
    orphans = find_orphans()
    print(f"INFO internal files without NORMALIZE/PASSED: {len(orphans)}")
    for path in orphans:
        print(f"     {path}")
    export_orphans = find_export_orphans()
    print(f"INFO export files without DERIVATIVE/CREATED: {len(export_orphans)}")
    for path in export_orphans:
        print(f"     {path}")
    return 1 if any(problems.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
