"""
QC EDGE_BORDER — чёрные полосы по краям (паспорт §35ZZZB).

Фиксированные окна: outer — линии 0–7, inner — 14–21; колонка полосная, если outer <= 25 и
inner - outer >= 30; сторона — доля полосных >= 50 %; ширина — по скачку mean, не больше 32 px.
Найденная полоса — предупреждение QC и существующий metadata.escalate (к человеку), не reject.
"""

import json

import numpy as np
import pytest
from PIL import Image

from app import asset_state, qc, reprocess, worker
from app.service import dispatch

from app.textnorm import normalize_text

from tests.conftest import events

REASON = normalize_text("EDGE_BORDER: чёрные полосы — top 11 px")  # как хранит metadata.escalate
from tests.test_publication import SRGB_ICC, StockVision

SIDES = ("top", "bottom", "left", "right")


def frame(band: int = 0, side: str = "top", covered: float = 1.0, base: float = 150.0, size=(320, 240), seed=0):
    """Текстура ~base±20; на стороне side — полоса ширины band на доле covered колонок (по центру)."""
    rng = np.random.default_rng(seed)
    a = np.clip(rng.normal(base, 20, (size[1], size[0])), 0, 255)
    if band:
        view = {"top": a, "bottom": a[::-1], "left": a.T, "right": a[:, ::-1].T}[side]
        n = view.shape[1]
        lo, hi = int(n * (1 - covered) / 2), int(n * (1 + covered) / 2)
        view[:band, lo:hi] = rng.integers(0, 3, (band, hi - lo))
    return Image.fromarray(a.astype(np.uint8)).convert("RGB")


# --- Критерий -------------------------------------------------------------------------------


@pytest.mark.parametrize("side", SIDES)
def test_band_on_each_side_is_found(side):
    assert qc.edge_border(frame(11, side)) == [{"side": side, "width": 11, "share": 1.0}]


def test_partial_band_like_prod_9_is_found():
    """prod #9: полоса по середине, у краёв кадра слабее — доля ~81 %."""
    (found,) = qc.edge_border(frame(11, "top", covered=0.8))
    assert found["side"] == "top" and found["width"] == 11 and 0.78 <= found["share"] <= 0.82


def test_band_on_less_than_half_of_the_edge_is_not_a_border():
    assert qc.edge_border(frame(11, "top", covered=0.4)) == []


def test_clean_frame_has_no_border():
    assert qc.edge_border(frame()) == []


def test_dark_frame_without_jump_is_not_a_border():
    assert qc.edge_border(frame(base=12)) == []
    rng = np.random.default_rng(3)
    gradient = np.clip(np.linspace(5, 120, 240)[:, None] + rng.normal(0, 3, (240, 320)), 0, 255)  # темнеет к верху
    assert qc.edge_border(Image.fromarray(gradient.astype(np.uint8)).convert("RGB")) == []


def test_12_px_band_width_is_measured():
    assert qc.edge_border(frame(12, "left")) == [{"side": "left", "width": 12, "share": 1.0}]


def test_1_px_band_is_below_the_outer_window():
    """Граница метода: 1 px растворяется в среднем по 8 внешним линиям — не находится."""
    assert qc.edge_border(frame(1, "top")) == []


def test_30_px_band_is_beyond_the_inner_window():
    """Граница метода: полоса 30 px накрывает inner (линии 14–21) — скачка в окнах нет, не находится."""
    assert qc.edge_border(frame(30, "top")) == []


def test_max_width_limits_what_counts_as_a_border(monkeypatch):
    monkeypatch.setattr(qc, "EDGE_MAX_WIDTH", 10)  # шире предела — сюжет
    assert qc.edge_border(frame(11, "top")) == []


def test_rules_include_edge_border_parameters():
    rules = qc.rules()
    assert rules["version"] == "qc-rules-v3"
    assert {k: v for k, v in rules.items() if k.startswith("edge_")} == {
        "edge_outer": [0, 8], "edge_inner": [14, 22], "edge_dark": 25, "edge_diff": 30, "edge_share": 0.5,
        "edge_max_width": 32, "edge_profile_lines": 64}


# --- QC и эскалация ---------------------------------------------------------------------------


def banded_asset(root, band=11, side="top", name="photo.jpg") -> int:
    path = root / "data" / "incoming" / name
    frame(band, side).save(path, "JPEG", quality=95, icc_profile=SRGB_ICC)
    return worker.process_file(path, analyzer=StockVision())


def gate_events(root, asset_id):
    return [(s, st, json.loads(m)) for s, st, m in events(root, asset_id) if s == "METADATA" and st == "ESCALATED"]


def test_qc_warns_and_worker_escalates_to_human(stocker_root):
    asset_id = banded_asset(stocker_root)
    state = asset_state.get(asset_id)
    assert state["state"] == asset_state.HUMAN_REVIEW and state["reasons"] == ["MANUAL_ESCALATION"]
    (escalation,) = gate_events(stocker_root, asset_id)
    assert escalation[2]["reason"] == REASON
    assert state["stages"]["readiness"]["status"] == asset_state.NOT_APPLICABLE  # на площадки не ушёл


def test_qc_result_records_the_warning_and_stays_passed(stocker_root):
    from app.database.db import get_asset

    asset_id = banded_asset(stocker_root)
    result = json.loads(get_asset(asset_id)["qc_result"])
    assert result["passed"] is True and "EDGE_BORDER" in result["warnings"]
    assert result["edge_border"] == [{"side": "top", "width": 11, "share": 1.0}]


def test_clean_asset_is_not_escalated(stocker_root):
    path = stocker_root / "data" / "incoming" / "clean.jpg"
    frame().save(path, "JPEG", quality=95, icc_profile=SRGB_ICC)
    asset_id = worker.process_file(path, analyzer=StockVision())
    assert gate_events(stocker_root, asset_id) == []
    assert asset_state.get(asset_id)["state"] != asset_state.HUMAN_REVIEW


def test_escalation_is_not_repeated(stocker_root):
    asset_id = banded_asset(stocker_root)
    assert worker.run_edge_escalation(asset_id) is None
    assert len(gate_events(stocker_root, asset_id)) == 1


def test_human_decision_is_not_touched(stocker_root, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(qc, "edge_border", lambda image: [])  # первый прогон — полос «не видно»
        asset_id = banded_asset(stocker_root)
    assert dispatch("metadata.approve", {"asset_id": asset_id}, actor="human")["ok"]
    monkeypatch.setattr(qc, "QC_RULES_VERSION", "qc-rules-test")  # QC устарел
    result = reprocess.run(asset_id, dry_run=False, analyzer=StockVision())
    assert result["escalated"] is None and gate_events(stocker_root, asset_id) == []
    assert asset_state.get(asset_id)["metadata_state"] == "approved"


def test_reprocess_from_qc_escalates_existing_metadata(stocker_root, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(qc, "edge_border", lambda image: [])  # до v3: полосы не искались
        asset_id = banded_asset(stocker_root)
    assert gate_events(stocker_root, asset_id) == []
    monkeypatch.setattr(qc, "QC_RULES_VERSION", "qc-rules-test")
    assert asset_state.get(asset_id)["reprocess_from"] == "qc"

    result = reprocess.run(asset_id, dry_run=False, analyzer=StockVision())
    assert normalize_text(result["escalated"]) == REASON
    assert result["state_after"] == asset_state.HUMAN_REVIEW
    assert [s for s, st, _ in events(stocker_root, asset_id)].count("REPROCESS") == 1
