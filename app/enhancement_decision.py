"""
Enhancement decision: события ENHANCEMENT/* (docs/STOCK_READINESS_CONTRACT.md §4.2).

Метрики и правила — app/enhancement.py; рекомендация модели для спорных
случаев — app/ai/enhancement_advisor.py. Пиксели — только AnalysisView
(app/analysis_view.py): файл сам здесь не открывается. Здесь — идемпотентная
запись событий и текущее состояние для представления. Решение не блокирует
pipeline: Topaz не запускается.

Модель не отменяет правила: она вызывается только для disputed (правила не
приняли решения), и её ответ — рекомендация.
"""

import json
import time
from datetime import datetime, timezone

from app import analysis_view
from app import enhancement as en
from app import normalization
from app.ai.analyzer import AIResponseError
from app.ai.enhancement_advisor import INPUTS, EnhancementAdvisor, LMStudioEnhancementAdvisor
from app.database.db import get_asset, get_connection, insert_event, transaction

STAGE = "ENHANCEMENT"

# Исходы операций.
ASSESSED = "ASSESSED"
UNCHANGED = "UNCHANGED"
ENHANCEMENT_FAILED = "ENHANCEMENT_FAILED"
ADVISED = "ADVISED"
NOT_DISPUTED = "NOT_DISPUTED"
ADVISOR_FAILED = "ADVISOR_FAILED"

DISPUTED = "disputed"
MAX_RAW_OUTPUT_CHARS = 4000


class EnhancementError(Exception):
    """Операция невозможна; code — машиночитаемая причина."""

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
            "SELECT id, stage, status, message FROM processing_events "
            "WHERE asset_id = ? AND stage = ? AND status = ? ORDER BY id DESC LIMIT 1",
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


def _write(asset_id: int, status: str, message: dict) -> int:
    with transaction() as connection:
        return insert_event(connection, asset_id, STAGE, status, json.dumps(message, ensure_ascii=False))


def _failed(asset_id: int, stage: str, outcome: str, error_type: str, error: str, **extra) -> dict:
    """FAILED идемпотентен: та же причина при тех же входах (fingerprint) — без нового события."""
    asset = get_asset(asset_id)
    current = en.fingerprint(asset["file_hash"]) if asset else None
    previous_event = _last_event(asset_id, "FAILED")
    previous = _stored(previous_event)
    newer = [_last_event(asset_id, status) for status in ("ASSESSED", "ADVISED")]
    if (previous and previous.get("fingerprint") == current and previous.get("error_type") == error_type
            and previous.get("stage") == stage and not any(e and e["id"] > previous_event["id"] for e in newer)):
        return {"asset_id": asset_id, "outcome": outcome, "assessment": None, "error": previous}
    message = {"stage": stage, "error_type": error_type, "error": error[:500], "fingerprint": current, **extra}
    _write(asset_id, "FAILED", message)
    return {"asset_id": asset_id, "outcome": outcome, "assessment": None, "error": message}


def _open_view(asset_id: int, view):
    """Переданный view (worker: один на все стадии) или view объекта; иначе (код, причина)."""
    if view is not None:
        return view, None
    try:
        return analysis_view.open_asset_view(asset_id), None
    except analysis_view.ViewUnavailable as exc:
        return None, (exc.code, str(exc))


def _advice_for(advised_event: dict | None, assessed_event: dict | None) -> dict | None:
    """Рекомендация действует только для той оценки правил, к которой она дана."""
    advice = _stored(advised_event)
    if not advice or not assessed_event or advice.get("assessment_event_id") != assessed_event["id"]:
        return None
    return advice


def effective_decision(result: dict | None, advice: dict | None = None) -> str | None:
    """Решение правил; иначе рекомендация модели; иначе disputed."""
    if not result:
        return None
    if result.get("decision"):
        return result["decision"]
    if advice:
        return advice["advice"]["decision"]
    return DISPUTED if result.get("disputed") else None


def summary(asset: dict, assessed_event: dict | None, advised_event: dict | None) -> dict:
    """pipeline.enhancement: решение, кем принято, причины и stale — без чтения файла."""
    result = _stored(assessed_event)
    if result is None:
        return {"assessed": False, "stale": False, "decision": None, "decided_by": None,
                "reasons": [], "confidence": None, "event_id": None}

    advice = _advice_for(advised_event, assessed_event)
    if result.get("decision"):
        decided_by, reasons, confidence = "rules", result.get("reasons", []), None
    elif advice:
        # confidence — некалиброванный score модели, только для показа (паспорт §3A.8).
        decided_by, reasons, confidence = "advisor", advice["advice"]["reasons"], advice["advice"]["confidence"]
    else:
        decided_by, reasons, confidence = None, result.get("reasons", []), None

    return {
        "assessed": True,
        "stale": result.get("fingerprint") != en.fingerprint(asset["file_hash"]),
        "decision": effective_decision(result, advice),
        "decided_by": decided_by,
        "reasons": [r["reason"] for r in reasons],
        "confidence": confidence,
        "event_id": assessed_event["id"],
    }


def summary_from_events(asset: dict, events: list[dict]) -> dict:
    def last(status):
        return next((e for e in reversed(events) if e["stage"] == STAGE and e["status"] == status), None)

    return summary(asset, last("ASSESSED"), last("ADVISED"))


def _require_asset(asset_id: int) -> dict:
    asset = get_asset(asset_id)
    if asset is None:
        raise EnhancementError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")
    return asset


def get(asset_id: int) -> dict:
    asset = _require_asset(asset_id)
    assessed, advised = _last_event(asset_id, "ASSESSED"), _last_event(asset_id, "ADVISED")
    return {**summary(asset, assessed, advised), "result": _stored(assessed), "advice": _advice_for(advised, assessed)}


def assess_asset(asset_id: int, view=None) -> dict:
    """Метрики и решение правилами по AnalysisView. Идемпотентно: тот же view и правила — без нового события."""
    asset = _require_asset(asset_id)

    last = _stored(_last_event(asset_id, "ASSESSED"))
    if last and last.get("fingerprint") == en.fingerprint(asset["file_hash"]):
        return {"asset_id": asset_id, "outcome": UNCHANGED, "assessment": last}

    view, problem = _open_view(asset_id, view)
    if problem:
        return _failed(asset_id, "rules", ENHANCEMENT_FAILED, *problem)

    try:
        quality = ((normalization.get(asset_id).get("facts")) or {}).get("jpeg_quality")
        metrics = en.measure_view(view, quality)
    except Exception as exc:  # noqa: BLE001 — сбой расчёта фиксируется событием
        return _failed(asset_id, "rules", ENHANCEMENT_FAILED, type(exc).__name__, str(exc))

    result = {**en.assess(metrics, asset["file_hash"]), "view": view.identity()}
    _write(asset_id, "ASSESSED", result)
    return {"asset_id": asset_id, "outcome": ASSESSED, "assessment": result}


def _provenance(advisor: EnhancementAdvisor) -> dict:
    return {
        "provider": getattr(advisor, "provider", type(advisor).__name__),
        "model": getattr(advisor, "model", None),
        "prompt_version": getattr(advisor, "prompt_version", None),
    }


def advise_asset(asset_id: int, advisor: EnhancementAdvisor | None = None, view=None) -> dict:
    """
    Рекомендация модели — только для disputed. Для решённых правилами случаев
    модель не вызывается (NOT_DISPUTED): она не отменяет детерминированный QC.
    """
    asset = _require_asset(asset_id)
    assessed_event = _last_event(asset_id, "ASSESSED")
    result = _stored(assessed_event)

    if result is None:
        raise EnhancementError("ENHANCEMENT_NOT_ASSESSED", f"Asset {asset_id} has no enhancement assessment; run enhancement.assess first")
    if result.get("fingerprint") != en.fingerprint(asset["file_hash"]):
        raise EnhancementError("ENHANCEMENT_STALE", f"Enhancement assessment of asset {asset_id} is stale; run enhancement.assess first")
    if not result.get("disputed"):
        return {"asset_id": asset_id, "outcome": NOT_DISPUTED, "assessment": result, "advice": None}

    existing = _advice_for(_last_event(asset_id, "ADVISED"), assessed_event)
    if existing:
        return {"asset_id": asset_id, "outcome": UNCHANGED, "assessment": result, "advice": existing}

    view, problem = _open_view(asset_id, view)
    if problem:
        return _failed(asset_id, "advisor", ADVISOR_FAILED, *problem)

    advisor = advisor or LMStudioEnhancementAdvisor()
    provenance = _provenance(advisor)
    started = time.perf_counter()
    try:
        advice = advisor.advise(view, result)
    except Exception as exc:  # noqa: BLE001 — сбой модели фиксируется событием и не блокирует pipeline
        extra = {**provenance, "duration_s": round(time.perf_counter() - started, 2)}
        if isinstance(exc, AIResponseError):
            extra["raw_output"] = exc.raw_output[:MAX_RAW_OUTPUT_CHARS]
        return _failed(asset_id, "advisor", ADVISOR_FAILED, type(exc).__name__, str(exc), **extra)

    message = {
        **provenance,
        "inputs": INPUTS,
        "view": view.identity(),
        "assessment_event_id": assessed_event["id"],
        "advised_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_s": round(time.perf_counter() - started, 2),
        "advice": advice.model_dump(),
    }
    _write(asset_id, "ADVISED", message)
    return {"asset_id": asset_id, "outcome": ADVISED, "assessment": result, "advice": message}
