"""
Stock Readiness: оценка asset и события READINESS/* (docs/STOCK_READINESS_CONTRACT.md §3.8).

Правила — app/readiness.py (чистые функции). Здесь — входы из БД и файла,
идемпотентная запись READINESS/EVALUATED и текущее состояние для представления.
Файл только читается; metadata, решения человека и assets.status не меняются.
"""

import json

from app import attestation, ingest
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
    source_facts = _last(events, "NORMALIZE", "EVALUATED")
    facts = {
        "file_hash": asset["file_hash"],
        "facts_event_id": source_facts["id"] if source_facts else None,
        "source_event_id": source_invalid["id"] if source_invalid else None,
        "qc_passed": bool(qc.get("passed")),
    }
    # Человеческая аттестация людей (§3.3b) — вход только если действует: иначе ключа нет,
    # и отпечатки прежних оценок не меняются. Отзыв / новая аттестация → stale.
    attested = attestation.current(asset, events)
    if attested:
        facts["people_attestation"] = attested
    return facts


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


def _file_facts(asset: dict, events: list[dict]) -> dict:
    """
    Сведения о файле из фактов источника (NORMALIZE/EVALUATED), а не из пикселей и не
    только из ICC. Целостность source проверяется по хешу; файл не меняется (§3.5b).
    """
    path = ingest.source_file(asset)
    facts = (_stored(_last(events, "NORMALIZE", "EVALUATED")) or {}).get("facts")
    source = "missing" if not path.exists() else "ok" if ingest.sha256_file(path) == asset["file_hash"] else "changed"
    if facts is None:
        return {"source": source, "format": None, "width": asset["width"] or 0, "height": asset["height"] or 0,
                "file_size": asset["file_size"] or 0, "color": None}
    color = facts["color_profile"]
    return {
        "source": source,
        "format": facts["format"],
        "width": facts["width"],
        "height": facts["height"],
        "file_size": facts["file_size"],
        "color": {k: color.get(k) for k in ("kind", "description", "source", "declared")},
    }


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
    current = summary(asset, events, metadata)
    stored = _stored(event)
    if stored is None or not current["stale"]:
        return {**current, "result": stored}
    # Устаревший результат — не текущий: result = None, а сохранённый результат отдаётся
    # отдельно и явно помеченным как история (его ready_for / export_plan не действуют).
    # История не меняется: это только представление последнего события.
    return {**current, "result": None, "last_result": {**stored, "current": False}}


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
        facts = {**facts, **_file_facts(asset, events)}
    except Exception as exc:  # noqa: BLE001 — сбой чтения файла фиксируется событием
        message = {"error_type": type(exc).__name__, "error": str(exc)[:500]}
        with transaction() as connection:
            insert_event(connection, asset_id, STAGE, "FAILED", json.dumps(message, ensure_ascii=False))
        return {"asset_id": asset_id, "outcome": READINESS_FAILED, "readiness": None, "error": message}

    result = rd.evaluate(facts, metadata, vision, platforms)
    with transaction() as connection:
        insert_event(connection, asset_id, STAGE, "EVALUATED", json.dumps(result, ensure_ascii=False))
    return {"asset_id": asset_id, "outcome": EVALUATED, "readiness": result}
