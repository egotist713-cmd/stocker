import json
import sqlite3

from app import worker
from app.ai.analyzer import AIResponseError
from app.database.db import get_asset
from app.ingest import ingest_file

from tests.conftest import FakeAnalyzer, OfflineMetadataAnalyzer, events, make_image


def test_new_file_runs_full_pipeline(stocker_root):
    analyzer = FakeAnalyzer()

    asset_id = worker.process_file(make_image(stocker_root), analyzer=analyzer)

    asset = get_asset(asset_id)
    assert json.loads(asset["ai_result"])["title"] == "Test title"
    assert [(stage, status) for stage, status, _ in events(stocker_root, asset_id)] == [
        ("INGEST", "DONE"),
        ("QC", "PASSED"),
        ("AI", "PASSED"),
        ("METADATA_AI", "PASSED"),
        ("METADATA", "DRAFTED"),
    ]


def test_ai_passed_event_records_provenance(stocker_root):
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())

    message = json.loads(next(m for s, st, m in events(stocker_root, asset_id) if (s, st) == ("AI", "PASSED")))
    assert message["provider"] == "fake"
    assert message["model"] == "fake-model"
    assert message["prompt_version"] == "test-v1"
    assert "analyzed_at" in message
    assert "duration_s" in message


def test_ai_failure_is_recorded_not_raised(stocker_root):
    analyzer = FakeAnalyzer(error=ConnectionError("LM Studio is down"))

    asset_id = worker.process_file(make_image(stocker_root), analyzer=analyzer)

    assert get_asset(asset_id)["ai_result"] is None
    stage, status, message = events(stocker_root, asset_id)[-1]
    assert (stage, status) == ("AI", "FAILED")
    failure = json.loads(message)
    assert failure["error_type"] == "ConnectionError"
    assert failure["error"] == "LM Studio is down"
    assert failure["model"] == "fake-model"


def test_invalid_model_output_keeps_raw_output(stocker_root):
    analyzer = FakeAnalyzer(error=AIResponseError("bad json", raw_output="```json {broken"))

    asset_id = worker.process_file(make_image(stocker_root), analyzer=analyzer)

    failure = json.loads(events(stocker_root, asset_id)[-1][2])
    assert failure["error_type"] == "AIResponseError"
    assert failure["raw_output"] == "```json {broken"


def test_failed_asset_can_be_retried_by_id(stocker_root):
    asset_id = worker.process_file(
        make_image(stocker_root),
        analyzer=FakeAnalyzer(error=TimeoutError("timeout")),
    )

    outcome = worker.process_asset(asset_id, analyzer=FakeAnalyzer())

    assert outcome == worker.AI_PASSED
    assert get_asset(asset_id)["ai_result"] is not None
    assert [(s, st) for s, st, _ in events(stocker_root, asset_id)][-5:] == [
        ("AI", "FAILED"),
        ("QC", "PASSED"),
        ("AI", "PASSED"),
        ("METADATA_AI", "PASSED"),
        ("METADATA", "DRAFTED"),
    ]


def test_asset_registered_without_ai_can_be_processed(stocker_root):
    asset_id = ingest_file(make_image(stocker_root))

    assert worker.process_asset(asset_id, analyzer=FakeAnalyzer()) == worker.AI_PASSED


def test_completed_asset_is_skipped_unless_forced(stocker_root):
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())
    events_before = len(events(stocker_root, asset_id))

    analyzer = FakeAnalyzer()
    assert worker.process_asset(asset_id, analyzer=analyzer) == worker.AI_ALREADY_DONE
    assert analyzer.calls == 0
    assert len(events(stocker_root, asset_id)) == events_before

    assert worker.process_asset(asset_id, force=True, analyzer=analyzer) == worker.AI_PASSED
    assert analyzer.calls == 1


def test_changed_source_is_recorded_and_not_processed(stocker_root):
    asset_id = ingest_file(make_image(stocker_root, seed=1))
    original_hash = get_asset(asset_id)["file_hash"]
    make_image(stocker_root, seed=2)  # тот же путь, другое содержимое

    analyzer = FakeAnalyzer()
    outcome = worker.process_asset(asset_id, analyzer=analyzer)

    assert outcome == worker.SOURCE_INVALID
    assert analyzer.calls == 0
    stage, status, message = events(stocker_root, asset_id)[-1]
    assert (stage, status) == ("SOURCE", "INVALID")
    problem = json.loads(message)
    assert problem["reason"] == "SOURCE_CHANGED"
    assert problem["expected_hash"] == original_hash
    # Asset и его история сохраняются, QC по подменённому файлу не выполняется.
    assert get_asset(asset_id)["file_hash"] == original_hash
    assert ("QC", "PASSED") not in [(s, st) for s, st, _ in events(stocker_root, asset_id)]


def test_missing_source_is_recorded(stocker_root):
    path = make_image(stocker_root)
    asset_id = ingest_file(path)
    path.unlink()

    assert worker.process_asset(asset_id, analyzer=FakeAnalyzer()) == worker.SOURCE_INVALID
    assert json.loads(events(stocker_root, asset_id)[-1][2])["reason"] == "SOURCE_MISSING"


def test_qc_failure_skips_ai(stocker_root, monkeypatch):
    from app import qc

    monkeypatch.setattr(qc, "MIN_WIDTH", 10_000)
    analyzer = FakeAnalyzer()

    asset_id = worker.process_file(make_image(stocker_root), analyzer=analyzer)

    assert analyzer.calls == 0
    assert events(stocker_root, asset_id)[-1][:2] == ("QC", "FAILED")


def test_unknown_asset_id(stocker_root):
    assert worker.process_asset(999, analyzer=FakeAnalyzer()) == worker.ASSET_NOT_FOUND


def test_cli_exit_code_reflects_outcome(stocker_root, monkeypatch):
    asset_id = ingest_file(make_image(stocker_root))

    monkeypatch.setattr(worker, "LocalAnalyzer", lambda: FakeAnalyzer(error=RuntimeError("down")))
    assert worker.main(["--asset-id", str(asset_id)]) == 1

    monkeypatch.setattr(worker, "LocalAnalyzer", FakeAnalyzer)
    assert worker.main(["--asset-id", str(asset_id)]) == 0
    assert worker.main(["--asset-id", "999"]) == 1


# --- metadata draft в worker ---------------------------------------------------------


def _metadata(asset_id):
    raw = get_asset(asset_id)["metadata_json"]
    return json.loads(raw) if raw else None


def test_worker_creates_draft_never_approves(stocker_root):
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())

    metadata = _metadata(asset_id)
    assert metadata["state"] == "draft"
    assert metadata["completeness"] == "full"
    assert not [s for s, st, _ in events(stocker_root, asset_id) if (s, st) == ("METADATA", "APPROVED")]


def test_no_metadata_when_ai_or_qc_failed(stocker_root, monkeypatch):
    failed_ai = worker.process_file(make_image(stocker_root, name="a.jpg", seed=1), analyzer=FakeAnalyzer(error=RuntimeError("x")))

    from app import qc
    monkeypatch.setattr(qc, "MIN_WIDTH", 10_000)
    failed_qc = worker.process_file(make_image(stocker_root, name="b.jpg", seed=2), analyzer=FakeAnalyzer())

    assert _metadata(failed_ai) is None
    assert _metadata(failed_qc) is None


def test_metadata_ai_failure_gives_partial_draft_and_success_outcome(stocker_root):
    asset_id = ingest_file(make_image(stocker_root))

    outcome = worker.process_asset(
        asset_id, analyzer=FakeAnalyzer(), metadata_analyzer=OfflineMetadataAnalyzer(error=ConnectionError("down"))
    )

    assert outcome == worker.AI_PASSED
    assert _metadata(asset_id)["completeness"] == "partial"


def test_asset_id_builds_missing_metadata_without_rerunning_ai(stocker_root):
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())
    with sqlite3.connect(stocker_root / "data" / "db" / "stocker.db") as connection:
        connection.execute("UPDATE assets SET metadata_json = NULL WHERE id = ?", (asset_id,))
    vision = FakeAnalyzer()

    outcome = worker.process_asset(asset_id, analyzer=vision)

    assert outcome == worker.AI_ALREADY_DONE
    assert vision.calls == 0
    assert _metadata(asset_id)["state"] == "draft"


def test_existing_metadata_is_not_rebuilt_by_worker(stocker_root):
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())
    events_before = len(events(stocker_root, asset_id))
    metadata_ai = OfflineMetadataAnalyzer()

    worker.process_asset(asset_id, metadata_analyzer=metadata_ai)

    assert metadata_ai.calls == 0
    assert len(events(stocker_root, asset_id)) == events_before
