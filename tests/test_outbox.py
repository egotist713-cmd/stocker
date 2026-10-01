"""
export.collect — сбор файлов к загрузке в плоские папки партий (паспорт §35ZZZM).
"""

import csv
import dataclasses
import hashlib
import json

import pytest

from app import export_preparation as ep
from app import outbox
from app import readiness as rd
from app.database import db
from app.service import dispatch

from app import worker

from tests.conftest import events, make_image
from tests.test_publication import SRGB_ICC, StockVision


@pytest.fixture(autouse=True)
def small_images_allowed(monkeypatch):
    monkeypatch.setattr(rd, "PROFILES", {n: dataclasses.replace(p, min_mp=0.001) for n, p in rd.PROFILES.items()})
    monkeypatch.setattr(ep, "PROFILES", {n: dataclasses.replace(p, min_mp=0.001) for n, p in ep.PROFILES.items()})


def exported(root, seed, platforms=("adobe",)) -> int:
    path = make_image(root, name=f"photo{seed}.jpg", seed=seed, icc_profile=SRGB_ICC)
    asset_id = worker.process_file(path, analyzer=StockVision())
    dispatch("publication.evaluate", {"asset_id": asset_id})
    for platform in platforms:
        assert ep.prepare(asset_id, platform)["outcome"] == ep.CREATED
    return asset_id


def collect(platform="adobe", actor="human"):
    return dispatch("export.collect", {"platform": platform}, actor=actor)


def collected_events(root, asset_id):
    return [json.loads(m) for s, st, m in events(root, asset_id) if (s, st) == ("OUTBOX", "COLLECTED")]


def test_collect_copies_current_files_with_manifest(stocker_root):
    ids = [exported(stocker_root, seed) for seed in (1, 2)]
    result = collect()
    assert result["ok"] and result["outcome"] == outbox.COLLECTED
    data = result["data"]
    assert data["batch"] == 1 and data["folder"].startswith("data/publish/adobe/001_")
    folder = stocker_root / data["folder"]
    assert sorted(p.name for p in folder.iterdir()) == sorted([f["filename"] for f in data["files"]] + ["manifest.csv"])
    for f in data["files"]:
        assert hashlib.sha256((folder / f["filename"]).read_bytes()).hexdigest() == f["sha256"]
    rows = list(csv.DictReader(open(folder / "manifest.csv", encoding="utf-8")))
    assert [int(r["asset_id"]) for r in rows] == ids and set(rows[0]) == {"asset_id", "filename", "title", "words", "sha256"}
    assert all(int(r["words"]) == len(r["title"].split()) for r in rows)
    for asset_id in ids:
        (event,) = collected_events(stocker_root, asset_id)
        assert event["batch"] == 1 and event["platform"] == "adobe" and event["folder"] == data["folder"]
    assert ep.get(ids[0])["collected"]["batch"] == 1


def test_repeat_collects_only_new_files_into_next_batch(stocker_root):
    first = exported(stocker_root, 1)
    assert collect()["data"]["batch"] == 1
    again = collect()
    assert again["ok"] and again["outcome"] == outbox.NOTHING_TO_COLLECT and again["data"]["folder"] is None
    second = exported(stocker_root, 2)
    data = collect()["data"]
    assert data["batch"] == 2 and data["folder"].startswith("data/publish/adobe/002_")
    assert [f["asset_id"] for f in data["files"]] == [second]
    assert len(collected_events(stocker_root, first)) == 1


def test_platforms_are_numbered_separately(stocker_root):
    exported(stocker_root, 1, platforms=("adobe", "shutterstock"))
    assert collect("adobe")["data"]["batch"] == 1
    data = collect("shutterstock")["data"]
    assert data["batch"] == 1 and data["folder"].startswith("data/publish/shutterstock/001_")
    rows = list(csv.DictReader(open(stocker_root / data["manifest"], encoding="utf-8")))
    assert "description" in rows[0]


def test_rejected_and_stale_are_not_collected(stocker_root):
    rejected = exported(stocker_root, 1)
    stale = exported(stocker_root, 2)
    good = exported(stocker_root, 3)
    dispatch("metadata.reject", {"asset_id": rejected, "reason": "no"}, actor="human")
    dispatch("metadata.edit", {"asset_id": stale, "add_keywords": ["factory floor"]})  # Readiness устарел → экспорт stale
    data = collect()["data"]
    assert [f["asset_id"] for f in data["files"]] == [good]
    assert [s["asset_id"] for s in data["skipped"]] == [stale]  # отклонённый — молча
    assert collected_events(stocker_root, rejected) == [] and collected_events(stocker_root, stale) == []


def test_changed_export_file_is_skipped(stocker_root):
    asset_id = exported(stocker_root, 1)
    (stocker_root / ep.get(asset_id)["export"]["path"]).write_bytes(b"x")
    result = collect()["data"]
    assert result["outcome"] == outbox.NOTHING_TO_COLLECT  # файл не прошёл бы проверку — экспорт уже stale
    assert collected_events(stocker_root, asset_id) == []


def test_only_human_can_collect(stocker_root):
    exported(stocker_root, 1)
    for actor in ("agent:openclaw", "workflow:n8n"):
        envelope = collect(actor=actor)
        assert not envelope["ok"] and envelope["error"]["code"] == "FORBIDDEN"
    assert not (db.data_dir() / "publish").exists()


def test_power_loss_before_events_leaves_folder_and_next_batch_skips_its_number(stocker_root, monkeypatch):
    asset_id = exported(stocker_root, 1)

    def crash(*args, **kwargs):
        raise RuntimeError("power loss")

    with monkeypatch.context() as m:
        m.setattr(outbox, "insert_event", crash)
        with pytest.raises(RuntimeError):
            outbox.collect("adobe")
    assert collected_events(stocker_root, asset_id) == []  # событий нет — объект не считается собранным
    data = outbox.collect("adobe")
    assert data["batch"] == 2 and [f["asset_id"] for f in data["files"]] == [asset_id]


def test_unknown_platform(stocker_root):
    assert collect("istock")["error"]["code"] == "UNKNOWN_PLATFORM"
