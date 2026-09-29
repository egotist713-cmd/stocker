"""
Export preparation export-v1 (docs/EXPORT_PREPARATION_CONTRACT.md, паспорт §35ZZW).

Файл площадки строится из AnalysisView по актуальным Readiness ready + Publication approved;
metadata — только белый список профиля, собранный заново; повтор с теми же входами — UNCHANGED.
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

from tests.conftest import events, make_image
from tests.fixtures.export_metadata_contamination import build_contaminated_jpeg
from tests.test_publication import ready_asset


@pytest.fixture(autouse=True)
def small_images_allowed(monkeypatch):
    """Тестовые изображения 64×48: минимум MP снижен и в Readiness, и в профиле экспорта."""
    monkeypatch.setattr(rd, "PROFILES", {n: dataclasses.replace(p, min_mp=0.001) for n, p in rd.PROFILES.items()})
    monkeypatch.setattr(ep, "PROFILES", {"adobe": dataclasses.replace(ep.ADOBE, min_mp=0.001)})


def approved_asset(root, seed=0) -> int:
    asset_id = ready_asset(root, seed=seed)
    assert dispatch("publication.evaluate", {"asset_id": asset_id})["data"]["publication"]["approved_for"] == ["adobe", "shutterstock"]
    return asset_id


def stage_events(root, asset_id, stage):
    return [(st, json.loads(m)) for s, st, m in events(root, asset_id) if s == stage]


def set_profile(monkeypatch, **changes):
    monkeypatch.setattr(ep, "PROFILES", {"adobe": dataclasses.replace(ep.PROFILES["adobe"], **changes)})


# --- Профиль adobe-2026-09 ------------------------------------------------------------------


def test_profile_is_the_readiness_profile_plus_file_parameters():
    profile, readiness = ep.ADOBE, rd.ADOBE
    assert profile.version == readiness.version == "adobe-2026-09"
    assert (profile.min_mp, profile.max_mp, profile.max_file_size) == (readiness.min_mp, readiness.max_mp, readiness.max_file_size)
    assert (profile.min_mp, profile.max_mp, profile.max_file_size) == (4.0, 100.0, 45 * 1024 * 1024)
    assert (profile.jpeg_quality, profile.jpeg_quality_min, profile.format) == (95, 90, "JPEG")
    assert (profile.keywords_min, profile.keywords_max, profile.title_max, profile.title_recommended) == (7, 49, 200, 70)
    assert profile.filename_max == 30 and profile.text_field == "title"
    # Только ICC и XMP title / keywords: ни description, ни автора, ни digitalSourceType.
    assert profile.metadata_whitelist == {"segment:APP2:ICC", "segment:APP1:XMP", "xmp:dc:title", "xmp:dc:subject"}
    assert all(source["checked_at"] for source in profile.sources)


# --- Белый список (§4.5): inventory(export) ⊆ whitelist -----------------------------------------


def test_export_contains_only_platform_whitelist(tmp_path):
    contaminated = build_contaminated_jpeg(tmp_path / "contaminated.jpg")
    source_icc = Image.open(contaminated).info["icc_profile"]
    for profile in ep.PROFILES.values():
        exported = ep.prepare_file(contaminated, profile, tmp_path)
        leftovers = ep.inventory(exported) - profile.metadata_whitelist
        assert leftovers == set(), f"{profile.name}: source metadata leaked into export: {sorted(leftovers)}"
        icc = Image.open(exported).info["icc_profile"]
        assert icc == ep.SRGB_ICC and icc != source_icc  # ICC назначен профилем, не скопирован


def test_mutation_extra_field_is_caught(tmp_path, monkeypatch):
    """Мутация: кодировщик «пропускает» лишнее поле (EXIF источника) — проверка шага 8 обязана упасть."""
    contaminated = build_contaminated_jpeg(tmp_path / "contaminated.jpg")
    source_exif = Image.open(contaminated).getexif()
    original_save = Image.Image.save

    def leaky_save(self, fp, format=None, **params):
        if format == "JPEG":
            params["exif"] = source_exif
        return original_save(self, fp, format, **params)

    monkeypatch.setattr(Image.Image, "save", leaky_save)
    with pytest.raises(ep.ExportRefused) as refused:
        ep.prepare_file(contaminated, ep.PROFILES["adobe"], tmp_path)
    assert refused.value.code == ep.METADATA_NOT_CLEAN
    assert "exif:Software" in refused.value.details["metadata_audit"]["not_allowed"]


def test_inventory_sees_xmp_attributes_not_only_elements(tmp_path):
    path = tmp_path / "attr.jpg"
    xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="r"><rdf:Description xmp:CreatorTool="Topaz"/></rdf:RDF></x:xmpmeta>'
    Image.new("RGB", (8, 8)).save(path, "JPEG", xmp=xmp)
    assert "xmp:xmp:CreatorTool" in ep.inventory(path)


def test_orientation_is_applied_to_pixels_and_not_written(tmp_path):
    """EXIF Orientation 6 (поворот на 90°): файл площадки уже повёрнут, тега ориентации нет."""
    from app.source_facts import read_facts

    path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (64, 48), (10, 120, 200)).save(path, "JPEG", exif=exif, icc_profile=ep.SRGB_ICC)
    exported = ep.prepare_file(path, ep.PROFILES["adobe"], tmp_path)
    facts = read_facts(exported)
    assert (facts["width"], facts["height"]) == (48, 64) and facts["orientation_raw"] is None
    assert "exif:Orientation" not in ep.inventory(exported)


def test_color_operation_records_what_happened_to_pixels():
    """to_srgb — пиксели конвертированы view (Display P3 → sRGB); assign_icc — source уже sRGB."""
    image = Image.new("RGB", (64, 48))
    _, converted = ep.render(image, ep.PROFILES["adobe"], "T", ["a"] * 7, color_converted=True)
    _, assigned = ep.render(image, ep.PROFILES["adobe"], "T", ["a"] * 7, color_converted=False)
    assert converted["operations"][0] == "to_srgb" and assigned["operations"][0] == "assign_icc:srgb"


def test_undeclared_color_is_refused(tmp_path):
    path = tmp_path / "undeclared.jpg"
    Image.new("RGB", (64, 48), (10, 20, 30)).save(path, "JPEG")
    with pytest.raises(ep.ExportRefused) as refused:
        ep.prepare_file(path, ep.PROFILES["adobe"], tmp_path)
    assert refused.value.code == ep.COLOR_SPACE_UNDECLARED


# --- Имя файла (§3.1) --------------------------------------------------------------------


def test_filename_from_title_with_asset_suffix_and_limit():
    profile = ep.ADOBE
    name = ep.export_filename("Close-up of Industrial Circuit Breaker with Copper Busbars", 1234, profile)
    assert name == "close_up_industrial_1234.jpg" and len(name) <= profile.filename_max
    noisy = ep.export_filename("IMG_001 Topaz AI final2 photo of Control Panel v2 edit", 7, profile)
    assert noisy == "control_panel_7.jpg" and ep.filename_problems(noisy, 7, profile) == []
    assert ep.export_filename("Щит управления", 12, profile) == "shchit_upravleniya_12.jpg"
    assert ep.export_filename("Lift PXL 20260927 143522847", 5, profile) == "lift_5.jpg"
    assert ep.export_filename("Photo", 9, profile) == "untitled_9.jpg"
    assert ep.filename_problems("IMG_1234_topaz_9.jpg", 9, profile)


# --- Пиксели: размер и качество -------------------------------------------------------------


def test_downscale_only_above_maximum_and_never_upscale():
    profile = dataclasses.replace(ep.ADOBE, min_mp=0.001, max_mp=0.002)
    image = Image.new("RGB", (64, 48), (200, 100, 50))
    data, info = ep.render(image, profile, "Title", ["a"] * 7)
    assert info["width"] * info["height"] <= 2000 and "downscale_to_mp:0.002" in info["operations"]
    with pytest.raises(ep.ExportRefused) as refused:
        ep.render(image, dataclasses.replace(ep.ADOBE, min_mp=1.0), "Title", ["a"] * 7)
    assert refused.value.code == ep.RESOLUTION_TOO_LOW


def test_fit_file_size_lowers_quality_down_to_profile_minimum():
    import numpy as np

    noise = Image.fromarray(np.random.default_rng(1).integers(0, 256, (240, 320, 3), dtype=np.uint8))
    full, _ = ep.render(noise, dataclasses.replace(ep.ADOBE, min_mp=0.001), "T", ["a"] * 7)
    at_min, _ = ep.render(noise, dataclasses.replace(ep.ADOBE, min_mp=0.001, jpeg_quality=90), "T", ["a"] * 7)
    profile = dataclasses.replace(ep.ADOBE, min_mp=0.001, max_file_size=(len(full) + len(at_min)) // 2)
    _, info = ep.render(noise, profile, "T", ["a"] * 7)
    assert 90 <= info["quality"] < 95
    with pytest.raises(ep.ExportRefused) as refused:
        ep.render(noise, dataclasses.replace(profile, max_file_size=len(at_min) - 1), "T", ["a"] * 7)
    assert refused.value.code == ep.FILE_TOO_LARGE


# --- export.prepare / export.get -------------------------------------------------------------


def test_prepare_creates_verified_file_and_ready_for_export(stocker_root):
    asset_id = approved_asset(stocker_root)
    result = dispatch("export.prepare", {"asset_id": asset_id, "platform": "adobe"})
    assert result["ok"] and result["outcome"] == ep.CREATED
    created = result["data"]["export"]

    path = stocker_root / created["path"]
    assert path.parent == db.export_dir() / "adobe" / str(asset_id)
    assert path.name == created["filename"] and path.name.endswith(f"_{asset_id}.jpg")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == created["sha256"] and path.stat().st_size == created["size_bytes"]
    assert created["metadata_audit"]["not_allowed"] == [] and set(created["metadata_audit"]["inventory"]) == ep.ADOBE.metadata_whitelist
    from app.database.db import get_asset

    assert created["source_sha256"] == get_asset(asset_id)["file_hash"] and created["source_derivative_id"] is None
    assert created["operations"][0] == "assign_icc:srgb" and "encode_jpeg:q95" in created["operations"]  # source уже sRGB
    assert created["metadata"]["keywords_count"] == len(created["metadata"]["keywords"])
    assert ep.read_xmp_fields(path) == {"title": created["metadata"]["title"], "keywords": created["metadata"]["keywords"]}
    assert [st for st, _ in stage_events(stocker_root, asset_id, "DERIVATIVE")] == ["CREATED"]

    state = asset_state.get(asset_id)
    assert state["state"] == asset_state.READY_FOR_EXPORT and state["ready_for_export"] == ["adobe"]
    got = dispatch("export.get", {"asset_id": asset_id})["data"]
    assert got["status"] == ep.READY_FOR_EXPORT and got["export"]["sha256"] == created["sha256"]
    assert not any(find_problems(db.db_path()).values()) and find_export_orphans(db.db_path()) == []


def test_title_and_keywords_come_from_the_readiness_snapshot(stocker_root):
    asset_id = approved_asset(stocker_root)
    created = ep.prepare(asset_id)["export"]
    readiness = [m for st, m in stage_events(stocker_root, asset_id, "READINESS") if st == "EVALUATED"][-1]
    plan = readiness["platforms"]["adobe"]["export_plan"]
    assert created["metadata"]["title"] == plan["title"] and created["metadata"]["keywords"] == plan["keywords"]


def test_repeat_is_unchanged_and_deterministic(stocker_root):
    asset_id = approved_asset(stocker_root)
    first = ep.prepare(asset_id)["export"]
    before = events(stocker_root, asset_id)

    again = ep.prepare(asset_id)
    assert again["outcome"] == ep.UNCHANGED and events(stocker_root, asset_id) == before

    (stocker_root / first["path"]).unlink()  # файл пропал — пересоздаётся тем же
    assert asset_state.get(asset_id)["stages"]["export"]["status"] == "stale"
    rebuilt = ep.prepare(asset_id)
    assert rebuilt["outcome"] == ep.CREATED and rebuilt["export"]["sha256"] == first["sha256"]


def test_stale_after_metadata_change(stocker_root):
    asset_id = approved_asset(stocker_root)
    ep.prepare(asset_id)
    dispatch("metadata.edit", {"asset_id": asset_id, "add_keywords": ["factory floor"]})
    assert asset_state.get(asset_id)["state"] == asset_state.STALE  # Readiness устарел — объект не ready_for_export
    got = ep.get(asset_id)
    assert got["status"] == ep.EXPORT_STALE and got["export"] is None and got["last_export"]["current"] is False

    result = ep.prepare(asset_id)
    assert result["outcome"] == ep.FAILED and result["refused"]["code"] == ep.STALE
    assert stage_events(stocker_root, asset_id, "EXPORT")[-1][1]["code"] == ep.STALE


def test_not_ready_without_publication_approval(stocker_root):
    asset_id = ready_asset(stocker_root)  # Readiness ready, Publication не оценивался
    result = dispatch("export.prepare", {"asset_id": asset_id})
    assert not result["ok"] and result["outcome"] == ep.FAILED and result["data"]["refused"]["code"] == ep.NOT_READY
    assert [st for st, _ in stage_events(stocker_root, asset_id, "EXPORT")] == ["FAILED"]
    assert stage_events(stocker_root, asset_id, "DERIVATIVE") == []


def test_source_changed_is_refused(stocker_root):
    asset_id = approved_asset(stocker_root)
    path = stocker_root / "data" / "incoming" / "photo.jpg"
    data = bytearray(path.read_bytes())
    data[-3] ^= 0xFF  # тот же размер — ловит только SHA256
    path.write_bytes(bytes(data))
    result = ep.prepare(asset_id)
    assert result["outcome"] == ep.FAILED and result["refused"]["code"] == ep.SOURCE_CHANGED


def test_color_space_undeclared_is_refused_in_prepare(stocker_root, monkeypatch):
    asset_id = approved_asset(stocker_root)
    from app import analysis_view

    real = analysis_view.open_asset_view

    def undeclared(asset):
        view = real(asset)
        view.color_space = "undeclared"
        return view

    monkeypatch.setattr(analysis_view, "open_asset_view", undeclared)
    result = ep.prepare(asset_id)
    assert result["refused"]["code"] == ep.COLOR_SPACE_UNDECLARED


def test_metadata_not_clean_in_prepare_writes_no_derivative(stocker_root, monkeypatch):
    asset_id = approved_asset(stocker_root)
    original_save = Image.Image.save

    def leaky_save(self, fp, format=None, **params):
        if format == "JPEG":
            params["comment"] = b"Processed with Topaz"
        return original_save(self, fp, format, **params)

    monkeypatch.setattr(Image.Image, "save", leaky_save)
    result = ep.prepare(asset_id)
    assert result["refused"]["code"] == ep.METADATA_NOT_CLEAN
    failed = stage_events(stocker_root, asset_id, "EXPORT")[-1][1]
    assert failed["metadata_audit"]["not_allowed"] == ["segment:COM"]
    assert stage_events(stocker_root, asset_id, "DERIVATIVE") == []
    assert asset_state.get(asset_id)["state"] == asset_state.PUBLICATION_APPROVED
    assert [p for p in db.export_dir().rglob("*") if p.is_file()] == []  # непроверенный файл не остался


def test_title_limit_is_a_refusal_and_recommendation_a_warning(stocker_root, monkeypatch):
    asset_id = approved_asset(stocker_root)
    title = ep._readiness_plan(ep._events(asset_id), "adobe")[1]["title"]

    set_profile(monkeypatch, title_max=len(title) - 1)
    assert ep.prepare(asset_id)["refused"]["code"] == ep.TITLE_TOO_LONG_FOR_PROFILE

    set_profile(monkeypatch, title_max=200, title_recommended=len(title) - 1)
    created = ep.prepare(asset_id)["export"]
    assert [w["code"] for w in created["warnings"]] == ["TITLE_LONG"]
    assert created["metadata"]["title"] == title  # не обрезан


@pytest.mark.parametrize("bounds", [{"keywords_min": 100}, {"keywords_max": 3}])
def test_keywords_count_outside_profile_is_refused(stocker_root, monkeypatch, bounds):
    asset_id = approved_asset(stocker_root)
    set_profile(monkeypatch, **bounds)
    assert ep.prepare(asset_id)["refused"]["code"] == ep.KEYWORDS_OUT_OF_RANGE


def test_unknown_platform_and_rights(stocker_root):
    asset_id = approved_asset(stocker_root)
    assert dispatch("export.prepare", {"asset_id": asset_id, "platform": "shutterstock"})["error"]["code"] == "UNKNOWN_PLATFORM"
    for actor in ("agent:openclaw", "workflow:n8n"):
        for operation in ("export.prepare", "export.get"):
            envelope = dispatch(operation, {"asset_id": asset_id}, actor=actor)
            assert not envelope["ok"] and envelope["error"] is not None
    assert stage_events(stocker_root, asset_id, "DERIVATIVE") == []


def test_consistency_detects_missing_export_file(stocker_root):
    asset_id = approved_asset(stocker_root)
    created = ep.prepare(asset_id)["export"]
    (stocker_root / created["path"]).write_bytes(b"changed")
    assert find_problems(db.db_path())["DERIVATIVE/CREATED export file missing or changed"] == [asset_id]


def test_export_orphan_file_is_reported(stocker_root):
    orphan = db.export_dir() / "adobe" / "99" / "x_99.jpg"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"x")
    assert find_export_orphans(db.db_path()) == ["data/export/adobe/99/x_99.jpg"]


def test_undeclared_source_never_reaches_export(stocker_root):
    """Readiness блокирует необъявленный цвет — Publication не применим, экспорт — NOT_READY."""
    from app import worker
    from tests.test_publication import StockVision

    asset_id = worker.process_file(make_image(stocker_root, icc_profile=None), analyzer=StockVision())
    assert ep.prepare(asset_id)["refused"]["code"] == ep.NOT_READY
