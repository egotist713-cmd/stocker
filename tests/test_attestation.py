"""
Человеческая аттестация людей в кадре (STOCK_READINESS_CONTRACT §3.3b, паспорт §35ZZY).

Решение человека — вход Readiness: снимает только MODEL_RELEASE_REQUIRED (взрослые), пока
source тот же и аттестация не отозвана; отзыв или новая аттестация — Readiness stale.
"""

import dataclasses
import json

import pytest

from app import asset_state, attestation, export_preparation as ep, worker
from app import readiness as rd
from app.ai.schema import PeopleInfo
from app.service import dispatch

from tests.conftest import FakeAnalyzer, events, make_image
from tests.test_metadata_service import VISION
from tests.test_publication import SRGB_ICC

WORKERS = VISION.model_copy(update={"people": PeopleInfo(present=True, count=2), "subject": "workers far away"})
CHILD = VISION.model_copy(update={"people": PeopleInfo(present=True, count=1), "subject": "child in a shaft"})


class PeopleVision(FakeAnalyzer):
    vision = WORKERS

    def analyze(self, view):
        return self.vision


@pytest.fixture(autouse=True)
def small_images_allowed(monkeypatch):
    monkeypatch.setattr(rd, "PROFILES", {n: dataclasses.replace(p, min_mp=0.001) for n, p in rd.PROFILES.items()})
    monkeypatch.setattr(ep, "PROFILES", {"adobe": dataclasses.replace(ep.ADOBE, min_mp=0.001)})


def approved_people_asset(root, vision=WORKERS, seed=0, icc=SRGB_ICC) -> int:
    analyzer = PeopleVision()
    analyzer.vision = vision
    asset_id = worker.process_file(make_image(root, seed=seed, icc_profile=icc), analyzer=analyzer)
    assert asset_state.get(asset_id)["state"] == asset_state.HUMAN_REVIEW  # PEOPLE_RECOGNIZABLE
    assert dispatch("metadata.approve", {"asset_id": asset_id}, actor="human")["ok"]
    dispatch("readiness.evaluate", {"asset_id": asset_id})
    return asset_id


def adobe(asset_id) -> dict:
    return dispatch("readiness.get", {"asset_id": asset_id})["data"]["result"]["platforms"]["adobe"]


def codes(platform: dict, level=None) -> list[str]:
    return [c["code"] for c in platform["checks"] if level is None or c["level"] == level]


def attest(asset_id, kind, note=None, actor="human"):
    return dispatch("asset.attest_people", {"asset_id": asset_id, "kind": kind, **({"note": note} if note else {})}, actor=actor)


# --- Без аттестации — как раньше ----------------------------------------------------------


def test_without_attestation_blocked_as_before(stocker_root):
    asset_id = approved_people_asset(stocker_root)
    assert codes(adobe(asset_id), rd.BLOCKER) == ["MODEL_RELEASE_REQUIRED"]
    assert asset_state.get(asset_id)["state"] == asset_state.BLOCKED


def test_fingerprint_without_attestation_is_unchanged():
    """Ключ появляется только при действующей аттестации: прежние оценки не становятся stale."""
    facts = {"file_hash": "h", "facts_event_id": 1, "source_event_id": None, "qc_passed": True}
    assert rd.fingerprint(facts, {}, ["adobe"]) == rd.fingerprint({**facts, "people_attestation": None}, {}, ["adobe"])
    attested = {**facts, "people_attestation": {"kind": "not_identifiable", "event_id": 7, "file_hash": "h"}}
    assert rd.fingerprint(attested, {}, ["adobe"]) != rd.fingerprint(facts, {}, ["adobe"])


# --- С аттестацией — ready, дальше Publication и Export -----------------------------------------


def test_not_identifiable_makes_ready_and_reaches_export(stocker_root):
    asset_id = approved_people_asset(stocker_root)
    result = attest(asset_id, "not_identifiable", "people from behind, far away")
    assert result["ok"] and result["outcome"] == attestation.ATTESTED
    stored = [json.loads(m) for s, st, m in events(stocker_root, asset_id) if (s, st) == ("HUMAN", "PEOPLE_ATTESTED")]
    assert stored == [{"kind": "not_identifiable", "note": "people from behind, far away",
                       "file_hash": stored[0]["file_hash"], "actor": "human"}]

    state = asset_state.get(asset_id)
    assert state["state"] == asset_state.STALE and state["reprocess_from"] == "readiness"  # вход изменился

    assert dispatch("readiness.evaluate", {"asset_id": asset_id})["outcome"] == "EVALUATED"
    platform = adobe(asset_id)
    assert platform["status"] == rd.READY and "MODEL_RELEASE_REQUIRED" not in codes(platform)
    attested = next(c for c in platform["checks"] if c["code"] == "PEOPLE_ATTESTED")
    assert attested["level"] == rd.INFO and attested["message"].startswith("people: not_identifiable (human)")
    assert platform["export_plan"]["people"] == "people: not_identifiable (human)"

    assert dispatch("publication.evaluate", {"asset_id": asset_id})["data"]["publication"]["approved_for"] == ["adobe", "shutterstock"]
    assert ep.prepare(asset_id)["outcome"] == ep.CREATED
    assert asset_state.get(asset_id)["state"] == asset_state.READY_FOR_EXPORT


def test_release_on_file_requires_note_and_is_reported(stocker_root):
    asset_id = approved_people_asset(stocker_root)
    missing = attest(asset_id, "release_on_file")
    assert not missing["ok"] and missing["error"]["code"] == "NOTE_REQUIRED"
    assert attest(asset_id, "release_on_file", "MR-2026-017")["ok"]
    dispatch("readiness.evaluate", {"asset_id": asset_id})
    platform = adobe(asset_id)
    assert platform["status"] == rd.READY and platform["export_plan"]["people"] == "people: release_on_file (human)"


def test_invalid_kind_is_refused(stocker_root):
    asset_id = approved_people_asset(stocker_root)
    assert attest(asset_id, "maybe")["error"]["code"] == "INVALID_KIND"


def test_revocation_makes_readiness_stale_and_blocked_again(stocker_root):
    asset_id = approved_people_asset(stocker_root)
    attest(asset_id, "not_identifiable")
    dispatch("readiness.evaluate", {"asset_id": asset_id})
    assert adobe(asset_id)["status"] == rd.READY

    assert attest(asset_id, "none")["ok"]
    assert asset_state.get(asset_id)["state"] == asset_state.STALE
    dispatch("readiness.evaluate", {"asset_id": asset_id})
    assert codes(adobe(asset_id), rd.BLOCKER) == ["MODEL_RELEASE_REQUIRED"]


# --- Только человек -----------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["agent:openclaw", "workflow:n8n"])
def test_agent_and_n8n_cannot_attest(stocker_root, actor):
    asset_id = approved_people_asset(stocker_root)
    before = events(stocker_root, asset_id)
    envelope = attest(asset_id, "not_identifiable", actor=actor)
    assert not envelope["ok"] and envelope["error"]["code"] == "FORBIDDEN"
    assert events(stocker_root, asset_id) == before


def test_attest_is_not_an_mcp_tool():
    from app.service import AGENT_FORBIDDEN, WORKFLOW_ALLOWED, mcp_server

    exposed = {operation.name for operation in mcp_server.exposed_operations().values()}
    assert "asset.attest_people" not in exposed and "export.prepare" not in exposed
    assert "asset.attest_people" in AGENT_FORBIDDEN and "asset.attest_people" not in WORKFLOW_ALLOWED


# --- Source и границы -------------------------------------------------------------------


def test_changed_source_is_not_attested_and_attestation_follows_file_hash(stocker_root):
    asset_id = approved_people_asset(stocker_root)
    attest(asset_id, "not_identifiable")
    dispatch("readiness.evaluate", {"asset_id": asset_id})
    assert adobe(asset_id)["status"] == rd.READY

    path = stocker_root / "data" / "incoming" / "photo.jpg"
    data = bytearray(path.read_bytes())
    data[-3] ^= 0xFF
    path.write_bytes(bytes(data))
    assert attest(asset_id, "not_identifiable")["error"]["code"] == "SOURCE_INVALID"
    assert asset_state.get(asset_id, verify_source=True)["state"] == asset_state.SOURCE_INVALID
    assert ep.prepare(asset_id)["refused"]["code"] == ep.SOURCE_CHANGED

    # Аттестация другого файла (иной file_hash) не действует.
    asset = {"file_hash": "other"}
    event = [{"id": 1, "stage": "HUMAN", "status": "PEOPLE_ATTESTED",
              "message": json.dumps({"kind": "not_identifiable", "file_hash": "original"})}]
    assert attestation.current(asset, event) is None


def test_children_are_not_lifted_by_attestation(stocker_root):
    asset_id = approved_people_asset(stocker_root, vision=CHILD)
    attest(asset_id, "not_identifiable")
    dispatch("readiness.evaluate", {"asset_id": asset_id})
    check = next(c for c in adobe(asset_id)["checks"] if c["code"] == "MODEL_RELEASE_REQUIRED")
    assert check["level"] == rd.BLOCKER and "does not apply to children" in check["message"]


def test_other_blockers_are_unchanged(stocker_root):
    asset_id = approved_people_asset(stocker_root, icc=None)  # цвет не объявлен
    attest(asset_id, "not_identifiable")
    dispatch("readiness.evaluate", {"asset_id": asset_id})
    platform = adobe(asset_id)
    assert codes(platform, rd.BLOCKER) == ["COLOR_SPACE_UNDECLARED"] and platform["status"] == rd.BLOCKED
