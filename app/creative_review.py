"""
Creative Review: коммерческая оценка кадра (docs/STOCK_READINESS_CONTRACT.md §4.3).

Отдельный инструмент-советник, не этап обязательного pipeline (паспорт
§3A.9–3A.10): запуск вручную или через n8n по запросу. Тематика — только в
профиле (app/creative_profiles.py), ядро от неё не зависит.

Модель (app/ai/creative_advisor.py) даёт первичные признаки; здесь —
детерминированный расчёт commercial_score (creative-score-v1), события
CREATIVE_REVIEW/* и текущее состояние. Только рекомендация: не блокирует
экспорт, не меняет metadata, решений не принимает; score в автоматических
решениях не используется (паспорт §3A.8).
"""

import hashlib
import json
import time
from datetime import datetime, timezone

from app import ingest
from app import review_gate as rg
from app.ai.analyzer import AIResponseError
from app.ai.creative_advisor import INPUTS, CreativeAdvisor, LMStudioCreativeAdvisor
from app.ai.schema import AIAnalysis
from app.creative_profiles import UnknownProfileError, get_profile
from app.database.db import get_asset, get_connection, insert_event, transaction

STAGE = "CREATIVE_REVIEW"
SCORE_VERSION = "creative-score-v1"

# Исходы операций.
REVIEWED = "REVIEWED"
UNCHANGED = "UNCHANGED"
REVIEW_FAILED = "REVIEW_FAILED"

MAX_RAW_OUTPUT_CHARS = 4000

# creative-score-v1 (контракт §4.3): начальные веса, калибруются по статистике.
COMPOSITION_POINTS = {"good": 35, "acceptable": 20, "weak": 5}
DEMAND_POINTS = {"high": 30, "medium": 18, "low": 5}
UNIQUENESS_POINTS = {"high": 25, "medium": 15, "low": 5}
USE_CASE_POINTS, USE_CASE_MAX_POINTS = 2, 10
HIGH_FROM, MEDIUM_FROM = 70, 40


class CreativeReviewError(Exception):
    """Операция невозможна; code — машиночитаемая причина."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def commercial_score(features: dict) -> dict:
    """Итог из первичных признаков — детерминированно, пересчитывается без модели."""
    score = (
        COMPOSITION_POINTS[features["composition"]]
        + DEMAND_POINTS[features["demand"]]
        + UNIQUENESS_POINTS[features["uniqueness"]]
        + min(USE_CASE_POINTS * len(features["commercial_use_cases"]), USE_CASE_MAX_POINTS)
    )
    potential = "high" if score >= HIGH_FROM else "medium" if score >= MEDIUM_FROM else "low"
    return {"score_version": SCORE_VERSION, "commercial_score": score, "commercial_potential": potential}


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


def fingerprint(file_hash: str | None, vision_json: str | None, prompt_version: str | None) -> str:
    payload = json.dumps({"file_hash": file_hash, "vision": vision_json, "prompt_version": prompt_version}, sort_keys=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def summary(event: dict | None) -> dict:
    """pipeline.creative_review: итог и рекомендация — без чтения файла."""
    result = _stored(event)
    if result is None:
        return {"reviewed": False, "profile": None, "commercial_score": None, "commercial_potential": None,
                "recommendation": None, "event_id": None}
    return {
        "reviewed": True,
        "profile": (result.get("profile") or {}).get("name"),
        # Пересчёт по сохранённым признакам: смена формулы не требует вызова модели.
        **{k: v for k, v in commercial_score(result["features"]).items() if k != "score_version"},
        "recommendation": result["features"]["recommendation"],
        "event_id": event["id"],
    }


def summary_from_events(events: list[dict]) -> dict:
    return summary(next((e for e in reversed(events) if e["stage"] == STAGE and e["status"] == "ADVISED"), None))


def _require(asset_id: int) -> tuple[dict, AIAnalysis]:
    asset = get_asset(asset_id)
    if asset is None:
        raise CreativeReviewError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")
    if not asset["ai_result"]:
        raise CreativeReviewError("VISION_MISSING", f"Asset {asset_id} has no Vision result; run asset.process first")
    return asset, AIAnalysis.model_validate_json(asset["ai_result"])


def _refuse_personal_document(asset_id: int, vision: AIAnalysis) -> None:
    """Документы с персональными данными модели повторно не показываются (gate-v1.2)."""
    if rg.personal_document(vision):
        raise CreativeReviewError("PERSONAL_DOCUMENT", f"Asset {asset_id} is a document with personal data; not reviewed")


def get(asset_id: int) -> dict:
    _require(asset_id)
    event = _last_event(asset_id, "ADVISED")
    return {**summary(event), "result": _stored(event)}


def _write(asset_id: int, status: str, message: dict) -> None:
    with transaction() as connection:
        insert_event(connection, asset_id, STAGE, status, json.dumps(message, ensure_ascii=False))


def review_asset(asset_id: int, advisor: CreativeAdvisor | None = None, profile: str | None = None) -> dict:
    """Оценка моделью по профилю. Идемпотентно: тот же файл, Vision, шаблон и профиль — без нового вызова."""
    asset, vision = _require(asset_id)
    _refuse_personal_document(asset_id, vision)
    if advisor is None:
        try:
            advisor = LMStudioCreativeAdvisor(profile=get_profile(profile))
        except UnknownProfileError as exc:
            raise CreativeReviewError("UNKNOWN_PROFILE", str(exc)) from exc
    used = getattr(advisor, "profile", None)
    provenance = {
        "provider": getattr(advisor, "provider", type(advisor).__name__),
        "model": getattr(advisor, "model", None),
        "prompt_version": getattr(advisor, "prompt_version", None),
        "profile": {"name": used.name, "version": used.version} if used else None,
    }
    current = fingerprint(asset["file_hash"], asset["ai_result"], provenance["prompt_version"])

    last = _stored(_last_event(asset_id, "ADVISED"))
    if last and last.get("fingerprint") == current:
        return {"asset_id": asset_id, "outcome": UNCHANGED, "review": last}

    path = ingest.source_file(asset)
    if not path.exists() or ingest.sha256_file(path) != asset["file_hash"]:
        message = {**provenance, "error_type": "SOURCE_INVALID", "error": "Source file is missing or changed"}
        _write(asset_id, "FAILED", message)
        return {"asset_id": asset_id, "outcome": REVIEW_FAILED, "review": None, "error": message}

    started = time.perf_counter()
    try:
        features = advisor.review(path, vision)
    except Exception as exc:  # noqa: BLE001 — сбой модели фиксируется событием
        message = {**provenance, "error_type": type(exc).__name__, "error": str(exc)[:500],
                   "duration_s": round(time.perf_counter() - started, 2)}
        if isinstance(exc, AIResponseError):
            message["raw_output"] = exc.raw_output[:MAX_RAW_OUTPUT_CHARS]
        _write(asset_id, "FAILED", message)
        return {"asset_id": asset_id, "outcome": REVIEW_FAILED, "review": None, "error": message}

    features = features.model_dump()
    message = {
        **provenance,
        "inputs": INPUTS,
        "fingerprint": current,
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_s": round(time.perf_counter() - started, 2),
        "features": features,
        **commercial_score(features),
    }
    _write(asset_id, "ADVISED", message)
    return {"asset_id": asset_id, "outcome": REVIEWED, "review": message}
