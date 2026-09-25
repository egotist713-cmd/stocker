import copy

import pytest

from app import metadata_builder as mb
from app.ai.schema import AIAnalysis, MetadataSuggestion, PeopleInfo

VISION_SOURCE = {"event_id": 17, "provider": "lmstudio", "model": "m", "prompt_version": "local-v2", "confidence": 0.9}
AI_SOURCE = {"event_id": 18, "provider": "lmstudio", "model": "m", "prompt_version": "metadata-v1", "inputs": ["vision_json"]}


def vision(**overrides) -> AIAnalysis:
    data = {
        "title": "Elevator Shaft Interior",
        "description": "An elevator shaft with concrete walls, metal rails and cables.",
        "keywords": ["elevator shaft", "concrete wall", "metal rail", "cable"],
        "subject": "Elevator shaft",
    }
    return AIAnalysis(**{**data, **overrides})


def keywords(count: int) -> list[str]:
    return [f"concept{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(count)]


def suggestion(**overrides) -> MetadataSuggestion:
    data = {
        "title": "Elevator shaft interior with steel rails",
        "description": "Concrete elevator shaft with metal rails and cables.",
        "keywords": ["elevator shaft", "industrial", "construction", *keywords(25)],
    }
    return MetadataSuggestion(**{**data, **overrides})


def draft(v=None, s="default", **kwargs) -> dict:
    v = v or vision()
    s = suggestion() if s == "default" else s
    return mb.build_draft(v, s, vision_source=VISION_SOURCE, metadata_ai_source=AI_SOURCE, **kwargs)


def codes(metadata, kind="errors") -> list[str]:
    return [issue["code"] for issue in metadata["validation"][kind]]


# --- нормализация -----------------------------------------------------------------


def test_title_and_description_are_normalized():
    m = draft(s=suggestion(title="  Shaft’s   “view”. ", description="Steel—concrete shaft."))

    assert m["fields"]["title"] == "Shaft's \"view\""
    assert m["fields"]["description"] == "Steel-concrete shaft."


def test_keyword_normalization_and_dropped_reasons():
    v = vision(brands=["Otis"])
    kws, dropped = mb.normalize_keywords(
        ["  Elevator  Shaft ", "shaft, cable; rail", "Otis elevator", "stock photo", "elevator shaft", "", "“Steel”!"],
        v,
    )

    assert kws == ["elevator shaft", "shaft", "cable", "rail", "steel"]
    assert {(d["keyword"], d["reason"]) for d in dropped} == {
        ("otis elevator", "BRAND"),
        ("stock photo", "STOPWORD"),
        ("elevator shaft", "DUPLICATE"),
        ("", "EMPTY"),
    }


def test_merge_keeps_ai_order_then_missing_vision_keywords():
    assert mb.merge_keywords(["b", "Elevator Shaft"], ["elevator shaft", "cable"]) == ["b", "Elevator Shaft", "cable"]


def test_keywords_are_never_truncated():
    m = draft(s=suggestion(keywords=keywords(60)))

    assert len(m["fields"]["keywords"]) == 60 + 4  # + Vision-keywords
    assert "TOO_MANY_KEYWORDS" in codes(m)


def test_long_keyword_is_error_not_dropped():
    long_keyword = "a" * 51
    m = draft(s=suggestion(keywords=[long_keyword, *keywords(30)]))

    assert long_keyword in m["fields"]["keywords"]
    assert "KEYWORD_TOO_LONG" in codes(m)


def test_long_text_is_error_not_truncated():
    m = draft(s=suggestion(title="x" * 201, description="y" * 201))

    assert len(m["fields"]["title"]) == 201
    assert {"TITLE_TOO_LONG", "DESCRIPTION_TOO_LONG"} <= set(codes(m))


# --- флаги --------------------------------------------------------------------------


def test_flags_from_vision():
    v = vision(
        people=PeopleInfo(present=True, count=2),
        logos=["X"],
        text_visible=["EXIT"],
        ai_generated=True,
    )

    assert mb.compute_flags(v) == {
        "model_release_required": True,
        "trademark_risk": True,
        "visible_text": True,
        "editorial_risk": True,
        "ai_generated": True,
    }


def test_flags_produce_warnings_not_errors():
    m = draft(v=vision(people=PeopleInfo(present=True, count=1), text_visible=["EXIT"]))

    assert {"MODEL_RELEASE_REQUIRED", "VISIBLE_TEXT"} <= set(codes(m, "warnings"))
    assert not {"MODEL_RELEASE_REQUIRED", "VISIBLE_TEXT"} & set(codes(m))


# --- опора на Vision ------------------------------------------------------------------


def test_keywords_classified_as_vision_or_concept():
    m = draft()
    grounding = m["grounding"]

    assert "elevator shaft" in grounding["vision_keywords"]
    assert "metal rail" in grounding["vision_keywords"]  # "rails" в Vision
    assert "construction" in grounding["concept_keywords"]
    assert grounding["specific_claims"] == []


def test_general_concepts_do_not_block_approval():
    m = draft()

    assert m["grounding"]["concept_keywords"]
    approved, _ = mb.approve(m, vision())
    assert approved["state"] == mb.APPROVED


def test_proper_noun_in_description_is_a_claim():
    m = draft(s=suggestion(description="Elevator shaft in Berlin with Cyrillic labels."))

    claims = {(c["term"], c["field"], c["reason"]) for c in m["grounding"]["specific_claims"]}
    assert ("Berlin", "description", "PROPER_NOUN") in claims
    assert ("Cyrillic", "description", "PROPER_NOUN") in claims
    assert "UNCONFIRMED_CLAIM" in codes(m)


def test_keyword_matching_claimed_proper_noun_is_a_claim():
    m = draft(s=suggestion(description="Shaft with Cyrillic labels.", keywords=["cyrillic labels", *keywords(30)]))

    assert ("cyrillic labels", "keywords") in {(c["term"], c["field"]) for c in m["grounding"]["specific_claims"]}
    assert "cyrillic labels" not in m["grounding"]["concept_keywords"]


def test_numbers_are_claims_unless_grounded():
    v = vision(text_visible=["Floor 5"])
    m = draft(v=v, s=suggestion(description="Shaft built in 1998 near floor 5.", keywords=["model x200", "floor 5", *keywords(30)]))

    terms = {c["term"] for c in m["grounding"]["specific_claims"]}
    assert {"1998", "model x200"} <= terms
    assert "5" not in terms and "floor 5" not in terms


def test_acronym_is_a_claim_even_at_sentence_start():
    m = draft(s=suggestion(description="USA elevator shaft."))

    assert "USA" in {c["term"] for c in m["grounding"]["specific_claims"]}


def test_sentence_start_capital_and_grounded_names_are_not_claims():
    v = vision(description="Metal rails in Otis elevator shaft.", brands=[])
    m = draft(v=v, s=suggestion(description="Concrete shaft. Metal rails near Otis cables."))

    assert m["grounding"]["specific_claims"] == []


def test_title_case_title_is_not_checked_for_proper_nouns():
    m = draft(s=suggestion(title="Industrial Elevator Shaft Looking Down"))

    assert m["grounding"]["specific_claims"] == []


def test_sentence_case_title_is_checked():
    m = draft(s=suggestion(title="Elevator shaft in Berlin"))

    assert ("Berlin", "title") in {(c["term"], c["field"]) for c in m["grounding"]["specific_claims"]}


def test_brand_named_by_vision_in_text_is_error():
    m = draft(v=vision(brands=["Otis"]), s=suggestion(description="Otis elevator shaft."))

    assert "BRAND_IN_TEXT" in codes(m)


# --- partial draft --------------------------------------------------------------------


def test_partial_draft_from_vision():
    m = mb.build_draft(
        vision(keywords=keywords(10)),
        None,
        vision_source=VISION_SOURCE,
        metadata_ai_source=AI_SOURCE,
        metadata_ai_failure_event_id=99,
    )

    assert m["completeness"] == mb.PARTIAL
    assert m["generated"] is None
    assert m["fields"]["title"] == "Elevator Shaft Interior"
    assert m["sources"]["metadata_ai"] is None
    assert m["sources"]["metadata_ai_failure_event_id"] == 99
    assert "PARTIAL_DRAFT" in codes(m, "warnings")


def test_partial_approve_requires_allow_partial():
    m = mb.build_draft(vision(keywords=keywords(10)), None, vision_source=VISION_SOURCE)

    with pytest.raises(mb.MetadataTransitionError, match="allow-partial"):
        mb.approve(m, vision())

    approved, _ = mb.approve(m, vision(), allow_partial=True)
    assert approved["review"]["allow_partial"] is True


# --- структура ------------------------------------------------------------------------


def test_full_draft_structure():
    m = draft()

    assert set(m) == {
        "metadata_version", "state", "completeness", "fields", "generated", "edited_fields",
        "flags", "grounding", "validation", "review", "sources",
    }
    assert m["state"] == mb.DRAFT and m["completeness"] == mb.FULL
    assert m["generated"]["title"] == suggestion().title
    assert set(m["validation"]) == {"errors", "warnings", "dropped_keywords"}
    assert m["sources"]["vision"] == VISION_SOURCE
    assert m["sources"]["metadata_ai"] == AI_SOURCE
    assert m["sources"]["builder_version"] == "metadata-v1"


# --- переходы -------------------------------------------------------------------------


def test_edit_records_changes_and_normalizes():
    m = draft()

    edited, changes = mb.edit(m, vision(), {"title": "  New “title”. "})

    assert edited["fields"]["title"] == 'New "title"'
    assert edited["edited_fields"] == ["title"]
    assert changes == [{"field": "title", "old": m["fields"]["title"], "new": 'New "title"', "state_before": "draft"}]
    assert m["fields"]["title"] != edited["fields"]["title"]  # вход не изменён


def test_edit_without_real_change_returns_no_changes():
    m = draft()

    edited, changes = mb.edit(m, vision(), {"title": m["fields"]["title"] + "."})

    assert changes == [] and edited is m


def test_edit_keywords_reports_dropped():
    edited, _ = mb.edit(draft(), vision(), {"keywords": ["a1", "stock", "a1", *keywords(10)]})

    assert {d["reason"] for d in edited["validation"]["dropped_keywords"]} == {"STOPWORD", "DUPLICATE"}


def test_edit_unknown_field_is_rejected():
    with pytest.raises(mb.MetadataTransitionError):
        mb.edit(draft(), vision(), {"state": "approved"})


def test_edit_after_approve_returns_to_draft():
    approved, _ = mb.approve(draft(), vision())

    edited, changes = mb.edit(approved, vision(), {"description": "Another factual description of the shaft."})

    assert edited["state"] == mb.DRAFT
    assert edited["review"]["decided_at"] is None
    assert changes[0]["state_before"] == mb.APPROVED


def test_approve_blocked_by_errors():
    m = draft(s=suggestion(title=""))

    with pytest.raises(mb.MetadataTransitionError, match="TITLE_EMPTY"):
        mb.approve(m, vision())


def test_approve_with_confirm_claims():
    m = draft(s=suggestion(description="Elevator shaft in Berlin."))

    with pytest.raises(mb.MetadataTransitionError, match="UNCONFIRMED_CLAIM"):
        mb.approve(m, vision())

    approved, confirmed = mb.approve(m, vision(), confirm_claims=True)
    assert approved["state"] == mb.APPROVED
    assert confirmed == ["Berlin"]
    assert approved["grounding"]["specific_claims"][0]["confirmed"] is True


def test_confirmed_claims_survive_unrelated_edit():
    approved, _ = mb.approve(draft(s=suggestion(description="Elevator shaft in Berlin.")), vision(), confirm_claims=True)

    edited, _ = mb.edit(approved, vision(), {"title": "Another elevator shaft title"})

    assert "UNCONFIRMED_CLAIM" not in codes(edited)


def test_new_claim_after_edit_needs_confirmation():
    approved, _ = mb.approve(draft(s=suggestion(description="Elevator shaft in Berlin.")), vision(), confirm_claims=True)

    edited, _ = mb.edit(approved, vision(), {"description": "Elevator shaft in Hamburg."})

    assert [c["term"] for c in edited["grounding"]["specific_claims"] if not c["confirmed"]] == ["Hamburg"]


def test_approve_only_from_draft():
    approved, _ = mb.approve(draft(), vision())

    with pytest.raises(mb.MetadataTransitionError):
        mb.approve(approved, vision())


def test_reject_requires_reason_and_valid_state():
    m = draft()

    with pytest.raises(mb.MetadataTransitionError):
        mb.reject(m, "   ")

    rejected = mb.reject(m, "Blurry subject")
    assert rejected["state"] == mb.REJECTED and rejected["review"]["reason"] == "Blurry subject"

    with pytest.raises(mb.MetadataTransitionError):
        mb.reject(rejected, "again")

    approved, _ = mb.approve(m, vision())
    assert mb.reject(approved, "Changed my mind")["state"] == mb.REJECTED


def test_rebuild_keeps_human_edits_and_resets_approval():
    edited, _ = mb.edit(draft(), vision(), {"title": "Human title"})
    approved, _ = mb.approve(edited, vision())

    rebuilt = mb.rebuild(approved, vision())

    assert rebuilt["fields"]["title"] == "Human title"
    assert rebuilt["fields"]["description"] == suggestion().description
    assert rebuilt["state"] == mb.DRAFT


def test_rebuild_partial_uses_vision():
    m = mb.build_draft(vision(keywords=keywords(10)), None, vision_source=VISION_SOURCE)

    rebuilt = mb.rebuild(m, vision(keywords=keywords(10), title="New vision title"))

    assert rebuilt["fields"]["title"] == "New vision title"


def test_functions_do_not_mutate_input():
    m = draft(s=suggestion(description="Elevator shaft in Berlin."))
    snapshot = copy.deepcopy(m)

    mb.edit(m, vision(), {"title": "Changed"})
    mb.approve(m, vision(), confirm_claims=True)
    mb.reject(m, "reason")
    mb.rebuild(m, vision())

    assert m == snapshot
