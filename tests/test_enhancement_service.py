import json

import pytest

from app import enhancement_decision, normalization, worker
from app.ingest import ingest_file
from app.service import dispatch
from app.service.mcp_server import tools

from tests.conftest import FakeAnalyzer, events, make_image

AGENT = "agent:openclaw"
N8N = "workflow:n8n"


def _enhancement_events(root, asset_id):
    return [(st, json.loads(m)) for s, st, m in events(root, asset_id) if s == "ENHANCEMENT"]


@pytest.fixture
def asset_id(stocker_root) -> int:
    asset_id = ingest_file(make_image(stocker_root))
    normalization.run_asset(asset_id)  # пиксели — только через AnalysisView (NORMALIZE/PASSED)
    return asset_id


def test_assess_writes_event_with_actor(stocker_root, asset_id):
    envelope = dispatch("enhancement.assess", {"asset_id": asset_id}, actor=N8N)

    assert envelope["ok"] and envelope["outcome"] == "ASSESSED"
    assessment = envelope["data"]["assessment"]
    assert assessment["provider"] == "rules" and assessment["rules_version"] == "enhancement-rules-v2"
    assert envelope["data"]["decision"] == assessment["decision"]

    ((status, message),) = _enhancement_events(stocker_root, asset_id)
    assert status == "ASSESSED" and message["actor"] == N8N
    assert message["fingerprint"] == assessment["fingerprint"]


def test_repeat_is_unchanged_without_event(stocker_root, asset_id):
    dispatch("enhancement.assess", {"asset_id": asset_id})

    envelope = dispatch("enhancement.assess", {"asset_id": asset_id})

    assert envelope["outcome"] == "UNCHANGED"
    assert len(_enhancement_events(stocker_root, asset_id)) == 1


def test_get_and_asset_view(stocker_root, asset_id):
    assert dispatch("enhancement.get", {"asset_id": asset_id})["data"]["assessed"] is False

    dispatch("enhancement.assess", {"asset_id": asset_id})

    data = dispatch("enhancement.get", {"asset_id": asset_id}, actor=AGENT)["data"]
    assert data["assessed"] and not data["stale"]
    assert data["result"]["metrics"]["megapixels"] == 0.0
    view = dispatch("asset.get", {"asset_id": asset_id})["data"]
    assert view["pipeline"]["enhancement"]["event_id"] == data["event_id"]


@pytest.mark.parametrize("damage", ["delete", "replace"])
def test_missing_or_changed_source_records_failed(stocker_root, asset_id, damage):
    path = stocker_root / "data" / "incoming" / "photo.jpg"
    if damage == "delete":
        path.unlink()
    else:
        make_image(stocker_root, seed=42)

    envelope = dispatch("enhancement.assess", {"asset_id": asset_id})

    assert envelope["ok"] is False and envelope["outcome"] == "ENHANCEMENT_FAILED"
    ((status, message),) = _enhancement_events(stocker_root, asset_id)
    assert status == "FAILED" and message["error_type"] == "REPRESENTATION_INVALID"


def test_repeated_failure_writes_one_event(stocker_root, asset_id):
    (stocker_root / "data" / "incoming" / "photo.jpg").unlink()
    for _ in range(3):
        dispatch("enhancement.assess", {"asset_id": asset_id})
    assert [st for st, _ in _enhancement_events(stocker_root, asset_id)] == ["FAILED"]


def test_assess_requires_normalization(stocker_root):
    # Без NORMALIZE/PASSED view нет — и обратного пути «открыть source как есть» тоже нет.
    asset_id = ingest_file(make_image(stocker_root))
    envelope = dispatch("enhancement.assess", {"asset_id": asset_id})
    assert envelope["ok"] is False and envelope["data"]["error"]["error_type"] == "NORMALIZE_NOT_PASSED"


def test_assessment_records_the_view(stocker_root, asset_id):
    view = dispatch("enhancement.assess", {"asset_id": asset_id})["data"]["assessment"]["view"]
    assert view["view_version"] == "analysis-view-v1" and view["color_space"] == "undeclared" and view["size"] == [64, 48]


def test_unknown_asset(stocker_root):
    for operation in ("enhancement.assess", "enhancement.get"):
        assert dispatch(operation, {"asset_id": 999})["error"]["code"] == "ASSET_NOT_FOUND"


@pytest.mark.parametrize("actor", ["human", AGENT, N8N])
def test_all_actors_may_assess_and_read(stocker_root, asset_id, actor):
    assert dispatch("enhancement.assess", {"asset_id": asset_id}, actor=actor)["ok"]
    assert dispatch("enhancement.get", {"asset_id": asset_id}, actor=actor)["ok"]


def test_tools_are_exposed_to_agent():
    assert {"enhancement_assess", "enhancement_get"} <= {tool.name for tool in tools()}


def test_worker_continues_when_assessment_crashes(stocker_root, monkeypatch):
    def broken(asset_id, view=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(enhancement_decision, "assess_asset", broken)

    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())

    stages = [(s, st) for s, st, _ in events(stocker_root, asset_id)]
    assert ("AI", "PASSED") in stages
    assert not [s for s in stages if s[0] == "ENHANCEMENT"]
