"""
Проверка согласованности состояния и событий (только чтение; паспорт §35ZN).

    python scripts/check_consistency.py

Принцип: результат стадии и его событие пишутся одной транзакцией. Скрипт ищет
следы нарушений (например, после аварийного сброса ПК 27.09.2026 — asset 118):
результат без события и событие без результата. Ничего не исправляет.
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "db" / "stocker.db"

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


def main() -> int:
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        problems = 0
        for name, query in CHECKS.items():
            ids = [row[0] for row in connection.execute(query)]
            problems += len(ids)
            print(f"{'OK  ' if not ids else 'FAIL'} {name}: {ids if ids else '-'}")
    finally:
        connection.close()
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
