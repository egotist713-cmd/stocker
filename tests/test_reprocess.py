"""
Этап C: контролируемый asset.reprocess (docs/ASSET_STATE_CONTRACT.md §6).

Пересчитывается указанная стадия и её downstream, только неактуальные стадии; upstream
не трогается; отказ при невалидном source / upstream; dry-run ничего не пишет; повтор —
no-op; история не переписывается; решения человека не перезаписываются.
"""

import json
import sqlite3

import pytest
from PIL import ImageCms

from app import asset_state, qc, reprocess, worker
from app.service import dispatch

from tests.conftest import FakeAnalyzer, OfflineMetadataAnalyzer, events, make_image
from tests.test_service import VISION

SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


class StockVision(FakeAnalyzer):
    def analyze(self, view):
        self.calls += 1
        return VISION


def _db(root):
    return sqlite3.connect(root / "data" / "db" / "stocker.db")


def processed(root, name="photo.jpg", seed=0) -> int:
    return worker.process_file(make_image(root, name=name, seed=seed, icc_profile=SRGB_ICC), analyzer=StockVision())


def make_legacy(root, asset_id, *, qc_legacy=True, vision_legacy=True):
    """Как 126 объектов рабочей БД: QC и Vision без отпечатка (история до этапа B)."""
    with _db(root) as connection:
        if qc_legacy:
            result = json.loads(connection.execute("SELECT qc_result FROM assets WHERE id = ?", (asset_id,)).fetchone()[0])
            result.pop("input_fingerprint"), result.pop("inputs")
            connection.execute("UPDATE assets SET qc_result = ? WHERE id = ?", (json.dumps(result), asset_id))
        if vision_legacy:
            connection.execute("UPDATE processing_events SET message = ? WHERE asset_id = ? AND stage = 'AI'",
                               (json.dumps({"provider": "fake", "prompt_version": "test-v1"}), asset_id))


def run(asset_id, **kwargs):
    kwargs.setdefault("analyzer", StockVision())
    kwargs.setdefault("metadata_analyzer", OfflineMetadataAnalyzer())
    return reprocess.run(asset_id, **kwargs)


def stages(result):
    return [(s["stage"], s["action"]) for s in result["steps"]]


def executed(result):
    return [(e["stage"], e["outcome"]) for e in result["executed"]]


# --- План (dry-run) -------------------------------------------------------------------------


def test_dry_run_shows_plan_and_writes_nothing(stocker_root):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id)
    before = events(stocker_root, asset_id)

    result = reprocess.run(asset_id)  # dry_run по умолчанию

    assert result["outcome"] == reprocess.DRY_RUN and result["executed"] == []
    assert result["state"] == "stale" and result["reasons"] == ["STALE:qc:NO_FINGERPRINT"]
    assert result["reprocess_from"] == "qc"
    assert stages(result) == [("qc", "run"), ("enhancement", "skip"), ("vision", "run"), ("metadata", "run"), ("readiness", "run")]
    steps = {s["stage"]: s for s in result["steps"]}
    assert steps["qc"]["why"] == "NO_FINGERPRINT" and steps["vision"]["why"] == "NO_FINGERPRINT"
    assert steps["metadata"]["why"] == "UPSTREAM_STALE:vision"
    assert "assets.ai_result" in steps["vision"]["writes"] and steps["enhancement"]["writes"] == []
    assert {n["stage"] for n in result["not_run"]} == {"creative_review", "publication"}
    assert events(stocker_root, asset_id) == before


def test_metadata_is_planned_when_vision_will_rerun(stocker_root, monkeypatch):
    # Собственный вход metadata не менялся, но Vision устарел — metadata в плане по наследованию.
    asset_id = processed(stocker_root)
    monkeypatch.setitem(asset_state.VISION_IDENTITIES, "fake", lambda: {**FakeAnalyzer.current_identity(), "model": "v2"})
    result = reprocess.run(asset_id, reprocess_from="vision")
    assert stages(result) == [("vision", "run"), ("metadata", "run"), ("readiness", "run")]
    assert result["steps"][1]["why"] == "UPSTREAM_STALE:vision"
    assert result["steps"][0]["why"] == "FINGERPRINT_CHANGED"


# --- Выполнение ------------------------------------------------------------------------------


def test_reprocess_from_vision_runs_vision_then_metadata(stocker_root):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id, qc_legacy=False)  # QC актуален, Vision — наследие
    qc_before = [e for e in events(stocker_root, asset_id) if e[0] in ("QC", "NORMALIZE", "ENHANCEMENT")]

    result = run(asset_id, reprocess_from="vision", dry_run=False)

    assert result["outcome"] == reprocess.REPROCESSED
    assert executed(result) == [("vision", "AI_PASSED"), ("metadata", "DRAFTED"), ("readiness", "EVALUATED")]
    state = asset_state.get(asset_id)
    assert state["stages"]["vision"]["status"] == "current" and state["stages"]["metadata"]["status"] == "current"
    assert state["stages"]["readiness"]["status"] == "current"
    # Upstream не тронут: ни одного нового события QC / NORMALIZE / ENHANCEMENT.
    assert [e for e in events(stocker_root, asset_id) if e[0] in ("QC", "NORMALIZE", "ENHANCEMENT")] == qc_before


def test_reprocess_from_qc_runs_the_chain(stocker_root):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id)

    result = run(asset_id, dry_run=False)

    assert executed(result) == [("qc", "QC_PASSED"), ("enhancement", "SKIPPED_CURRENT"),
                                ("vision", "AI_PASSED"), ("metadata", "DRAFTED"), ("readiness", "EVALUATED")]
    state = asset_state.get(asset_id)
    assert all(state["stages"][s]["status"] == "current" for s in reprocess.PIPELINE_ORDER)
    # 64×48 ниже минимума площадок: актуальная оценка Readiness — blocked (не «готов», не «stale»).
    assert state["state"] == asset_state.BLOCKED and state["reasons"][0] == "READINESS_BLOCKED"
    done = [json.loads(m) for s, st, m in events(stocker_root, asset_id) if (s, st) == ("REPROCESS", "DONE")]
    assert len(done) == 1 and done[0]["state_before"] == "stale" and done[0]["state_after"] == "blocked"


def test_repeat_is_a_noop(stocker_root):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id)
    run(asset_id, dry_run=False)
    before = events(stocker_root, asset_id)
    fingerprints = asset_state.get(asset_id)["stages"]

    again = run(asset_id, reprocess_from="qc", dry_run=False)

    assert again["outcome"] == reprocess.NOTHING_TO_DO
    assert {e["outcome"] for e in again["executed"]} == {"SKIPPED_CURRENT"}
    assert events(stocker_root, asset_id) == before  # ни результатов стадий, ни REPROCESS-события
    after = asset_state.get(asset_id)["stages"]
    assert after["qc"]["current_fp"] == fingerprints["qc"]["current_fp"]
    assert after["vision"]["stored_fp"] == fingerprints["vision"]["stored_fp"]
    # Без reprocess_from у актуального объекта — отказ «нечего пересчитывать».
    assert reprocess.run(asset_id, dry_run=False)["refused"]["code"] == "NOTHING_STALE"


def test_through_limits_the_chain(stocker_root):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id)
    result = run(asset_id, through="qc", dry_run=False)
    assert executed(result) == [("qc", "QC_PASSED")]
    state = asset_state.get(asset_id)
    assert state["reprocess_from"] == "vision" and state["stages"]["qc"]["status"] == "current"


def test_history_is_not_rewritten(stocker_root):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id)
    before = events(stocker_root, asset_id)
    run(asset_id, dry_run=False)
    after = events(stocker_root, asset_id)
    assert after[: len(before)] == before and len(after) > len(before)


# --- Защиты ---------------------------------------------------------------------------------


def test_upstream_must_be_current(stocker_root):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id)  # QC — наследие
    before = events(stocker_root, asset_id)
    result = run(asset_id, reprocess_from="vision", dry_run=False)
    assert result["outcome"] == reprocess.REFUSED
    assert result["refused"]["code"] == "UPSTREAM_NOT_CURRENT" and result["refused"]["stage"] == "qc"
    assert events(stocker_root, asset_id) == before


@pytest.mark.parametrize("damage", ["delete", "same_size"])
def test_invalid_source_is_refused(stocker_root, damage):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id)
    path = stocker_root / "data" / "incoming" / "photo.jpg"
    if damage == "delete":
        path.unlink()
    else:  # тот же размер — ловит только SHA256
        data = bytearray(path.read_bytes())
        data[-3] ^= 0xFF
        path.write_bytes(bytes(data))
    before = events(stocker_root, asset_id)
    result = run(asset_id, dry_run=False)
    assert result["outcome"] == reprocess.REFUSED and result["refused"]["code"] == "SOURCE_INVALID"
    assert events(stocker_root, asset_id) == before


def test_rejected_is_refused(stocker_root):
    asset_id = processed(stocker_root)
    dispatch("metadata.reject", {"asset_id": asset_id, "reason": "no"}, actor="human")
    make_legacy(stocker_root, asset_id)
    assert run(asset_id, dry_run=False)["refused"]["code"] == "REJECTED"


def test_qc_failure_stops_before_vision(stocker_root, monkeypatch):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id)
    monkeypatch.setattr(qc, "MIN_MEGAPIXELS", 4.0)
    result = run(asset_id, dry_run=False)
    assert result["outcome"] == reprocess.STOPPED and result["stopped"]["code"] == "QC_FAILED"
    assert executed(result) == [("qc", "QC_FAILED")]
    assert asset_state.get(asset_id)["state"] == asset_state.BLOCKED


def test_vision_failure_stops_before_metadata(stocker_root):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id, qc_legacy=False)
    metadata_before = dispatch("asset.get", {"asset_id": asset_id})["data"]["metadata"]
    result = run(asset_id, reprocess_from="vision", dry_run=False, analyzer=FakeAnalyzer(error=RuntimeError("down")))
    assert result["stopped"]["code"] == worker.AI_FAILED and executed(result) == [("vision", worker.AI_FAILED)]
    assert dispatch("asset.get", {"asset_id": asset_id})["data"]["metadata"] == metadata_before


@pytest.mark.parametrize("human", ["edit", "approve"])
def test_human_metadata_is_never_overwritten(stocker_root, human):
    asset_id = processed(stocker_root)
    if human == "edit":
        dispatch("metadata.edit", {"asset_id": asset_id, "title": "Human title for the elevator shaft"})
    else:
        dispatch("metadata.escalate", {"asset_id": asset_id, "reason": "check"})
        dispatch("metadata.approve", {"asset_id": asset_id}, actor="human")
    make_legacy(stocker_root, asset_id, qc_legacy=False)
    metadata_before = dispatch("asset.get", {"asset_id": asset_id})["data"]["metadata"]

    planned = reprocess.run(asset_id, reprocess_from="vision")
    assert stages(planned)[-1] == ("metadata", "stop")

    result = run(asset_id, reprocess_from="vision", dry_run=False)
    assert result["outcome"] == reprocess.STOPPED
    assert result["stopped"]["code"] == ("HUMAN_EDITS" if human == "edit" else "HUMAN_DECISION")
    assert dispatch("asset.get", {"asset_id": asset_id})["data"]["metadata"] == metadata_before
    assert asset_state.get(asset_id)["stages"]["metadata"]["reason"] == "VISION_CHANGED"  # честно stale


def test_not_connected_stage_is_refused(stocker_root):
    asset_id = processed(stocker_root)
    result = dispatch("asset.reprocess", {"asset_id": asset_id, "reprocess_from": "creative_review"})
    assert result["ok"] is False and result["error"]["code"] == "INVALID_PARAMS"


# --- Сервис ----------------------------------------------------------------------------------


def test_service_operation_and_rights(stocker_root):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id)
    envelope = dispatch("asset.reprocess", {"asset_id": asset_id}, actor="agent:openclaw")
    assert envelope["ok"] and envelope["outcome"] == "DRY_RUN" and envelope["data"]["reprocess_from"] == "qc"
    # Массовый пересчёт через n8n не подключён.
    assert dispatch("asset.reprocess", {"asset_id": asset_id}, actor="workflow:n8n")["error"]["code"] == "FORBIDDEN"
    view = dispatch("asset.get", {"asset_id": asset_id})["data"]
    assert view["allowed_actions"][0] == {"operation": "asset.reprocess", "access": "pipeline", "params": {"reprocess_from": "qc"}}


# --- Диагностика этапа C: normalize, metadata, through, no-op ----------------------------------


def test_from_normalize_uses_the_same_pipeline_chain(stocker_root):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id)
    planned = reprocess.run(asset_id, reprocess_from="normalize")
    assert [s["stage"] for s in planned["steps"]] == list(reprocess.PIPELINE_ORDER)
    assert stages(planned) == [("normalize", "skip"), ("view", "skip"), ("qc", "run"), ("enhancement", "skip"),
                               ("vision", "run"), ("metadata", "run"), ("readiness", "run")]
    result = run(asset_id, reprocess_from="normalize", dry_run=False)
    assert executed(result) == [("normalize", "SKIPPED_CURRENT"), ("view", "SKIPPED_CURRENT"), ("qc", "QC_PASSED"),
                                ("enhancement", "SKIPPED_CURRENT"), ("vision", "AI_PASSED"), ("metadata", "DRAFTED"),
                                ("readiness", "EVALUATED")]


def test_from_normalize_after_normalizer_change_reruns_every_pixel_stage(stocker_root, monkeypatch):
    asset_id = processed(stocker_root)
    monkeypatch.setattr(normalizer_module(), "PARAMS_HASH", "sha256:new-params")
    planned = reprocess.run(asset_id)
    assert planned["reprocess_from"] == "normalize"
    assert stages(planned) == [("normalize", "run"), ("view", "derive"), ("qc", "run"), ("enhancement", "run"),
                               ("vision", "run"), ("metadata", "run"), ("readiness", "run")]
    result = run(asset_id, dry_run=False)
    assert executed(result) == [("normalize", "NORMALIZED"), ("view", "DERIVED"), ("qc", "QC_PASSED"),
                                ("enhancement", "ASSESSED"), ("vision", "AI_PASSED"), ("metadata", "DRAFTED"),
                                ("readiness", "EVALUATED")]
    state = asset_state.get(asset_id)
    assert all(state["stages"][s]["status"] == "current" for s in reprocess.PIPELINE_ORDER)


def normalizer_module():
    from app import normalizer

    return normalizer


def test_from_metadata_with_current_vision(stocker_root):
    asset_id = processed(stocker_root)
    passed = [json.loads(m) for s, st, m in events(stocker_root, asset_id) if (s, st) == ("AI", "PASSED")][-1]
    from app.database.db import add_event

    add_event(asset_id, "AI", "PASSED", json.dumps(passed))  # новый актуальный Vision event, metadata — на старом
    assert asset_state.get(asset_id)["reprocess_from"] == "metadata"
    result = run(asset_id, reprocess_from="metadata", dry_run=False)
    assert executed(result) == [("metadata", "DRAFTED"), ("readiness", "EVALUATED")]
    assert asset_state.get(asset_id)["stages"]["metadata"]["status"] == "current"


def test_from_metadata_with_stale_vision_is_refused_without_writing(stocker_root):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id, qc_legacy=False)
    before = events(stocker_root, asset_id)
    result = run(asset_id, reprocess_from="metadata", dry_run=False)
    assert result["refused"]["code"] == "UPSTREAM_NOT_CURRENT" and result["refused"]["stage"] == "vision"
    assert events(stocker_root, asset_id) == before


def test_upstream_is_rechecked_right_before_the_stage_writes(stocker_root, monkeypatch):
    # План построен, когда Vision был актуален; перед записью metadata Vision устарел —
    # проверка при выполнении, а не только в плане, останавливает запись.
    asset_id = processed(stocker_root)
    passed = [json.loads(m) for s, st, m in events(stocker_root, asset_id) if (s, st) == ("AI", "PASSED")][-1]
    from app.database.db import add_event

    add_event(asset_id, "AI", "PASSED", json.dumps(passed))
    original_plan = reprocess.plan

    def plan_then_vision_goes_stale(*args, **kwargs):
        result = original_plan(*args, **kwargs)
        make_legacy(stocker_root, asset_id, qc_legacy=False)
        return result

    monkeypatch.setattr(reprocess, "plan", plan_then_vision_goes_stale)
    metadata_before = [e for e in events(stocker_root, asset_id) if e[0].startswith("METADATA")]
    result = run(asset_id, reprocess_from="metadata", dry_run=False)
    assert result["outcome"] == reprocess.STOPPED
    assert result["stopped"] == {"stage": "metadata", "code": "UPSTREAM_NOT_CURRENT", "upstream": "vision",
                                 "status": "stale", "reason": "NO_FINGERPRINT"}
    assert [e for e in events(stocker_root, asset_id) if e[0].startswith("METADATA")] == metadata_before
    assert not [e for e in events(stocker_root, asset_id) if e[0] == "REPROCESS"]


@pytest.mark.parametrize("start, through, chain", [
    ("qc", "qc", ["qc"]),
    ("qc", "vision", ["qc", "enhancement", "vision"]),
    ("vision", "metadata", ["vision", "metadata"]),
    ("normalize", "view", ["normalize", "view"]),
])
def test_through_follows_pipeline_order(stocker_root, start, through, chain):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id, qc_legacy=start != "vision")
    planned = reprocess.run(asset_id, reprocess_from=start, through=through)
    assert [s["stage"] for s in planned["steps"]] == chain


@pytest.mark.parametrize("start, through", [("vision", "qc"), ("metadata", "vision"), ("enhancement", "vision")])
def test_through_upstream_of_start_is_refused_without_writing(stocker_root, start, through):
    asset_id = processed(stocker_root)
    before = events(stocker_root, asset_id)
    result = run(asset_id, reprocess_from=start, through=through, dry_run=False)
    assert result["outcome"] == reprocess.REFUSED and result["refused"]["code"] == "INVALID_THROUGH"
    assert events(stocker_root, asset_id) == before


def test_noop_on_current_object_writes_no_event_from_any_start(stocker_root):
    asset_id = processed(stocker_root)
    before = events(stocker_root, asset_id)
    for start in ("normalize", "qc", "enhancement", "vision", "metadata"):
        assert run(asset_id, reprocess_from=start, dry_run=False)["outcome"] == reprocess.NOTHING_TO_DO
    assert events(stocker_root, asset_id) == before


# --- Сценарий #8: новый Vision evidence → human_review; старый Readiness — только история ---------


class BrandVision(FakeAnalyzer):
    """Новый Vision увидел бренд на оборудовании (как #8: «Вектор Технологий, P220»)."""

    def analyze(self, view):
        self.calls += 1
        return VISION.model_copy(update={"brands": ["Acme"]})


def test_case_8_stale_readiness_is_history_not_current(stocker_root, monkeypatch):
    import dataclasses

    from app import readiness as rd

    monkeypatch.setattr(rd, "PROFILES", {n: dataclasses.replace(p, min_mp=0.001) for n, p in rd.PROFILES.items()})
    asset_id = processed(stocker_root)
    ready = dispatch("readiness.evaluate", {"asset_id": asset_id})["data"]["readiness"]
    assert ready["ready_for"] == ["adobe", "shutterstock"]
    assert asset_state.get(asset_id)["state"] == asset_state.PLATFORM_READY

    # Vision пересчитан (устарел → reprocess) и увидел бренд: metadata current + human_review.
    make_legacy(stocker_root, asset_id, qc_legacy=False)
    result = run(asset_id, reprocess_from="vision", dry_run=False, analyzer=BrandVision())
    assert [e["stage"] for e in result["executed"] if e["outcome"] != "SKIPPED_CURRENT"][:2] == ["vision", "metadata"]

    before = events(stocker_root, asset_id)
    # История: старый READINESS/EVALUATED на месте, не изменён.
    readiness_events = [(st, json.loads(m)) for s, st, m in before if s == "READINESS"]
    assert [st for st, _ in readiness_events] == ["EVALUATED"]
    assert readiness_events[0][1]["ready_for"] == ["adobe", "shutterstock"]

    state = asset_state.get(asset_id)
    assert state["state"] == asset_state.HUMAN_REVIEW and state["stages"]["metadata"]["status"] == "current"
    assert "TRADEMARK" in state["reasons"] and state["ready_for"] == []
    assert state["stages"]["readiness"]["status"] == "not_applicable"

    # readiness.get: текущая готовность пуста; старый результат — только как история.
    data = dispatch("readiness.get", {"asset_id": asset_id}, actor="agent:openclaw")["data"]
    assert data["stale"] is True and data["ready_for"] == [] and data["result"] is None
    assert set(data["platforms"].values()) == {"stale"}
    assert data["last_result"]["current"] is False
    assert data["last_result"]["ready_for"] == ["adobe", "shutterstock"]  # исторический, явно помечен

    # Остальные потребители согласованы.
    view = dispatch("asset.get", {"asset_id": asset_id})["data"]
    assert view["state"]["state"] == "human_review" and view["state"]["ready_for"] == []
    assert view["pipeline"]["stock_readiness"]["ready_for"] == [] and view["pipeline"]["stock_readiness"]["stale"]
    assert view["pipeline"]["metadata_approved"] is False
    assert asset_id not in [i["id"] for i in dispatch("asset.list", {"state": "platform_ready"})["data"]["items"]]
    queue = dispatch("review.queue", {})["data"]
    assert asset_id in [i["id"] for i in queue["items"]] and queue["summary"]["platform_ready"] == 0
    # Переоценка сейчас невозможна (metadata не одобрена) — и события не пишет.
    assert dispatch("readiness.evaluate", {"asset_id": asset_id})["outcome"] == "NOT_EVALUATED"

    assert events(stocker_root, asset_id) == before  # все проверки выше — только чтение


def test_current_readiness_result_is_returned_as_result(stocker_root, monkeypatch):
    import dataclasses

    from app import readiness as rd

    monkeypatch.setattr(rd, "PROFILES", {n: dataclasses.replace(p, min_mp=0.001) for n, p in rd.PROFILES.items()})
    asset_id = processed(stocker_root)
    dispatch("readiness.evaluate", {"asset_id": asset_id})
    data = dispatch("readiness.get", {"asset_id": asset_id})["data"]
    assert data["stale"] is False and data["result"]["ready_for"] == data["ready_for"] == ["adobe", "shutterstock"]
    assert "last_result" not in data


# --- Этап Readiness: стадия после metadata, зависимость от metadata ---------------------------


@pytest.fixture
def small_images_allowed(monkeypatch):
    import dataclasses

    from app import readiness as rd

    monkeypatch.setattr(rd, "PROFILES", {n: dataclasses.replace(p, min_mp=0.001) for n, p in rd.PROFILES.items()})


def readiness_events(root, asset_id):
    return [st for s, st, _ in events(root, asset_id) if s == "READINESS"]


def test_worker_evaluates_readiness_after_gate(stocker_root, small_images_allowed):
    asset_id = processed(stocker_root)
    assert readiness_events(stocker_root, asset_id) == ["EVALUATED"]
    state = asset_state.get(asset_id)
    assert state["state"] == asset_state.PLATFORM_READY and state["ready_for"] == ["adobe", "shutterstock"]


def test_worker_does_not_evaluate_readiness_for_human_review(stocker_root):
    asset_id = worker.process_file(make_image(stocker_root, icc_profile=SRGB_ICC), analyzer=BrandVision())
    assert asset_state.get(asset_id)["state"] == asset_state.HUMAN_REVIEW
    assert readiness_events(stocker_root, asset_id) == []


def test_worker_readiness_guard_skips_on_stale_upstream(stocker_root):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id, qc_legacy=False)  # Vision устарел
    before = events(stocker_root, asset_id)
    assert worker.run_readiness(asset_id) is None
    assert events(stocker_root, asset_id) == before


def test_metadata_edit_makes_readiness_stale_and_reprocess_reevaluates_only_it(stocker_root, small_images_allowed):
    asset_id = processed(stocker_root)
    dispatch("metadata.edit", {"asset_id": asset_id, "add_keywords": ["factory floor"]})
    state = asset_state.get(asset_id)
    assert state["reprocess_from"] == "readiness" and state["stages"]["readiness"]["reason"] == "FINGERPRINT_CHANGED"

    planned = reprocess.run(asset_id)
    assert stages(planned) == [("readiness", "run")]
    before = events(stocker_root, asset_id)
    result = run(asset_id, dry_run=False)
    assert executed(result) == [("readiness", "EVALUATED")]
    added = [(s, st) for s, st, _ in events(stocker_root, asset_id)[len(before):]]
    assert added == [("READINESS", "EVALUATED"), ("REPROCESS", "DONE")]
    assert asset_state.get(asset_id)["state"] == asset_state.PLATFORM_READY
    assert run(asset_id, reprocess_from="readiness", dry_run=False)["outcome"] == reprocess.NOTHING_TO_DO
    assert len(events(stocker_root, asset_id)) == len(before) + 2


def test_readiness_is_skipped_when_metadata_goes_to_human_review(stocker_root, small_images_allowed):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id, qc_legacy=False)
    planned = reprocess.run(asset_id, reprocess_from="vision")
    assert stages(planned)[-1] == ("readiness", "run")  # план: если metadata останется одобренной

    result = run(asset_id, reprocess_from="vision", dry_run=False, analyzer=BrandVision())
    assert executed(result)[-1] == ("readiness", reprocess.NOT_APPLICABLE)  # metadata ушла человеку
    assert readiness_events(stocker_root, asset_id) == ["EVALUATED"]  # только старая, история
    state = asset_state.get(asset_id)
    assert state["state"] == asset_state.HUMAN_REVIEW and state["stages"]["readiness"]["status"] == "not_applicable"


def test_current_blocked_readiness_is_a_final_decision_not_stale(stocker_root):
    asset_id = processed(stocker_root)  # 64×48 < 4 MP площадок
    state = asset_state.get(asset_id)
    assert state["state"] == asset_state.BLOCKED and state["reasons"][:2] == ["READINESS_BLOCKED", "RESOLUTION_TOO_LOW"]
    assert state["stages"]["readiness"]["status"] == "current"
    before = events(stocker_root, asset_id)
    assert run(asset_id, reprocess_from="readiness", dry_run=False)["outcome"] == reprocess.NOTHING_TO_DO
    assert events(stocker_root, asset_id) == before


def test_not_applicable_skip_alone_writes_no_event(stocker_root):
    asset_id = worker.process_file(make_image(stocker_root, icc_profile=SRGB_ICC), analyzer=BrandVision())
    before = events(stocker_root, asset_id)
    result = run(asset_id, reprocess_from="readiness", dry_run=False)
    assert executed(result) == [("readiness", reprocess.NOT_APPLICABLE)] and result["outcome"] == reprocess.NOTHING_TO_DO
    assert events(stocker_root, asset_id) == before
