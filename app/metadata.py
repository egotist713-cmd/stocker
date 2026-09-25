"""
Metadata pipeline: загрузка и сохранение metadata_json, события, вызов Metadata AI.

Правила и переходы — в app/metadata_builder.py (чистые функции).
Контракт — docs/METADATA_CONTRACT.md (metadata-v1).

Каждая операция адресуется по asset_id и возвращает структурированный
результат {asset_id, outcome, metadata}: это основа будущего сервисного слоя.
"""

import json
import time
from datetime import datetime, timezone

from app import metadata_builder as mb
from app.ai.analyzer import AIResponseError
from app.ai.metadata_analyzer import LMStudioMetadataAnalyzer, MetadataAnalyzer
from app.ai.schema import AIAnalysis
from app.database.db import get_asset, get_last_event, insert_event, transaction, update_metadata


MAX_RAW_OUTPUT_CHARS = 4000

# Исходы операций.
DRAFTED = "DRAFTED"
METADATA_EXISTS = "METADATA_EXISTS"
METADATA_AI_FAILED = "METADATA_AI_FAILED"
REBUILT = "REBUILT"
EDITED = "EDITED"
NO_CHANGES = "NO_CHANGES"
APPROVED = "APPROVED"
REJECTED = "REJECTED"


class MetadataError(Exception):
    """Операция невозможна; code — машиночитаемая причина."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _result(asset_id: int, outcome: str, metadata: dict | None) -> dict:
    return {"asset_id": asset_id, "outcome": outcome, "metadata": metadata}


def _load(asset_id: int) -> tuple[AIAnalysis, dict | None]:
    asset = get_asset(asset_id)

    if asset is None:
        raise MetadataError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")

    if not asset["ai_result"]:
        raise MetadataError("VISION_MISSING", f"Asset {asset_id} has no Vision ai_result")

    vision = AIAnalysis.model_validate_json(asset["ai_result"])
    metadata = json.loads(asset["metadata_json"]) if asset["metadata_json"] else None
    return vision, metadata


def _require_metadata(asset_id: int) -> tuple[AIAnalysis, dict]:
    vision, metadata = _load(asset_id)

    if metadata is None:
        raise MetadataError("METADATA_MISSING", f"Asset {asset_id} has no metadata; run build first")

    return vision, metadata


def _vision_source(asset_id: int, vision: AIAnalysis) -> dict:
    """Provenance Vision из последнего AI/PASSED. У старых событий сообщение — не JSON."""
    event = get_last_event(asset_id, "AI", "PASSED")
    details = {}

    if event and event["message"]:
        try:
            details = json.loads(event["message"])
        except json.JSONDecodeError:
            details = {}

    return {
        "event_id": event["id"] if event else None,
        "provider": details.get("provider"),
        "model": details.get("model"),
        "prompt_version": details.get("prompt_version"),
        "confidence": vision.confidence,
    }


def _save(asset_id: int, metadata: dict, events: list[tuple[str, str, dict]]) -> list[int]:
    with transaction() as connection:
        event_ids = [insert_event(connection, asset_id, stage, status, _dumps(message)) for stage, status, message in events]
        update_metadata(connection, asset_id, _dumps(metadata))
    return event_ids


def _drafted_event(metadata: dict, trigger: str) -> tuple[str, str, dict]:
    return (
        "METADATA",
        "DRAFTED",
        {
            "builder_version": metadata["sources"]["builder_version"],
            "completeness": metadata["completeness"],
            "trigger": trigger,
            "errors": len(metadata["validation"]["errors"]),
            "warnings": len(metadata["validation"]["warnings"]),
            "keywords": len(metadata["fields"]["keywords"]),
        },
    )


def _event_args(event: tuple[str, str, dict]) -> tuple[str, str, str]:
    stage, status, message = event
    return stage, status, _dumps(message)


def _save_event_only(asset_id: int, event: tuple[str, str, dict]) -> None:
    with transaction() as connection:
        insert_event(connection, asset_id, *_event_args(event))


def build(asset_id: int, force: bool = False, analyzer: MetadataAnalyzer | None = None) -> dict:
    """
    Создать draft: Metadata AI → Python. При сбое Metadata AI — partial draft из Vision.

    Без force существующие metadata не трогаются, кроме partial draft без правок
    человека: для него Metadata AI повторяется.
    """
    vision, existing = _load(asset_id)

    retry_partial = (
        existing is not None
        and existing["completeness"] == mb.PARTIAL
        and not existing["edited_fields"]
    )

    if existing is not None and not force and not retry_partial:
        return _result(asset_id, METADATA_EXISTS, existing)

    analyzer = analyzer or LMStudioMetadataAnalyzer()
    provenance = {
        "provider": analyzer.provider,
        "model": analyzer.model,
        "prompt_version": analyzer.prompt_version,
        "inputs": analyzer.inputs(),
    }
    vision_source = _vision_source(asset_id, vision)
    trigger = "build_force" if force else "build"
    started = time.perf_counter()

    try:
        suggestion = analyzer.suggest(vision)

    except Exception as exc:
        failure = {
            **provenance,
            "failed_at": _now(),
            "duration_s": round(time.perf_counter() - started, 2),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        if isinstance(exc, AIResponseError):
            failure["raw_output"] = exc.raw_output[:MAX_RAW_OUTPUT_CHARS]

        if retry_partial and not force:
            # Повтор для partial не удался: существующий draft остаётся как есть.
            _save_event_only(asset_id, ("METADATA_AI", "FAILED", failure))
            return _result(asset_id, METADATA_AI_FAILED, existing)

        with transaction() as connection:
            failure_id = insert_event(connection, asset_id, "METADATA_AI", "FAILED", _dumps(failure))
            metadata = mb.build_draft(
                vision, None, vision_source=vision_source, metadata_ai_failure_event_id=failure_id
            )
            update_metadata(connection, asset_id, _dumps(metadata))
            insert_event(connection, asset_id, *_event_args(_drafted_event(metadata, trigger)))

        return _result(asset_id, DRAFTED, metadata)

    passed = {
        **provenance,
        "generated_at": _now(),
        "duration_s": round(time.perf_counter() - started, 2),
    }

    with transaction() as connection:
        passed_id = insert_event(connection, asset_id, "METADATA_AI", "PASSED", _dumps(passed))
        metadata_ai_source = {
            "event_id": passed_id,
            "provider": provenance["provider"],
            "model": provenance["model"],
            "prompt_version": provenance["prompt_version"],
            "inputs": provenance["inputs"],
            "generated_at": passed["generated_at"],
        }
        metadata = mb.build_draft(
            vision, suggestion, vision_source=vision_source, metadata_ai_source=metadata_ai_source
        )
        update_metadata(connection, asset_id, _dumps(metadata))
        insert_event(connection, asset_id, *_event_args(_drafted_event(metadata, trigger)))

    return _result(asset_id, DRAFTED, metadata)


def rebuild(asset_id: int) -> dict:
    """Применить текущие Python-правила без AI-вызова, сохранив правки человека."""
    vision, metadata = _require_metadata(asset_id)
    rebuilt = mb.rebuild(metadata, vision)
    _save(asset_id, rebuilt, [_drafted_event(rebuilt, "rebuild")])
    return _result(asset_id, REBUILT, rebuilt)


def edit(asset_id: int, changes: dict) -> dict:
    vision, metadata = _require_metadata(asset_id)

    try:
        edited, applied = mb.edit(metadata, vision, changes)
    except mb.MetadataTransitionError as exc:
        raise MetadataError("INVALID_EDIT", str(exc)) from exc

    if not applied:
        return _result(asset_id, NO_CHANGES, metadata)

    _save(asset_id, edited, [("METADATA", "EDITED", change) for change in applied])
    return _result(asset_id, EDITED, edited)


def approve(asset_id: int, allow_partial: bool = False, confirm_claims: bool = False) -> dict:
    vision, metadata = _require_metadata(asset_id)

    try:
        approved, confirmed = mb.approve(
            metadata, vision, allow_partial=allow_partial, confirm_claims=confirm_claims
        )
    except mb.MetadataTransitionError as exc:
        raise MetadataError("INVALID_TRANSITION", str(exc)) from exc

    event = {
        "builder_version": approved["sources"]["builder_version"],
        "completeness": approved["completeness"],
        "allow_partial": allow_partial,
        "confirmed_claims": confirmed,
    }
    _save(asset_id, approved, [("METADATA", "APPROVED", event)])
    return _result(asset_id, APPROVED, approved)


def reject(asset_id: int, reason: str) -> dict:
    _, metadata = _require_metadata(asset_id)

    try:
        rejected = mb.reject(metadata, reason)
    except mb.MetadataTransitionError as exc:
        raise MetadataError("INVALID_TRANSITION", str(exc)) from exc

    event = {"reason": rejected["review"]["reason"], "state_before": metadata["state"]}
    _save(asset_id, rejected, [("METADATA", "REJECTED", event)])
    return _result(asset_id, REJECTED, rejected)


def show(asset_id: int) -> dict:
    _, metadata = _require_metadata(asset_id)
    return _result(asset_id, metadata["state"].upper(), metadata)
