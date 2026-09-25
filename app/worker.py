import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from app.ai.analyzer import AIAnalyzer, AIResponseError
from app.ai.local_analyzer import LocalAnalyzer
from app.database.db import add_event, get_asset, save_ai_result
from app.ingest import ingest_file, sha256_file, source_file
from app.qc import check_asset, save_qc_result


# Исходы process_asset. Это не assets.status: статус не меняется,
# история фиксируется в processing_events.
AI_PASSED = "AI_PASSED"
AI_FAILED = "AI_FAILED"
AI_ALREADY_DONE = "AI_ALREADY_DONE"
QC_FAILED = "QC_FAILED"
SOURCE_INVALID = "SOURCE_INVALID"
ASSET_NOT_FOUND = "ASSET_NOT_FOUND"

# Исходы, при которых обработка не выполнила свою работу из-за ошибки.
ERROR_OUTCOMES = {AI_FAILED, SOURCE_INVALID, ASSET_NOT_FOUND}

MAX_RAW_OUTPUT_CHARS = 4000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _provenance(analyzer: AIAnalyzer) -> dict:
    return {
        "provider": getattr(analyzer, "provider", type(analyzer).__name__),
        "model": getattr(analyzer, "model", None),
        "prompt_version": getattr(analyzer, "prompt_version", None),
    }


def verify_source(asset: dict) -> dict | None:
    """Вернуть описание проблемы, если исходный файл отсутствует или изменился."""
    path = source_file(asset)

    if not path.exists():
        return {"reason": "SOURCE_MISSING", "source_path": asset["source_path"]}

    actual_hash = sha256_file(path)

    if actual_hash != asset["file_hash"]:
        return {
            "reason": "SOURCE_CHANGED",
            "source_path": asset["source_path"],
            "expected_hash": asset["file_hash"],
            "actual_hash": actual_hash,
        }

    return None


def run_ai(asset_id: int, path: Path, analyzer: AIAnalyzer) -> str:
    provenance = _provenance(analyzer)
    started = time.perf_counter()

    try:
        ai_result = analyzer.analyze(path)

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

        add_event(asset_id, "AI", "FAILED", json.dumps(failure, ensure_ascii=False))
        print(f"AI: FAILED ({failure['error_type']}: {failure['error']})")
        return AI_FAILED

    save_ai_result(asset_id, ai_result.model_dump_json())

    passed = {
        **provenance,
        "analyzed_at": _now(),
        "duration_s": round(time.perf_counter() - started, 2),
    }
    add_event(asset_id, "AI", "PASSED", json.dumps(passed, ensure_ascii=False))
    print("AI: PASSED")
    return AI_PASSED


def process_asset(
    asset_id: int,
    force: bool = False,
    analyzer: AIAnalyzer | None = None,
) -> str:
    """Проверить источник, выполнить QC и AI для уже зарегистрированного asset."""
    asset = get_asset(asset_id)

    if asset is None:
        print(f"Asset not found: {asset_id}")
        return ASSET_NOT_FOUND

    print(f"Asset ID: {asset_id} ({asset['filename']})")

    if asset["ai_result"] and not force:
        print("AI: already done (use --force to re-run)")
        return AI_ALREADY_DONE

    problem = verify_source(asset)

    if problem is not None:
        add_event(asset_id, "SOURCE", "INVALID", json.dumps(problem, ensure_ascii=False))
        print(f"SOURCE: INVALID ({problem['reason']})")
        return SOURCE_INVALID

    qc_result = check_asset(asset)
    save_qc_result(asset_id, qc_result)

    print(f"QC: {qc_result['passed']}")

    if not qc_result["passed"]:
        print("AI: skipped because QC failed")
        return QC_FAILED

    return run_ai(asset_id, source_file(asset), analyzer or LocalAnalyzer())


def _ingest_and_process(path: Path, analyzer: AIAnalyzer | None) -> tuple[int | None, str | None]:
    print(f"WORKER: {path}")

    asset_id = ingest_file(path)

    if asset_id is None:
        print("Worker stopped: file was not added")
        return None, None

    return asset_id, process_asset(asset_id, analyzer=analyzer)


def process_file(path: Path, analyzer: AIAnalyzer | None = None) -> int | None:
    asset_id, _ = _ingest_and_process(path, analyzer)
    return asset_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.worker",
        description="Stocker worker: ingest -> QC -> AI.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("image_path", nargs="?", type=Path, help="new image to ingest and process")
    target.add_argument("--asset-id", type=int, help="re-process an already registered asset")
    parser.add_argument("--force", action="store_true", help="re-run AI even if ai_result exists")
    args = parser.parse_args(argv)

    if args.asset_id is not None:
        outcome = process_asset(args.asset_id, force=args.force)
    else:
        _, outcome = _ingest_and_process(args.image_path, analyzer=None)

    print(f"Outcome: {outcome}")
    return 1 if outcome is None or outcome in ERROR_OUTCOMES else 0


if __name__ == "__main__":
    raise SystemExit(main())
