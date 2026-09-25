import json
import sqlite3
from pathlib import Path

from PIL import Image

from app import worker
from app.database.db import get_asset
from app.ingest import ingest_file, source_file
from scripts import normalize_source_paths

from tests.conftest import FakeAnalyzer, events, make_image

# Так source_path записывался из Windows до нормализации.
LEGACY_PATH = r"data\incoming\photo.jpg"


def _set_source_path(root, asset_id, value):
    with sqlite3.connect(root / "data" / "db" / "stocker.db") as connection:
        connection.execute("UPDATE assets SET source_path = ? WHERE id = ?", (value, asset_id))


def test_ingest_stores_posix_source_path(stocker_root):
    asset_id = ingest_file(make_image(stocker_root))

    assert get_asset(asset_id)["source_path"] == "data/incoming/photo.jpg"


def test_relative_path_is_accepted(stocker_root, monkeypatch):
    make_image(stocker_root)
    monkeypatch.chdir(stocker_root)

    asset_id = ingest_file(Path("data") / "incoming" / "photo.jpg")

    assert get_asset(asset_id)["source_path"] == "data/incoming/photo.jpg"


def test_file_outside_project_is_skipped(stocker_root, tmp_path_factory):
    outside = make_image(stocker_root)
    target = tmp_path_factory.mktemp("elsewhere") / "photo.jpg"
    outside.rename(target)

    assert ingest_file(target) is None


def test_legacy_windows_source_path_is_resolved(stocker_root):
    asset_id = ingest_file(make_image(stocker_root))
    _set_source_path(stocker_root, asset_id, LEGACY_PATH)

    assert source_file(get_asset(asset_id)) == stocker_root / "data" / "incoming" / "photo.jpg"
    assert worker.process_asset(asset_id, analyzer=FakeAnalyzer()) == worker.AI_PASSED


def test_tiff_passes_ingest_qc_and_ai(stocker_root):
    path = stocker_root / "data" / "incoming" / "scan.tif"
    Image.new("RGB", (64, 48), "gray").save(path, compression="tiff_lzw")

    asset_id = worker.process_file(path, analyzer=FakeAnalyzer())

    assert [(s, st) for s, st, _ in events(stocker_root, asset_id)] == [
        ("INGEST", "DONE"),
        ("QC", "PASSED"),
        ("AI", "PASSED"),
        ("METADATA_AI", "PASSED"),
        ("METADATA", "DRAFTED"),
    ]


def test_normalize_script_dry_run_changes_nothing(stocker_root):
    asset_id = ingest_file(make_image(stocker_root))
    _set_source_path(stocker_root, asset_id, LEGACY_PATH)

    normalize_source_paths.main([])

    assert get_asset(asset_id)["source_path"] == LEGACY_PATH


def test_normalize_script_apply_records_event_and_is_idempotent(stocker_root):
    asset_id = ingest_file(make_image(stocker_root))
    _set_source_path(stocker_root, asset_id, LEGACY_PATH)

    normalize_source_paths.main(["--apply"])
    normalize_source_paths.main(["--apply"])

    assert get_asset(asset_id)["source_path"] == "data/incoming/photo.jpg"
    normalized = [m for s, st, m in events(stocker_root, asset_id) if (s, st) == ("SOURCE", "NORMALIZED")]
    assert [json.loads(m) for m in normalized] == [
        {"old": LEGACY_PATH, "new": "data/incoming/photo.jpg"}
    ]
