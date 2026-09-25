import json
import sqlite3

import pytest

from app import metadata as service
from app.ai.schema import AIAnalysis
from app.database.db import add_event, get_asset, save_ai_result
from app.ingest import ingest_file

from tests.conftest import events, make_image
from tests.test_metadata_service import FakeMetadataAnalyzer, SUGGESTION, VISION, stored


def _asset(stocker_root, vision: AIAnalysis, name="photo.jpg", seed=0) -> int:
    asset_id = ingest_file(make_image(stocker_root, name=name, seed=seed))
    save_ai_result(asset_id, vision.model_dump_json())
    add_event(asset_id, "AI", "PASSED", json.dumps({"provider": "lmstudio", "model": "v", "prompt_version": "local-v2"}))
    return asset_id


def gate_events(root, asset_id):
    return [json.loads(m) for s, st, m in events(root, asset_id) if (s, st) == ("METADATA", "GATED")]


def test_low_risk_build_is_auto_approved(stocker_root):
    asset_id = _asset(stocker_root, VISION)

    result = service.build(asset_id, analyzer=FakeMetadataAnalyzer())

    assert result["metadata"]["state"] == "auto_approved"
    (gated,) = gate_events(stocker_root, asset_id)
    assert gated == {
        "policy_version": "gate-v1.1",
        "decision": "auto_approved",
        "reasons": [],
        "notes": [],
        "state_before": None,
        "trigger": "build",
    }


def test_company_name_in_text_goes_to_human_review(stocker_root):
    asset_id = _asset(stocker_root, VISION.model_copy(update={"text_visible": ['АО "ШПЗ"', "0411Е.06.05.090"]}))

    service.build(asset_id, analyzer=FakeMetadataAnalyzer())

    metadata = stored(asset_id)
    assert metadata["state"] == "human_review"
    assert gate_events(stocker_root, asset_id)[-1]["reasons"] == ["TEXT_BRAND_OR_LEGAL"]
    assert gate_events(stocker_root, asset_id)[-1]["notes"] == ["TEXT_TECHNICAL"]


def test_edit_is_regated_and_records_both_events(stocker_root):
    asset_id = _asset(stocker_root, VISION)
    service.build(asset_id, analyzer=FakeMetadataAnalyzer(result=SUGGESTION.model_copy(update={"description": "Shaft in Berlin."})))
    assert stored(asset_id)["state"] == "human_review"

    result = service.edit(asset_id, {"description": "Concrete elevator shaft with rails."})

    assert result["metadata"]["state"] == "auto_approved"
    stages = [(s, st) for s, st, _ in events(stocker_root, asset_id)][-2:]
    assert stages == [("METADATA", "EDITED"), ("METADATA", "GATED")]
    last = gate_events(stocker_root, asset_id)[-1]
    assert last["trigger"] == "edit" and last["state_before"] == "human_review"


def test_edit_adding_legal_claim_moves_auto_approved_to_review(stocker_root):
    asset_id = _asset(stocker_root, VISION)
    service.build(asset_id, analyzer=FakeMetadataAnalyzer())

    service.edit(asset_id, {"description": "ISO 9001 certified elevator shaft."})

    assert stored(asset_id)["state"] == "human_review"
    assert "LEGAL_CLAIM" in gate_events(stocker_root, asset_id)[-1]["reasons"]


def test_gate_command_migrates_v1_draft(stocker_root):
    asset_id = _asset(stocker_root, VISION)
    service.build(asset_id, analyzer=FakeMetadataAnalyzer())
    v1 = stored(asset_id)
    v1.pop("review_gate")
    v1["state"], v1["metadata_version"] = "draft", "1"
    with sqlite3.connect(stocker_root / "data" / "db" / "stocker.db") as connection:
        connection.execute("UPDATE assets SET metadata_json = ? WHERE id = ?", (json.dumps(v1), asset_id))

    result = service.gate(asset_id)

    assert result["outcome"] == service.GATED
    assert stored(asset_id)["metadata_version"] == "2"
    assert stored(asset_id)["state"] == "auto_approved"
    assert gate_events(stocker_root, asset_id)[-1]["trigger"] == "gate"


def test_gate_never_changes_human_decisions(stocker_root):
    asset_id = _asset(stocker_root, VISION)
    service.build(asset_id, analyzer=FakeMetadataAnalyzer())
    service.approve(asset_id)
    events_before = len(events(stocker_root, asset_id))

    with pytest.raises(service.MetadataError) as info:
        service.gate(asset_id)

    assert info.value.code == "INVALID_TRANSITION"
    assert stored(asset_id)["state"] == "approved"
    assert len(events(stocker_root, asset_id)) == events_before


def test_escalate_is_sticky_until_human_decision(stocker_root):
    asset_id = _asset(stocker_root, VISION)
    service.build(asset_id, analyzer=FakeMetadataAnalyzer())

    result = service.escalate(asset_id, "Possible staged scene")

    assert result["metadata"]["state"] == "human_review"
    stages = [(s, st) for s, st, _ in events(stocker_root, asset_id)][-2:]
    assert stages == [("METADATA", "ESCALATED"), ("METADATA", "GATED")]

    service.gate(asset_id)
    service.edit(asset_id, {"title": "Another elevator shaft title"})
    assert stored(asset_id)["state"] == "human_review"

    service.approve(asset_id)
    assert stored(asset_id)["state"] == "approved"
    assert stored(asset_id)["review_gate"]["escalation"] is None


def test_auto_approve_kill_switch(stocker_root, monkeypatch):
    monkeypatch.setenv("STOCKER_AUTO_APPROVE", "0")
    asset_id = _asset(stocker_root, VISION)

    service.build(asset_id, analyzer=FakeMetadataAnalyzer())

    assert stored(asset_id)["state"] == "human_review"
    assert gate_events(stocker_root, asset_id)[-1]["reasons"] == ["AUTO_APPROVE_DISABLED"]


def test_partial_draft_is_deferred_and_upgraded_later(stocker_root):
    asset_id = _asset(stocker_root, VISION)

    service.build(asset_id, analyzer=FakeMetadataAnalyzer(error=ConnectionError("down")))
    assert stored(asset_id)["state"] == "draft"
    assert gate_events(stocker_root, asset_id)[-1]["decision"] == "deferred"

    service.build(asset_id, analyzer=FakeMetadataAnalyzer())
    assert stored(asset_id)["state"] == "auto_approved"
    assert gate_events(stocker_root, asset_id)[-1]["state_before"] == "draft"


def test_human_can_reject_auto_approved(stocker_root):
    asset_id = _asset(stocker_root, VISION)
    service.build(asset_id, analyzer=FakeMetadataAnalyzer())

    service.reject(asset_id, "Not interesting")

    assert stored(asset_id)["state"] == "rejected"
    rejected = json.loads([m for s, st, m in events(stocker_root, asset_id) if st == "REJECTED"][0])
    assert rejected["state_before"] == "auto_approved"


def test_cli_gate_and_escalate(stocker_root, capsys):
    asset_id = _asset(stocker_root, VISION)
    service.build(asset_id, analyzer=FakeMetadataAnalyzer())
    capsys.readouterr()

    assert service.main(["escalate", str(asset_id), "--reason", "check"]) == 0
    assert "Gate: human_review" in capsys.readouterr().out
    assert service.main(["gate", str(asset_id)]) == 0

    service.approve(asset_id)
    assert service.main(["gate", str(asset_id)]) == 1


def test_assets_status_and_vision_untouched(stocker_root):
    asset_id = _asset(stocker_root, VISION)
    before = get_asset(asset_id)

    service.build(asset_id, analyzer=FakeMetadataAnalyzer())
    service.escalate(asset_id, "x")
    service.approve(asset_id)

    after = get_asset(asset_id)
    assert (after["status"], after["ai_result"]) == (before["status"], before["ai_result"])
