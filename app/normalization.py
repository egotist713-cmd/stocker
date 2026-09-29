"""
Normalization, шаг 1: факты об исходнике и события NORMALIZE/* (docs/FORMAT_CONTRACT.md §3).

Файл только читается (app/source_facts.py). Пока нормализованное представление не
создаётся и pipeline не останавливается: шаг 1 наблюдает и записывает. Событие —
это и есть результат (атомарно, паспорт §3A.14).
"""

import hashlib
import json
from pathlib import Path

from app import ingest
from app import normalizer
from app.database import db
from app.database.db import get_asset, get_connection, insert_event, transaction
from app.source_facts import FACTS_VERSION, detect_container, read_facts

STAGE = "NORMALIZE"

# Исходы операций.
EVALUATED = "EVALUATED"
NORMALIZED = "NORMALIZED"
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


def _failed(asset_id: int, error_type: str, error: str, fingerprint: str, **extra) -> dict:
    """FAILED идемпотентен: та же причина при тех же входах — без нового события."""
    previous = _stored(_last_event(asset_id, "FAILED"))
    if previous and previous.get("fingerprint") == fingerprint and previous.get("error_type") == error_type:
        return {"asset_id": asset_id, "outcome": NORMALIZE_FAILED, "facts": None, "manifest": None, "error": previous}
    message = {"facts_version": FACTS_VERSION, "error_type": error_type, "error": error[:500], "fingerprint": fingerprint, **extra}
    _write(asset_id, "FAILED", message)
    return {"asset_id": asset_id, "outcome": NORMALIZE_FAILED, "facts": None, "manifest": None, "error": message}


def summary(asset: dict, evaluated: dict | None, failed: dict | None = None, passed: dict | None = None) -> dict:
    """pipeline.normalize: факты об исходнике и representation — без чтения файла."""
    result = _stored(evaluated)
    newest = max((e for e in (evaluated, passed) if e), key=lambda e: e["id"], default=None)
    last_failed = _stored(failed) if failed and (not newest or failed["id"] > newest["id"]) else None
    manifest = _stored(passed)
    representation = {
        "normalized": manifest is not None,
        "representation": (manifest or {}).get("representation"),
        "representation_stale": bool(manifest) and manifest.get("fingerprint") != normalizer.fingerprint(asset["file_hash"]),
    }
    if result is None:
        return {"evaluated": False, "stale": False, "failed": (last_failed or {}).get("error_type"), "event_id": None, **representation}
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
        **representation,
    }


def summary_from_events(asset: dict, events: list[dict]) -> dict:
    def last(status):
        return next((e for e in reversed(events) if e["stage"] == STAGE and e["status"] == status), None)

    return summary(asset, last("EVALUATED"), last("FAILED"), last("PASSED"))


def get(asset_id: int) -> dict:
    asset = get_asset(asset_id)
    if asset is None:
        raise NormalizationError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")
    evaluated, passed = _last_event(asset_id, "EVALUATED"), _last_event(asset_id, "PASSED")
    return {
        **summary(asset, evaluated, _last_event(asset_id, "FAILED"), passed),
        "facts": (_stored(evaluated) or {}).get("facts"),
        "manifest": _stored(passed),
    }


def representation_path(asset: dict, manifest: dict) -> Path:
    """Файл representation: сам source (pass-through) или internal derivative."""
    if manifest["representation"] == normalizer.SOURCE:
        return ingest.source_file(asset)
    return ingest.ROOT / manifest["derivative"]["path"]


def _manifest_valid(asset: dict, manifest: dict) -> bool:
    if manifest["representation"] == normalizer.SOURCE:
        return True
    path = representation_path(asset, manifest)
    return path.exists() and ingest.sha256_file(path) == manifest["derivative"]["sha256"]


def run_asset(asset_id: int) -> dict:
    """
    Internal Normalization (шаг 2): representation объекта по контракту.
    Идемпотентно для успеха и отказа: те же входы — без нового события.
    """
    asset = get_asset(asset_id)
    if asset is None:
        raise NormalizationError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")

    facts_result = evaluate_asset(asset_id)
    if facts_result["outcome"] == NORMALIZE_FAILED:
        return {"asset_id": asset_id, "outcome": NORMALIZE_FAILED, "manifest": None, "error": facts_result["error"]}
    facts = facts_result["facts"]
    current = normalizer.fingerprint(asset["file_hash"])

    last = _stored(_last_event(asset_id, "PASSED"))
    if last and last.get("fingerprint") == current and _manifest_valid(asset, last):
        return {"asset_id": asset_id, "outcome": UNCHANGED, "manifest": last}

    def refuse(code: str, error: str) -> dict:
        return _failed(asset_id, code, error, current, stage="engine", normalizer_version=normalizer.NORMALIZER_VERSION)

    try:
        planned = normalizer.plan(facts)
    except normalizer.NormalizationRefused as exc:
        return refuse(exc.code, str(exc))

    derivative = None
    if planned["representation"] == normalizer.DERIVATIVE:
        target = db.internal_dir() / str(asset_id) / normalizer.NORMALIZER_VERSION
        try:
            built = normalizer.build_derivative(ingest.source_file(asset), target)
        except Exception as exc:  # noqa: BLE001 — не декодировано: отказ с причиной, без догадок
            return refuse(normalizer.DECODE_ERROR, f"{type(exc).__name__}: {exc}")
        derivative = {**built, "path": built["path"].relative_to(ingest.ROOT).as_posix()}

    source_color = planned["preserved"]["color"]
    color = normalizer.representation_color(source_color, derivative)
    normalizer.check_color(source_color, color)

    manifest = {
        "normalizer_version": normalizer.NORMALIZER_VERSION,
        "params_hash": normalizer.PARAMS_HASH,
        "fingerprint": current,
        "source_sha256": asset["file_hash"],
        "facts_fingerprint": fingerprint(asset["file_hash"]),
        "representation": planned["representation"],
        "derivative": derivative,
        "color": color,
        "transforms": planned["transforms"],
        "preserved": planned["preserved"],
        "decoder": normalizer.decoder_versions(),
    }
    # Файл derivative уже на месте (атомарно); событие — одной транзакцией (§8).
    _write(asset_id, "PASSED", manifest)
    return {"asset_id": asset_id, "outcome": NORMALIZED, "manifest": manifest}


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
        return _failed(asset_id, "SOURCE_MISSING", f"Source file not found: {asset['source_path']}", current)
    if ingest.sha256_file(path) != asset["file_hash"]:
        return _failed(asset_id, "SOURCE_CHANGED", "Source file hash does not match assets.file_hash", current)

    try:
        facts = read_facts(path, asset["filename"])
    except Exception as exc:  # noqa: BLE001 — «не угадывать»: непрочитанный файл — FAILED с причиной
        container = detect_container(path.read_bytes()[:16])
        error_type = "MISSING_CODEC" if container in ("HEIF",) else "DECODE_ERROR" if container else "UNSUPPORTED_FORMAT"
        return _failed(asset_id, error_type, f"{type(exc).__name__}: {exc}", current, container=container)

    _write(asset_id, "EVALUATED", {"facts_version": FACTS_VERSION, "fingerprint": current, "facts": facts})
    return {"asset_id": asset_id, "outcome": EVALUATED, "facts": facts}
