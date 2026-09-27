"""
AnalysisView (INTERNAL_IMAGE_REPRESENTATION_CONTRACT §14): единственный вход пикселей
для QC, Enhancement, Vision (и Creative Review); Readiness пиксели не читает.
"""

import re
from pathlib import Path

import pytest
from PIL import Image

from app import analysis_view, normalization, normalizer, worker
from app.ingest import ingest_file, sha256_file
from app.service import dispatch
from scripts.check_consistency import find_orphans, find_problems

from tests.conftest import FakeAnalyzer, events, make_image
from tests.fixtures.icc import matrix_profile

ROOT = Path(__file__).resolve().parents[1]
P3_ICC = matrix_profile()


def _asset(root, **kwargs) -> int:
    asset_id = ingest_file(make_image(root, **kwargs))
    normalization.run_asset(asset_id)
    return asset_id


# --- Построение view ----------------------------------------------------------------


def test_view_requires_passed_normalization(stocker_root):
    asset_id = ingest_file(make_image(stocker_root))
    with pytest.raises(analysis_view.ViewUnavailable) as refused:
        analysis_view.open_asset_view(asset_id)
    assert refused.value.code == "NORMALIZE_NOT_PASSED"


def test_stale_normalization_gives_no_view(stocker_root, monkeypatch):
    asset_id = _asset(stocker_root)
    monkeypatch.setattr(normalizer, "PARAMS_HASH", "sha256:other")
    with pytest.raises(analysis_view.ViewUnavailable) as refused:
        analysis_view.open_asset_view(asset_id)
    assert refused.value.code == "NORMALIZE_STALE"


def test_changed_representation_gives_no_view_and_no_fallback(stocker_root):
    asset_id = _asset(stocker_root)
    make_image(stocker_root, seed=9)  # тот же путь, другие пиксели
    with pytest.raises(analysis_view.ViewUnavailable) as refused:
        analysis_view.open_asset_view(asset_id)
    assert refused.value.code == "REPRESENTATION_INVALID"


def test_view_is_canonical(stocker_root):
    asset_id = _asset(stocker_root, size=(3000, 1000), icc_profile=P3_ICC)
    view = analysis_view.open_asset_view(asset_id)

    assert view.full.mode == "RGB" and view.full.info == {}
    assert view.color_space == "srgb" and view.color_converted
    assert view.size == (3000, 1000) and view.source_format == "JPEG"
    assert view.variant("preview").size == (2048, 683) and view.variant("overview").size == (1536, 512)
    assert view.variant("preview") is view.variant("preview")  # один раз на view
    identity = view.identity()
    assert identity["fingerprint"] == normalizer.view_fingerprint(sha256_file(stocker_root / "data/incoming/photo.jpg"))
    assert identity["view_version"] == "analysis-view-v1" and identity["size"] == [3000, 1000]


def test_small_image_is_never_upscaled(stocker_root):
    view = analysis_view.open_asset_view(_asset(stocker_root))
    assert view.variant("preview").size == view.variant("overview").size == view.size == (64, 48)


def test_undeclared_view_is_not_converted(stocker_root):
    view = analysis_view.open_asset_view(_asset(stocker_root))
    assert view.color_space == "undeclared" and not view.color_converted


# --- Один view на все стадии ----------------------------------------------------------


def test_worker_gives_every_pixel_stage_the_same_view(stocker_root, monkeypatch):
    seen = {}
    real_qc, real_assess = worker.check_asset, worker.enhancement_decision.assess_asset

    def qc_spy(asset, view):
        seen["qc"] = view
        return real_qc(asset, view)

    def assess_spy(asset_id, view=None):
        seen["enhancement"] = view
        return real_assess(asset_id, view=view)

    class VisionSpy(FakeAnalyzer):
        def analyze(self, view):
            seen["vision"] = view
            return super().analyze(view)

    monkeypatch.setattr(worker, "check_asset", qc_spy)
    monkeypatch.setattr(worker.enhancement_decision, "assess_asset", assess_spy)
    opened = []
    real_open = analysis_view.open_asset_view
    monkeypatch.setattr(analysis_view, "open_asset_view", lambda asset_id: opened.append(asset_id) or real_open(asset_id))

    worker.process_file(make_image(stocker_root), analyzer=VisionSpy())

    assert len(opened) == 1
    assert seen["qc"] is seen["enhancement"] is seen["vision"]
    assert isinstance(seen["qc"], analysis_view.AnalysisView)


def test_stage_events_record_the_view(stocker_root):
    import json

    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())
    messages = {(s, st): json.loads(m) for s, st, m in events(stocker_root, asset_id) if m.startswith("{")}
    fingerprints = {
        messages[("AI", "PASSED")]["view"]["fingerprint"],
        messages[("ENHANCEMENT", "ASSESSED")]["view"]["fingerprint"],
        messages[("QC", "PASSED")]["metrics"]["view"]["fingerprint"],
    }
    assert len(fingerprints) == 1


def test_worker_stops_without_view(stocker_root, monkeypatch):
    def unavailable(asset_id):
        raise analysis_view.ViewUnavailable("REPRESENTATION_INVALID", "gone")

    monkeypatch.setattr(analysis_view, "open_asset_view", unavailable)
    asset_id, outcome = worker.ingest_and_process(make_image(stocker_root), analyzer=FakeAnalyzer())
    assert outcome == worker.VIEW_UNAVAILABLE
    stages = [(s, st) for s, st, _ in events(stocker_root, asset_id)]
    assert ("VIEW", "FAILED") in stages and not [s for s in stages if s[0] in ("QC", "AI", "ENHANCEMENT")]


# --- AVIF через весь pipeline ---------------------------------------------------------


def _avif(path: Path, orientation: int = 6) -> Path:
    image = Image.new("RGB", (60, 40), (200, 100, 50))
    exif = Image.Exif()
    exif[0x0112] = orientation
    image.save(path, "AVIF", exif=exif.tobytes(), icc_profile=P3_ICC)
    return path


def test_avif_goes_through_the_pipeline_on_its_derivative(stocker_root):
    path = _avif(stocker_root / "data" / "incoming" / "photo.avif")
    before = path.read_bytes()

    asset_id, outcome = worker.ingest_and_process(path, analyzer=FakeAnalyzer())

    assert outcome == worker.AI_PASSED
    manifest = normalization.get(asset_id)["manifest"]
    assert manifest["representation"] == "derivative" and manifest["color"]["icc_is_source"]
    view = analysis_view.open_asset_view(asset_id)
    assert view.representation == "derivative" and view.size == (40, 60)  # ориентация применена один раз
    assert view.color_space == "srgb" and view.color_converted
    assert path.read_bytes() == before
    db = stocker_root / "data" / "db" / "stocker.db"
    assert not any(find_problems(db).values()) and find_orphans(db) == []

    # Повторная нормализация — без новых событий и файлов.
    assert normalization.run_asset(asset_id)["outcome"] == normalization.UNCHANGED


def test_view_of_unregistered_file_matches_asset_view(stocker_root):
    path = _avif(stocker_root / "data" / "incoming" / "photo.avif")
    loose = analysis_view.view_of_file(path)
    asset_id = ingest_file(path)
    normalization.run_asset(asset_id)
    registered = analysis_view.open_asset_view(asset_id)
    assert loose.full.tobytes() == registered.full.tobytes() and loose.fingerprint == registered.fingerprint


def test_heic_is_still_not_ingested(stocker_root):
    path = stocker_root / "data" / "incoming" / "photo.heic"
    path.write_bytes(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64)
    assert ingest_file(path) is None


# --- Архитектурная граница ------------------------------------------------------------

# Кто вправе декодировать файл изображения: Normalization (representation и views),
# факты источника (заголовки, metadata) и ingest (размеры при регистрации).
DECODERS = {"normalizer.py", "source_facts.py", "ingest.py"}


def test_no_stage_opens_image_files_itself():
    pattern = re.compile(r"Image\.open\(|exif_transpose|ImageCms\.profileToProfile")
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "app").rglob("*.py")
        if path.name not in DECODERS and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"pixel access outside AnalysisView: {offenders}"


def test_readiness_does_not_use_analysis_view():
    for name in ("readiness.py", "stock_readiness.py"):
        text = (ROOT / "app" / name).read_text(encoding="utf-8")
        assert "analysis_view" not in text and "Image.open" not in text


def test_readiness_via_service_needs_no_view(stocker_root, monkeypatch):
    def forbidden(asset_id):
        raise AssertionError("Readiness must not open a view")

    monkeypatch.setattr(analysis_view, "open_asset_view", forbidden)
    asset_id = _asset(stocker_root)
    assert dispatch("readiness.evaluate", {"asset_id": asset_id})["error"] is None
