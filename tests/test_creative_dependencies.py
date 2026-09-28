"""
Creative Review: зависимости и устаревание (docs/ASSET_STATE_CONTRACT.md §2.1b).

Данные (отпечаток): source, AnalysisView (overview), результат Vision, шаблон + профиль.
Применимость: metadata одобрена и актуальна → актуальный Readiness готов хотя бы для одной
площадки → Creative. Советник: ничего не меняет в metadata, Readiness и решениях.
"""

import dataclasses
import json

import pytest
from PIL import ImageCms

from app import asset_state, creative_review, reprocess, worker
from app import readiness as rd
from app.service import dispatch

from tests.conftest import FakeAnalyzer, OfflineCreativeAdvisor, OfflineMetadataAnalyzer, events, make_image
from tests.test_service import VISION

SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


class StockVision(FakeAnalyzer):
    def __init__(self, vision=VISION, **kwargs):
        super().__init__(**kwargs)
        self.vision = vision

    def analyze(self, view):
        self.calls += 1
        return self.vision


@pytest.fixture(autouse=True)
def small_images_allowed(monkeypatch):
    monkeypatch.setattr(rd, "PROFILES", {n: dataclasses.replace(p, min_mp=0.001) for n, p in rd.PROFILES.items()})


def advisor():
    from app.ai.creative_advisor import prompt_version_for

    a = OfflineCreativeAdvisor()
    a.prompt_version = prompt_version_for(a.profile)  # как у настоящего советника
    return a


def ready_asset(root, icc_profile=SRGB_ICC, seed=0) -> int:
    asset_id = worker.process_file(make_image(root, seed=seed, icc_profile=icc_profile), analyzer=StockVision())
    return asset_id


def creative(asset_id) -> dict:
    return asset_state.get(asset_id)["stages"]["creative_review"]


def creative_events(root, asset_id):
    return [st for s, st, _ in events(root, asset_id) if s == "CREATIVE_REVIEW"]


def run(asset_id, **kwargs):
    kwargs.setdefault("analyzer", StockVision())
    kwargs.setdefault("metadata_analyzer", OfflineMetadataAnalyzer())
    kwargs.setdefault("creative_advisor", advisor())
    return reprocess.run(asset_id, **kwargs)


# --- Применимость и основной путь ----------------------------------------------------------


def test_applicable_only_after_ready_readiness(stocker_root):
    asset_id = ready_asset(stocker_root)
    assert asset_state.get(asset_id)["state"] == asset_state.PLATFORM_READY
    assert creative(asset_id)["status"] == "missing"

    result = creative_review.review_asset(asset_id, advisor())
    assert result["outcome"] == creative_review.REVIEWED
    assert creative(asset_id)["status"] == "current"
    data = dispatch("creative.get", {"asset_id": asset_id})["data"]
    assert data["current"] is True and data["result"]["fingerprint"] and "last_result" not in data


def test_repeat_is_unchanged_without_event(stocker_root):
    asset_id = ready_asset(stocker_root)
    creative_review.review_asset(asset_id, advisor())
    before = events(stocker_root, asset_id)
    assert creative_review.review_asset(asset_id, advisor())["outcome"] == creative_review.UNCHANGED
    assert events(stocker_root, asset_id) == before


def test_creative_is_advisory_and_changes_nothing_else(stocker_root):
    asset_id = ready_asset(stocker_root)
    before = dispatch("asset.get", {"asset_id": asset_id})["data"]
    readiness_before = dispatch("readiness.get", {"asset_id": asset_id})["data"]
    creative_review.review_asset(asset_id, advisor())
    after = dispatch("asset.get", {"asset_id": asset_id})["data"]
    assert after["metadata"] == before["metadata"] and after["state"]["state"] == before["state"]["state"]
    assert dispatch("readiness.get", {"asset_id": asset_id})["data"] == readiness_before
    assert [s for s, _, _ in events(stocker_root, asset_id)][-1] == "CREATIVE_REVIEW"


# --- Не применим: human_review, Readiness blocked --------------------------------------------


def test_human_review_makes_creative_not_applicable_and_history(stocker_root):
    asset_id = ready_asset(stocker_root)
    creative_review.review_asset(asset_id, advisor())
    dispatch("metadata.escalate", {"asset_id": asset_id, "reason": "check"})

    stage = creative(asset_id)
    assert stage["status"] == "not_applicable" and stage["reason"] == "METADATA_NOT_APPROVED" and stage["has_result"]
    before = events(stocker_root, asset_id)
    with pytest.raises(creative_review.CreativeReviewError) as error:
        creative_review.review_asset(asset_id, advisor())
    assert error.value.code == "CREATIVE_NOT_APPLICABLE" and events(stocker_root, asset_id) == before
    data = dispatch("creative.get", {"asset_id": asset_id})["data"]
    assert data["current"] is False and data["result"] is None and data["last_result"]["current"] is False
    assert dispatch("asset.get", {"asset_id": asset_id})["data"]["pipeline"]["creative_review"]["current"] is False


def test_blocked_readiness_makes_creative_not_applicable(stocker_root):
    asset_id = ready_asset(stocker_root, icc_profile=None)  # цвет не объявлен → COLOR_SPACE_UNDECLARED
    state = asset_state.get(asset_id)
    assert state["state"] == asset_state.BLOCKED and "COLOR_SPACE_UNDECLARED" in state["reasons"]
    assert creative(asset_id) == {"status": "not_applicable", "reason": "READINESS_BLOCKED", "has_result": False}
    with pytest.raises(creative_review.CreativeReviewError) as error:
        creative_review.review_asset(asset_id, advisor())
    assert error.value.code == "CREATIVE_NOT_APPLICABLE"
    assert creative_events(stocker_root, asset_id) == []


# --- Зависимость от metadata / Readiness / Vision --------------------------------------------


def test_metadata_change_stales_creative_via_readiness_without_new_model_call(stocker_root):
    asset_id = ready_asset(stocker_root)
    creative_review.review_asset(asset_id, advisor())
    dispatch("metadata.edit", {"asset_id": asset_id, "add_keywords": ["factory floor"]})

    stage = creative(asset_id)
    assert stage["status"] == "stale" and stage["reason"] == "UPSTREAM_STALE:readiness"
    with pytest.raises(creative_review.CreativeReviewError) as error:  # запрет записи при stale upstream
        creative_review.review_asset(asset_id, advisor())
    assert error.value.code == "UPSTREAM_NOT_CURRENT"

    run(asset_id, reprocess_from="readiness", dry_run=False)
    # Данные Creative (view, Vision, шаблон) не менялись — оценка снова актуальна без вызова модели.
    assert creative(asset_id)["status"] == "current"
    assert creative_events(stocker_root, asset_id) == ["ADVISED"]


def test_vision_change_stales_creative_own_inputs(stocker_root):
    asset_id = ready_asset(stocker_root)
    creative_review.review_asset(asset_id, advisor())
    from tests.test_reprocess import make_legacy

    make_legacy(stocker_root, asset_id, qc_legacy=False)
    new_vision = StockVision(vision=VISION.model_copy(update={"title": "Elevator shaft with new guide rails"}))
    run(asset_id, reprocess_from="vision", dry_run=False, analyzer=new_vision)  # цепочка по умолчанию — без Creative
    assert creative_events(stocker_root, asset_id) == ["ADVISED"]
    stage = creative(asset_id)
    assert stage["status"] == "stale" and stage["reason"] == "FINGERPRINT_CHANGED"  # другой результат Vision

    result = run(asset_id, reprocess_from="creative_review", dry_run=False)
    assert [(e["stage"], e["outcome"]) for e in result["executed"]] == [("creative_review", "REVIEWED")]
    assert creative(asset_id)["status"] == "current"


# --- reprocess: только по запросу, dry-run, no-op, защита при записи ---------------------------


def test_default_chains_do_not_run_creative(stocker_root):
    asset_id = ready_asset(stocker_root)
    for start in ("qc", "vision", "metadata", "readiness"):
        planned = reprocess.run(asset_id, reprocess_from=start)
        assert "creative_review" not in [s["stage"] for s in planned["steps"]]


def test_reprocess_creative_dry_run_noop_and_refusals(stocker_root):
    asset_id = ready_asset(stocker_root)
    before = events(stocker_root, asset_id)
    planned = reprocess.run(asset_id, reprocess_from="creative_review")
    assert planned["outcome"] == reprocess.DRY_RUN and [(s["stage"], s["action"]) for s in planned["steps"]] == [("creative_review", "run")]
    assert events(stocker_root, asset_id) == before

    done = run(asset_id, reprocess_from="creative_review", dry_run=False)
    assert done["outcome"] == reprocess.REPROCESSED
    after = events(stocker_root, asset_id)
    assert [(s, st) for s, st, _ in after[len(before):]] == [("CREATIVE_REVIEW", "ADVISED"), ("REPROCESS", "DONE")]
    assert run(asset_id, reprocess_from="creative_review", dry_run=False)["outcome"] == reprocess.NOTHING_TO_DO
    assert events(stocker_root, asset_id) == after

    dispatch("metadata.edit", {"asset_id": asset_id, "add_keywords": ["factory floor"]})  # Readiness устарел
    refused = run(asset_id, reprocess_from="creative_review", dry_run=False)
    assert refused["refused"]["code"] == "UPSTREAM_NOT_CURRENT" and refused["refused"]["stage"] == "readiness"


def test_creative_write_is_blocked_if_upstream_goes_stale_after_plan(stocker_root, monkeypatch):
    asset_id = ready_asset(stocker_root)
    original = reprocess.plan

    def plan_then_metadata_changes(*args, **kwargs):
        result = original(*args, **kwargs)
        dispatch("metadata.edit", {"asset_id": asset_id, "add_keywords": ["factory floor"]})
        return result

    monkeypatch.setattr(reprocess, "plan", plan_then_metadata_changes)
    result = run(asset_id, reprocess_from="creative_review", dry_run=False)
    assert result["outcome"] == reprocess.STOPPED and result["stopped"]["upstream"] == "readiness"
    assert creative_events(stocker_root, asset_id) == []
    assert not [e for e in events(stocker_root, asset_id) if e[0] == "REPROCESS"]


def test_profile_selection_is_preserved_on_reprocess(stocker_root):
    asset_id = ready_asset(stocker_root)
    dispatch("creative.review", {"asset_id": asset_id, "profile": "auto"})  # как 108 оценок рабочей БД
    assert reprocess._previous_profile(asset_id) == "auto"
