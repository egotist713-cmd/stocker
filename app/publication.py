"""
Publication Gate (docs/ASSET_STATE_CONTRACT.md §2.2, publication-v1): можно ли выпускать
объект в конкретный export profile — отдельный детерминированный слой после
Metadata → Readiness → (Creative Review — советник).

Решение принимается **по площадке** и не повторяет оценку Metadata / Readiness / Creative:
- площадка разрешена, только если metadata одобрена и актуальна, Readiness актуален
  и эта площадка в его `ready_for`, а source подтверждён SHA256 непосредственно перед
  решением;
- Creative Review — советник (STOCK_READINESS §4.1, §4.3): не блокирует и не обязателен;
  его рекомендация фиксируется в результате как информация (`advice`). Смена
  рекомендации делает результат устаревшим; повторная оценка с той же
  рекомендацией — нет (без бессмысленного каскада).

Ничего не меняет в metadata, Readiness и Creative Review. Событие PUBLICATION/EVALUATED —
это и есть результат (атомарно).
"""

import hashlib
import json
from datetime import datetime, timezone

from app import ingest
from app import readiness as rd
from app.database.db import get_asset, get_connection, insert_event, transaction

STAGE = "PUBLICATION"
POLICY_VERSION = "publication-v1"

# Исходы.
EVALUATED = "EVALUATED"
UNCHANGED = "UNCHANGED"

APPROVED = "approved"
BLOCKED = "blocked"

# Рекомендации советника, о которых человеку стоит знать (только информация).
ADVICE_NOTES = {"attention": "ADVISOR_ATTENTION", "skip_suggested": "ADVISOR_SKIP_SUGGESTED"}


class PublicationError(Exception):
    """Оценка невозможна; code — причина. Отказ не пишет событий."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _json(value):
    try:
        return json.loads(value) if value else None
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


def _stored(event: dict | None) -> dict | None:
    result = _json(event["message"]) if event else None
    return {k: v for k, v in result.items() if k != "actor"} if isinstance(result, dict) else None


def inputs(asset: dict, events: list[dict], stages: dict) -> dict:
    """
    Входы решения (контракт §2.2): отпечаток и id актуального Readiness (он уже включает
    metadata, Vision, факты, QC, source), source SHA256, снимок совета Creative Review.
    """
    readiness_event = _last(events, "READINESS", "EVALUATED")
    readiness = _stored(readiness_event) or {}
    creative_event = _last(events, "CREATIVE_REVIEW", "ADVISED")
    creative = _stored(creative_event) or {}
    creative_current = stages["creative_review"]["status"] == "current"
    return {
        "policy_version": POLICY_VERSION,
        "profiles": {name: profile.version for name, profile in sorted(rd.PROFILES.items())},
        "source_sha256": asset["file_hash"],
        "readiness_fingerprint": readiness.get("fingerprint"),
        "readiness_event_id": readiness_event["id"] if readiness_event else None,
        # Советник: только актуальная рекомендация как информация; её смена → stale.
        "advice": {
            "current": creative_current,
            "recommendation": (creative.get("features") or {}).get("recommendation") if creative_current else None,
        },
    }


def fingerprint(inputs_: dict) -> str:
    # readiness_event_id — для трассировки; решение от него не зависит (переоценка с теми же
    # входами даёт UNCHANGED и нового события Readiness не пишет).
    payload = {k: v for k, v in inputs_.items() if k != "readiness_event_id"}
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def decide(readiness_result: dict, inputs_: dict) -> dict:
    """Решение по площадкам — детерминированно из актуального Readiness и совета Creative."""
    platforms = {}
    for name, result in sorted((readiness_result.get("platforms") or {}).items()):
        blockers = sorted({c["code"] for c in result.get("checks", []) if c.get("level") == rd.BLOCKER})
        ready = result.get("status") == rd.READY
        notes = [ADVICE_NOTES[inputs_["advice"]["recommendation"]]] if ready and inputs_["advice"]["recommendation"] in ADVICE_NOTES else []
        if ready and not inputs_["advice"]["current"]:
            notes.append("NO_CURRENT_ADVICE")
        platforms[name] = {
            "status": APPROVED if ready else BLOCKED,
            "reasons": [] if ready else (blockers or [f"READINESS_{str(result.get('status')).upper()}"]),
            "notes": notes,
            "profile": result.get("profile"),
        }
    return {"platforms": platforms, "approved_for": [p for p, r in platforms.items() if r["status"] == APPROVED]}


def summary(stage: dict, event: dict | None) -> dict:
    stored = _stored(event)
    current = stage["status"] == "current"
    return {
        "evaluated": stored is not None,
        "current": current,
        "status": stage["status"],
        "status_reason": stage.get("reason"),
        "approved_for": (stored or {}).get("approved_for", []) if current else [],
        "event_id": event["id"] if event else None,
    }


def get(asset_id: int) -> dict:
    from app import asset_state

    asset = get_asset(asset_id)
    if asset is None:
        raise PublicationError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")
    event = _last(_events(asset_id), STAGE, "EVALUATED")
    stage = asset_state.get(asset_id)["stages"]["publication"]
    stored = _stored(event)
    base = summary(stage, event)
    if stored is None or base["current"]:
        return {**base, "result": stored}
    return {**base, "result": None, "last_result": {**stored, "current": False}}


def evaluate_asset(asset_id: int) -> dict:
    """
    Решение Publication Gate по площадкам. Только при актуальном upstream; source — SHA256
    непосредственно перед решением. Идемпотентно: те же входы — без нового события.
    """
    from app import asset_state

    asset = get_asset(asset_id)
    if asset is None:
        raise PublicationError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")
    state = asset_state.get(asset_id, verify_source=True)
    stages = state["stages"]
    if state["state"] in (asset_state.REJECTED, asset_state.SOURCE_INVALID):
        raise PublicationError(state["state"].upper(), f"Asset is {state['state']}")
    if stages["publication"]["status"] == asset_state.NOT_APPLICABLE:
        raise PublicationError("PUBLICATION_NOT_APPLICABLE", f"Publication is not applicable: {stages['publication']['reason']}")
    for name in ("normalize", "view", "qc", "vision", "metadata", "readiness"):
        if stages[name]["status"] != asset_state.CURRENT:
            raise PublicationError("UPSTREAM_NOT_CURRENT", f"Upstream '{name}' is {stages[name]['status']} ({stages[name].get('reason')})")

    events = _events(asset_id)
    current_inputs = inputs(asset, events, stages)
    current = fingerprint(current_inputs)
    last = _stored(_last(events, STAGE, "EVALUATED"))
    if last and last.get("fingerprint") == current:
        return {"asset_id": asset_id, "outcome": UNCHANGED, "publication": last}

    readiness_result = _stored(_last(events, "READINESS", "EVALUATED")) or {}
    decision = decide(readiness_result, current_inputs)
    message = {
        "policy_version": POLICY_VERSION,
        "fingerprint": current,
        "inputs": current_inputs,
        "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **decision,
    }
    with transaction() as connection:
        insert_event(connection, asset_id, STAGE, "EVALUATED", json.dumps(message, ensure_ascii=False))
    return {"asset_id": asset_id, "outcome": EVALUATED, "publication": message}


def stage_status(asset: dict, events: list[dict], stages: dict) -> dict:
    """Статус стадии для asset_state: применимость, отпечаток, решение."""
    readiness = stages["readiness"]
    has_result = _last(events, STAGE, "EVALUATED") is not None
    if readiness["status"] == "not_applicable":
        return {"status": "not_applicable", "reason": "METADATA_NOT_APPROVED", "has_result": has_result}
    if readiness["status"] == "missing":
        return {"status": "not_applicable", "reason": "READINESS_NOT_EVALUATED", "has_result": has_result}
    if readiness["status"] == "current" and readiness.get("reason") == "READINESS_BLOCKED":
        return {"status": "not_applicable", "reason": "READINESS_BLOCKED", "has_result": has_result}
    event = _last(events, STAGE, "EVALUATED")
    if event is None:
        return {"status": "missing", "reason": "NOT_EVALUATED"}
    stored = _stored(event) or {}
    current_inputs = inputs(asset, events, stages)
    current = fingerprint(current_inputs)
    if stored.get("fingerprint") != current:
        old = stored.get("inputs") or {}
        changed = sorted(k for k in set(old) | set(current_inputs)
                         if k != "readiness_event_id" and old.get(k) != current_inputs.get(k))
        return {"status": "stale", "reason": "FINGERPRINT_CHANGED", "changed": changed}
    return {"status": "current", "reason": None, "approved_for": stored.get("approved_for", [])}
