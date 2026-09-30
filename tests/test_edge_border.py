"""
QC EDGE_BORDER — чёрные полосы по краям (паспорт §35ZZZB, qc-rules-v4 — §35ZZZC).

По каждой колонке: r — тёмный (<= 25) прогон от края; outer = mean линий 0…min(r, 8), inner =
mean линий r+2…r+9; полосная, если outer <= 25 и inner - outer >= 20. Ширина — медиана r,
согласованная доля (|r - w| <= max(3, w / 4)) >= 0.40; прогон шире 3 % кадра — сюжет.
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
from tests.test_publication import SRGB_ICC, StockVision

REASON = normalize_text("EDGE_BORDER: чёрные полосы — top 11 px")  # как хранит metadata.escalate
SIDES = ("top", "bottom", "left", "right")
SIZE = (2400, 2200)  # предел 3 %: 66 px сверху / снизу, 72 px слева / справа


def frame(band: int = 0, side: str = "top", covered: float = 1.0, base: float = 150.0, size=SIZE, seed=0):
    """Текстура ~base±20; на стороне side — полоса ширины band на доле covered колонок (по центру)."""
    rng = np.random.default_rng(seed)
    a = np.clip(rng.normal(base, 20, (size[1], size[0])), 0, 255)
    if band:
        view = {"top": a, "bottom": a[::-1], "left": a.T, "right": a[:, ::-1].T}[side]
        n = view.shape[1]
        lo, hi = int(n * (1 - covered) / 2), int(n * (1 + covered) / 2)
        view[:band, lo:hi] = rng.integers(0, 3, (band, hi - lo))
    return Image.fromarray(a.astype(np.uint8)).convert("RGB")


def gradient_shadow(size=SIZE, depth=300, seed=3):
    """Тень у левого края (как prod #4 слева): яркость плавно растёт от 5 до 120 на depth px, без скачка."""
    rng = np.random.default_rng(seed)
    a = np.full((size[1], size[0]), 150.0)
    a[:, :depth] = np.linspace(5, 120, depth)[None, :]
    a = np.clip(a + rng.normal(0, 3, a.shape), 0, 255)
    return Image.fromarray(a.astype(np.uint8)).convert("RGB")


# --- Критерий -------------------------------------------------------------------------------


@pytest.mark.parametrize("side", SIDES)
def test_band_on_each_side_is_found(side):
    assert qc.edge_border(frame(11, side)) == [{"side": side, "width": 11, "share": 1.0}]


def test_partial_band_like_prod_9_is_found():
    """prod #9: полоса по середине, у краёв кадра слабее — доля ~80 %."""
    (found,) = qc.edge_border(frame(11, "top", covered=0.8))
    assert found["side"] == "top" and found["width"] == 11 and 0.78 <= found["share"] <= 0.82


def test_band_on_less_than_40_percent_of_the_edge_is_not_a_border():
    assert qc.edge_border(frame(11, "top", covered=0.3)) == []


def test_clean_frame_has_no_border():
    assert qc.edge_border(frame()) == []


def test_dark_frame_without_jump_is_not_a_border():
    assert qc.edge_border(frame(base=12)) == []


def test_shadow_gradient_like_prod_4_left_is_not_a_border():
    assert qc.edge_border(gradient_shadow()) == []


@pytest.mark.parametrize("band", [12, 30, 60])
def test_wide_bands_are_found_with_their_width(band):
    """Фиксированные окна v3 не находили полосы шире ~20 px (prod #4 низ ~60 px); v4 — находит."""
    assert qc.edge_border(frame(band, "bottom")) == [{"side": "bottom", "width": band, "share": 1.0}]


def test_1_px_band_is_found():
    """Граница метода (результат v4): outer — линия 0, inner — линии 3–10; полоса 1 px находится."""
    assert qc.edge_border(frame(1, "top")) == [{"side": "top", "width": 1, "share": 1.0}]


def test_dark_run_wider_than_3_percent_is_subject():
    assert qc.edge_border(frame(66, "top")) == []  # 3 % от 2200 = 66: прогон >= предела — сюжет
    assert qc.edge_border(frame(65, "top")) == [{"side": "top", "width": 65, "share": 1.0}]


def test_rules_include_edge_border_parameters():
    rules = qc.rules()
    assert rules["version"] == "qc-rules-v4"
    assert {k: v for k, v in rules.items() if k.startswith("edge_")} == {
        "edge_dark": 25, "edge_diff": 20, "edge_share": 0.4, "edge_outer_lines": 8, "edge_inner_offset": 2,
        "edge_inner_lines": 8, "edge_consistency_px": 3, "edge_consistency_ratio": 0.25, "edge_max_width_ratio": 0.03}


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


def test_existing_edge_escalation_is_kept_without_duplicate(stocker_root, monkeypatch):
    asset_id = banded_asset(stocker_root)
    monkeypatch.setattr(qc, "edge_border", lambda image: [{"side": "top", "width": 12, "share": 0.9}])
    monkeypatch.setattr(qc, "QC_RULES_VERSION", "qc-rules-test")  # новая версия меряет иначе
    result = reprocess.run(asset_id, dry_run=False, analyzer=StockVision())
    assert result["escalated"] is None and len(gate_events(stocker_root, asset_id)) == 1


def test_other_manual_escalation_is_not_overwritten(stocker_root, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(qc, "edge_border", lambda image: [])
        asset_id = banded_asset(stocker_root)
    dispatch("metadata.escalate", {"asset_id": asset_id, "reason": "brand check"})
    monkeypatch.setattr(qc, "QC_RULES_VERSION", "qc-rules-test")
    reprocess.run(asset_id, dry_run=False, analyzer=StockVision())
    last = gate_events(stocker_root, asset_id)[-1][2]["reason"]
    assert last == "brand check; " + REASON


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
