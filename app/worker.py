import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from app import analysis_view
from app import asset_state
from app import stock_readiness
from app import enhancement_decision
from app import metadata as metadata_service
from app import normalization
from app import review_gate
from app.ai.analyzer import AIAnalyzer, AIResponseError, input_fingerprint, vision_inputs
from app.ai.enhancement_advisor import advisor_enabled
from app.ai.metadata_analyzer import MetadataAnalyzer
from app.ai.local_analyzer import LocalAnalyzer
from app.database import db
from app.database.db import add_event, get_asset, insert_event, transaction, update_ai_result
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
NORMALIZE_FAILED = "NORMALIZE_FAILED"
VIEW_UNAVAILABLE = "VIEW_UNAVAILABLE"

# Исходы, при которых обработка не выполнила свою работу из-за ошибки.
ERROR_OUTCOMES = {AI_FAILED, SOURCE_INVALID, ASSET_NOT_FOUND, NORMALIZE_FAILED, VIEW_UNAVAILABLE}

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


def run_ai(asset_id: int, view: analysis_view.AnalysisView, analyzer: AIAnalyzer) -> str:
    identity = analyzer.identity()
    provenance = {
        **_provenance(analyzer),
        "view": view.identity(),
        # Отпечаток входов (ASSET_STATE §2.1a): результат актуален, пока совпадает.
        "inputs": vision_inputs(view.fingerprint, identity),
        "input_fingerprint": input_fingerprint(view.fingerprint, identity),
    }
    started = time.perf_counter()

    try:
        ai_result = analyzer.analyze(view)

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

    # Документ с персональными данными: распознанный текст не хранится (gate-v1.2).
    privacy = None
    document = review_gate.personal_document(ai_result)
    if document and ai_result.text_visible:
        privacy = {"reason": "PERSONAL_DOCUMENT", "terms": document, "text_visible_removed": len(ai_result.text_visible)}
        ai_result = ai_result.model_copy(update={"text_visible": []})

    passed = {
        **provenance,
        "analyzed_at": _now(),
        "duration_s": round(time.perf_counter() - started, 2),
    }
    if privacy:
        passed["privacy"] = privacy

    # Результат и событие — одной транзакцией: сбой между ними (аппаратный сброс
    # 27.09, asset 118) оставлял ai_result без AI/PASSED.
    with transaction() as connection:
        update_ai_result(connection, asset_id, ai_result.model_dump_json())
        insert_event(connection, asset_id, "AI", "PASSED", json.dumps(passed, ensure_ascii=False))
    print("AI: PASSED")
    return AI_PASSED


def run_metadata(asset_id: int, analyzer: MetadataAnalyzer | None = None) -> str:
    """
    Создать metadata draft после Vision. Только draft и события: approve
    всегда выполняет человек. Существующие metadata не перезаписываются.
    """
    result = metadata_service.build(asset_id, analyzer=analyzer)
    metadata = result["metadata"]
    print(f"METADATA: {result['outcome']} ({metadata['state']}, {metadata['completeness']})")
    return result["outcome"]


def run_normalization(asset_id: int) -> str:
    """
    Internal Normalization (docs/INTERNAL_IMAGE_REPRESENTATION_CONTRACT.md): факты об
    исходнике и representation. NORMALIZE/FAILED останавливает pipeline объекта (§7):
    без безопасного представления дальше ничего не угадывается.
    """
    try:
        result = normalization.run_asset(asset_id)
    except Exception as exc:  # noqa: BLE001 — без representation дальше не идём; события нет — результата нет
        print(f"NORMALIZE: crashed ({type(exc).__name__}: {exc})")
        return NORMALIZE_FAILED
    if result["outcome"] == normalization.NORMALIZE_FAILED:
        print(f"NORMALIZE: FAILED ({result['error'].get('error_type')})")
        return NORMALIZE_FAILED
    manifest = result["manifest"]
    color = manifest["preserved"]["color"]["kind"]
    print(f"NORMALIZE: {result['outcome']} ({manifest['representation']}, {color})")
    return result["outcome"]


def run_readiness(asset_id: int) -> str | None:
    """
    Stock Readiness после metadata gate (STOCK_READINESS_CONTRACT §3.9). Только если
    metadata одобрена и всё выше актуально (ASSET_STATE_CONTRACT): оценка на
    устаревших входах не пишется. Правила, без модели; не блокирует pipeline.
    """
    try:
        stages = asset_state.get(asset_id)["stages"]
        if stages["readiness"]["status"] == asset_state.NOT_APPLICABLE:
            print("READINESS: skipped (metadata not approved)")
            return None
        stale = [s for s in ("normalize", "view", "qc", "vision", "metadata") if stages[s]["status"] != asset_state.CURRENT]
        if stale:
            print(f"READINESS: skipped (upstream not current: {stale[0]})")
            return None
        result = stock_readiness.evaluate_asset(asset_id)
    except Exception as exc:  # noqa: BLE001 — оценка готовности не должна останавливать обработку
        print(f"READINESS: skipped ({type(exc).__name__}: {exc})")
        return None
    ready_for = (result.get("readiness") or {}).get("ready_for")
    print(f"READINESS: {result['outcome']} (ready_for={ready_for})")
    return result["outcome"]


def run_enhancement(asset_id: int, view: analysis_view.AnalysisView) -> str | None:
    """
    Enhancement decision правилами (docs/STOCK_READINESS_CONTRACT.md §4.2) по тому же view.
    Только рекомендация: не блокирует pipeline, Topaz не запускается.
    """
    try:
        result = enhancement_decision.assess_asset(asset_id, view=view)
        decision = enhancement_decision.effective_decision(result["assessment"])
        print(f"ENHANCEMENT: {result['outcome']} ({decision})")

        # Модель — только для спорных случаев; её ответ — рекомендация.
        if decision == enhancement_decision.DISPUTED and advisor_enabled():
            advice = enhancement_decision.advise_asset(asset_id, view=view)
            detail = (advice.get("advice") or {}).get("advice", {}).get("decision") or advice.get("error", {}).get("error_type")
            print(f"ENHANCEMENT ADVISOR: {advice['outcome']} ({detail})")
    except Exception as exc:  # noqa: BLE001 — рекомендация не должна останавливать обработку
        print(f"ENHANCEMENT: skipped ({type(exc).__name__}: {exc})")
        return None
    return result["outcome"]


def process_asset(
    asset_id: int,
    force: bool = False,
    analyzer: AIAnalyzer | None = None,
    metadata_analyzer: MetadataAnalyzer | None = None,
) -> str:
    """Проверить источник, выполнить QC, AI и metadata draft для уже зарегистрированного asset."""
    asset = get_asset(asset_id)

    if asset is None:
        print(f"Asset not found: {asset_id}")
        return ASSET_NOT_FOUND

    print(f"Asset ID: {asset_id} ({asset['filename']})")

    if asset["ai_result"] and not force:
        print("AI: already done (use --force to re-run)")
        run_metadata(asset_id, metadata_analyzer)
        return AI_ALREADY_DONE

    problem = verify_source(asset)

    if problem is not None:
        add_event(asset_id, "SOURCE", "INVALID", json.dumps(problem, ensure_ascii=False))
        print(f"SOURCE: INVALID ({problem['reason']})")
        return SOURCE_INVALID

    if run_normalization(asset_id) == NORMALIZE_FAILED:
        print("QC, AI: skipped because normalization failed")
        return NORMALIZE_FAILED

    # Один AnalysisView на все стадии, читающие пиксели (контракт §14).
    try:
        view = analysis_view.open_asset_view(asset_id)
    except analysis_view.ViewUnavailable as exc:
        add_event(asset_id, "VIEW", "FAILED", json.dumps({"error_type": exc.code, "error": str(exc)}, ensure_ascii=False))
        print(f"VIEW: FAILED ({exc.code})")
        return VIEW_UNAVAILABLE

    qc_result = check_asset(asset, view)
    save_qc_result(asset_id, qc_result)

    print(f"QC: {qc_result['passed']}")

    if not qc_result["passed"]:
        print("AI: skipped because QC failed")
        return QC_FAILED

    run_enhancement(asset_id, view)

    outcome = run_ai(asset_id, view, analyzer or LocalAnalyzer())

    if outcome == AI_PASSED:
        run_metadata(asset_id, metadata_analyzer)
        run_readiness(asset_id)

    return outcome


def ingest_and_process(path: Path, analyzer: AIAnalyzer | None = None) -> tuple[int | None, str | None]:
    """Новый файл: ingest, затем process_asset. Возвращает (asset_id, outcome)."""
    print(f"WORKER: {path}")

    asset_id = ingest_file(path)

    if asset_id is None:
        print("Worker stopped: file was not added")
        return None, None

    return asset_id, process_asset(asset_id, analyzer=analyzer)


def process_file(path: Path, analyzer: AIAnalyzer | None = None) -> int | None:
    asset_id, _ = ingest_and_process(path, analyzer)
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
    print(db.describe())

    if args.asset_id is not None:
        outcome = process_asset(args.asset_id, force=args.force)
    else:
        _, outcome = ingest_and_process(args.image_path, analyzer=None)

    print(f"Outcome: {outcome}")
    return 1 if outcome is None or outcome in ERROR_OUTCOMES else 0


if __name__ == "__main__":
    raise SystemExit(main())
