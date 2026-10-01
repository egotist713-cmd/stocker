"""
Export preparation: профиль shutterstock-2026-09 (EXPORT_PREPARATION_CONTRACT §5, паспорт §35ZZZG).

Текстовое поле площадки — description (XMP dc:description) ≤ 150 символов, keywords 7–50;
в файл — только ICC и XMP dc:description / dc:subject; длиннее лимита — отказ без обрезки.
Adobe-профиль и его отпечатки не меняются.
"""

import dataclasses
import hashlib
import json

import pytest
from PIL import Image

from app import asset_state, readiness as rd
from app import export_preparation as ep
from app.database import db
from app.service import dispatch
from scripts.check_consistency import find_export_orphans, find_problems

from tests.conftest import events
from tests.fixtures.export_metadata_contamination import build_contaminated_jpeg
from tests.test_publication import ready_asset


@pytest.fixture(autouse=True)
def small_images_allowed(monkeypatch):
    monkeypatch.setattr(rd, "PROFILES", {n: dataclasses.replace(p, min_mp=0.001) for n, p in rd.PROFILES.items()})
    monkeypatch.setattr(ep, "PROFILES", {n: dataclasses.replace(p, min_mp=0.001) for n, p in ep.PROFILES.items()})


def approved_asset(root, seed=0) -> int:
    asset_id = ready_asset(root, seed=seed)
    assert dispatch("publication.evaluate", {"asset_id": asset_id})["data"]["publication"]["approved_for"] == ["adobe", "shutterstock"]
    return asset_id


def set_profile(monkeypatch, **changes):
    profiles = dict(ep.PROFILES)
    profiles["shutterstock"] = dataclasses.replace(profiles["shutterstock"], **changes)
    monkeypatch.setattr(ep, "PROFILES", profiles)


def derivative_events(root, asset_id, platform):
    return [json.loads(m) for s, st, m in events(root, asset_id)
            if (s, st) == ("DERIVATIVE", "CREATED") and json.loads(m)["platform"] == platform]


# --- Профиль ------------------------------------------------------------------------------


def test_profile_is_the_readiness_profile_plus_file_parameters():
    profile, readiness = ep.SHUTTERSTOCK, rd.SHUTTERSTOCK
    assert profile.version == readiness.version == "shutterstock-2026-09"
    assert (profile.min_mp, profile.max_mp, profile.max_file_size) == (4.0, None, 50 * 1024 * 1024)
    # Портал (пробная загрузка 30.09.2026): лимит 2048 — отказ; 150 (справка) — рекомендация.
    assert (profile.text_field, profile.xmp_text, profile.title_max) == ("description", "dc:description", 2048)
    assert (profile.title_recommended, profile.text_recommended_code) == (150, "DESCRIPTION_LONG_FOR_RECOMMENDATION")
    assert (profile.keywords_min, profile.keywords_max) == (7, 50)
    assert profile.metadata_whitelist == {"segment:APP2:ICC", "segment:APP1:XMP", "xmp:dc:description", "xmp:dc:subject"}
    assert all(source["checked_at"] == "2026-09-30" for source in profile.sources)


def test_adobe_spec_and_xmp_are_unchanged():
    """Новые параметры профиля не входят в отпечаток Adobe: существующие экспорты не становятся stale."""
    spec = ep.ADOBE.spec()
    assert not {"xmp_text", "text_no_commas_warning", "text_min_words"} & set(spec)
    xmp = ep.build_xmp("Title", ["a"]).decode()
    assert "<dc:title>" in xmp and "dc:description" not in xmp
    assert "xmp_text" in ep.SHUTTERSTOCK.spec()


# --- Белый список -------------------------------------------------------------------------


def test_export_contains_only_the_shutterstock_whitelist(tmp_path):
    contaminated = build_contaminated_jpeg(tmp_path / "contaminated.jpg")
    exported = ep.prepare_file(contaminated, ep.PROFILES["shutterstock"], tmp_path, title="A worker at a machine", keywords=["k"] * 7)
    assert ep.inventory(exported) == ep.SHUTTERSTOCK.metadata_whitelist  # dc:title не пишется
    assert ep.read_xmp_fields(exported) == {"description": "A worker at a machine", "keywords": ["k"] * 7}


def test_mutation_title_in_shutterstock_file_is_caught(tmp_path, monkeypatch):
    """Мутация: в файл Shutterstock попадает и dc:title — проверка шага 8 обязана упасть."""
    contaminated = build_contaminated_jpeg(tmp_path / "contaminated.jpg")
    real = ep.build_xmp

    def with_title(text, keywords, text_property="dc:title"):
        packet = real(text, keywords, text_property).decode()
        return packet.replace("<dc:subject>", f"<dc:title><rdf:Alt><rdf:li xml:lang=\"x-default\">{text}</rdf:li></rdf:Alt></dc:title><dc:subject>").encode()

    monkeypatch.setattr(ep, "build_xmp", with_title)
    with pytest.raises(ep.ExportRefused) as refused:
        ep.prepare_file(contaminated, ep.PROFILES["shutterstock"], tmp_path, title="A worker at a machine", keywords=["k"] * 7)
    assert refused.value.code == ep.METADATA_NOT_CLEAN
    assert refused.value.details["metadata_audit"]["not_allowed"] == ["xmp:dc:title"]


# --- export.prepare ----------------------------------------------------------------------


def test_prepare_shutterstock_creates_file_next_to_adobe(stocker_root):
    asset_id = approved_asset(stocker_root)
    adobe = dispatch("export.prepare", {"asset_id": asset_id, "platform": "adobe"})["data"]["export"]
    result = dispatch("export.prepare", {"asset_id": asset_id, "platform": "shutterstock"})
    assert result["ok"] and result["outcome"] == ep.CREATED
    created = result["data"]["export"]

    path = stocker_root / created["path"]
    assert path.parent == db.export_dir() / "shutterstock" / str(asset_id)
    assert path.name.endswith(f"_{asset_id}.jpg") and len(path.name) <= 30
    assert hashlib.sha256(path.read_bytes()).hexdigest() == created["sha256"]
    plan = dispatch("readiness.get", {"asset_id": asset_id})["data"]["result"]["platforms"]["shutterstock"]["export_plan"]
    assert created["metadata"]["description"] == plan["description"] and created["metadata"]["keywords"] == plan["keywords"]
    assert created["metadata"]["category"] == plan["categories"]
    assert created["metadata"]["words"] == len(plan["description"].split())
    assert ep.read_xmp_fields(path) == {"description": plan["description"], "keywords": plan["keywords"]}
    assert set(created["metadata_audit"]["inventory"]) == ep.SHUTTERSTOCK.metadata_whitelist
    assert (stocker_root / adobe["path"]).exists() and adobe["path"] != created["path"]  # Adobe не тронут

    state = asset_state.get(asset_id)
    assert state["state"] == asset_state.READY_FOR_EXPORT and state["ready_for_export"] == ["adobe", "shutterstock"]
    assert dispatch("export.get", {"asset_id": asset_id, "platform": "shutterstock"})["data"]["status"] == ep.READY_FOR_EXPORT
    assert not any(find_problems(db.db_path()).values()) and find_export_orphans(db.db_path()) == []


def test_short_description_is_a_warning(stocker_root):
    asset_id = approved_asset(stocker_root)
    created = ep.prepare(asset_id, "shutterstock")["export"]
    words = len(created["metadata"]["description"].split())
    expected = ["DESCRIPTION_SHORT"] if words < 5 else []
    assert [w["code"] for w in created["warnings"]] == expected


def test_repeat_is_unchanged_and_deterministic(stocker_root):
    asset_id = approved_asset(stocker_root)
    first = ep.prepare(asset_id, "shutterstock")["export"]
    before = events(stocker_root, asset_id)
    assert ep.prepare(asset_id, "shutterstock")["outcome"] == ep.UNCHANGED and events(stocker_root, asset_id) == before
    (stocker_root / first["path"]).unlink()
    rebuilt = ep.prepare(asset_id, "shutterstock")
    assert rebuilt["outcome"] == ep.CREATED and rebuilt["export"]["sha256"] == first["sha256"]
    assert len(derivative_events(stocker_root, asset_id, "shutterstock")) == 2


def test_description_longer_than_profile_is_refused_not_truncated(stocker_root, monkeypatch):
    asset_id = approved_asset(stocker_root)
    description = ep._readiness_plan(ep._events(asset_id), "shutterstock")[1]["description"]
    set_profile(monkeypatch, title_max=len(description) - 1)
    result = ep.prepare(asset_id, "shutterstock")
    assert result["outcome"] == ep.FAILED and result["refused"]["code"] == ep.DESCRIPTION_TOO_LONG_FOR_PROFILE
    got = ep.get(asset_id, "shutterstock")
    assert got["status"] == ep.NOT_PREPARED and got["last_failure"]["code"] == ep.DESCRIPTION_TOO_LONG_FOR_PROFILE
    assert derivative_events(stocker_root, asset_id, "shutterstock") == []
    assert [p for p in (db.export_dir() / "shutterstock").rglob("*") if p.is_file()] == []


def test_description_over_recommendation_is_a_warning_not_a_refusal(stocker_root, monkeypatch):
    asset_id = approved_asset(stocker_root)
    description = ep._readiness_plan(ep._events(asset_id), "shutterstock")[1]["description"]
    set_profile(monkeypatch, title_recommended=len(description) - 1)
    result = ep.prepare(asset_id, "shutterstock")
    assert result["outcome"] == ep.CREATED
    codes = [w["code"] for w in result["export"]["warnings"]]
    assert "DESCRIPTION_LONG_FOR_RECOMMENDATION" in codes
    assert result["export"]["metadata"]["description"] == description  # не обрезано


def test_adobe_keeps_its_title_long_code():
    assert ep.ADOBE.text_recommended_code is None and "text_recommended_code" not in ep.ADOBE.spec()


@pytest.mark.parametrize("bounds", [{"keywords_min": 100}, {"keywords_max": 3}])
def test_keywords_count_outside_profile_is_refused(stocker_root, monkeypatch, bounds):
    asset_id = approved_asset(stocker_root)
    set_profile(monkeypatch, **bounds)
    assert ep.prepare(asset_id, "shutterstock")["refused"]["code"] == ep.KEYWORDS_OUT_OF_RANGE


def test_not_ready_without_publication_approval(stocker_root):
    asset_id = ready_asset(stocker_root)
    assert ep.prepare(asset_id, "shutterstock")["refused"]["code"] == ep.NOT_READY


def test_source_changed_is_refused(stocker_root):
    asset_id = approved_asset(stocker_root)
    path = stocker_root / "data" / "incoming" / "photo.jpg"
    data = bytearray(path.read_bytes())
    data[-3] ^= 0xFF
    path.write_bytes(bytes(data))
    assert ep.prepare(asset_id, "shutterstock")["refused"]["code"] == ep.SOURCE_CHANGED


def test_no_downscale_without_maximum():
    image = Image.new("RGB", (64, 48))
    _, info = ep.render(image, ep.PROFILES["shutterstock"], "A description of five words", ["k"] * 7, color_converted=False)
    assert (info["width"], info["height"]) == (64, 48) and not any(op.startswith("downscale") for op in info["operations"])
