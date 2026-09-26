import json

from app.service import dispatch
from app.service.mcp_server import tools

from tests.conftest import events
from tests.test_service import _vision_asset

N8N = "workflow:n8n"


def _record(asset_ids, key="k1", kind="retry_exhausted", actor=N8N, **extra):
    params = {
        "channel": "test",
        "kind": kind,
        "severity": "warning",
        "title": "Stocker: повторы исчерпаны",
        "items": [{"asset_id": asset_id, "key": key} for asset_id in asset_ids],
        **extra,
    }
    return dispatch("notification.record", params, actor=actor)


def _notify_events(root, asset_id):
    return [json.loads(m) for s, st, m in events(root, asset_id) if (s, st) == ("NOTIFY", "SENT")]


def test_record_writes_notify_sent_event_per_asset(stocker_root):
    a = _vision_asset(stocker_root, name="a.jpg", seed=1)
    b = _vision_asset(stocker_root, name="b.jpg", seed=2)

    envelope = _record([a, b])

    assert envelope["ok"] and envelope["outcome"] == "RECORDED"
    assert [r["asset_id"] for r in envelope["data"]["recorded"]] == [a, b]
    (event,) = _notify_events(stocker_root, a)
    assert event == {
        "channel": "test",
        "kind": "retry_exhausted",
        "severity": "warning",
        "title": "Stocker: повторы исчерпаны",
        "key": "k1",
        "actor": N8N,
    }


def test_record_is_idempotent_per_asset_kind_key(stocker_root):
    a = _vision_asset(stocker_root)
    _record([a])

    again = _record([a])
    other_key = _record([a], key="k2")
    other_kind = _record([a], kind="daily_digest")

    assert again["outcome"] == "ALREADY_SENT" and again["data"]["already_sent"] == [{"asset_id": a, "key": "k1"}]
    assert other_key["outcome"] == "RECORDED"
    assert other_kind["outcome"] == "RECORDED"
    assert len(_notify_events(stocker_root, a)) == 3


def test_record_rejects_unknown_asset_without_writing(stocker_root):
    a = _vision_asset(stocker_root)

    envelope = _record([a, 999])

    assert envelope["error"]["code"] == "ASSET_NOT_FOUND"
    assert _notify_events(stocker_root, a) == []


def test_record_validates_params(stocker_root):
    a = _vision_asset(stocker_root)

    assert _record([a], kind="Bad Kind!")["error"]["code"] == "INVALID_PARAMS"
    assert dispatch("notification.record", {"channel": "test", "kind": "x", "title": "t", "items": []}, actor=N8N)["error"]["code"] == "INVALID_PARAMS"
    assert _record([a], severity="panic")["error"]["code"] == "INVALID_PARAMS"


def test_agent_cannot_record_and_does_not_see_tool(stocker_root):
    a = _vision_asset(stocker_root)

    assert _record([a], actor="agent:openclaw")["error"]["code"] == "FORBIDDEN"
    assert "notification_record" not in {tool.name for tool in tools()}


def test_notifications_visible_in_history(stocker_root):
    a = _vision_asset(stocker_root)
    _record([a], key="AI:3")

    history = dispatch("asset.history", {"asset_id": a, "stage": "NOTIFY"}, actor=N8N)["data"]

    assert [e["message"]["key"] for e in history] == ["AI:3"]


def test_notify_events_do_not_change_pipeline_state(stocker_root):
    a = _vision_asset(stocker_root)
    dispatch("metadata.build", {"asset_id": a})
    before = dispatch("asset.get", {"asset_id": a})["data"]["pipeline"]

    _record([a])

    assert dispatch("asset.get", {"asset_id": a})["data"]["pipeline"] == before
