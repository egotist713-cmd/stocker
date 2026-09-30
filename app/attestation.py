"""
Человеческая аттестация людей в кадре (STOCK_READINESS_CONTRACT.md §3.3b, паспорт §35ZZY).

Stocker определяет людей грубо (Vision → people_risk); глаза человека — основной фильтр.
Вывод человека записывается событием HUMAN/PEOPLE_ATTESTED и становится входом Readiness:

    not_identifiable — люди в кадре не узнаваемы (решение человека);
    release_on_file  — model release есть у пользователя (note — номер / ссылка; Stocker его
                       не хранит и не проверяет);
    none             — отзыв аттестации.

Аттестация снимает только MODEL_RELEASE_REQUIRED (взрослые); ничего больше не меняет.
Действует, пока source тот же (file_hash) и она не отозвана. Только actor human.
"""

import json

from app import ingest
from app.database.db import get_asset, get_connection, insert_event, transaction

STAGE = "HUMAN"
STATUS = "PEOPLE_ATTESTED"

KINDS = ("not_identifiable", "release_on_file", "none")
ATTESTED = "ATTESTED"


class AttestationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def current(asset: dict, events: list[dict]) -> dict | None:
    """Актуальная аттестация по событиям (без чтения файла): последняя, не отозвана, тот же file_hash."""
    event = next((e for e in reversed(events) if (e["stage"], e["status"]) == (STAGE, STATUS)), None)
    if event is None:
        return None
    try:
        message = json.loads(event["message"])
    except (json.JSONDecodeError, TypeError):
        return None
    if message.get("kind") not in ("not_identifiable", "release_on_file"):
        return None  # none — отзыв
    if message.get("file_hash") != asset["file_hash"]:
        return None
    return {"kind": message["kind"], "event_id": event["id"], "file_hash": message["file_hash"]}


def attest(asset_id: int, kind: str, note: str | None) -> dict:
    """Записать аттестацию. Source проверяется по SHA256: аттестуется именно зарегистрированный файл."""
    if kind not in KINDS:
        raise AttestationError("INVALID_KIND", f"kind must be one of {', '.join(KINDS)}")
    note = (note or "").strip()
    if kind == "release_on_file" and not note:
        raise AttestationError("NOTE_REQUIRED", "release_on_file requires a note (release number or link)")
    asset = get_asset(asset_id)
    if asset is None:
        raise AttestationError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")
    path = ingest.source_file(asset)
    if not path.exists() or ingest.sha256_file(path) != asset["file_hash"]:
        raise AttestationError("SOURCE_INVALID", f"Source of asset {asset_id} is missing or changed; nothing to attest")

    message = {"kind": kind, "note": note or None, "file_hash": asset["file_hash"]}
    with transaction() as connection:
        insert_event(connection, asset_id, STAGE, STATUS, json.dumps(message, ensure_ascii=False))
    return {"asset_id": asset_id, "outcome": ATTESTED, "attestation": message}


def get(asset_id: int) -> dict | None:
    asset = get_asset(asset_id)
    if asset is None:
        return None
    connection = get_connection()
    try:
        rows = connection.execute("SELECT id, stage, status, message FROM processing_events WHERE asset_id = ? "
                                  "ORDER BY id", (asset_id,)).fetchall()
    finally:
        connection.close()
    return current(asset, [dict(row) for row in rows])
