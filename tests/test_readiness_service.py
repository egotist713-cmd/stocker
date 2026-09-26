import dataclasses
import json
import sqlite3

import pytest

from app import metadata as metadata_service
from app import qc
from app import readiness as rd
from app.database.db import get_asset
from app.service import dispatch
from app.service.mcp_server import tools

from tests.conftest import events, make_image
from tests.test_metadata_service import FakeMetadataAnalyzer
from tests.test_service import _vision_asset

AGENT = "agent:openclaw"
N8N = "workflow:n8n"


@pytest.fixture
def small_images_allowed(monkeypatch):
    """Тестовые изображения 64×48: минимум мегапикселей площадок снижен только в тесте."""
    profiles = {name: dataclasses.replace(profile, min_mp=0.001) for name, profile in rd.PROFILES.items()}
    monkeypatch.setattr(rd, "PROFILES", profiles)


def _approved_asset(root, name="photo.jpg", seed=0) -> int:
    """Asset с QC, Vision и metadata после gate (auto_approved)."""
    asset_id = _vision_asset(root, name=name, seed=seed)
    qc.save_qc_result(asset_id, qc.check_asset(get_asset(asset_id)))
    metadata_service.build(asset_id, analyzer=FakeMetadataAnalyzer())
    return asset_id


def _readiness_events(root, asset_id):
    return [(st, json.loads(m)) for s, st, m in events(root, asset_id) if s == "READINESS"]


def _row(root, asset_id):
    with sqlite3.connect(root / "data" / "db" / "stocker.db") as connection:
        return connection.execute("SELECT status, metadata_json FROM assets WHERE id = ?", (asset_id,)).fetchone()


# --- оценка и событие ------------------------------------------------------------


def test_evaluate_writes_event_and_is_ready(stocker_root, small_images_allowed):
    asset_id = _approved_asset(stocker_root)

    envelope = dispatch("readiness.evaluate", {"asset_id": asset_id}, actor=N8N)

    assert envelope["ok"] and envelope["outcome"] == "EVALUATED"
    readiness = envelope["data"]["readiness"]
    assert readiness["ready_for"] == ["adobe", "shutterstock"]
    assert readiness["platforms"]["adobe"]["export_plan"]["category"] == "Industry"

    ((status, message),) = _readiness_events(stocker_root, asset_id)
    assert status == "EVALUATED"
    assert message["actor"] == N8N
    assert message["fingerprint"] == readiness["fingerprint"]


def test_repeat_with_same_inputs_is_unchanged(stocker_root, small_images_allowed):
    asset_id = _approved_asset(stocker_root)
    dispatch("readiness.evaluate", {"asset_id": asset_id})

    envelope = dispatch("readiness.evaluate", {"asset_id": asset_id})

    assert envelope["ok"] and envelope["outcome"] == "UNCHANGED"
    assert len(_readiness_events(stocker_root, asset_id)) == 1


def test_evaluate_does_not_change_metadata_or_status(stocker_root, small_images_allowed):
    asset_id = _approved_asset(stocker_root)
    before = _row(stocker_root, asset_id)

    dispatch("readiness.evaluate", {"asset_id": asset_id})

    assert _row(stocker_root, asset_id) == before


def test_real_profile_blocks_small_image(stocker_root):
    asset_id = _approved_asset(stocker_root)

    readiness = dispatch("readiness.evaluate", {"asset_id": asset_id})["data"]["readiness"]

    assert readiness["platforms"]["adobe"]["status"] == "blocked"
    assert "RESOLUTION_TOO_LOW" in [c["code"] for c in readiness["platforms"]["adobe"]["checks"]]
    assert readiness["ready_for"] == []


def test_metadata_not_ready_is_not_evaluated_and_writes_no_event(stocker_root):
    asset_id = _vision_asset(stocker_root)  # metadata ещё нет

    envelope = dispatch("readiness.evaluate", {"asset_id": asset_id})

    assert envelope["ok"] and envelope["outcome"] == "NOT_EVALUATED"
    assert envelope["data"]["readiness"]["platforms"]["adobe"]["status"] == "not_evaluated"
    assert _readiness_events(stocker_root, asset_id) == []


def test_changed_source_blocks(stocker_root, small_images_allowed):
    asset_id = _approved_asset(stocker_root)
    make_image(stocker_root, seed=99)  # тот же путь, другое содержимое

    readiness = dispatch("readiness.evaluate", {"asset_id": asset_id})["data"]["readiness"]

    assert "SOURCE_NOT_OK" in [c["code"] for c in readiness["platforms"]["adobe"]["checks"]]


def test_unreadable_file_records_failed_event(stocker_root, small_images_allowed):
    asset_id = _approved_asset(stocker_root)
    (stocker_root / "data" / "incoming" / "photo.jpg").write_bytes(b"not an image")

    envelope = dispatch("readiness.evaluate", {"asset_id": asset_id})

    assert envelope["ok"] is False and envelope["outcome"] == "READINESS_FAILED"
    assert envelope["data"]["error"]["error_type"]
    ((status, message),) = _readiness_events(stocker_root, asset_id)
    assert status == "FAILED" and message["error_type"]


def test_unknown_asset(stocker_root):
    for operation in ("readiness.evaluate", "readiness.get"):
        envelope = dispatch(operation, {"asset_id": 999})
        assert envelope["error"]["code"] == "ASSET_NOT_FOUND"


# --- чтение, представление, stale -------------------------------------------------


def test_get_and_asset_view(stocker_root, small_images_allowed):
    asset_id = _approved_asset(stocker_root)

    before = dispatch("readiness.get", {"asset_id": asset_id})["data"]
    assert before["evaluated"] is False and before["result"] is None
    view = dispatch("asset.get", {"asset_id": asset_id})["data"]
    assert {"operation": "readiness.evaluate", "access": "pipeline"} in view["allowed_actions"]

    dispatch("readiness.evaluate", {"asset_id": asset_id})

    data = dispatch("readiness.get", {"asset_id": asset_id}, actor=AGENT)["data"]
    assert data["evaluated"] and not data["stale"]
    assert data["platforms"] == {"adobe": "ready", "shutterstock": "ready"}
    assert data["result"]["platforms"]["shutterstock"]["export_plan"]["keywords"]

    view = dispatch("asset.get", {"asset_id": asset_id})["data"]
    assert view["pipeline"]["stock_readiness"]["ready_for"] == ["adobe", "shutterstock"]
    assert view["pipeline"]["stock_readiness"]["event_id"] == data["event_id"]
    assert {"operation": "readiness.evaluate", "access": "pipeline"} not in view["allowed_actions"]


def test_metadata_edit_makes_result_stale_and_reevaluation_records_new_event(stocker_root, small_images_allowed):
    asset_id = _approved_asset(stocker_root)
    dispatch("readiness.evaluate", {"asset_id": asset_id})

    dispatch("metadata.edit", {"asset_id": asset_id, "title": "Elevator shaft with steel guide rails"})

    stale = dispatch("readiness.get", {"asset_id": asset_id})["data"]
    assert stale["stale"] is True
    assert stale["platforms"] == {"adobe": "stale", "shutterstock": "stale"}
    assert stale["ready_for"] == []

    envelope = dispatch("readiness.evaluate", {"asset_id": asset_id})
    assert envelope["outcome"] == "EVALUATED"
    assert envelope["data"]["readiness"]["platforms"]["adobe"]["export_plan"]["title"] == "Elevator shaft with steel guide rails"
    assert len(_readiness_events(stocker_root, asset_id)) == 2
    assert dispatch("readiness.get", {"asset_id": asset_id})["data"]["stale"] is False


# --- права и транспорт ---------------------------------------------------------------


@pytest.mark.parametrize("actor", ["human", AGENT, N8N])
def test_all_actors_may_evaluate_and_read(stocker_root, actor):
    asset_id = _vision_asset(stocker_root)
    assert dispatch("readiness.evaluate", {"asset_id": asset_id}, actor=actor)["ok"]
    assert dispatch("readiness.get", {"asset_id": asset_id}, actor=actor)["ok"]


def test_tools_are_exposed_to_agent():
    names = {tool.name for tool in tools()}
    assert {"readiness_evaluate", "readiness_get"} <= names
