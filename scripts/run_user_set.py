"""
Калибровка на отобранных пользователем фотографиях (паспорт §3A.9, §35ZK).

    python scripts/run_user_set.py            # оценка набора из data/incoming
    python scripts/run_user_set.py --ids-only  # только список id набора

Набор — asset, содержимое которых (SHA256) совпадает с файлами в
data/incoming (включая совпавшие с другими выборками). Для каждого:
readiness.evaluate и creative.review через service layer; по одному asset,
последовательно. Список id — logs/user_set_ids.json (для pipeline_stats.py --ids).
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ingest import INCOMING, SUPPORTED_EXTENSIONS, sha256_file  # noqa: E402
from app.service import dispatch  # noqa: E402

DB = ROOT / "data" / "db" / "stocker.db"


def user_set_ids() -> tuple[list[int], list[str]]:
    hashes = {sha256_file(p): p.name for p in INCOMING.iterdir() if p.suffix.lower() in SUPPORTED_EXTENSIONS}
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT id, file_hash FROM assets").fetchall()
    finally:
        connection.close()
    found = {file_hash: asset_id for asset_id, file_hash in rows if file_hash in hashes}
    missing = sorted(name for file_hash, name in hashes.items() if file_hash not in found)
    return sorted(set(found.values())), missing


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Calibration run on the user-selected set")
    parser.add_argument("--ids-only", action="store_true")
    parser.add_argument("--actor", default="agent:claude-code")
    args = parser.parse_args(argv)

    ids, missing = user_set_ids()
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "user_set_ids.json").write_text(json.dumps(ids), encoding="utf-8")
    print(f"user set: {len(ids)} assets; files without asset: {missing}", flush=True)
    if args.ids_only:
        return 0

    started = time.time()
    for index, asset_id in enumerate(ids, 1):
        readiness = dispatch("readiness.evaluate", {"asset_id": asset_id}, actor=args.actor)
        creative = dispatch("creative.review", {"asset_id": asset_id}, actor=args.actor)
        print(f"[{index}/{len(ids)}] #{asset_id} readiness={readiness['outcome'] or readiness['error']['code']} "
              f"creative={creative['outcome'] or creative['error']['code']}", flush=True)
    print(f"done in {time.time() - started:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
