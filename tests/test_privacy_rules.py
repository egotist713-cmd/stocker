"""gate-v1.2 (аудит 26.09.2026): дети и документы с персональными данными."""

import json

import pytest

from app import readiness as rd
from app import review_gate as rg
from app import worker
from app.ai.analyzer import AIAnalyzer
from app.ai.schema import AIAnalysis, PeopleInfo
from app.database.db import get_asset

from tests.conftest import events, make_image
from tests.test_readiness import codes, facts, metadata_for
from tests.test_review_gate import gated, suggestion, vision

ONE_PERSON = PeopleInfo(present=True, count=1)


def reasons(metadata) -> list[str]:
    return [r["code"] for r in metadata["review_gate"]["reasons"]]


# --- Дети -----------------------------------------------------------------------------


@pytest.mark.parametrize("field,value", [
    ("subject", "child"),                                                   # asset 47
    ("title", "Child on Rocking Horse in Garden"),
    ("description", "A young girl sits on a wooden rocking horse."),
    ("description", "Two kids play with a ball."),
    ("description", "A baby sleeps in a crib."),
])
def test_child_in_frame_goes_to_human(field, value):
    v = vision(people=ONE_PERSON, **{field: value})
    metadata = gated(v=v, s=suggestion())

    assert metadata["state"] == "human_review"
    assert "PEOPLE_RECOGNIZABLE" in reasons(metadata)
    assert rg.people_risk(v)["markers"][0].startswith("child:")


def test_child_seen_from_behind_is_still_recognizable_level():
    v = vision(people=ONE_PERSON, description="A child seen from behind, face not visible.")
    assert rg.people_risk(v)["level"] == "recognizable"


def test_children_concept_keyword_without_child_in_frame_is_not_a_child():
    # asset 9: «children's playground» — концепт в keywords, человек не ребёнок по описанию
    v = vision(people=ONE_PERSON, subject="Playground equipment", keywords=["children's playground", "park"])
    assert rg.people_risk(v)["level"] == "unclear"


def test_child_word_without_people_is_ignored():
    v = vision(subject="Children's toy blocks on a table")
    assert rg.people_risk(v)["level"] == "none"


def test_readiness_blocks_child_without_release():
    v = vision(people=ONE_PERSON, subject="child")
    result = rd.evaluate(facts(), metadata_for(v, state="approved"), v)
    assert codes(result, "adobe")["MODEL_RELEASE_REQUIRED"] == rd.BLOCKER


# --- Документы ------------------------------------------------------------------------


@pytest.mark.parametrize("override", [
    {"subject": "Passport", "keywords": ["passport", "identity document"]},                    # asset 67
    {"subject": "Document", "keywords": ["residential registration", "propiska", "document"]},  # asset 68
    {"title": "Driver's license on a desk"},
    {"description": "A bank card lying on a keyboard."},
])
def test_personal_document_goes_to_human_and_is_blocked_for_export(override):
    v = vision(**override)
    metadata = gated(v=v, s=suggestion())

    assert metadata["state"] == "human_review"
    assert "PERSONAL_DOCUMENT" in reasons(metadata)

    result = rd.evaluate(facts(), metadata_for(v, state="approved"), v)
    assert codes(result, "adobe")["PERSONAL_DOCUMENT"] == rd.BLOCKER
    assert result["ready_for"] == []


def test_ordinary_document_words_do_not_trigger():
    v = vision(subject="Technical drawing", keywords=["document", "blueprint", "registration plate"])
    assert rg.personal_document(v) == []


# --- Распознанный текст документа не хранится ------------------------------------------


class DocumentAnalyzer(AIAnalyzer):
    provider = "fake"
    model = "fake-model"
    prompt_version = "test-v1"

    def analyze(self, image_path):
        return AIAnalysis(
            title="Passport opened to personal details",
            subject="Passport",
            keywords=["passport", "identity document"],
            text_visible=["PASSPORT", "1234 567890", "01.01.1990"],
            people=ONE_PERSON,
            confidence=0.9,
        )


def test_worker_does_not_store_document_text(stocker_root):
    asset_id = worker.process_file(make_image(stocker_root), analyzer=DocumentAnalyzer())

    stored = json.loads(get_asset(asset_id)["ai_result"])
    assert stored["text_visible"] == []
    assert stored["subject"] == "Passport"

    everything = json.dumps([m for _, _, m in events(stocker_root, asset_id)]) + get_asset(asset_id)["metadata_json"]
    assert "1234 567890" not in everything and "01.01.1990" not in everything

    (ai_passed,) = [json.loads(m) for s, st, m in events(stocker_root, asset_id) if (s, st) == ("AI", "PASSED")]
    assert ai_passed["privacy"] == {
        "reason": "PERSONAL_DOCUMENT",
        "terms": ["passport", "identity document", "personal details"],
        "text_visible_removed": 3,
    }

    metadata = json.loads(get_asset(asset_id)["metadata_json"])
    assert metadata["state"] == "human_review" and "PERSONAL_DOCUMENT" in reasons(metadata)
