"""
Прогон полного pipeline на выборке реальных фотографий (аудит, паспорт §35ZF).

    python scripts/run_sample_batch.py data/samples/2026-09-26

Для каждого файла последовательно: asset.process_file (ingest → QC →
Enhancement → Vision → metadata → gate), затем readiness.evaluate — через
service layer, как любой другой actor. Ничего сверх обычного pipeline не
делает: файлы только читаются, решения человека не принимаются. Журнал —
logs/sample_batch_<папка>.jsonl. Повторный запуск пропускает уже
зарегистрированные файлы.
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ingest import SUPPORTED_EXTENSIONS, sha256_file, to_source_path  # noqa: E402
from app.service import dispatch  # noqa: E402

DB = ROOT / "data" / "db" / "stocker.db"


def _lookup(source_path: str, file_hash: str) -> tuple[int | None, int | None]:
    """(asset по этому пути, asset с таким же содержимым)."""
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        by_path = connection.execute("SELECT id FROM assets WHERE source_path = ?", (source_path,)).fetchone()
        by_hash = connection.execute("SELECT id FROM assets WHERE file_hash = ?", (file_hash,)).fetchone()
    finally:
        connection.close()
    return (by_path[0] if by_path else None), (by_hash[0] if by_hash else None)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Full pipeline on a sample folder")
    parser.add_argument("folder")
    parser.add_argument("--actor", default="agent:claude-code")
    args = parser.parse_args(argv)

    folder = (ROOT / args.folder).resolve()
    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS)
    log_path = ROOT / "logs" / f"sample_batch_{folder.name}.jsonl"
    log_path.parent.mkdir(exist_ok=True)
    print(f"{len(files)} files, actor {args.actor}, log {log_path.relative_to(ROOT)}", flush=True)

    with log_path.open("a", encoding="utf-8") as log:
        for index, path in enumerate(files, 1):
            source_path = to_source_path(path)
            file_hash = sha256_file(path)
            registered, duplicate_of = _lookup(source_path, file_hash)
            record = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "file": source_path}

            if registered:
                record.update(skipped="already_registered", asset_id=registered)
            elif duplicate_of:
                record.update(skipped="duplicate_content", duplicate_of=duplicate_of)
            else:
                started = time.perf_counter()
                envelope = dispatch("asset.process_file", {"path": source_path}, actor=args.actor)
                record.update(
                    asset_id=envelope["asset_id"], ok=envelope["ok"], outcome=envelope["outcome"],
                    error=envelope["error"], duration_s=round(time.perf_counter() - started, 1),
                )
                if envelope["asset_id"]:
                    readiness = dispatch("readiness.evaluate", {"asset_id": envelope["asset_id"]}, actor=args.actor)
                    record["readiness"] = readiness["outcome"]
                    pipeline = (envelope["data"] or {}).get("pipeline", {})
                    record["pipeline"] = {k: pipeline.get(k) for k in ("qc", "vision", "metadata", "review_reasons")}
                    record["enhancement"] = (pipeline.get("enhancement") or {}).get("decision")

            log.write(json.dumps(record, ensure_ascii=False) + "\n")
            log.flush()
            short = {k: record.get(k) for k in ("asset_id", "outcome", "skipped", "duplicate_of", "enhancement", "readiness", "duration_s") if record.get(k) is not None}
            print(f"[{index}/{len(files)}] {path.name}: {short}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
