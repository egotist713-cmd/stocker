import copy

import pytest

from app import metadata_builder as mb
from app import review_gate as rg
from app.ai.schema import AIAnalysis, MetadataSuggestion, PeopleInfo

VISION_SOURCE = {"event_id": 1}


def vision(**overrides) -> AIAnalysis:
    data = {
        "title": "Electrical junction box",
        "description": "An electrical junction box with wires on a concrete wall.",
        "keywords": ["electrical box", "wiring", "terminal block", "concrete wall"],
        "subject": "Electrical junction box",
    }
    return AIAnalysis(**{**data, **overrides})


def suggestion(**overrides) -> MetadataSuggestion:
    data = {
        "title": "Electrical junction box with wiring",
        "description": "Electrical junction box with wires on a concrete wall.",
        "keywords": ["electrical box", "wiring", *[f"concept{chr(97 + i)}" for i in range(26)]],
    }
    return MetadataSuggestion(**{**data, **overrides})


def draft(v=None, s="default") -> dict:
    v = v or vision()
    s = suggestion() if s == "default" else s
    return mb.build_draft(v, s, vision_source=VISION_SOURCE)


def gated(v=None, s="default", **kwargs) -> dict:
    v = v or vision()
    metadata, _ = rg.apply_gate(draft(v, s), v, **kwargs)
    return metadata


def reason_codes(metadata) -> list[str]:
    return [r["code"] for r in metadata["review_gate"]["reasons"]]


def note_codes(metadata) -> list[str]:
    return [n["code"] for n in metadata["review_gate"]["notes"]]


# --- решение ------------------------------------------------------------------------


def test_low_risk_is_auto_approved():
    m = gated()

    assert m["state"] == "auto_approved"
    assert m["metadata_version"] == "2"
    assert m["review_gate"]["policy_version"] == "gate-v1.1"
    assert m["review_gate"]["decision"] == "auto_approved"
    assert reason_codes(m) == []


def test_validation_warnings_do_not_block():
    m = gated(s=suggestion(title="x" * 80, keywords=["a", "b", "c", "d", "e", "f", "g", "h"]))

    assert {"TITLE_LONG", "FEW_KEYWORDS"} <= {w["code"] for w in m["validation"]["warnings"]}
    assert m["state"] == "auto_approved"


def test_validation_errors_require_review():
    m = gated(s=suggestion(title=""))

    assert m["state"] == "human_review"
    assert reason_codes(m) == ["VALIDATION_ERRORS"]


def test_unconfirmed_claim_requires_review():
    m = gated(s=suggestion(description="Junction box in Berlin."))

    assert "UNCONFIRMED_CLAIM" in reason_codes(m)
    assert "VALIDATION_ERRORS" not in reason_codes(m)


def test_partial_draft_is_deferred_not_queued():
    v = vision(keywords=[f"kw{chr(97 + i)}" for i in range(10)])
    m, _ = rg.apply_gate(mb.build_draft(v, None, vision_source=VISION_SOURCE), v)

    assert m["state"] == "draft"
    assert m["review_gate"]["decision"] == "deferred"


def test_auto_approve_kill_switch():
    m = gated(auto_approve_enabled=False)

    assert m["state"] == "human_review"
    assert reason_codes(m) == ["AUTO_APPROVE_DISABLED"]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"brands": ["Siemens"]}, "TRADEMARK"),
        ({"logos": ["ABB logo"]}, "TRADEMARK"),
        ({"editorial_risk": ["news event"]}, "EDITORIAL_RISK"),
        ({"ai_generated": True}, "AI_GENERATED"),
    ],
)
def test_vision_risk_flags_require_review(overrides, code):
    assert code in reason_codes(gated(v=vision(**overrides)))


def test_human_decisions_are_never_changed_by_gate():
    approved, _ = mb.approve(draft(), vision())

    for decided in (approved, mb.reject(draft(), "no")):
        result, gate = rg.apply_gate(decided, vision(brands=["Siemens"]))
        assert gate is None
        assert result is decided


def test_apply_gate_does_not_mutate_input():
    m = draft()
    snapshot = copy.deepcopy(m)

    rg.apply_gate(m, vision())

    assert m == snapshot


# --- text_visible -------------------------------------------------------------------


def test_real_asset_6_industrial_markings_are_technical():
    # text_visible asset 6 (IMG_20260911_130507.jpg), Vision local-v2.
    texts = ["KM6000-УХЛ4", "N° 0626023", "IP20", "ВИД ЗАЗЕМЛЕНИЯ TN-S", "0,6kg", "06.2016", "X3", "X4", "X5", "X7", "X1"]
    m = gated(v=vision(text_visible=texts))

    assert m["state"] == "auto_approved"
    assert {item["category"] for item in m["review_gate"]["text_items"]} == {"technical"}
    assert note_codes(m).count("TEXT_TECHNICAL") == len(texts)


def test_real_asset_5_company_name_requires_review():
    # text_visible asset 5 (IMG_20260911_130437.jpg): название компании, которое Vision не вынес в brands.
    m = gated(v=vision(text_visible=['АО "ШПЗ"', "ЗАМОК ДВЕРИ ШАХТЫ", "0411Е.06.05.090"]))

    items = {item["text"]: (item["category"], item["rule"]) for item in m["review_gate"]["text_items"]}
    assert items['АО "ШПЗ"'] == ("brand_or_legal", "LEGAL_FORM")
    assert items["ЗАМОК ДВЕРИ ШАХТЫ"] == ("descriptive", "DEFAULT")
    assert items["0411Е.06.05.090"] == ("technical", "DIGITS")
    assert m["state"] == "human_review"
    assert reason_codes(m) == ["TEXT_BRAND_OR_LEGAL"]


@pytest.mark.parametrize(
    ("text", "category", "rule"),
    [
        ("IP20", "technical", "DIGITS"),
        ("220V", "technical", "DIGITS"),
        ("TN-S", "technical", "CODE"),
        ("WARNING", "technical", "WARNING_WORD"),
        ("HIGH VOLTAGE", "technical", "WARNING_WORD"),
        ("ОСТОРОЖНО", "technical", "WARNING_WORD"),
        ("Electrical room", "descriptive", "DEFAULT"),
        ("Maintenance area", "descriptive", "DEFAULT"),
        ('ООО "Ромашка"', "brand_or_legal", "LEGAL_FORM"),
        ("Acme Ltd.", "brand_or_legal", "LEGAL_FORM"),
        ("Siemens AG", "brand_or_legal", "LEGAL_FORM"),
        ("PowerBox®", "brand_or_legal", "LEGAL_SYMBOL"),
        ("Patent pending", "brand_or_legal", "LEGAL_WORD"),
        ("www.example.com", "brand_or_legal", "CONTACT"),
        ("info@example.ru", "brand_or_legal", "CONTACT"),
        ("+7 495 123-45-67", "brand_or_legal", "CONTACT"),
    ],
)
def test_text_classification(text, category, rule):
    item = rg.classify_text_item(text, vision())

    assert (item["category"], item["rule"]) == (category, rule)


def test_vision_brand_in_text_wins_over_digits():
    item = rg.classify_text_item("SIEMENS 220V", vision(brands=["Siemens"]))

    assert (item["category"], item["rule"]) == ("brand_or_legal", "BRAND")


def test_descriptive_text_is_note_only():
    m = gated(v=vision(text_visible=["Electrical room"]))

    assert m["state"] == "auto_approved"
    assert note_codes(m) == ["TEXT_DESCRIPTIVE"]


# --- люди ---------------------------------------------------------------------------


def people(description: str, subject: str = "Electrical work", count: int = 1) -> AIAnalysis:
    return vision(description=description, subject=subject, people=PeopleInfo(present=True, count=count))


@pytest.mark.parametrize(
    ("description", "subject", "level"),
    [
        ("A gloved hand tightens a bolt on the panel.", "Bolt tightening", "partial"),
        ("Worker seen from behind near the switchboard.", "Switchboard", "partial"),
        ("Silhouette of a person against the furnace glow.", "Furnace", "partial"),
        ("Technician at the panel, face not visible.", "Control panel", "partial"),
        ("Engineer smiling at the camera in a hard hat.", "Engineer", "recognizable"),
        ("Portrait of a welder with a visible face.", "Welder", "recognizable"),
        ("An engineer inspects the control cabinet.", "Engineer inspecting cabinet", "recognizable"),
        ("A person stands near the conveyor.", "Conveyor line", "unclear"),
    ],
)
def test_people_risk_levels(description, subject, level):
    assert rg.people_risk(people(description, subject))["level"] == level


def test_no_people_is_none():
    assert rg.people_risk(vision())["level"] == "none"


def test_technical_phrases_are_not_people_markers():
    v = people("A person next to a robotic arm and hand tools.", subject="Robotic arm")

    assert rg.people_risk(v)["level"] == "unclear"


def test_face_marker_beats_hand_marker():
    v = people("Worker's hands on the valve, face clearly visible.", subject="Valve")

    assert rg.people_risk(v)["level"] == "recognizable"


def test_partial_people_do_not_block_auto_approval():
    m = gated(v=people("A gloved hand tightens a bolt on the panel.", "Bolt tightening"))

    assert m["state"] == "auto_approved"
    assert "PEOPLE_PARTIAL" in note_codes(m)


@pytest.mark.parametrize(
    ("description", "subject"),
    [
        ("Engineer smiling at the camera.", "Engineer"),
        ("Portrait of a welder with a visible face.", "Welder"),
        ("An engineer inspects the control cabinet.", "Engineer inspecting cabinet"),
    ],
)
def test_recognizable_people_require_review(description, subject):
    m = gated(v=people(description, subject))

    assert m["state"] == "human_review"
    assert "PEOPLE_RECOGNIZABLE" in reason_codes(m)


def test_incidental_worker_in_industrial_frame_does_not_block():
    # Рабочий случайно попал в промышленный кадр: не главный объект, лица в описании нет.
    m = gated(v=people("A person stands near the conveyor line.", "Conveyor line", count=1))

    assert m["state"] == "auto_approved"
    assert "PEOPLE_INCIDENTAL" in note_codes(m)
    assert not [code for code in reason_codes(m) if code.startswith("PEOPLE")]


# --- юридические утверждения ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["ISO 9001 certified factory", "CE marked equipment", "Guaranteed quality", "Meets EU safety standards"],
)
def test_legal_claims_in_metadata_require_review(text):
    m = gated(s=suggestion(description=f"{text} on a concrete wall."))

    assert "LEGAL_CLAIM" in reason_codes(m)


def test_general_safety_concept_is_not_legal_claim():
    m = gated(s=suggestion(keywords=["industrial safety", "electrical box", *[f"concept{chr(97 + i)}" for i in range(26)]]))

    assert m["state"] == "auto_approved"


# --- правка → validation → gate -------------------------------------------------------


def test_edit_result_is_decided_by_gate_not_by_edit():
    v = vision()
    m = gated(s=suggestion(description="Junction box in Berlin."))
    assert m["state"] == "human_review"

    edited, _ = mb.edit(m, v, {"description": "Junction box on a concrete wall."})
    assert edited["state"] == "draft"  # правка сама по себе не даёт auto_approved

    regated, _ = rg.apply_gate(edited, v)
    assert regated["state"] == "auto_approved"


def test_edit_introducing_risk_goes_to_review():
    v = vision()
    edited, _ = mb.edit(gated(), v, {"description": "ISO 9001 certified junction box."})
    regated, _ = rg.apply_gate(edited, v)

    assert regated["state"] == "human_review"
    assert "LEGAL_CLAIM" in reason_codes(regated)


# --- эскалация ---------------------------------------------------------------------------


def test_escalation_is_sticky_until_human_decision():
    v = vision()
    escalated = rg.escalate(gated(), v, "Looks like a staged photo")

    assert escalated["state"] == "human_review"
    assert "MANUAL_ESCALATION" in reason_codes(escalated)

    edited, _ = mb.edit(escalated, v, {"title": "Another junction box title"})
    regated, _ = rg.apply_gate(edited, v)
    assert regated["state"] == "human_review"

    approved, _ = mb.approve(rg.clear_escalation(regated), v)
    assert approved["state"] == "approved"
    assert approved["review_gate"]["escalation"] is None


def test_escalation_overrides_deferred_partial():
    v = vision(keywords=[f"kw{chr(97 + i)}" for i in range(10)])
    partial, _ = rg.apply_gate(mb.build_draft(v, None, vision_source=VISION_SOURCE), v)

    assert rg.escalate(partial, v, "check")["state"] == "human_review"


def test_escalate_requires_reason_and_gateable_state():
    v = vision()
    with pytest.raises(mb.MetadataTransitionError):
        rg.escalate(gated(), v, "  ")

    approved, _ = mb.approve(draft(), v)
    with pytest.raises(mb.MetadataTransitionError):
        rg.escalate(approved, v, "late")


# --- решения человека после gate ------------------------------------------------------------


def test_human_can_approve_or_reject_after_gate():
    v = vision()
    auto = gated()
    review = gated(v=vision(brands=["Siemens"]), s=suggestion())

    assert mb.approve(auto, v)[0]["state"] == "approved"
    assert mb.approve(review, vision(brands=["Siemens"]))[0]["state"] == "approved"
    assert mb.reject(auto, "not needed")["state"] == "rejected"
    assert mb.reject(review, "trademark")["state"] == "rejected"
