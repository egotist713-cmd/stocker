import json
import sqlite3

import pytest

from app import metadata as metadata_service
from app.database.db import add_event, save_ai_result
from app.ingest import ingest_file
from app.service import dispatch

from tests.conftest import FakeAnalyzer, OfflineMetadataAnalyzer, events, make_image
from tests.test_metadata_service import VISION

AGENT = "agent:openclaw"


@pytest.fixture
def fake_vision(monkeypatch):
    from app import worker

    monkeypatch.setattr(worker, "LocalAnalyzer", FakeAnalyzer)


def _vision_asset(stocker_root, name="photo.jpg", seed=0, vision=VISION) -> int:
    asset_id = ingest_file(make_image(stocker_root, name=name, seed=seed))
    save_ai_result(asset_id, vision.model_dump_json())
    add_event(asset_id, "AI", "PASSED", json.dumps({"provider": "lmstudio", "model": "v", "prompt_version": "local-v2"}))
    return asset_id


def _messages(root, asset_id, stage=None):
    return [
        (s, st, json.loads(m) if m and m.startswith("{") else m)
        for s, st, m in events(root, asset_id)
        if stage is None or s == stage
    ]


# --- envelope и ошибки -----------------------------------------------------------------


def test_envelope_shape(stocker_root):
    asset_id = _vision_asset(stocker_root)

    envelope = dispatch("asset.get", {"asset_id": asset_id})

    assert set(envelope) == {"api_version", "operation", "ok", "asset_id", "outcome", "data", "error"}
    assert envelope["api_version"] == "1"
    assert envelope["ok"] is True and envelope["error"] is None
    assert envelope["data"]["id"] == asset_id


@pytest.mark.parametrize(
    ("operation", "params", "code"),
    [
        ("asset.nope", {}, "UNKNOWN_OPERATION"),
        ("asset.get", {}, "INVALID_PARAMS"),
        ("asset.get", {"asset_id": "x"}, "INVALID_PARAMS"),
        ("asset.get", {"asset_id": 1, "extra": True}, "INVALID_PARAMS"),
        ("metadata.edit", {"asset_id": 1}, "INVALID_PARAMS"),
        ("asset.get", {"asset_id": 999}, "ASSET_NOT_FOUND"),
        ("metadata.get", {"asset_id": 999}, "ASSET_NOT_FOUND"),
    ],
)
def test_errors(stocker_root, operation, params, code):
    envelope = dispatch(operation, params)

    assert envelope["ok"] is False
    assert envelope["error"]["code"] == code


def test_invalid_actor(stocker_root):
    assert dispatch("asset.list", {}, actor="root")["error"]["code"] == "INVALID_PARAMS"
    assert dispatch("asset.list", {}, actor="agent:")["error"]["code"] == "INVALID_PARAMS"


def test_metadata_errors_keep_their_codes(stocker_root):
    no_vision = ingest_file(make_image(stocker_root))

    assert dispatch("metadata.build", {"asset_id": no_vision})["error"]["code"] == "VISION_MISSING"
    assert dispatch("metadata.gate", {"asset_id": no_vision})["error"]["code"] == "VISION_MISSING"


def test_unexpected_exception_becomes_internal(stocker_root, monkeypatch):
    from app.service import views

    monkeypatch.setattr(views, "list_assets", lambda **_: 1 / 0)

    envelope = dispatch("asset.list", {})

    assert envelope["error"]["code"] == "INTERNAL"
    assert "ZeroDivisionError" in envelope["error"]["message"]


# --- права actor ----------------------------------------------------------------------------


@pytest.mark.parametrize("actor", [AGENT, "workflow:n8n"])
@pytest.mark.parametrize(
    ("operation", "extra"),
    [("metadata.approve", {}), ("metadata.reject", {"reason": "x"})],
)
def test_review_is_forbidden_for_non_humans(stocker_root, actor, operation, extra):
    asset_id = _vision_asset(stocker_root)
    dispatch("metadata.build", {"asset_id": asset_id}, actor=actor)
    state_before = dispatch("asset.get", {"asset_id": asset_id})["data"]["pipeline"]["metadata"]

    envelope = dispatch(operation, {"asset_id": asset_id, **extra}, actor=actor)

    assert envelope["error"]["code"] == "FORBIDDEN"
    assert dispatch("asset.get", {"asset_id": asset_id})["data"]["pipeline"]["metadata"] == state_before


def test_human_can_approve(stocker_root):
    asset_id = _vision_asset(stocker_root)
    dispatch("metadata.build", {"asset_id": asset_id})

    envelope = dispatch("metadata.approve", {"asset_id": asset_id})

    assert envelope["ok"] and envelope["outcome"] == "APPROVED"
    assert envelope["data"]["pipeline"]["metadata"] == "approved"


def test_auto_approved_only_through_gate(stocker_root):
    # Никакая операция не принимает состояние как параметр.
    asset_id = _vision_asset(stocker_root)
    dispatch("metadata.build", {"asset_id": asset_id}, actor=AGENT)

    for operation in ("metadata.gate", "metadata.edit", "metadata.build"):
        envelope = dispatch(operation, {"asset_id": asset_id, "state": "auto_approved"}, actor=AGENT)
        assert envelope["error"]["code"] == "INVALID_PARAMS"


def test_agent_can_escalate_but_not_approve(stocker_root):
    asset_id = _vision_asset(stocker_root)
    dispatch("metadata.build", {"asset_id": asset_id}, actor=AGENT)

    escalated = dispatch("metadata.escalate", {"asset_id": asset_id, "reason": "odd label"}, actor=AGENT)

    assert escalated["ok"] and escalated["data"]["pipeline"]["metadata"] == "human_review"
    assert dispatch("metadata.approve", {"asset_id": asset_id}, actor=AGENT)["error"]["code"] == "FORBIDDEN"


# --- actor в событиях --------------------------------------------------------------------------


def test_actor_recorded_in_events(stocker_root):
    asset_id = _vision_asset(stocker_root)

    dispatch("metadata.build", {"asset_id": asset_id}, actor=AGENT)
    dispatch("metadata.edit", {"asset_id": asset_id, "title": "Agent title"}, actor=AGENT)

    metadata_events = _messages(stocker_root, asset_id, "METADATA") + _messages(stocker_root, asset_id, "METADATA_AI")
    assert metadata_events
    assert {message["actor"] for _, _, message in metadata_events} == {AGENT}


def test_direct_cli_events_have_no_actor(stocker_root):
    asset_id = _vision_asset(stocker_root)

    metadata_service.build(asset_id, analyzer=OfflineMetadataAnalyzer())

    assert all("actor" not in message for _, _, message in _messages(stocker_root, asset_id, "METADATA"))


def test_actor_in_worker_events(stocker_root, fake_vision):
    make_image(stocker_root)

    envelope = dispatch("asset.process_file", {"path": "data/incoming/photo.jpg"}, actor="workflow:n8n")

    asset_id = envelope["asset_id"]
    by_stage = {(s, st): m for s, st, m in _messages(stocker_root, asset_id)}
    assert by_stage[("QC", "PASSED")]["actor"] == "workflow:n8n"
    assert by_stage[("AI", "PASSED")]["actor"] == "workflow:n8n"
    assert isinstance(by_stage[("INGEST", "DONE")], str)  # текстовое сообщение не меняется


# --- pipeline операции ----------------------------------------------------------------------------


def test_process_file_runs_full_pipeline(stocker_root, fake_vision):
    make_image(stocker_root)

    envelope = dispatch("asset.process_file", {"path": "data/incoming/photo.jpg"}, actor=AGENT)

    assert envelope["ok"] and envelope["outcome"] == "AI_PASSED"
    assert envelope["data"]["pipeline"] == {
        "source": "ok",
        "qc": "passed",
        "vision": "done",
        "metadata": "auto_approved",
        "metadata_completeness": "full",
        "ready": True,
        "review_reasons": [],
        "enhancement": {
            "assessed": True,
            "stale": False,
            "decision": "enhancement_risky",  # 64×48 px случайного шума: < 1 MP и сильный шум
            "reasons": ["noise", "resolution"],
            "event_id": envelope["data"]["pipeline"]["enhancement"]["event_id"],
        },
        "stock_readiness": {
            "evaluated": False,
            "stale": False,
            "platforms": {"adobe": "not_evaluated", "shutterstock": "not_evaluated"},
            "ready_for": [],
            "event_id": None,
        },
    }


def test_process_file_not_added(stocker_root, fake_vision):
    make_image(stocker_root)
    dispatch("asset.process_file", {"path": "data/incoming/photo.jpg"})

    envelope = dispatch("asset.process_file", {"path": "data/incoming/photo.jpg"})

    assert envelope["error"]["code"] == "FILE_NOT_ADDED"


def test_ai_failure_is_not_ok_without_error(stocker_root, monkeypatch):
    from app import worker

    monkeypatch.setattr(worker, "LocalAnalyzer", lambda: FakeAnalyzer(error=ConnectionError("down")))
    make_image(stocker_root)

    envelope = dispatch("asset.process_file", {"path": "data/incoming/photo.jpg"})

    assert envelope["ok"] is False and envelope["error"] is None
    assert envelope["outcome"] == "AI_FAILED"
    assert envelope["data"]["pipeline"]["vision"] == "failed"
    assert envelope["data"]["allowed_actions"] == [{"operation": "asset.process", "access": "pipeline"}]


def test_edit_keywords_add_remove(stocker_root):
    asset_id = _vision_asset(stocker_root)
    dispatch("metadata.build", {"asset_id": asset_id})

    envelope = dispatch(
        "metadata.edit",
        {"asset_id": asset_id, "add_keywords": ["Steel Beam"], "remove_keywords": ["TEST"]},
        actor=AGENT,
    )

    keywords = envelope["data"]["metadata"]["fields"]["keywords"]
    assert keywords[-1] == "steel beam" and "test" not in keywords
    assert envelope["outcome"] == "EDITED"


# --- read-модели ----------------------------------------------------------------------------------


def test_pipeline_source_changed(stocker_root):
    path = make_image(stocker_root, seed=1)
    asset_id = ingest_file(path)
    save_ai_result(asset_id, VISION.model_dump_json())
    make_image(stocker_root, seed=2)
    dispatch("asset.process", {"asset_id": asset_id, "force": True})

    view = dispatch("asset.get", {"asset_id": asset_id})["data"]

    assert view["pipeline"]["source"] == "changed"
    assert view["allowed_actions"] == []


def test_allowed_actions_for_human_review(stocker_root):
    asset_id = _vision_asset(stocker_root, vision=VISION.model_copy(update={"text_visible": ['ООО "Ромашка"']}))
    dispatch("metadata.build", {"asset_id": asset_id})

    view = dispatch("asset.get", {"asset_id": asset_id})["data"]

    assert view["pipeline"]["metadata"] == "human_review"
    assert view["pipeline"]["review_reasons"] == ["TEXT_BRAND_OR_LEGAL"]
    assert {a["operation"] for a in view["allowed_actions"]} == {
        "metadata.edit", "metadata.rebuild", "metadata.gate", "metadata.escalate", "metadata.approve", "metadata.reject",
    }


def test_list_filters_and_review_queue(stocker_root):
    ready_id = _vision_asset(stocker_root, name="a.jpg", seed=1)
    review_id = _vision_asset(stocker_root, name="b.jpg", seed=2, vision=VISION.model_copy(update={"brands": ["Acme"]}))
    no_metadata_id = _vision_asset(stocker_root, name="c.jpg", seed=3)
    dispatch("metadata.build", {"asset_id": ready_id})
    dispatch("metadata.build", {"asset_id": review_id})

    def ids(params):
        return [item["id"] for item in dispatch("asset.list", params)["data"]["items"]]

    assert ids({"ready": True}) == [ready_id]
    assert ids({"metadata_state": "human_review"}) == [review_id]
    assert ids({"metadata_state": "none"}) == [no_metadata_id]
    assert ids({"limit": 1, "offset": 1}) == [review_id]

    queue = dispatch("review.queue", {})["data"]
    assert [item["id"] for item in queue["items"]] == [review_id]
    assert queue["items"][0]["review_reasons"][0]["code"] == "TRADEMARK"


def test_history_parses_json_and_filters(stocker_root):
    asset_id = _vision_asset(stocker_root)
    dispatch("metadata.build", {"asset_id": asset_id})

    all_events = dispatch("asset.history", {"asset_id": asset_id})["data"]
    gated = dispatch("asset.history", {"asset_id": asset_id, "stage": "METADATA"})["data"]

    assert isinstance(all_events[0]["message"], str)  # INGEST
    assert {e["stage"] for e in gated} == {"METADATA"}
    assert gated[-1]["message"]["decision"] == "auto_approved"


def test_operations_manifest_has_json_schemas(stocker_root):
    manifest = dispatch("operations.list")["data"]
    by_name = {op["name"]: op for op in manifest}

    assert by_name["metadata.approve"]["access"] == "review"
    assert by_name["asset.get"]["mutating"] is False
    assert by_name["metadata.edit"]["params_schema"]["properties"]["asset_id"]["type"] == "integer"
    assert by_name["asset.get"]["params_schema"]["additionalProperties"] is False


def test_reads_do_not_write(stocker_root):
    asset_id = _vision_asset(stocker_root)
    dispatch("metadata.build", {"asset_id": asset_id})
    db = stocker_root / "data" / "db" / "stocker.db"
    with sqlite3.connect(db) as connection:
        before = connection.execute("SELECT COUNT(*), MAX(updated_at) FROM processing_events JOIN assets").fetchone()

    for operation, params in [
        ("asset.get", {"asset_id": asset_id}), ("asset.list", {}), ("asset.history", {"asset_id": asset_id}),
        ("review.queue", {}), ("metadata.get", {"asset_id": asset_id}), ("operations.list", {}),
    ]:
        assert dispatch(operation, params, actor=AGENT)["ok"]

    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*), MAX(updated_at) FROM processing_events JOIN assets").fetchone() == before


def test_invalid_params_error_lists_allowed_params(stocker_root):
    # Агент передал camelCase и выдуманный объект фильтра: ошибка подсказывает, как исправиться.
    wrong_key = dispatch("asset.get", {"assetId": 6}, actor=AGENT)["error"]["message"]
    wrong_filter = dispatch("asset.list", {"filter": '{"state": "ready"}'}, actor=AGENT)["error"]["message"]

    assert "Allowed params: asset_id (required)" in wrong_key
    assert "Allowed params: qc, vision, metadata_state, ready, limit, offset" in wrong_filter


def test_descriptions_carry_argument_examples():
    from app.service.registry import build_registry

    for operation in build_registry().values():
        if operation.access != "review":
            assert "Args:" in operation.description or "Example:" in operation.description, operation.name


def test_review_queue_summary_covers_whole_catalog(stocker_root, monkeypatch):
    from app import worker

    ready_id = _vision_asset(stocker_root, name="a.jpg", seed=1)
    review_id = _vision_asset(stocker_root, name="b.jpg", seed=2, vision=VISION.model_copy(update={"brands": ["Acme"]}))
    no_metadata_id = _vision_asset(stocker_root, name="c.jpg", seed=3)
    partial_id = _vision_asset(stocker_root, name="d.jpg", seed=4)
    dispatch("metadata.build", {"asset_id": ready_id})
    dispatch("metadata.build", {"asset_id": review_id})
    monkeypatch.setattr(metadata_service, "LMStudioMetadataAnalyzer", lambda: OfflineMetadataAnalyzer(error=ConnectionError("down")))
    dispatch("metadata.build", {"asset_id": partial_id})

    data = dispatch("review.queue", {}, actor=AGENT)["data"]

    summary = data["summary"]
    assert summary["total_assets"] == 4
    assert summary["ready"] == 1
    assert summary["by_metadata_state"] == {
        "none": 1, "draft": 1, "auto_approved": 1, "human_review": 1, "approved": 0, "rejected": 0,
    }
    problems = {item["id"]: item["problems"] for item in summary["problem_assets"]}
    assert problems == {review_id: ["TRADEMARK"], partial_id: ["METADATA_PARTIAL"]}
    assert no_metadata_id not in problems  # просто ещё не обработан — не проблема
    assert [item["id"] for item in data["items"]] == [review_id]  # очередь как раньше


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize(
    ("operation", "extra"),
    [
        ("metadata.edit", {"title": "Agent title"}),
        ("metadata.rebuild", {}),
        ("metadata.build", {"force": True}),
    ],
)
@pytest.mark.parametrize("actor", [AGENT, "workflow:n8n"])
def test_non_humans_cannot_change_human_decisions(stocker_root, decision, operation, extra, actor):
    asset_id = _vision_asset(stocker_root)
    dispatch("metadata.build", {"asset_id": asset_id})
    params = {"asset_id": asset_id} if decision == "approve" else {"asset_id": asset_id, "reason": "no"}
    dispatch(f"metadata.{decision}", params)
    state = dispatch("asset.get", {"asset_id": asset_id})["data"]["pipeline"]["metadata"]
    events_before = len(events(stocker_root, asset_id))

    envelope = dispatch(operation, {"asset_id": asset_id, **extra}, actor=actor)

    assert envelope["error"]["code"] == "FORBIDDEN"
    assert dispatch("asset.get", {"asset_id": asset_id})["data"]["pipeline"]["metadata"] == state
    assert len(events(stocker_root, asset_id)) == events_before


def test_agent_can_edit_drafts_and_auto_approved(stocker_root):
    asset_id = _vision_asset(stocker_root)
    dispatch("metadata.build", {"asset_id": asset_id}, actor=AGENT)  # auto_approved

    assert dispatch("metadata.edit", {"asset_id": asset_id, "title": "Agent title"}, actor=AGENT)["ok"]


def test_human_can_change_after_own_decision(stocker_root):
    asset_id = _vision_asset(stocker_root)
    dispatch("metadata.build", {"asset_id": asset_id})
    dispatch("metadata.reject", {"asset_id": asset_id, "reason": "no"})

    envelope = dispatch("metadata.edit", {"asset_id": asset_id, "title": "Human fix"})

    assert envelope["ok"] and envelope["data"]["pipeline"]["metadata"] != "rejected"
