"""
Stock Readiness: оценка asset и события READINESS/* (docs/STOCK_READINESS_CONTRACT.md §3.8).

Правила — app/readiness.py (чистые функции). Здесь — входы из БД и файла,
идемпотентная запись READINESS/EVALUATED и текущее состояние для представления.
Файл только читается; metadata, решения человека и assets.status не меняются.
"""

import json

from app import ingest
from app import readiness as rd
from app.ai.schema import AIAnalysis
from app.database.db import get_asset, get_connection, insert_event, transaction

STAGE = "READINESS"

# Исходы операций.
EVALUATED = "EVALUATED"
UNCHANGED = "UNCHANGED"
NOT_EVALUATED = "NOT_EVALUATED"
READINESS_FAILED = "READINESS_FAILED"


class ReadinessError(Exception):
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


def _events(asset_id: int) -> list[dict]:
    connection = get_connection()
    try:
        rows = connection.execute(
            "SELECT id, stage, status, message FROM processing_events WHERE asset_id = ? ORDER BY id", (asset_id,)
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _last(events: list[dict], stage: str, status: str) -> dict | None:
    return next((e for e in reversed(events) if e["stage"] == stage and e["status"] == status), None)


def _db_facts(asset: dict, events: list[dict]) -> dict:
    """Входы fingerprint из БД — без чтения файла."""
    qc = _json(asset["qc_result"]) or {}
    source_invalid = _last(events, "SOURCE", "INVALID")
    return {
        "file_hash": asset["file_hash"],
        "source_event_id": source_invalid["id"] if source_invalid else None,
        "qc_passed": bool(qc.get("passed")),
    }


def _inputs(asset: dict) -> tuple[dict | None, AIAnalysis | None]:
    metadata = _json(asset["metadata_json"])
    vision = AIAnalysis.model_validate_json(asset["ai_result"]) if asset["ai_result"] else None
    return metadata, vision


def _stored(event: dict | None) -> dict | None:
    """Результат из события READINESS/EVALUATED (без служебного actor)."""
    result = _json(event["message"]) if event else None
    if not isinstance(result, dict):
        return None
    return {key: value for key, value in result.items() if key != "actor"}


def _file_facts(asset: dict) -> dict:
    """Сведения о файле; исходник только читается (оригинал не меняется, §3.5b)."""
    path = ingest.source_file(asset)
    qc_metrics = (_json(asset["qc_result"]) or {}).get("metrics", {})

    if not path.exists():
        return {
            "source": "missing",
            "format": qc_metrics.get("format"),
            "width": asset["width"] or 0,
            "height": asset["height"] or 0,
            "file_size": asset["file_size"] or 0,
            "color_profile": None,
            "color_profile_description": None,
        }

    source = "ok" if ingest.sha256_file(path) == asset["file_hash"] else "changed"
    return {"source": source, **rd.read_file_facts(path)}


def summary(asset: dict, events: list[dict], metadata: dict | None) -> dict:
    """pipeline.stock_readiness: статусы по площадкам, stale, ready_for — без чтения файла."""
    event = _last(events, STAGE, "EVALUATED")
    current = rd.fingerprint(_db_facts(asset, events), metadata, sorted(rd.PROFILES))
    return {**rd.current_status(_stored(event), current), "event_id": event["id"] if event else None}


def get(asset_id: int) -> dict:
    asset = get_asset(asset_id)
    if asset is None:
        raise ReadinessError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")

    events = _events(asset_id)
    metadata, _ = _inputs(asset)
    event = _last(events, STAGE, "EVALUATED")
    return {**summary(asset, events, metadata), "result": _stored(event)}


def evaluate_asset(asset_id: int) -> dict:
    """Оценить asset для всех площадок. Идемпотентно: тот же fingerprint — без нового события."""
    asset = get_asset(asset_id)
    if asset is None:
        raise ReadinessError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")

    events = _events(asset_id)
    metadata, vision = _inputs(asset)
    platforms = sorted(rd.PROFILES)
    facts = _db_facts(asset, events)

    # Metadata не готова: оценка без события (не засоряем историю черновиками).
    if vision is None or (metadata or {}).get("state") not in rd.READY_METADATA_STATES:
        return {"asset_id": asset_id, "outcome": NOT_EVALUATED, "readiness": rd.evaluate(facts, metadata, vision, platforms)}

    last = _stored(_last(events, STAGE, "EVALUATED"))
    if last and last.get("fingerprint") == rd.fingerprint(facts, metadata, platforms):
        return {"asset_id": asset_id, "outcome": UNCHANGED, "readiness": last}

    try:
        facts = {**facts, **_file_facts(asset)}
    except Exception as exc:  # noqa: BLE001 — сбой чтения файла фиксируется событием
        message = {"error_type": type(exc).__name__, "error": str(exc)[:500]}
        with transaction() as connection:
            insert_event(connection, asset_id, STAGE, "FAILED", json.dumps(message, ensure_ascii=False))
        return {"asset_id": asset_id, "outcome": READINESS_FAILED, "readiness": None, "error": message}

    result = rd.evaluate(facts, metadata, vision, platforms)
    with transaction() as connection:
        insert_event(connection, asset_id, STAGE, "EVALUATED", json.dumps(result, ensure_ascii=False))
    return {"asset_id": asset_id, "outcome": EVALUATED, "readiness": result}
