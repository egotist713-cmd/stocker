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
    assert stages(result) == [("qc", "run"), ("enhancement", "skip"), ("vision", "run"), ("metadata", "run")]
    steps = {s["stage"]: s for s in result["steps"]}
    assert steps["qc"]["why"] == "NO_FINGERPRINT" and steps["vision"]["why"] == "NO_FINGERPRINT"
    assert steps["metadata"]["why"] == "UPSTREAM_STALE:vision"
    assert "assets.ai_result" in steps["vision"]["writes"] and steps["enhancement"]["writes"] == []
    assert {n["stage"] for n in result["not_run"]} == {"readiness", "creative_review", "publication"}
    assert events(stocker_root, asset_id) == before


def test_metadata_is_planned_when_vision_will_rerun(stocker_root, monkeypatch):
    # Собственный вход metadata не менялся, но Vision устарел — metadata в плане по наследованию.
    asset_id = processed(stocker_root)
    monkeypatch.setitem(asset_state.VISION_IDENTITIES, "fake", lambda: {**FakeAnalyzer.current_identity(), "model": "v2"})
    result = reprocess.run(asset_id, reprocess_from="vision")
    assert stages(result) == [("vision", "run"), ("metadata", "run")]
    assert result["steps"][1]["why"] == "UPSTREAM_STALE:vision"
    assert result["steps"][0]["why"] == "FINGERPRINT_CHANGED"


# --- Выполнение ------------------------------------------------------------------------------


def test_reprocess_from_vision_runs_vision_then_metadata(stocker_root):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id, qc_legacy=False)  # QC актуален, Vision — наследие
    qc_before = [e for e in events(stocker_root, asset_id) if e[0] in ("QC", "NORMALIZE", "ENHANCEMENT")]

    result = run(asset_id, reprocess_from="vision", dry_run=False)

    assert result["outcome"] == reprocess.REPROCESSED
    assert executed(result) == [("vision", "AI_PASSED"), ("metadata", "DRAFTED")]
    state = asset_state.get(asset_id)
    assert state["stages"]["vision"]["status"] == "current" and state["stages"]["metadata"]["status"] == "current"
    # Upstream не тронут: ни одного нового события QC / NORMALIZE / ENHANCEMENT.
    assert [e for e in events(stocker_root, asset_id) if e[0] in ("QC", "NORMALIZE", "ENHANCEMENT")] == qc_before


def test_reprocess_from_qc_runs_the_chain(stocker_root):
    asset_id = processed(stocker_root)
    make_legacy(stocker_root, asset_id)

    result = run(asset_id, dry_run=False)

    assert executed(result) == [("qc", "QC_PASSED"), ("enhancement", "SKIPPED_CURRENT"),
                                ("vision", "AI_PASSED"), ("metadata", "DRAFTED")]
    state = asset_state.get(asset_id)
    assert all(state["stages"][s]["status"] == "current" for s in ("normalize", "view", "qc", "enhancement", "vision", "metadata"))
    assert state["state"] == asset_state.METADATA_APPROVED  # Readiness ещё не оценён — reprocess его не запускает
    done = [json.loads(m) for s, st, m in events(stocker_root, asset_id) if (s, st) == ("REPROCESS", "DONE")]
    assert len(done) == 1 and done[0]["state_before"] == "stale" and done[0]["state_after"] == "metadata_approved"


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
    result = dispatch("asset.reprocess", {"asset_id": asset_id, "reprocess_from": "readiness"})
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
