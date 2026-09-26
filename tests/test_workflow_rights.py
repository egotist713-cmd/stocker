import shutil

import pytest

from app.service import dispatch

from tests.conftest import FakeAnalyzer, make_image
from tests.test_service import _vision_asset

N8N = "workflow:n8n"


@pytest.fixture
def fake_vision(monkeypatch):
    from app import worker

    monkeypatch.setattr(worker, "LocalAnalyzer", FakeAnalyzer)


@pytest.mark.parametrize(
    ("operation", "params"),
    [
        ("metadata.edit", {"title": "x"}),
        ("metadata.rebuild", {}),
        ("metadata.escalate", {"reason": "x"}),
        ("metadata.approve", {}),
        ("metadata.reject", {"reason": "x"}),
        ("metadata.build", {"force": True}),
        ("asset.process", {"force": True}),
    ],
)
def test_workflow_forbidden_operations(stocker_root, operation, params):
    asset_id = _vision_asset(stocker_root)
    dispatch("metadata.build", {"asset_id": asset_id})

    envelope = dispatch(operation, {"asset_id": asset_id, **params}, actor=N8N)

    assert envelope["error"]["code"] == "FORBIDDEN"


def test_workflow_allowed_operations(stocker_root, fake_vision):
    make_image(stocker_root, name="new.jpg", seed=9)
    asset_id = _vision_asset(stocker_root)

    assert dispatch("incoming.list", {}, actor=N8N)["ok"]
    assert dispatch("review.queue", {}, actor=N8N)["ok"]
    assert dispatch("asset.list", {"ready": True}, actor=N8N)["ok"]
    assert dispatch("metadata.build", {"asset_id": asset_id}, actor=N8N)["ok"]
    assert dispatch("metadata.gate", {"asset_id": asset_id}, actor=N8N)["ok"]
    assert dispatch("asset.get", {"asset_id": asset_id}, actor=N8N)["ok"]
    processed = dispatch("asset.process_file", {"path": "data/incoming/new.jpg"}, actor=N8N)
    assert processed["ok"] and processed["data"]["pipeline"]["metadata"] == "auto_approved"
    assert dispatch("asset.process", {"asset_id": processed["asset_id"]}, actor=N8N)["ok"]


def test_agent_keeps_its_wider_rights(stocker_root):
    asset_id = _vision_asset(stocker_root)
    dispatch("metadata.build", {"asset_id": asset_id})

    assert dispatch("metadata.edit", {"asset_id": asset_id, "title": "Agent"}, actor="agent:openclaw")["ok"]


def test_incoming_list_reports_new_files_and_duplicates(stocker_root):
    registered = make_image(stocker_root, name="registered.jpg", seed=1)
    asset_id = _vision_asset(stocker_root, name="other.jpg", seed=2)
    from app.ingest import ingest_file

    registered_id = ingest_file(registered)
    make_image(stocker_root, name="fresh.jpg", seed=3)
    shutil.copy(registered, stocker_root / "data" / "incoming" / "copy_of_registered.jpg")
    (stocker_root / "data" / "incoming" / "notes.txt").write_text("not an image")

    data = dispatch("incoming.list", {}, actor=N8N)["data"]

    by_name = {item["filename"]: item for item in data["items"]}
    assert set(by_name) == {"fresh.jpg", "copy_of_registered.jpg"}
    assert by_name["fresh.jpg"]["duplicate_of"] is None
    assert by_name["fresh.jpg"]["path"] == "data/incoming/fresh.jpg"
    assert by_name["copy_of_registered.jpg"]["duplicate_of"] == registered_id
    assert asset_id not in [item.get("duplicate_of") for item in data["items"]]
