"""
Normalization, шаг 1: факты об исходнике и события NORMALIZE/* (docs/FORMAT_CONTRACT.md §3).

Файл только читается (app/source_facts.py). Пока нормализованное представление не
создаётся и pipeline не останавливается: шаг 1 наблюдает и записывает. Событие —
это и есть результат (атомарно, паспорт §3A.14).
"""

import hashlib
import json

from app import ingest
from app.database.db import get_asset, get_connection, insert_event, transaction
from app.source_facts import FACTS_VERSION, detect_container, read_facts

STAGE = "NORMALIZE"

# Исходы операций.
EVALUATED = "EVALUATED"
UNCHANGED = "UNCHANGED"
NORMALIZE_FAILED = "NORMALIZE_FAILED"


class NormalizationError(Exception):
    """Операция невозможна; code — машиночитаемая причина."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def fingerprint(file_hash: str | None) -> str:
    payload = json.dumps({"facts_version": FACTS_VERSION, "file_hash": file_hash}, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json(value):
    try:
        return json.loads(value) if value else None
    except (json.JSONDecodeError, TypeError):
        return None


def _last_event(asset_id: int, status: str) -> dict | None:
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT id, message FROM processing_events WHERE asset_id = ? AND stage = ? AND status = ? ORDER BY id DESC LIMIT 1",
            (asset_id, STAGE, status),
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row else None


def _stored(event: dict | None) -> dict | None:
    result = _json(event["message"]) if event else None
    return {k: v for k, v in result.items() if k != "actor"} if isinstance(result, dict) else None


def _write(asset_id: int, status: str, message: dict) -> None:
    with transaction() as connection:
        insert_event(connection, asset_id, STAGE, status, json.dumps(message, ensure_ascii=False))


def _failed(asset_id: int, error_type: str, error: str, **extra) -> dict:
    message = {"facts_version": FACTS_VERSION, "error_type": error_type, "error": error[:500], **extra}
    _write(asset_id, "FAILED", message)
    return {"asset_id": asset_id, "outcome": NORMALIZE_FAILED, "facts": None, "error": message}


def summary(asset: dict, evaluated: dict | None, failed: dict | None = None) -> dict:
    """pipeline.normalize: ключевые факты об исходнике — без чтения файла."""
    result = _stored(evaluated)
    last_failed = _stored(failed) if failed and (not evaluated or failed["id"] > evaluated["id"]) else None
    if result is None:
        return {"evaluated": False, "stale": False, "failed": (last_failed or {}).get("error_type"), "event_id": None}
    facts = result["facts"]
    return {
        "evaluated": True,
        "stale": result.get("fingerprint") != fingerprint(asset["file_hash"]),
        "failed": (last_failed or {}).get("error_type"),
        "container": facts["container"],
        "color_profile": facts["color_profile"]["kind"],
        "hdr": facts["hdr"],
        "motion_video": facts["embedded"]["motion_video"] is not None,
        "event_id": evaluated["id"],
    }


def summary_from_events(asset: dict, events: list[dict]) -> dict:
    def last(status):
        return next((e for e in reversed(events) if e["stage"] == STAGE and e["status"] == status), None)

    return summary(asset, last("EVALUATED"), last("FAILED"))


def get(asset_id: int) -> dict:
    asset = get_asset(asset_id)
    if asset is None:
        raise NormalizationError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")
    evaluated = _last_event(asset_id, "EVALUATED")
    return {**summary(asset, evaluated, _last_event(asset_id, "FAILED")), "facts": (_stored(evaluated) or {}).get("facts")}


def evaluate_asset(asset_id: int) -> dict:
    """Факты об исходнике. Идемпотентно: тот же файл и версия фактов — без нового события."""
    asset = get_asset(asset_id)
    if asset is None:
        raise NormalizationError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")

    current = fingerprint(asset["file_hash"])
    last = _stored(_last_event(asset_id, "EVALUATED"))
    if last and last.get("fingerprint") == current:
        return {"asset_id": asset_id, "outcome": UNCHANGED, "facts": last["facts"]}

    path = ingest.source_file(asset)
    if not path.exists():
        return _failed(asset_id, "SOURCE_MISSING", f"Source file not found: {asset['source_path']}")
    if ingest.sha256_file(path) != asset["file_hash"]:
        return _failed(asset_id, "SOURCE_CHANGED", "Source file hash does not match assets.file_hash")

    try:
        facts = read_facts(path, asset["filename"])
    except Exception as exc:  # noqa: BLE001 — «не угадывать»: непрочитанный файл — FAILED с причиной
        container = detect_container(path.read_bytes()[:16])
        error_type = "MISSING_CODEC" if container in ("HEIF",) else "DECODE_ERROR" if container else "UNSUPPORTED_FORMAT"
        return _failed(asset_id, error_type, f"{type(exc).__name__}: {exc}", container=container)

    _write(asset_id, "EVALUATED", {"facts_version": FACTS_VERSION, "fingerprint": current, "facts": facts})
    return {"asset_id": asset_id, "outcome": EVALUATED, "facts": facts}
