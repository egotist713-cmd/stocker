"""
Publication Gate publication-v1 (docs/ASSET_STATE_CONTRACT.md §2.2, §2.2a).

Решение по площадкам после Metadata → Readiness; Creative Review — советник (информация,
не блокирует). Отдельный детерминированный слой: ничего не меняет в upstream.
"""

import dataclasses
import json

import pytest
from PIL import ImageCms

from app import asset_state, creative_review, publication, reprocess, worker
from app import readiness as rd
from app import review_gate as rg
from app.ai.creative_advisor import prompt_version_for
from app.ai.schema import CreativeReview
from app.service import dispatch

from tests.conftest import FakeAnalyzer, OfflineCreativeAdvisor, events, make_image
from tests.test_service import VISION

SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


class StockVision(FakeAnalyzer):
    def analyze(self, view):
        return VISION


@pytest.fixture(autouse=True)
def small_images_allowed(monkeypatch):
    monkeypatch.setattr(rd, "PROFILES", {n: dataclasses.replace(p, min_mp=0.001) for n, p in rd.PROFILES.items()})


def ready_asset(root, seed=0) -> int:
    return worker.process_file(make_image(root, seed=seed, icc_profile=SRGB_ICC), analyzer=StockVision())


def advisor(recommendation="proceed"):
    a = OfflineCreativeAdvisor(review=CreativeReview(composition="good", uniqueness="medium", demand="high",
                                                     commercial_use_cases=["a"], recommendation=recommendation, confidence=0.8))
    a.prompt_version = prompt_version_for(a.profile)
    return a


def pub(asset_id) -> dict:
    return asset_state.get(asset_id)["stages"]["publication"]


def pub_events(root, asset_id):
    return [st for s, st, _ in events(root, asset_id) if s == "PUBLICATION"]


# --- Решение по площадкам ------------------------------------------------------------------


def test_per_platform_decision_and_state(stocker_root):
    asset_id = ready_asset(stocker_root)
    assert pub(asset_id)["status"] == "missing" and asset_state.get(asset_id)["state"] == asset_state.PLATFORM_READY

    result = dispatch("publication.evaluate", {"asset_id": asset_id})
    assert result["ok"] and result["outcome"] == "EVALUATED"
    decision = result["data"]["publication"]
    assert decision["approved_for"] == ["adobe", "shutterstock"]
    assert {p: r["status"] for p, r in decision["platforms"].items()} == {"adobe": "approved", "shutterstock": "approved"}
    assert decision["platforms"]["adobe"]["notes"] == ["NO_CURRENT_ADVICE"]  # Creative не обязателен

    state = asset_state.get(asset_id)
    assert state["state"] == asset_state.PUBLICATION_APPROVED and state["approved_for"] == ["adobe", "shutterstock"]
    assert pub_events(stocker_root, asset_id) == ["EVALUATED"]


def test_platform_specific_semantics_not_a_single_flag(stocker_root, monkeypatch):
    profiles = dict(rd.PROFILES)
    profiles["shutterstock"] = dataclasses.replace(profiles["shutterstock"], min_mp=1000.0, version="shutterstock-test")
    monkeypatch.setattr(rd, "PROFILES", profiles)
    asset_id = ready_asset(stocker_root)
    assert asset_state.get(asset_id)["ready_for"] == ["adobe"]

    decision = publication.evaluate_asset(asset_id)["publication"]
    assert decision["approved_for"] == ["adobe"]
    assert decision["platforms"]["shutterstock"] == {"status": "blocked", "reasons": ["RESOLUTION_TOO_LOW"],
                                                     "notes": [], "profile": "shutterstock-test"}
    assert asset_state.get(asset_id)["approved_for"] == ["adobe"]


def test_creative_advice_is_information_not_a_blocker(stocker_root):
    asset_id = ready_asset(stocker_root)
    creative_review.review_asset(asset_id, advisor("skip_suggested"))
    decision = publication.evaluate_asset(asset_id)["publication"]
    assert decision["approved_for"] == ["adobe", "shutterstock"]
    assert decision["platforms"]["adobe"]["notes"] == ["ADVISOR_SKIP_SUGGESTED"]
    assert decision["inputs"]["advice"] == {"current": True, "recommendation": "skip_suggested"}


# --- Применимость и отказы (без событий) -----------------------------------------------------


def test_not_applicable_without_approved_metadata_or_ready_readiness(stocker_root):
    asset_id = ready_asset(stocker_root)
    dispatch("metadata.escalate", {"asset_id": asset_id, "reason": "check"})
    before = events(stocker_root, asset_id)
    assert pub(asset_id)["reason"] == "METADATA_NOT_APPROVED"
    with pytest.raises(publication.PublicationError) as error:
        publication.evaluate_asset(asset_id)
    assert error.value.code == "PUBLICATION_NOT_APPLICABLE" and events(stocker_root, asset_id) == before


def test_not_applicable_when_readiness_blocked(stocker_root):
    asset_id = worker.process_file(make_image(stocker_root, icc_profile=None), analyzer=StockVision())  # COLOR_SPACE_UNDECLARED
    assert pub(asset_id) == {"status": "not_applicable", "reason": "READINESS_BLOCKED", "has_result": False}


def test_source_is_verified_with_sha256_right_before_decision(stocker_root):
    asset_id = ready_asset(stocker_root)
    path = stocker_root / "data" / "incoming" / "photo.jpg"
    data = bytearray(path.read_bytes())
    data[-3] ^= 0xFF  # тот же размер — ловит только SHA256
    path.write_bytes(bytes(data))
    before = events(stocker_root, asset_id)
    with pytest.raises(publication.PublicationError) as error:
        publication.evaluate_asset(asset_id)
    assert error.value.code == "SOURCE_INVALID" and events(stocker_root, asset_id) == before


def test_rejected_is_refused(stocker_root):
    asset_id = ready_asset(stocker_root)
    dispatch("metadata.reject", {"asset_id": asset_id, "reason": "no"}, actor="human")
    with pytest.raises(publication.PublicationError) as error:
        publication.evaluate_asset(asset_id)
    assert error.value.code == "REJECTED"


def test_upstream_not_current_is_refused(stocker_root):
    asset_id = ready_asset(stocker_root)
    dispatch("metadata.edit", {"asset_id": asset_id, "add_keywords": ["factory floor"]})  # Readiness устарел
    before = events(stocker_root, asset_id)
    with pytest.raises(publication.PublicationError) as error:
        publication.evaluate_asset(asset_id)
    assert error.value.code == "UPSTREAM_NOT_CURRENT" and events(stocker_root, asset_id) == before


# --- Идемпотентность и «ничего не меняет» ------------------------------------------------------


def test_repeat_is_unchanged_without_event(stocker_root):
    asset_id = ready_asset(stocker_root)
    publication.evaluate_asset(asset_id)
    before = events(stocker_root, asset_id)
    assert publication.evaluate_asset(asset_id)["outcome"] == publication.UNCHANGED
    assert events(stocker_root, asset_id) == before


def test_publication_changes_nothing_upstream(stocker_root):
    asset_id = ready_asset(stocker_root)
    creative_review.review_asset(asset_id, advisor())
    before = {op: dispatch(op, {"asset_id": asset_id})["data"] for op in ("readiness.get", "creative.get")}
    metadata_before = dispatch("asset.get", {"asset_id": asset_id})["data"]["metadata"]
    publication.evaluate_asset(asset_id)
    assert {op: dispatch(op, {"asset_id": asset_id})["data"] for op in before} == before
    assert dispatch("asset.get", {"asset_id": asset_id})["data"]["metadata"] == metadata_before


# --- Устаревание ----------------------------------------------------------------------------


def test_metadata_change_cascades_to_publication(stocker_root):
    asset_id = ready_asset(stocker_root)
    creative_review.review_asset(asset_id, advisor())
    publication.evaluate_asset(asset_id)
    dispatch("metadata.edit", {"asset_id": asset_id, "add_keywords": ["factory floor"]})

    stages = asset_state.get(asset_id)["stages"]
    assert stages["readiness"]["status"] == "stale"
    assert stages["creative_review"]["reason"] == "UPSTREAM_STALE:readiness"
    assert stages["publication"]["status"] == "stale" and stages["publication"]["reason"] == "UPSTREAM_STALE:readiness"
    data = dispatch("publication.get", {"asset_id": asset_id})["data"]
    assert data["result"] is None and data["approved_for"] == [] and data["last_result"]["current"] is False

    reprocess.run(asset_id, reprocess_from="readiness", dry_run=False)  # новые поля → новый отпечаток Readiness
    stage = pub(asset_id)
    assert stage["status"] == "stale" and stage["changed"] == ["readiness_fingerprint"]
    assert asset_state.get(asset_id)["reprocess_from"] == "publication"
    result = reprocess.run(asset_id, reprocess_from="publication", dry_run=False)
    assert [(e["stage"], e["outcome"]) for e in result["executed"]] == [("publication", "EVALUATED")]
    assert asset_state.get(asset_id)["state"] == asset_state.PUBLICATION_APPROVED


def test_readiness_change_affecting_a_platform_invalidates_publication(stocker_root, monkeypatch):
    asset_id = ready_asset(stocker_root)
    publication.evaluate_asset(asset_id)
    profiles = dict(rd.PROFILES)
    profiles["shutterstock"] = dataclasses.replace(profiles["shutterstock"], min_mp=1000.0, version="shutterstock-2026-10")
    monkeypatch.setattr(rd, "PROFILES", profiles)

    assert pub(asset_id)["status"] == "stale"
    reprocess.run(asset_id, reprocess_from="readiness", dry_run=False)
    reprocess.run(asset_id, reprocess_from="publication", dry_run=False)
    data = dispatch("publication.get", {"asset_id": asset_id})["data"]
    assert data["current"] and data["approved_for"] == ["adobe"]
    assert data["result"]["platforms"]["shutterstock"]["status"] == "blocked"


def test_creative_recommendation_change_invalidates_publication(stocker_root):
    asset_id = ready_asset(stocker_root)
    creative_review.review_asset(asset_id, advisor("proceed"))
    publication.evaluate_asset(asset_id)
    with _db_update(stocker_root, asset_id, recommendation="attention"):
        pass
    stage = pub(asset_id)
    assert stage["status"] == "stale" and stage["changed"] == ["advice"]
    publication.evaluate_asset(asset_id)
    assert dispatch("publication.get", {"asset_id": asset_id})["data"]["result"]["platforms"]["adobe"]["notes"] == ["ADVISOR_ATTENTION"]


def test_same_advice_after_creative_rereview_is_no_cascade(stocker_root, monkeypatch):
    asset_id = ready_asset(stocker_root)
    creative_review.review_asset(asset_id, advisor("proceed"))
    publication.evaluate_asset(asset_id)
    # Новая оценка Creative (другой отпечаток — например, новая версия шаблона) с тем же советом.
    with _db_update(stocker_root, asset_id, fingerprint_only=True):
        pass
    monkeypatch.setattr(creative_review, "fingerprint", lambda *a, **k: "sha256:rereviewed")
    assert asset_state.get(asset_id)["stages"]["creative_review"]["status"] == "current"
    assert pub(asset_id)["status"] == "current"  # совет тот же — Publication не устарел
    before = events(stocker_root, asset_id)
    assert publication.evaluate_asset(asset_id)["outcome"] == publication.UNCHANGED
    assert events(stocker_root, asset_id) == before


def test_gate_policy_regate_with_same_decision_is_no_cascade(stocker_root, monkeypatch):
    asset_id = ready_asset(stocker_root)
    creative_review.review_asset(asset_id, advisor())
    publication.evaluate_asset(asset_id)
    monkeypatch.setattr(rg, "POLICY_VERSION", "gate-v1.4-test")
    assert pub(asset_id)["status"] == "stale"  # до re-gate — по наследованию

    before = events(stocker_root, asset_id)
    result = reprocess.run(asset_id, dry_run=False)
    assert [(e["stage"], e["outcome"]) for e in result["executed"]] == [("metadata", "GATED"), ("readiness", "SKIPPED_CURRENT")]
    assert pub(asset_id)["status"] == "current"  # решение gate не изменилось → Publication не пересчитывается
    assert [s for s, _, _ in events(stocker_root, asset_id)[len(before):]] == ["METADATA", "REPROCESS"]
    assert asset_state.get(asset_id)["state"] == asset_state.PUBLICATION_APPROVED


# --- reprocess и права ------------------------------------------------------------------------


def test_publication_only_on_request_and_not_for_n8n(stocker_root):
    asset_id = ready_asset(stocker_root)
    for start in ("qc", "vision", "metadata", "readiness"):
        assert "publication" not in [s["stage"] for s in reprocess.run(asset_id, reprocess_from=start)["steps"]]
    assert dispatch("publication.evaluate", {"asset_id": asset_id}, actor="workflow:n8n")["error"]["code"] == "FORBIDDEN"
    planned = reprocess.run(asset_id, reprocess_from="publication")
    assert [(s["stage"], s["action"]) for s in planned["steps"]] == [("publication", "run")]
    done = reprocess.run(asset_id, reprocess_from="publication", dry_run=False)
    assert done["outcome"] == reprocess.REPROCESSED
    assert reprocess.run(asset_id, reprocess_from="publication", dry_run=False)["outcome"] == reprocess.NOTHING_TO_DO


class _db_update:
    """Подмена последней оценки Creative в БД (рекомендация или только отпечаток) — для сценариев совета."""

    def __init__(self, root, asset_id, recommendation=None, fingerprint_only=False):
        import sqlite3

        self.connection = sqlite3.connect(root / "data" / "db" / "stocker.db")
        row = self.connection.execute(
            "SELECT id, message FROM processing_events WHERE asset_id = ? AND stage = 'CREATIVE_REVIEW' ORDER BY id DESC LIMIT 1",
            (asset_id,)).fetchone()
        message = json.loads(row[1])
        if recommendation:
            message["features"]["recommendation"] = recommendation
        if fingerprint_only:
            message["fingerprint"] = "sha256:rereviewed"
        self.connection.execute("UPDATE processing_events SET message = ? WHERE id = ?", (json.dumps(message), row[0]))
        self.connection.commit()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.connection.close()


def test_on_request_stages_skip_not_applicable_instead_of_refusing(stocker_root):
    # human_review: Readiness не применим — это не «устаревший upstream»; повтор — no-op без событий.
    asset_id = ready_asset(stocker_root)
    dispatch("metadata.escalate", {"asset_id": asset_id, "reason": "check"})
    before = events(stocker_root, asset_id)
    for start in ("publication", "creative_review"):
        result = reprocess.run(asset_id, reprocess_from=start, dry_run=False)
        assert result["refused"] is None and result["outcome"] == reprocess.NOTHING_TO_DO
        assert [(e["stage"], e["outcome"]) for e in result["executed"]] == [(start, reprocess.NOT_APPLICABLE)]
    assert events(stocker_root, asset_id) == before
    # А устаревший Readiness по-прежнему — отказ.
    other = ready_asset(stocker_root, seed=3)
    dispatch("metadata.edit", {"asset_id": other, "add_keywords": ["factory floor"]})
    assert reprocess.run(other, reprocess_from="publication", dry_run=False)["refused"]["code"] == "UPSTREAM_NOT_CURRENT"
