"""
Enhancement decision: события ENHANCEMENT/* (docs/STOCK_READINESS_CONTRACT.md §4.2).

Метрики и правила — app/enhancement.py. Здесь — чтение файла (только чтение,
оригинал не меняется), идемпотентная запись ENHANCEMENT/ASSESSED и текущее
состояние для представления. Решение не блокирует pipeline: Topaz не
запускается, Vision работает с оригиналом.
"""

import json

from app import enhancement as en
from app import ingest
from app.database.db import get_asset, get_connection, insert_event, transaction

STAGE = "ENHANCEMENT"

# Исходы операций.
ASSESSED = "ASSESSED"
UNCHANGED = "UNCHANGED"
ENHANCEMENT_FAILED = "ENHANCEMENT_FAILED"

DISPUTED = "disputed"


class EnhancementError(Exception):
    """Оценка невозможна; code — машиночитаемая причина."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _json(value):
    if not value:
        return None
    try:
        return json.loads(value)
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
    if not isinstance(result, dict):
        return None
    return {key: value for key, value in result.items() if key != "actor"}


def _failed(asset_id: int, error_type: str, error: str) -> dict:
    message = {"stage": "rules", "error_type": error_type, "error": error[:500]}
    with transaction() as connection:
        insert_event(connection, asset_id, STAGE, "FAILED", json.dumps(message, ensure_ascii=False))
    return {"asset_id": asset_id, "outcome": ENHANCEMENT_FAILED, "assessment": None, "error": message}


def effective_decision(result: dict | None) -> str | None:
    """Решение правил; спорный случай без рекомендации модели — disputed."""
    if not result:
        return None
    return result.get("decision") or (DISPUTED if result.get("disputed") else None)


def summary(asset: dict, event: dict | None) -> dict:
    """pipeline.enhancement: решение, причины и stale — без чтения файла."""
    result = _stored(event)
    if result is None:
        return {"assessed": False, "stale": False, "decision": None, "reasons": [], "event_id": None}
    return {
        "assessed": True,
        "stale": result.get("fingerprint") != en.fingerprint(asset["file_hash"]),
        "decision": effective_decision(result),
        "reasons": [r["reason"] for r in result.get("reasons", [])],
        "event_id": event["id"],
    }


def summary_from_events(asset: dict, events: list[dict]) -> dict:
    event = next((e for e in reversed(events) if e["stage"] == STAGE and e["status"] == "ASSESSED"), None)
    return summary(asset, event)


def get(asset_id: int) -> dict:
    asset = get_asset(asset_id)
    if asset is None:
        raise EnhancementError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")
    event = _last_event(asset_id, "ASSESSED")
    return {**summary(asset, event), "result": _stored(event)}


def assess_asset(asset_id: int) -> dict:
    """Метрики и решение правилами. Идемпотентно: тот же файл и правила — без нового события."""
    asset = get_asset(asset_id)
    if asset is None:
        raise EnhancementError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")

    last = _stored(_last_event(asset_id, "ASSESSED"))
    if last and last.get("fingerprint") == en.fingerprint(asset["file_hash"]):
        return {"asset_id": asset_id, "outcome": UNCHANGED, "assessment": last}

    path = ingest.source_file(asset)
    if not path.exists():
        return _failed(asset_id, "SOURCE_MISSING", f"Source file not found: {asset['source_path']}")
    if ingest.sha256_file(path) != asset["file_hash"]:
        return _failed(asset_id, "SOURCE_CHANGED", "Source file hash does not match assets.file_hash")

    try:
        metrics = en.read_metrics(path)
    except Exception as exc:  # noqa: BLE001 — сбой чтения фиксируется событием
        return _failed(asset_id, type(exc).__name__, str(exc))

    result = en.assess(metrics, asset["file_hash"])
    with transaction() as connection:
        insert_event(connection, asset_id, STAGE, "ASSESSED", json.dumps(result, ensure_ascii=False))
    return {"asset_id": asset_id, "outcome": ASSESSED, "assessment": result}
