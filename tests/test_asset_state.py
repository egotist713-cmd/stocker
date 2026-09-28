"""
Unified Asset State и Staleness (docs/ASSET_STATE_CONTRACT.md): регрессии.

Read-model: состояние выводится из результатов стадий и отпечатков; ничего не пишет.
"""

import dataclasses
import json
import sqlite3

import pytest
from PIL import ImageCms

from app import analysis_view, asset_state, normalization, normalizer, worker
from app import readiness as rd
from app.database.db import add_event
from app.ingest import ingest_file, sha256_file
from app.service import dispatch

from tests.conftest import FakeAnalyzer, events, make_image
from tests.test_service import VISION

SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
HUMAN = "human"


class StockVision(FakeAnalyzer):
    """Vision с содержательным ответом (категории, keywords) — чтобы Readiness мог быть ready."""

    def analyze(self, view):
        self.calls += 1
        return VISION


@pytest.fixture
def small_images_allowed(monkeypatch):
    profiles = {name: dataclasses.replace(p, min_mp=0.001) for name, p in rd.PROFILES.items()}
    monkeypatch.setattr(rd, "PROFILES", profiles)


def processed(root, *, icc_profile=SRGB_ICC, analyzer=None, name="photo.jpg", seed=0) -> int:
    path = make_image(root, name=name, seed=seed, icc_profile=icc_profile)
    return worker.process_file(path, analyzer=analyzer or StockVision())


def state(asset_id: int) -> dict:
    return asset_state.get(asset_id)


def ops(result: dict) -> list[str]:
    return [a["operation"] for a in result["allowed_actions"]]


def _db(root):
    return sqlite3.connect(root / "data" / "db" / "stocker.db")


# --- Основной путь ----------------------------------------------------------------------


def test_processed_asset_is_metadata_approved_then_platform_ready(stocker_root, small_images_allowed):
    asset_id = processed(stocker_root)
    result = state(asset_id)
    assert result["state"] == asset_state.METADATA_APPROVED and result["metadata_approved"]
    assert result["ready_for"] == [] and "readiness.evaluate" in ops(result)
    assert all(result["stages"][s]["status"] == "current" for s in ("normalize", "view", "qc", "vision", "metadata"))

    dispatch("readiness.evaluate", {"asset_id": asset_id})
    result = state(asset_id)
    assert result["state"] == asset_state.PLATFORM_READY
    assert result["ready_for"] == ["adobe", "shutterstock"]
    assert "creative.review" in ops(result)  # советующая стадия ещё не выполнена
    assert result["stages"]["publication"]["status"] == "not_implemented"


def test_metadata_approval_is_not_platform_readiness(stocker_root, small_images_allowed):
    # Цвет не объявлен: metadata одобрена, но площадке нужен sRGB → blocked, а не «ready».
    asset_id = processed(stocker_root, icc_profile=None)
    dispatch("readiness.evaluate", {"asset_id": asset_id})
    result = state(asset_id)
    assert result["metadata_approved"] and result["state"] == asset_state.BLOCKED
    assert result["reasons"][0] == "READINESS_BLOCKED" and "COLOR_SPACE_UNDECLARED" in result["reasons"]
    assert result["ready_for"] == []


def test_state_is_read_only(stocker_root):
    asset_id = processed(stocker_root)
    before = len(events(stocker_root, asset_id))
    for _ in range(3):
        state(asset_id)
    assert len(events(stocker_root, asset_id)) == before


# --- Source integrity: регрессии #67 / #68 ------------------------------------------------


@pytest.mark.parametrize("metadata_state", ["auto_approved", "human_review"])
def test_missing_source_is_never_ready_nor_approvable(stocker_root, metadata_state):
    asset_id = processed(stocker_root)
    if metadata_state == "human_review":
        dispatch("metadata.escalate", {"asset_id": asset_id, "reason": "check"})
    (stocker_root / "data" / "incoming" / "photo.jpg").unlink()  # как #67 / #68

    result = state(asset_id)
    assert result["metadata_state"] == metadata_state
    assert result["state"] == asset_state.SOURCE_INVALID and result["reasons"] == ["SOURCE_MISSING"]
    assert result["problems"] == ["SOURCE_MISSING"]
    assert result["allowed_actions"] == []  # и уж точно не metadata.approve
    assert result["ready_for"] == []


def test_changed_source_is_detected_without_hashing(stocker_root):
    asset_id = processed(stocker_root)
    (stocker_root / "data" / "incoming" / "photo.jpg").write_bytes(b"x" * 10)  # другой размер
    assert state(asset_id)["reasons"] == ["SOURCE_CHANGED"]


def test_same_size_change_needs_verify(stocker_root):
    asset_id = processed(stocker_root)
    path = stocker_root / "data" / "incoming" / "photo.jpg"
    data = bytearray(path.read_bytes())
    data[-3] ^= 0xFF  # тот же размер, другие байты
    path.write_bytes(bytes(data))
    assert asset_state.get(asset_id, verify_source=True)["state"] == asset_state.SOURCE_INVALID


def test_negative_integrity_evidence_wins_until_rechecked(stocker_root):
    asset_id = processed(stocker_root)
    add_event(asset_id, "SOURCE", "INVALID", json.dumps({"reason": "SOURCE_CHANGED"}))
    assert state(asset_id)["state"] == asset_state.SOURCE_INVALID
    assert asset_state.get(asset_id, verify_source=True)["state"] != asset_state.SOURCE_INVALID


# --- Блокирующие отказы ---------------------------------------------------------------------


def test_normalize_failed_blocks(stocker_root):
    path = stocker_root / "data" / "incoming" / "photo.jpg"
    path.write_bytes(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64)
    with _db(stocker_root) as connection:  # регистрация в обход ingest (HEIC ingest не принимает)
        connection.execute("INSERT INTO assets (filename, source_path, file_hash, file_size, status) VALUES (?,?,?,?,?)",
                           ("photo.jpg", "data/incoming/photo.jpg", sha256_file(path),
                            path.stat().st_size, "NEW"))
        asset_id = connection.execute("SELECT max(id) FROM assets").fetchone()[0]
        connection.execute("INSERT INTO processing_events (asset_id, stage, status, message) VALUES (?, 'INGEST', 'DONE', '')", (asset_id,))
    assert worker.process_asset(asset_id, analyzer=FakeAnalyzer()) == worker.NORMALIZE_FAILED

    result = state(asset_id)
    assert result["state"] == asset_state.BLOCKED and result["reasons"] == ["NORMALIZE_FAILED:MISSING_CODEC"]
    assert result["problems"] == ["NORMALIZE_FAILED:MISSING_CODEC"] and ops(result) == ["normalize.get"]


def test_view_failed_blocks(stocker_root, monkeypatch):
    def unavailable(asset_id):
        raise analysis_view.ViewUnavailable("REPRESENTATION_INVALID", "gone")

    monkeypatch.setattr(analysis_view, "open_asset_view", unavailable)
    asset_id, outcome = worker.ingest_and_process(make_image(stocker_root), analyzer=FakeAnalyzer())
    assert outcome == worker.VIEW_UNAVAILABLE

    result = state(asset_id)
    assert result["state"] == asset_state.BLOCKED and result["reasons"] == ["VIEW_FAILED:REPRESENTATION_INVALID"]
    assert ops(result) == ["normalize.run"]


def test_qc_failed_blocks(stocker_root, monkeypatch):
    from app import qc

    monkeypatch.setattr(qc, "MIN_MEGAPIXELS", 4.0)
    asset_id = processed(stocker_root)
    result = state(asset_id)
    assert result["state"] == asset_state.BLOCKED and result["reasons"] == ["QC_FAILED"]


def test_vision_failure_is_retryable_error(stocker_root):
    asset_id = processed(stocker_root, analyzer=FakeAnalyzer(error=RuntimeError("model down")))
    result = state(asset_id)
    assert result["state"] == asset_state.ERROR and result["reasons"] == ["VISION_FAILED"]
    assert ops(result) == ["asset.process"]


# --- Staleness --------------------------------------------------------------------------------


def test_normalizer_change_makes_everything_downstream_stale(stocker_root, monkeypatch):
    asset_id = processed(stocker_root)
    monkeypatch.setattr(normalizer, "PARAMS_HASH", "sha256:new-params")
    result = state(asset_id)
    assert result["state"] == asset_state.STALE and result["reprocess_from"] == "normalize"
    stages = result["stages"]
    assert stages["normalize"]["reason"] == "FINGERPRINT_CHANGED"
    for name in ("view", "qc", "vision", "metadata", "enhancement", "creative_review"):
        if stages[name]["status"] != "missing":
            assert stages[name]["status"] == "stale", name


def test_view_version_change_stales_pixel_stages_not_facts(stocker_root, monkeypatch):
    asset_id = processed(stocker_root)
    monkeypatch.setattr(normalizer, "VIEW_VERSION", "analysis-view-v2")
    result = state(asset_id)
    assert result["stages"]["normalize"]["status"] == "current"
    assert result["reprocess_from"] == "qc" and result["stages"]["qc"]["reason"] == "FINGERPRINT_CHANGED"
    assert result["stages"]["vision"]["reason"] == "FINGERPRINT_CHANGED"
    assert result["stages"]["enhancement"]["status"] == "stale"


def test_legacy_vision_without_view_is_stale(stocker_root):
    # Как 128 Vision-результатов, полученных до AnalysisView.
    asset_id = processed(stocker_root)
    with _db(stocker_root) as connection:
        connection.execute("UPDATE processing_events SET message = ? WHERE asset_id = ? AND stage = 'AI' AND status = 'PASSED'",
                           (json.dumps({"provider": "fake", "prompt_version": "test-v1"}), asset_id))
    result = state(asset_id)
    assert result["stages"]["vision"]["reason"] == "NO_FINGERPRINT"
    assert result["stages"]["metadata"]["reason"] == "UPSTREAM_STALE:vision"
    assert result["state"] == asset_state.STALE and result["reprocess_from"] == "vision"


@pytest.mark.parametrize("legacy_keys", [("input_fingerprint", "inputs"), ("input_fingerprint", "inputs", "view")])
def test_legacy_qc_without_fingerprint_is_stale(stocker_root, legacy_keys):
    # Как 129 QC до этапа B: без input_fingerprint (с view в метриках или без) — STALE.
    asset_id = processed(stocker_root)
    with _db(stocker_root) as connection:
        qc = json.loads(connection.execute("SELECT qc_result FROM assets WHERE id = ?", (asset_id,)).fetchone()[0])
        for key in legacy_keys:
            (qc["metrics"] if key == "view" else qc).pop(key)
        connection.execute("UPDATE assets SET qc_result = ? WHERE id = ?", (json.dumps(qc), asset_id))
    result = state(asset_id)
    assert result["reprocess_from"] == "qc" and result["stages"]["qc"]["reason"] == "NO_FINGERPRINT"
    assert "STALE:qc:NO_FINGERPRINT" in result["problems"]


@pytest.mark.parametrize("change", ["model", "prompt_version"])
def test_vision_identity_change_is_stale_and_explained(stocker_root, monkeypatch, change):
    asset_id = processed(stocker_root)
    current = FakeAnalyzer.current_identity()
    monkeypatch.setitem(asset_state.VISION_IDENTITIES, "fake", lambda: {**current, change: "other"})
    vision = state(asset_id)["stages"]["vision"]
    assert vision["reason"] == "FINGERPRINT_CHANGED" and vision["changed"] == [change]


def test_unknown_vision_provider_is_stale(stocker_root, monkeypatch):
    asset_id = processed(stocker_root)
    monkeypatch.setattr(asset_state, "VISION_IDENTITIES", {})
    assert state(asset_id)["stages"]["vision"]["reason"] == "UNKNOWN_PROVIDER"


def test_metadata_on_older_vision_is_stale(stocker_root):
    asset_id = processed(stocker_root)
    passed = [json.loads(m) for s, st, m in events(stocker_root, asset_id) if (s, st) == ("AI", "PASSED")][-1]
    add_event(asset_id, "AI", "PASSED", json.dumps(passed))  # новый Vision event, metadata — на старом
    result = state(asset_id)
    assert result["stages"]["metadata"]["reason"] == "VISION_CHANGED"
    assert result["reprocess_from"] == "metadata" and ops(result) == ["metadata.build"]


def test_readiness_is_stale_after_metadata_edit(stocker_root, small_images_allowed):
    asset_id = processed(stocker_root)
    dispatch("readiness.evaluate", {"asset_id": asset_id})
    dispatch("metadata.edit", {"asset_id": asset_id, "add_keywords": ["factory floor"]})
    result = state(asset_id)
    assert result["metadata_state"] == "auto_approved"
    assert result["state"] == asset_state.STALE and result["reprocess_from"] == "readiness"
    assert ops(result) == ["readiness.evaluate"]


def test_old_readiness_is_not_applicable_after_human_review(stocker_root, small_images_allowed):
    # Как #47: Readiness v1 был оценён, затем metadata ушла в human_review.
    asset_id = processed(stocker_root)
    dispatch("readiness.evaluate", {"asset_id": asset_id})
    dispatch("metadata.escalate", {"asset_id": asset_id, "reason": "people"})
    result = state(asset_id)
    assert result["state"] == asset_state.HUMAN_REVIEW
    assert result["stages"]["readiness"]["status"] == "not_applicable" and result["ready_for"] == []


def test_stale_creative_review_is_advisory(stocker_root, small_images_allowed):
    from tests.conftest import OfflineCreativeAdvisor

    asset_id = processed(stocker_root)
    dispatch("readiness.evaluate", {"asset_id": asset_id})
    from app import creative_review
    from app.ai.creative_advisor import prompt_version_for

    advisor = OfflineCreativeAdvisor()
    advisor.prompt_version = prompt_version_for(advisor.profile)  # текущий шаблон и профиль
    creative_review.review_asset(asset_id, advisor=advisor)
    assert state(asset_id)["stages"]["creative_review"]["status"] == "current"
    assert "creative.review" not in ops(state(asset_id))

    with _db(stocker_root) as connection:  # как 373 оценки с отпечатком до AnalysisView
        row = connection.execute("SELECT id, message FROM processing_events WHERE asset_id = ? AND stage = 'CREATIVE_REVIEW'", (asset_id,)).fetchone()
        message = json.loads(row[1])
        message["fingerprint"] = "sha256:before-analysis-view"
        connection.execute("UPDATE processing_events SET message = ? WHERE id = ?", (json.dumps(message), row[0]))
    result = state(asset_id)
    assert result["state"] == asset_state.PLATFORM_READY  # советующая стадия состояние не меняет
    assert "STALE:creative_review:FINGERPRINT_CHANGED" in result["problems"] and "creative.review" in ops(result)


# --- Human review: реальные переходы (в рабочей БД их ещё не было) -------------------------------


@pytest.fixture
def in_human_review(stocker_root):
    asset_id = processed(stocker_root)
    dispatch("metadata.escalate", {"asset_id": asset_id, "reason": "manual check"})
    result = state(asset_id)
    assert result["state"] == asset_state.HUMAN_REVIEW and "MANUAL_ESCALATION" in result["reasons"]
    assert {"metadata.approve", "metadata.reject"} <= set(ops(result))
    return asset_id


def test_human_review_to_approve(stocker_root, in_human_review):
    assert dispatch("metadata.approve", {"asset_id": in_human_review}, actor="agent:openclaw")["error"]["code"] == "FORBIDDEN"
    envelope = dispatch("metadata.approve", {"asset_id": in_human_review}, actor=HUMAN)
    assert envelope["ok"], envelope

    result = state(in_human_review)
    assert result["metadata_state"] == "approved" and result["metadata_approved"]
    assert result["state"] == asset_state.METADATA_APPROVED and "readiness.evaluate" in ops(result)
    assert ("METADATA", "APPROVED") in [(s, st) for s, st, _ in events(stocker_root, in_human_review)]


def test_human_review_to_reject(stocker_root, in_human_review):
    envelope = dispatch("metadata.reject", {"asset_id": in_human_review, "reason": "not commercial"}, actor=HUMAN)
    assert envelope["ok"], envelope

    result = state(in_human_review)
    assert result["state"] == asset_state.REJECTED and result["terminal"]
    assert result["allowed_actions"] == [] and result["problems"] == []
    # Rejected — terminal даже при потере source.
    (stocker_root / "data" / "incoming" / "photo.jpg").unlink()
    assert state(in_human_review)["state"] == asset_state.REJECTED


def test_partial_metadata_is_processing(stocker_root):
    from tests.conftest import OfflineMetadataAnalyzer

    asset_id = ingest_file(make_image(stocker_root, icc_profile=SRGB_ICC))
    worker.process_asset(asset_id, analyzer=StockVision(),
                         metadata_analyzer=OfflineMetadataAnalyzer(error=RuntimeError("metadata model down")))
    result = state(asset_id)
    assert result["metadata_state"] == "draft"
    assert result["state"] == asset_state.PROCESSING and result["reasons"] == ["METADATA_PARTIAL"]
    assert "metadata.build" in ops(result)


# --- Этап A: asset.get и review.queue показывают derived state -----------------------------------


def test_asset_get_shows_state_and_no_approve_without_source(stocker_root):
    asset_id = processed(stocker_root)
    dispatch("metadata.escalate", {"asset_id": asset_id, "reason": "check"})
    (stocker_root / "data" / "incoming" / "photo.jpg").unlink()

    view = dispatch("asset.get", {"asset_id": asset_id})["data"]
    assert view["state"]["state"] == "source_invalid" and view["state"]["problems"] == ["SOURCE_MISSING"]
    assert view["pipeline"]["source"] == "missing"  # раньше — «ok» (#67 / #68)
    assert view["pipeline"]["metadata"] == "human_review"  # результат стадии не скрыт
    assert view["allowed_actions"] == []
    assert "ready" not in view["pipeline"] and view["pipeline"]["metadata_approved"] is False


def test_review_queue_reports_stale_and_source_invalid(stocker_root, monkeypatch):
    stale_id = processed(stocker_root, name="a.jpg")
    missing_id = processed(stocker_root, name="b.jpg", seed=1)
    (stocker_root / "data" / "incoming" / "b.jpg").unlink()
    monkeypatch.setattr(normalizer, "VIEW_VERSION", "analysis-view-v2")  # всё по пикселям устарело

    summary = dispatch("review.queue", {})["data"]["summary"]
    assert summary["by_state"]["stale"] == 1 and summary["by_state"]["source_invalid"] == 1
    assert summary["metadata_approved"] == 2 and summary["platform_ready"] == 0
    problems = {item["id"]: item for item in summary["problem_assets"]}
    assert problems[missing_id]["problems"] == ["SOURCE_MISSING"]
    assert problems[stale_id]["state"] == "stale" and "STALE:qc:FINGERPRINT_CHANGED" in problems[stale_id]["problems"]
    assert summary["problem_counts"]["SOURCE_MISSING"] == 1 and summary["problem_counts"]["STALE:qc"] == 1

    stale_view = dispatch("asset.get", {"asset_id": stale_id})["data"]
    assert {"operation": "metadata.approve", "access": "review"} not in stale_view["allowed_actions"]
