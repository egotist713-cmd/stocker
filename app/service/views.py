"""
Read-модели service layer (docs/SERVICE_CONTRACT.md §4).

Только чтение. pipeline — сводки результатов стадий; state — Unified Asset State
(docs/ASSET_STATE_CONTRACT.md, app/asset_state.py): итоговое производное состояние,
problems и allowed_actions. assets.status — наследие, отдаётся как есть.
"""

import json

from app import asset_state
from app import publication
from app import metadata_builder as mb
from app import creative_review
from app import enhancement_decision
from app import normalization
from app import review_gate as rg
from app import stock_readiness
from app.database import db
from app.database.db import get_asset, get_connection

METADATA_APPROVED_STATES = (mb.AUTO_APPROVED, mb.APPROVED)

# Итоговые состояния объекта в порядке контракта (§3.1) — для сводок.
ASSET_STATES = (
    asset_state.REJECTED, asset_state.SOURCE_INVALID, asset_state.BLOCKED, asset_state.ERROR, asset_state.STALE,
    asset_state.PROCESSING, asset_state.HUMAN_REVIEW, asset_state.METADATA_APPROVED, asset_state.PLATFORM_READY,
    asset_state.PUBLICATION_APPROVED, asset_state.READY_FOR_EXPORT,
)


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


def pipeline_state(asset: dict, events: list[dict], metadata: dict | None, derived: dict) -> dict:
    """Сводки стадий. Итоговое состояние — не здесь, а в derived (asset_state)."""
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
        # ok / missing / changed — по контракту §3.5 (fast); раньше выводилось только из SOURCE/INVALID.
        "source": derived["stages"]["source"]["status"],
        "qc": qc,
        "vision": vision,
        "metadata": state,
        "metadata_completeness": metadata["completeness"] if metadata else None,
        # Одобрение metadata — не готовность к площадкам и не ready_for_export (контракт §3.3).
        "metadata_approved": state in METADATA_APPROVED_STATES,
        "review_reasons": [r["code"] for r in gate.get("reasons", [])] if state == mb.HUMAN_REVIEW else [],
    }


def _state_view(derived: dict) -> dict:
    keys = ("state_version", "state", "reasons", "reprocess_from", "terminal", "metadata_approved",
            "ready_for", "problems", "stages")
    return {key: derived[key] for key in keys}


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
    derived = asset_state.derive(asset, events)
    pipeline = pipeline_state(asset, events, metadata, derived)
    pipeline["normalize"] = normalization.summary_from_events(asset, events)
    pipeline["enhancement"] = enhancement_decision.summary_from_events(asset, events)
    pipeline["stock_readiness"] = stock_readiness.summary(asset, events, metadata)
    pipeline["publication"] = publication.summary(derived["stages"]["publication"],
                                                  next((e for e in reversed(events) if (e["stage"], e["status"]) == ("PUBLICATION", "EVALUATED")), None))
    pipeline["creative_review"] = {**creative_review.summary_from_events(events),
                                   "current": derived["stages"]["creative_review"]["status"] == asset_state.CURRENT}

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
        "state": _state_view(derived),
        "pipeline": pipeline,
        "qc": _parse_json(asset["qc_result"]),
        "vision": _vision_view(asset, events),
        "metadata": metadata,
        "allowed_actions": derived["allowed_actions"],
    }


def asset_summary(view: dict) -> dict:
    metadata = view["metadata"] or {}
    return {
        "id": view["id"],
        "filename": view["filename"],
        "status": view["status"],
        "state": view["state"]["state"],
        "problems": view["state"]["problems"],
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
    state: str | None = None,
    metadata_approved: bool | None = None,
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
        if state is not None and view["state"]["state"] != state:
            continue
        if metadata_approved is not None and pipeline["metadata_approved"] != metadata_approved:
            continue
        summaries.append(asset_summary(view))

    return {"total": len(summaries), "limit": limit, "offset": offset, "items": summaries[offset:offset + limit]}


METADATA_STATES = ("none", mb.DRAFT, mb.AUTO_APPROVED, mb.HUMAN_REVIEW, mb.APPROVED, mb.REJECTED)


def _problem_key(code: str) -> str:
    """STALE:qc:NO_FINGERPRINT → STALE:qc; NORMALIZE_FAILED:MISSING_CODEC → NORMALIZE_FAILED."""
    parts = code.split(":")
    return ":".join(parts[:2]) if parts[0] == "STALE" else parts[0]


def review_queue(limit: int = 50, offset: int = 0) -> dict:
    """
    Очередь человека (items — объекты в итоговом состоянии human_review: metadata ждёт
    человека и всё выше актуально) и сводка по каталогу: итоговые состояния, одобрение
    metadata и готовность к площадкам — отдельно (контракт §3.3), проблемы.
    """
    by_state = {state: 0 for state in ASSET_STATES}
    by_metadata_state = {state: 0 for state in METADATA_STATES}
    metadata_approved = platform_ready = publication_approved = ready_for_export = 0
    problem_counts = {}
    problems = []
    review_items = []

    for asset_id in all_asset_ids():
        view = asset_view(asset_id)
        pipeline, state = view["pipeline"], view["state"]
        by_state[state["state"]] = by_state.get(state["state"], 0) + 1
        by_metadata_state[pipeline["metadata"]] = by_metadata_state.get(pipeline["metadata"], 0) + 1
        metadata_approved += state["metadata_approved"]
        # ready_for_export — дальше по той же цепочке: одобрение площадки и Publication сохраняются.
        platform_ready += state["state"] in (asset_state.PLATFORM_READY, asset_state.PUBLICATION_APPROVED,
                                             asset_state.READY_FOR_EXPORT)
        publication_approved += state["state"] in (asset_state.PUBLICATION_APPROVED, asset_state.READY_FOR_EXPORT)
        ready_for_export += state["state"] == asset_state.READY_FOR_EXPORT

        if state["problems"]:
            problems.append({"id": view["id"], "filename": view["filename"], "state": state["state"],
                             "problems": state["problems"]})
            for key in {_problem_key(code) for code in state["problems"]}:
                problem_counts[key] = problem_counts.get(key, 0) + 1

        if state["state"] == asset_state.HUMAN_REVIEW:
            item = asset_summary(view)
            item["review_reasons"] = ((view["metadata"] or {}).get("review_gate") or {}).get("reasons", [])
            review_items.append(item)

    return {
        "summary": {
            "total_assets": sum(by_state.values()),
            "by_state": by_state,
            "metadata_approved": metadata_approved,
            "platform_ready": platform_ready,
            "publication_approved": publication_approved,
            "ready_for_export": ready_for_export,
            "by_metadata_state": by_metadata_state,
            "problem_counts": dict(sorted(problem_counts.items())),
            # Сначала самое важное: порядок состояний контракта (source_invalid, blocked, error, stale…).
            "problem_assets": sorted(problems, key=lambda item: (ASSET_STATES.index(item["state"]), item["id"])),
        },
        "total": len(review_items),
        "limit": limit,
        "offset": offset,
        "items": review_items[offset:offset + limit],
    }


def history(asset_id: int, stage: str | None = None) -> list[dict]:
    return [
        {**event, "message": _parse_json(event["message"])}
        for event in _events(asset_id)
        if stage is None or event["stage"] == stage
    ]


def incoming_files() -> dict:
    """
    Файлы data/incoming поддерживаемых форматов, которых ещё нет в Stocker
    (нет asset с таким source_path). Для них считается SHA256: совпадение с
    зарегистрированным hash → duplicate_of (обрабатывать не нужно).
    """
    from app import ingest

    connection = get_connection()
    try:
        rows = connection.execute("SELECT id, source_path, file_hash FROM assets").fetchall()
    finally:
        connection.close()

    known_paths = {row["source_path"].replace("\\", "/") for row in rows}
    known_hashes = {row["file_hash"]: row["id"] for row in rows if row["file_hash"]}

    incoming = db.incoming_dir()
    items = []
    for path in sorted(incoming.iterdir()) if incoming.exists() else []:
        if not path.is_file() or path.suffix.lower() not in ingest.SUPPORTED_EXTENSIONS:
            continue
        source_path = ingest.to_source_path(path)
        if source_path in known_paths:
            continue
        items.append(
            {
                "path": source_path,
                "filename": path.name,
                "file_size": path.stat().st_size,
                "duplicate_of": known_hashes.get(ingest.sha256_file(path)),
            }
        )

    return {"total": len(items), "items": items}
