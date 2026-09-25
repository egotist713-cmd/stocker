import json
import sqlite3

import pytest

from app import metadata as service
from app import metadata_builder as mb
from app.ai.analyzer import AIResponseError
from app.ai.metadata_analyzer import MetadataAnalyzer
from app.ai.schema import AIAnalysis, MetadataSuggestion
from app.database.db import add_event, get_asset, save_ai_result
from app.ingest import ingest_file

from tests.conftest import events, make_image

VISION = AIAnalysis(
    title="Elevator Shaft Interior",
    description="An elevator shaft with concrete walls, metal rails and cables.",
    keywords=["elevator shaft", "concrete wall", "metal rail", "cable", "industrial", "architecture", "engineering"],
    confidence=0.9,
)

SUGGESTION = MetadataSuggestion(
    title="Elevator shaft interior with steel rails",
    description="Concrete elevator shaft with metal rails and cables.",
    keywords=["elevator shaft", "construction", *[f"concept{chr(97 + i)}" for i in range(26)]],
)


class FakeMetadataAnalyzer(MetadataAnalyzer):
    provider = "fake"
    model = "fake-metadata"
    prompt_version = "metadata-test"

    def __init__(self, result=SUGGESTION, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = 0

    def suggest(self, analysis, image_path=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def asset_id(stocker_root) -> int:
    asset_id = ingest_file(make_image(stocker_root))
    save_ai_result(asset_id, VISION.model_dump_json())
    add_event(asset_id, "AI", "PASSED", json.dumps({"provider": "lmstudio", "model": "vision-m", "prompt_version": "local-v2"}))
    return asset_id


def stored(asset_id) -> dict:
    return json.loads(get_asset(asset_id)["metadata_json"])


def metadata_events(root, asset_id):
    return [(s, st, json.loads(m)) for s, st, m in events(root, asset_id) if s.startswith("METADATA")]


def test_build_full_draft_saves_metadata_and_events(stocker_root, asset_id):
    result = service.build(asset_id, analyzer=FakeMetadataAnalyzer())

    assert result["outcome"] == service.DRAFTED
    metadata = stored(asset_id)
    assert metadata == result["metadata"]
    assert metadata["state"] == "auto_approved" and metadata["completeness"] == "full"

    (ai_stage, ai_status, ai_msg), (d_stage, d_status, d_msg), gated = metadata_events(stocker_root, asset_id)
    assert gated[:2] == ("METADATA", "GATED")
    assert gated[2]["decision"] == "auto_approved" and gated[2]["state_before"] is None
    assert (ai_stage, ai_status) == ("METADATA_AI", "PASSED")
    assert set(ai_msg) == {"provider", "model", "prompt_version", "inputs", "generated_at", "duration_s"}
    assert ai_msg["inputs"] == ["vision_json"]
    assert (d_stage, d_status) == ("METADATA", "DRAFTED")
    assert d_msg["completeness"] == "full" and d_msg["trigger"] == "build"
    assert d_msg["keywords"] == len(metadata["fields"]["keywords"])


def test_build_records_provenance_of_both_layers(stocker_root, asset_id):
    service.build(asset_id, analyzer=FakeMetadataAnalyzer())

    sources = stored(asset_id)["sources"]
    ai_passed_id = [row for row in _event_rows(stocker_root, asset_id) if row[1:3] == ("AI", "PASSED")][0][0]
    metadata_ai_id = [row for row in _event_rows(stocker_root, asset_id) if row[1:3] == ("METADATA_AI", "PASSED")][0][0]

    assert sources["vision"] == {
        "event_id": ai_passed_id,
        "provider": "lmstudio",
        "model": "vision-m",
        "prompt_version": "local-v2",
        "confidence": 0.9,
    }
    assert sources["metadata_ai"]["event_id"] == metadata_ai_id
    assert sources["metadata_ai"]["model"] == "fake-metadata"


def _event_rows(root, asset_id):
    with sqlite3.connect(root / "data" / "db" / "stocker.db") as connection:
        return connection.execute(
            "SELECT id, stage, status FROM processing_events WHERE asset_id = ? ORDER BY id", (asset_id,)
        ).fetchall()


def test_legacy_vision_event_without_json(stocker_root):
    asset_id = ingest_file(make_image(stocker_root))
    save_ai_result(asset_id, VISION.model_dump_json())
    add_event(asset_id, "AI", "PASSED", "AI analysis completed")

    service.build(asset_id, analyzer=FakeMetadataAnalyzer())

    vision_source = stored(asset_id)["sources"]["vision"]
    assert vision_source["event_id"] is not None
    assert vision_source["model"] is None


def test_build_does_not_overwrite_existing(stocker_root, asset_id):
    service.build(asset_id, analyzer=FakeMetadataAnalyzer())
    events_before = len(events(stocker_root, asset_id))
    analyzer = FakeMetadataAnalyzer()

    result = service.build(asset_id, analyzer=analyzer)

    assert result["outcome"] == service.METADATA_EXISTS
    assert analyzer.calls == 0
    assert len(events(stocker_root, asset_id)) == events_before


def test_metadata_ai_failure_creates_partial_draft(stocker_root, asset_id):
    analyzer = FakeMetadataAnalyzer(error=AIResponseError("bad", raw_output='{"title": "x", "keyw'))

    result = service.build(asset_id, analyzer=analyzer)

    assert result["outcome"] == service.DRAFTED
    metadata = stored(asset_id)
    assert metadata["completeness"] == "partial"
    assert metadata["fields"]["title"] == VISION.title

    (f_stage, f_status, failure), (d_stage, d_status, drafted), gated = metadata_events(stocker_root, asset_id)
    assert gated[2]["decision"] == "deferred" and metadata["state"] == "draft"
    assert (f_stage, f_status) == ("METADATA_AI", "FAILED")
    assert failure["error_type"] == "AIResponseError"
    assert failure["raw_output"] == '{"title": "x", "keyw'
    assert drafted["completeness"] == "partial"
    failure_id = [row[0] for row in _event_rows(stocker_root, asset_id) if row[1:3] == ("METADATA_AI", "FAILED")][0]
    assert metadata["sources"]["metadata_ai_failure_event_id"] == failure_id


def test_connection_failure_has_no_raw_output(stocker_root, asset_id):
    service.build(asset_id, analyzer=FakeMetadataAnalyzer(error=ConnectionError("down")))

    failure = metadata_events(stocker_root, asset_id)[0][2]
    assert "raw_output" not in failure
    assert failure["error"] == "down"


def test_partial_is_upgraded_by_plain_build(stocker_root, asset_id):
    service.build(asset_id, analyzer=FakeMetadataAnalyzer(error=ConnectionError("down")))

    result = service.build(asset_id, analyzer=FakeMetadataAnalyzer())

    assert result["outcome"] == service.DRAFTED
    assert stored(asset_id)["completeness"] == "full"


def test_failed_retry_keeps_existing_partial(stocker_root, asset_id):
    service.build(asset_id, analyzer=FakeMetadataAnalyzer(error=ConnectionError("down")))
    before = stored(asset_id)

    result = service.build(asset_id, analyzer=FakeMetadataAnalyzer(error=ConnectionError("still down")))

    assert result["outcome"] == service.METADATA_AI_FAILED
    assert stored(asset_id) == before
    assert [(s, st) for s, st, _ in metadata_events(stocker_root, asset_id)][-1] == ("METADATA_AI", "FAILED")


def test_edited_partial_needs_force(stocker_root, asset_id):
    service.build(asset_id, analyzer=FakeMetadataAnalyzer(error=ConnectionError("down")))
    service.edit(asset_id, {"title": "Human title"})
    analyzer = FakeMetadataAnalyzer()

    assert service.build(asset_id, analyzer=analyzer)["outcome"] == service.METADATA_EXISTS
    assert analyzer.calls == 0

    result = service.build(asset_id, force=True, analyzer=analyzer)
    assert result["metadata"]["completeness"] == "full"
    assert result["metadata"]["edited_fields"] == []
    assert metadata_events(stocker_root, asset_id)[-1][2]["trigger"] == "build_force"


def test_edit_writes_event_per_field(stocker_root, asset_id):
    service.build(asset_id, analyzer=FakeMetadataAnalyzer())

    result = service.edit(asset_id, {"title": "New title", "description": "New description of the shaft."})

    assert result["outcome"] == service.EDITED
    edited = [msg for s, st, msg in metadata_events(stocker_root, asset_id) if (s, st) == ("METADATA", "EDITED")]
    assert [e["field"] for e in edited] == ["title", "description"]
    assert edited[0]["new"] == "New title" and edited[0]["state_before"] == "auto_approved"
    assert stored(asset_id)["edited_fields"] == ["title", "description"]


def test_edit_without_changes_writes_nothing(stocker_root, asset_id):
    service.build(asset_id, analyzer=FakeMetadataAnalyzer())
    events_before = len(events(stocker_root, asset_id))

    result = service.edit(asset_id, {"title": stored(asset_id)["fields"]["title"]})

    assert result["outcome"] == service.NO_CHANGES
    assert len(events(stocker_root, asset_id)) == events_before


def test_approve_and_reject_events(stocker_root, asset_id):
    service.build(asset_id, analyzer=FakeMetadataAnalyzer(result=SUGGESTION.model_copy(update={"description": "Shaft in Berlin."})))

    with pytest.raises(service.MetadataError) as info:
        service.approve(asset_id)
    assert info.value.code == "INVALID_TRANSITION"
    assert stored(asset_id)["state"] == "human_review"  # UNCONFIRMED_CLAIM

    result = service.approve(asset_id, confirm_claims=True)
    assert result["outcome"] == service.APPROVED
    approved = metadata_events(stocker_root, asset_id)[-1]
    assert approved[:2] == ("METADATA", "APPROVED")
    assert approved[2] == {
        "builder_version": "metadata-v1",
        "completeness": "full",
        "allow_partial": False,
        "confirmed_claims": ["Berlin"],
    }

    service.reject(asset_id, "Wrong location")
    rejected = metadata_events(stocker_root, asset_id)[-1]
    assert rejected == ("METADATA", "REJECTED", {"reason": "Wrong location", "state_before": "approved"})
    assert stored(asset_id)["state"] == "rejected"


def test_rebuild_keeps_edits_and_records_trigger(stocker_root, asset_id):
    service.build(asset_id, analyzer=FakeMetadataAnalyzer())
    service.edit(asset_id, {"title": "Human title"})

    result = service.rebuild(asset_id)

    assert result["metadata"]["fields"]["title"] == "Human title"
    assert metadata_events(stocker_root, asset_id)[-1][2]["trigger"] == "rebuild"


def test_errors_for_missing_asset_vision_or_metadata(stocker_root, asset_id):
    with pytest.raises(service.MetadataError) as info:
        service.build(999)
    assert info.value.code == "ASSET_NOT_FOUND"

    no_vision = ingest_file(make_image(stocker_root, name="other.jpg", seed=5))
    with pytest.raises(service.MetadataError) as info:
        service.build(no_vision, analyzer=FakeMetadataAnalyzer())
    assert info.value.code == "VISION_MISSING"

    with pytest.raises(service.MetadataError) as info:
        service.approve(asset_id)
    assert info.value.code == "METADATA_MISSING"


def test_failed_transition_writes_nothing(stocker_root, asset_id):
    service.build(asset_id, analyzer=FakeMetadataAnalyzer(error=ConnectionError("down")))
    before, events_before = stored(asset_id), len(events(stocker_root, asset_id))

    with pytest.raises(service.MetadataError):
        service.approve(asset_id)  # partial без --allow-partial

    assert stored(asset_id) == before
    assert len(events(stocker_root, asset_id)) == events_before


def test_vision_result_is_never_modified(stocker_root, asset_id):
    before = get_asset(asset_id)["ai_result"]

    service.build(asset_id, analyzer=FakeMetadataAnalyzer())
    service.edit(asset_id, {"keywords": ["a", "b"]})
    service.rebuild(asset_id)

    assert get_asset(asset_id)["ai_result"] == before
    assert get_asset(asset_id)["status"] == "NEW"  # assets.status не трогается
