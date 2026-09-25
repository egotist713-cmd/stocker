"""
Read-модели service layer (docs/SERVICE_CONTRACT.md §4).

Только чтение. pipeline — производное представление из assets и событий;
assets.status не меняется и отдаётся как есть.
"""

import json

from app import metadata_builder as mb
from app import review_gate as rg
from app.database.db import get_asset, get_connection

READY_STATES = (mb.AUTO_APPROVED, mb.APPROVED)


def _parse_json(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value  # старые текстовые сообщения


def _events(asset_id: int) -> list[dict]:
    connection = get_connection()
    try:
        rows = connection.execute(
            "SELECT * FROM processing_events WHERE asset_id = ? ORDER BY id", (asset_id,)
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _last(events: list[dict], stage: str, status: str | None = None) -> dict | None:
    for event in reversed(events):
        if event["stage"] == stage and (status is None or event["status"] == status):
            return event
    return None


def pipeline_state(asset: dict, events: list[dict], metadata: dict | None) -> dict:
    last_qc = _last(events, "QC")
    last_source_invalid = _last(events, "SOURCE", "INVALID")

    if last_source_invalid and (last_qc is None or last_source_invalid["id"] > last_qc["id"]):
        reason = (_parse_json(last_source_invalid["message"]) or {}).get("reason")
        source = "missing" if reason == "SOURCE_MISSING" else "changed"
    elif last_qc:
        source = "ok"
    else:
        source = "unknown"

    qc_result = _parse_json(asset["qc_result"])
    qc = "pending" if not qc_result else ("passed" if qc_result.get("passed") else "failed")

    if asset["ai_result"]:
        vision = "done"
    else:
        last_ai = _last(events, "AI")
        vision = "failed" if last_ai and last_ai["status"] == "FAILED" else "pending"

    state = metadata["state"] if metadata else "none"
    gate = (metadata or {}).get("review_gate") or {}

    return {
        "source": source,
        "qc": qc,
        "vision": vision,
        "metadata": state,
        "metadata_completeness": metadata["completeness"] if metadata else None,
        "ready": state in READY_STATES,
        "review_reasons": [r["code"] for r in gate.get("reasons", [])] if state == mb.HUMAN_REVIEW else [],
    }


def allowed_actions(pipeline: dict, metadata: dict | None) -> list[dict]:
    """Подсказка для агента: что имеет смысл сделать сейчас. Проверка — в самой операции."""
    actions = []

    def allow(operation: str, access: str):
        actions.append({"operation": operation, "access": access})

    if pipeline["source"] in ("changed", "missing"):
        return actions

    if pipeline["vision"] != "done":
        allow("asset.process", "pipeline")
        return actions

    if metadata is None:
        allow("metadata.build", "pipeline")
        return actions

    state = metadata["state"]

    if metadata["completeness"] == mb.PARTIAL and not metadata["edited_fields"]:
        allow("metadata.build", "pipeline")

    allow("metadata.edit", "pipeline")
    allow("metadata.rebuild", "pipeline")

    if state in rg.GATEABLE_STATES:
        allow("metadata.gate", "pipeline")
        allow("metadata.escalate", "pipeline")

    if state in mb.APPROVABLE_STATES and not metadata["validation"]["errors"]:
        allow("metadata.approve", "review")

    if state in mb.REJECTABLE_STATES:
        allow("metadata.reject", "review")

    return actions


def _vision_view(asset: dict, events: list[dict]) -> dict | None:
    if not asset["ai_result"]:
        return None

    passed = _last(events, "AI", "PASSED")
    details = _parse_json(passed["message"]) if passed else None

    return {
        "analysis": json.loads(asset["ai_result"]),
        "provenance": {
            "event_id": passed["id"] if passed else None,
            **({k: details.get(k) for k in ("provider", "model", "prompt_version", "analyzed_at")}
               if isinstance(details, dict) else {"provider": None, "model": None, "prompt_version": None}),
        },
    }


def asset_view(asset_id: int) -> dict | None:
    asset = get_asset(asset_id)
    if asset is None:
        return None

    events = _events(asset_id)
    metadata = _parse_json(asset["metadata_json"])
    pipeline = pipeline_state(asset, events, metadata)

    return {
        "id": asset["id"],
        "filename": asset["filename"],
        "source_path": asset["source_path"],
        "file_hash": asset["file_hash"],
        "width": asset["width"],
        "height": asset["height"],
        "file_size": asset["file_size"],
        "status": asset["status"],
        "created_at": asset["created_at"],
        "updated_at": asset["updated_at"],
        "pipeline": pipeline,
        "qc": _parse_json(asset["qc_result"]),
        "vision": _vision_view(asset, events),
        "metadata": metadata,
        "allowed_actions": allowed_actions(pipeline, metadata),
    }


def asset_summary(view: dict) -> dict:
    metadata = view["metadata"] or {}
    return {
        "id": view["id"],
        "filename": view["filename"],
        "status": view["status"],
        "pipeline": view["pipeline"],
        "title": (metadata.get("fields") or {}).get("title"),
    }


def all_asset_ids() -> list[int]:
    connection = get_connection()
    try:
        return [row["id"] for row in connection.execute("SELECT id FROM assets ORDER BY id")]
    finally:
        connection.close()


def list_assets(
    *,
    qc: str | None = None,
    vision: str | None = None,
    metadata_state: str | None = None,
    ready: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    # Каталог пока небольшой: фильтрация по производному состоянию в Python.
    summaries = []
    for asset_id in all_asset_ids():
        view = asset_view(asset_id)
        pipeline = view["pipeline"]
        if qc is not None and pipeline["qc"] != qc:
            continue
        if vision is not None and pipeline["vision"] != vision:
            continue
        if metadata_state is not None and pipeline["metadata"] != metadata_state:
            continue
        if ready is not None and pipeline["ready"] != ready:
            continue
        summaries.append(asset_summary(view))

    return {"total": len(summaries), "limit": limit, "offset": offset, "items": summaries[offset:offset + limit]}


def review_queue(limit: int = 50, offset: int = 0) -> dict:
    result = list_assets(metadata_state=mb.HUMAN_REVIEW, limit=limit, offset=offset)
    for item in result["items"]:
        gate = (asset_view(item["id"])["metadata"] or {}).get("review_gate") or {}
        item["review_reasons"] = gate.get("reasons", [])
    return result


def history(asset_id: int, stage: str | None = None) -> list[dict]:
    return [
        {**event, "message": _parse_json(event["message"])}
        for event in _events(asset_id)
        if stage is None or event["stage"] == stage
    ]
