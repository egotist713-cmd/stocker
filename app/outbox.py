"""
Outbox: сбор файлов к загрузке (export.collect, паспорт §35ZZZM).

Актуальные экспортные файлы площадки (Export preparation, ready_for_export) копируются в
плоскую папку партии <DATA_DIR>/publish/<platform>/<NNN>_<YYYY-MM-DD>/ с manifest.csv.
Ничего не решает и не загружает: только копия уже проверенных файлов и учёт «объект в
партии N». Отклонённые, устаревшие и уже собранные (тот же файл) не включаются.

Порядок записи как у экспорта: файлы во временную папку → проверка sha256 копий →
manifest → переименование в папку партии → события OUTBOX/COLLECTED одной транзакцией.
Сбой до событий оставляет папку без событий (её номер не переиспользуется); объекты
остаются несобранными и попадут в следующую партию.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app import asset_state
from app import export_preparation as ep
from app import ingest
from app.database import db
from app.database.db import get_connection, insert_event, transaction

STAGE = "OUTBOX"
STATUS = "COLLECTED"

COLLECTED = "COLLECTED"
NOTHING_TO_COLLECT = "NOTHING_TO_COLLECT"

_BATCH_DIR = re.compile(r"^(\d{3,})_\d{4}-\d{2}-\d{2}$")


def publish_dir() -> Path:
    return db.data_dir() / "publish"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _collected_events(platform: str | None = None) -> list[dict]:
    connection = get_connection()
    try:
        rows = connection.execute(
            "SELECT id, asset_id, message, created_at FROM processing_events WHERE stage = ? AND status = ? ORDER BY id",
            (STAGE, STATUS),
        ).fetchall()
    finally:
        connection.close()
    events = []
    for row in rows:
        message = json.loads(row["message"])
        if platform is None or message.get("platform") == platform:
            events.append({"id": row["id"], "asset_id": row["asset_id"], "created_at": row["created_at"], **message})
    return events


def collected(asset_id: int, platform: str, sha256: str | None) -> dict | None:
    """Последний сбор этого файла (тот же sha256) объекта для площадки; None — не собирался."""
    hits = [e for e in _collected_events(platform) if e["asset_id"] == asset_id and e.get("sha256") == sha256]
    return {"batch": hits[-1]["batch"], "folder": hits[-1]["folder"], "event_id": hits[-1]["id"]} if hits else None


def _next_batch(platform: str) -> int:
    """Номер партии: больше всех учтённых событиями и всех папок (в т. ч. без событий после сбоя)."""
    numbers = [e["batch"] for e in _collected_events(platform)]
    folder = publish_dir() / platform
    if folder.exists():
        numbers += [int(m.group(1)) for p in folder.iterdir() if (m := _BATCH_DIR.match(p.name.lstrip(".").removesuffix(".tmp")))]
    return max(numbers, default=0) + 1


def pending(platform: str) -> tuple[list[dict], list[dict]]:
    """(к сбору, пропущенные с причиной): актуальные экспорты площадки, ещё не собранные."""
    profile = ep._profile(platform)
    connection = get_connection()
    try:
        ids = [row["id"] for row in connection.execute("SELECT id FROM assets ORDER BY id")]
    finally:
        connection.close()
    items, skipped = [], []
    for asset_id in ids:
        status = ep.get(asset_id, platform)
        current = status.get("export")
        if current is None:
            if status.get("last_export") and asset_state.get(asset_id)["state"] != asset_state.REJECTED:
                skipped.append({"asset_id": asset_id, "reason": f"export {status['status']} ({status.get('reason')})"})
            continue  # отклонённые и без экспорта — молча
        if collected(asset_id, platform, current["sha256"]):
            continue  # уже в партии
        path = ingest.ROOT / current["path"]
        if not path.exists() or _sha256(path) != current["sha256"]:
            skipped.append({"asset_id": asset_id, "reason": "export file missing or changed"})
            continue
        text = current["metadata"].get(profile.text_field) or ""
        items.append({"asset_id": asset_id, "path": path, "filename": current["filename"], "sha256": current["sha256"],
                      "text": text, "words": len(text.split()), "export_event_id": current.get("event_id")})
    return items, skipped


def collect(platform: str) -> dict:
    """export.collect: новая партия из ещё не собранных актуальных файлов площадки."""
    profile = ep._profile(platform)
    items, skipped = pending(platform)
    if not items:
        return {"platform": platform, "outcome": NOTHING_TO_COLLECT, "batch": None, "folder": None,
                "files": [], "skipped": skipped}

    batch = _next_batch(platform)
    name = f"{batch:03d}_{datetime.now(timezone.utc).astimezone().date().isoformat()}"
    final = publish_dir() / platform / name
    temporary = final.parent / f".{name}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        for item in items:
            target = temporary / item["filename"]
            shutil.copy2(item["path"], target)
            if _sha256(target) != item["sha256"]:
                raise RuntimeError(f"copy of {item['filename']} does not match sha256")
        with open(temporary / "manifest.csv", "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["asset_id", "filename", profile.text_field, "words", "sha256"])
            for item in items:
                writer.writerow([item["asset_id"], item["filename"], item["text"], item["words"], item["sha256"]])
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, final)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    folder = final.relative_to(ingest.ROOT).as_posix()
    with transaction() as connection:
        for item in items:
            insert_event(connection, item["asset_id"], STAGE, STATUS, json.dumps({
                "platform": platform, "profile": profile.version, "batch": batch, "folder": folder,
                "filename": item["filename"], "sha256": item["sha256"], "export_event_id": item["export_event_id"],
            }, ensure_ascii=False))
    files = [{"asset_id": i["asset_id"], "filename": i["filename"], profile.text_field: i["text"], "words": i["words"],
              "sha256": i["sha256"]} for i in items]
    return {"platform": platform, "outcome": COLLECTED, "batch": batch, "folder": folder, "files": files,
            "manifest": f"{folder}/manifest.csv", "skipped": skipped}
