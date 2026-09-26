import copy
import io
from pathlib import Path

import pytest
from PIL import Image, ImageCms

from app import metadata_builder as mb
from app import readiness as rd
from app import review_gate as rg
from app.ai.schema import AIAnalysis, MetadataSuggestion, PeopleInfo

ROOT = Path(__file__).resolve().parents[1]


def vision(**overrides) -> AIAnalysis:
    data = {
        "title": "Electrical junction box with wires",
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


def metadata_for(v: AIAnalysis, s=None, state=None) -> dict:
    draft = mb.build_draft(v, s or suggestion(), vision_source={"event_id": 17})
    gated, _ = rg.apply_gate(draft, v)
    if state:
        gated["state"] = state
    return gated


def facts(**overrides) -> dict:
    data = {
        "file_hash": "abc",
        "source": "ok",
        "qc_passed": True,
        "format": "JPEG",
        "width": 8192,
        "height": 6144,
        "file_size": 14 * rd.MB,
        "color_profile": rd.SRGB,
        "color_profile_description": "sRGB built-in",
    }
    return {**data, **overrides}


def run(v=None, metadata=None, f=None, platforms=None) -> dict:
    v = v or vision()
    return rd.evaluate(f or facts(), metadata or metadata_for(v), v, platforms)


def codes(result: dict, platform: str) -> dict:
    return {check["code"]: check["level"] for check in result["platforms"][platform]["checks"]}


# --- Чистый случай ---------------------------------------------------------------

def test_clean_industrial_asset_is_ready_for_both_platforms():
    result = run()

    assert result["readiness_version"] == "readiness-v1"
    assert result["ready_for"] == ["adobe", "shutterstock"]

    adobe = result["platforms"]["adobe"]
    assert adobe["status"] == rd.READY
    assert adobe["profile"] == "adobe-2026-09"
    assert adobe["export_plan"]["title"] == "Electrical junction box with wiring"
    assert adobe["export_plan"]["category"] == "Industry"
    assert adobe["export_plan"]["file"] == {"from": "original", "operations": []}

    shutterstock = result["platforms"]["shutterstock"]["export_plan"]
    assert shutterstock["description"] == "Electrical junction box with wires on a concrete wall."
    assert shutterstock["categories"][0] == "Industrial"
    assert shutterstock["keywords"] == metadata_for(vision())["fields"]["keywords"]


def test_evaluate_does_not_mutate_inputs_and_is_deterministic():
    v = vision()
    metadata = metadata_for(v)
    f = facts()
    before = (copy.deepcopy(metadata), copy.deepcopy(f))

    first = rd.evaluate(f, metadata, v)
    second = rd.evaluate(f, metadata, v)

    assert (metadata, f) == before
    first.pop("evaluated_at"), second.pop("evaluated_at")
    assert first == second


def test_unknown_platform_is_rejected():
    with pytest.raises(ValueError):
        run(platforms=["getty"])


# --- Metadata не готова ----------------------------------------------------------

@pytest.mark.parametrize("state", ["draft", "human_review", "rejected"])
def test_not_evaluated_unless_metadata_auto_approved_or_approved(state):
    result = run(metadata=metadata_for(vision(), state=state))

    for platform in ("adobe", "shutterstock"):
        assert result["platforms"][platform]["status"] == rd.NOT_EVALUATED
        assert codes(result, platform) == {"METADATA_NOT_READY": rd.BLOCKER}
    assert result["ready_for"] == []


def test_not_evaluated_without_vision():
    result = rd.evaluate(facts(), metadata_for(vision()), None)
    assert result["platforms"]["adobe"]["status"] == rd.NOT_EVALUATED


def test_partial_metadata_approved_by_human_is_still_blocked():
    v = vision()
    metadata = mb.build_draft(v, None, vision_source={"event_id": 17})
    metadata["state"] = mb.APPROVED

    result = run(v, metadata)

    assert metadata["completeness"] == mb.PARTIAL
    assert codes(result, "adobe")["METADATA_PARTIAL"] == rd.BLOCKER
    assert result["platforms"]["adobe"]["export_plan"] is None


# --- Файл: original → derivative ---------------------------------------------------

def test_display_p3_needs_srgb_derivative_but_stays_ready():
    result = run(f=facts(color_profile=rd.OTHER, color_profile_description="sRGB EOTF with DCI-P3 Color Gamut"))

    for platform in ("adobe", "shutterstock"):
        assert codes(result, platform)["COLOR_PROFILE_CONVERSION"] == rd.DERIVATIVE
        assert result["platforms"][platform]["status"] == rd.READY
        assert result["platforms"][platform]["export_plan"]["file"] == {"from": "original", "operations": ["to_srgb"]}


def test_missing_profile_is_warning():
    result = run(f=facts(color_profile=rd.MISSING, color_profile_description=None))
    assert codes(result, "adobe")["COLOR_PROFILE_MISSING"] == rd.WARNING
    assert result["platforms"]["adobe"]["export_plan"]["file"]["operations"] == []


def test_low_resolution_blocks_both():
    result = run(f=facts(width=1800, height=1200))
    assert codes(result, "adobe")["RESOLUTION_TOO_LOW"] == rd.BLOCKER
    assert codes(result, "shutterstock")["RESOLUTION_TOO_LOW"] == rd.BLOCKER
    assert result["ready_for"] == []


def test_over_100_mp_downscales_for_adobe_only():
    result = run(f=facts(width=12000, height=9000))
    assert result["platforms"]["adobe"]["export_plan"]["file"]["operations"] == ["downscale_to_mp:100"]
    assert "RESOLUTION_TOO_HIGH" not in codes(result, "shutterstock")


def test_tiff_converted_for_adobe_accepted_by_shutterstock():
    result = run(f=facts(format="TIFF"))
    assert result["platforms"]["adobe"]["export_plan"]["file"]["operations"] == ["to_jpeg"]
    assert result["platforms"]["shutterstock"]["export_plan"]["file"]["operations"] == []


def test_file_size_between_platform_limits():
    result = run(f=facts(file_size=48 * rd.MB))
    assert result["platforms"]["adobe"]["export_plan"]["file"]["operations"] == [f"fit_file_size:{45 * rd.MB}"]
    assert "FILE_TOO_LARGE" not in codes(result, "shutterstock")


@pytest.mark.parametrize("override,code", [({"source": "changed"}, "SOURCE_NOT_OK"), ({"qc_passed": False}, "QC_NOT_PASSED")])
def test_source_and_qc_block(override, code):
    result = run(f=facts(**override))
    assert codes(result, "adobe")[code] == rd.BLOCKER


# --- Metadata площадки -------------------------------------------------------------

def test_keyword_ranges_differ_by_platform():
    v = vision()
    metadata = metadata_for(v, state=mb.APPROVED)
    metadata["fields"]["keywords"] = ["electrical box", "wiring", "junction box", "cable", "industrial"]
    result = run(v, metadata)

    assert codes(result, "shutterstock")["KEYWORDS_TOO_FEW"] == rd.BLOCKER
    assert "KEYWORDS_TOO_FEW" not in codes(result, "adobe")


def test_fifty_keywords_too_many_for_adobe_only():
    v = vision()
    metadata = metadata_for(v)
    metadata["fields"]["keywords"] = ["wiring", *[f"term{i}" for i in range(49)]]

    result = run(v, metadata)

    assert codes(result, "adobe")["KEYWORDS_TOO_MANY"] == rd.BLOCKER
    assert "KEYWORDS_TOO_MANY" not in codes(result, "shutterstock")


def test_long_adobe_title_is_warning_and_short_description_is_warning():
    v = vision()
    metadata = metadata_for(v)
    metadata["fields"]["title"] = "Electrical junction box with wires " * 3
    metadata["fields"]["description"] = "Junction box."

    result = run(v, metadata)

    assert codes(result, "adobe")["TITLE_LONG"] == rd.WARNING
    assert codes(result, "shutterstock")["DESCRIPTION_NOT_SENTENCE"] == rd.WARNING
    assert result["ready_for"] == ["adobe", "shutterstock"]


def test_empty_description_falls_back_to_title_for_shutterstock():
    v = vision()
    metadata = metadata_for(v)
    metadata["fields"]["description"] = ""

    plan = run(v, metadata)["platforms"]["shutterstock"]["export_plan"]

    assert plan["description"] == metadata["fields"]["title"]


def test_text_too_long_blocks_without_truncation():
    v = vision()
    metadata = metadata_for(v)
    metadata["fields"]["title"] = "x" * 201

    result = run(v, metadata)

    assert codes(result, "adobe")["TEXT_TOO_LONG"] == rd.BLOCKER


# --- Категории -----------------------------------------------------------------

def test_playground_maps_to_leisure_categories():
    v = vision(
        title="Wooden playground structure with rope net",
        subject="Playground equipment",
        description="A wooden playground in a park.",
        categories=["Outdoor", "Recreation"],
    )
    s = suggestion(title="Wooden playground with rope netting in urban park",
                   description="Wooden playground with rope netting in an urban park.",
                   keywords=["playground", "park", "rope net", *[f"concept{chr(97 + i)}" for i in range(20)]])
    result = run(v, metadata_for(v, s))

    assert result["platforms"]["adobe"]["export_plan"]["category"] == "Hobbies and leisure"
    assert result["platforms"]["shutterstock"]["export_plan"]["categories"][0] == "Parks/Outdoor"


def test_unmapped_category_warning_for_adobe_blocker_for_shutterstock():
    v = vision(title="Abstract shape", subject="Abstract shape", description="An abstract shape.", keywords=["shape"])
    s = suggestion(title="Abstract shape", description="An abstract shape in soft light.",
                   keywords=["shape", *[f"concept{chr(97 + i)}" for i in range(20)]])
    result = run(v, metadata_for(v, s))

    assert codes(result, "adobe")["CATEGORY_UNMAPPED"] == rd.WARNING
    assert result["platforms"]["adobe"]["export_plan"]["category"] is None
    assert codes(result, "shutterstock")["CATEGORY_UNMAPPED"] == rd.BLOCKER
    assert result["ready_for"] == ["adobe"]


def test_second_shutterstock_category_needs_weight():
    v = vision(subject="Electrical junction box on a building facade")
    scores = rd.category_scores(v, {"keywords": ["wiring"]})

    assert [s["group"] for s in scores][:2] == ["industry", "buildings"]
    assert rd.map_categories(rd.SHUTTERSTOCK, scores) == ["Industrial", "Buildings/Landmarks"]
    assert rd.map_categories(rd.ADOBE, scores) == ["Industry"]


def test_minor_second_category_is_dropped():
    # asset 6: «concrete wall» в keywords не делает распределительную коробку архитектурой
    scores = [{"group": "industry", "score": 29, "order": 0}, {"group": "buildings", "score": 2, "order": 1}]
    assert rd.map_categories(rd.SHUTTERSTOCK, scores) == ["Industrial"]

    scores = [{"group": "industry", "score": 30, "order": 0}, {"group": "buildings", "score": 12, "order": 1}]
    assert rd.map_categories(rd.SHUTTERSTOCK, scores) == ["Industrial", "Buildings/Landmarks"]


# --- Бренды (§3.3a) ------------------------------------------------------------

def test_manufacturer_marking_is_incidental_info():
    # asset 5: юрлицо на замке, главный объект — механизм
    v = vision(
        title="Mechanical door locking mechanism",
        subject="Mechanical door locking system",
        description="A mechanical door lock on a brick wall.",
        text_visible=['АО "ШПЗ"', "ЗАМОК ДВЕРИ ШАХТЫ", "0411Е.06.05.090"],
    )
    result = run(v, metadata_for(v, state=mb.APPROVED))

    assert [b["prominence"] for b in rd.brand_presence(v)] == [rd.INCIDENTAL]
    assert codes(result, "adobe")["INCIDENTAL_MARKING"] == rd.INFO
    assert result["ready_for"] == ["adobe", "shutterstock"]


def test_brand_as_main_subject_is_blocker():
    v = vision(brands=["Siemens"], subject="Siemens control panel", title="Siemens control panel")
    result = run(v, metadata_for(v, state=mb.APPROVED))

    assert codes(result, "adobe")["DOMINANT_BRAND"] == rd.BLOCKER
    assert result["ready_for"] == []


def test_logo_subject_makes_brand_dominant():
    v = vision(logos=["Acme"], subject="Company logo on a wall")
    assert rd.brand_presence(v)[0]["prominence"] == rd.DOMINANT


def test_brand_on_equipment_is_component_warning():
    v = vision(brands=["Siemens"], subject="Electrical junction box")
    result = run(v, metadata_for(v, state=mb.APPROVED))

    assert rd.brand_presence(v) == [{"term": "Siemens", "source": "brand", "prominence": rd.COMPONENT}]
    assert codes(result, "adobe")["COMPONENT_BRAND"] == rd.WARNING
    assert result["ready_for"] == ["adobe", "shutterstock"]


def test_brand_in_metadata_blocks_even_when_component():
    v = vision(brands=["Siemens"])
    metadata = metadata_for(v, state=mb.APPROVED)
    metadata["fields"]["keywords"] = [*metadata["fields"]["keywords"][:-1], "siemens"]

    result = run(v, metadata)

    assert codes(result, "adobe")["TRADEMARK_IN_METADATA"] == rd.BLOCKER
    assert codes(result, "adobe")["COMPONENT_BRAND"] == rd.WARNING


# --- Люди, editorial, AI ----------------------------------------------------------

def test_recognizable_people_need_model_release():
    v = vision(people=PeopleInfo(present=True, count=1), subject="Electrician", description="An electrician smiling at the camera.")
    result = run(v, metadata_for(v, state=mb.APPROVED))
    assert codes(result, "adobe")["MODEL_RELEASE_REQUIRED"] == rd.BLOCKER


def test_incidental_people_are_warning_only():
    v = vision(people=PeopleInfo(present=True, count=1))
    result = run(v, metadata_for(v))
    assert codes(result, "adobe")["PEOPLE_NOT_RECOGNIZABLE"] == rd.WARNING
    assert result["ready_for"] == ["adobe", "shutterstock"]


@pytest.mark.parametrize("override,code", [({"editorial_risk": ["landmark"]}, "EDITORIAL_ONLY"), ({"ai_generated": True}, "AI_GENERATED")])
def test_editorial_and_ai_generated_block(override, code):
    v = vision(**override)
    result = run(v, metadata_for(v, state=mb.APPROVED))
    assert codes(result, "shutterstock")[code] == rd.BLOCKER


# --- Fingerprint и stale -------------------------------------------------------

def test_fingerprint_changes_with_inputs_and_marks_stale():
    v = vision()
    metadata = metadata_for(v)
    result = run(v, metadata)
    platforms = ["adobe", "shutterstock"]

    assert rd.current_status(result, rd.fingerprint(facts(), metadata, platforms))["stale"] is False

    edited = copy.deepcopy(metadata)
    edited["fields"]["title"] = "Another title for the junction box"
    status = rd.current_status(result, rd.fingerprint(facts(), edited, platforms))
    assert status == {"evaluated": True, "stale": True, "platforms": {"adobe": rd.STALE, "shutterstock": rd.STALE}, "ready_for": []}

    assert rd.fingerprint(facts(file_hash="other"), metadata, platforms) != result["fingerprint"]
    empty = rd.current_status(None, "x")
    assert empty["evaluated"] is False
    assert empty["platforms"] == {"adobe": rd.NOT_EVALUATED, "shutterstock": rd.NOT_EVALUATED}


# --- Цветовой профиль файла ---------------------------------------------------------

def _srgb_icc() -> bytes:
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def test_color_profile_kind_by_primaries():
    assert rd.color_profile_kind(None) == (rd.MISSING, None)
    assert rd.color_profile_kind(b"not a profile")[0] == rd.OTHER
    assert rd.color_profile_kind(_srgb_icc())[0] == rd.SRGB


def test_read_file_facts_on_generated_jpeg(tmp_path):
    path = tmp_path / "img.jpg"
    Image.new("RGB", (64, 48), "gray").save(path, "JPEG", icc_profile=_srgb_icc())

    result = rd.read_file_facts(path)

    assert result["format"] == "JPEG"
    assert (result["width"], result["height"]) == (64, 48)
    assert result["file_size"] == path.stat().st_size
    assert result["color_profile"] == rd.SRGB


_PHONE_PHOTO = ROOT / "data" / "incoming" / "IMG_20260911_130437.jpg"


@pytest.mark.skipif(not _PHONE_PHOTO.exists(), reason="real phone photo is not available")
def test_real_phone_photo_is_display_p3_not_srgb():
    with Image.open(_PHONE_PHOTO) as image:
        kind, description = rd.color_profile_kind(image.info.get("icc_profile"))
    assert "sRGB" in description  # описание вводит в заблуждение
    assert kind == rd.OTHER
