"""
gate-v1.3: надпись с именем собственным — brand / legal, даже если Vision не заполнил brands
(паспорт §35ZZE; реальный случай #8: одна и та же фотография, brands то заполнен, то пуст).
"""

import copy
import json
import sqlite3

import pytest
from PIL import ImageCms

from app import asset_state, reprocess, worker
from app import review_gate as rg
from app.ai.schema import AIAnalysis
from app.service import dispatch

from tests.conftest import FakeAnalyzer, events, make_image
from tests.test_service import VISION

TEXT_8 = ["Вектор Технологий", "Устройство контроля загрузки лифта", "УК3-ВТ", "POWER SUPPLY UNIT", "MODEL P220",
          "INPUT:175-300V AC 0.05A 50/60Hz"]


def category(text, brands=()):
    item = rg.classify_text_item(text, AIAnalysis(brands=list(brands)))
    return item["category"], item["rule"]


# --- Пять случаев из постановки ------------------------------------------------------------


def test_brands_filled_is_brand():
    assert category("Вектор Технологий", brands=["Вектор Технологий"]) == ("brand_or_legal", "BRAND")
    assert category("MODEL P220", brands=["P220"]) == ("brand_or_legal", "BRAND")


@pytest.mark.parametrize("text, rule", [
    ("Вектор Технологий", "PROPER_NAME"),
    ("Schneider Electric", "PROPER_NAME"),
    ("Mitsubishi-Electric Corporation", "PROPER_NAME"),
    ("STMicroelectronics", "MIXED_CASE_NAME"),
    ("iPhone", "MIXED_CASE_NAME"),
])
def test_brands_empty_but_explicit_brand_name_in_text(text, rule):
    assert category(text) == ("brand_or_legal", rule)


@pytest.mark.parametrize("text", ["Устройство контроля загрузки лифта", "POWER SUPPLY UNIT", "ЗАМОК ДВЕРИ ШАХТЫ",
                                  "QR code", "Сделано в России", "Барнаул", "ON", "OFF", "ENTER"])
def test_ordinary_text_is_not_a_brand(text):
    assert category(text)[0] == "descriptive"


def test_model_number_without_known_brand_is_technical():
    assert category("MODEL P220") == ("technical", "DIGITS")
    assert category("УК3-ВТ") == ("technical", "DIGITS")


@pytest.mark.parametrize("text, expected", [
    ("220V", ("technical", "DIGITS")), ("INPUT:175-300V AC 0.05A 50/60Hz", ("technical", "DIGITS")),
    ("IP20", ("technical", "DIGITS")), ("CE", ("technical", "CONFORMITY_MARK")), ("EAC", ("technical", "CONFORMITY_MARK")),
    ("kWh", ("descriptive", "DEFAULT")), ("Emergency Exit", ("technical", "WARNING_WORD")),
    ("Высокое Напряжение", ("technical", "WARNING_WORD")),
])
def test_technical_markings_and_warnings_are_not_brands(text, expected):
    assert category(text) == expected


def test_non_empty_text_alone_does_not_send_to_human():
    vision = AIAnalysis(text_visible=["POWER SUPPLY UNIT", "220V", "CE", "EAC", "MODEL P220", "ON", "OFF"])
    items = [rg.classify_text_item(t, vision) for t in vision.text_visible]
    assert not [i for i in items if i["category"] == "brand_or_legal"]


# --- Регрессия #8: решение не зависит от того, заполнил ли модель brands -----------------------


def _metadata(root):
    asset_id = worker.process_file(make_image(root), analyzer=FakeAnalyzer())
    from app.database.db import get_asset

    return json.loads(get_asset(asset_id)["metadata_json"])


@pytest.mark.parametrize("brands", [["Вектор Технологий", "P220"], []])
def test_case_8_decision_is_stable_whatever_brands_says(stocker_root, brands):
    metadata = _metadata(stocker_root)
    vision = AIAnalysis(subject="Elevator load control unit", title="Elevator Load Control System",
                        keywords=["elevator", "control unit"], text_visible=TEXT_8, brands=brands)
    gate = rg.evaluate(metadata, vision, auto_approve_enabled=True)
    assert gate["decision"] == rg.HUMAN_REVIEW
    assert ("TEXT_BRAND_OR_LEGAL", "Вектор Технологий") in [(r["code"], r["detail"]) for r in gate["reasons"]]


# --- Смена политики: устаревание и дешёвый повторный gate --------------------------------------


def test_policy_change_is_stale_and_regated_without_metadata_ai(stocker_root, monkeypatch):
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()

    class StockVision(FakeAnalyzer):
        def analyze(self, view):
            return VISION

    asset_id = worker.process_file(make_image(stocker_root, icc_profile=srgb), analyzer=StockVision())
    monkeypatch.setattr(rg, "POLICY_VERSION", "gate-v1.4-test")
    stage = asset_state.get(asset_id)["stages"]["metadata"]
    assert stage["status"] == "stale" and stage["reason"] == "GATE_POLICY_CHANGED"

    before = events(stocker_root, asset_id)
    result = reprocess.run(asset_id, dry_run=False)
    assert result["reprocess_from"] == "metadata"
    added = [(s, st) for s, st, _ in events(stocker_root, asset_id)[len(before):]]
    assert ("METADATA_AI", "PASSED") not in added and ("METADATA", "GATED") in added  # только gate, модель не вызывалась
    assert asset_state.get(asset_id)["stages"]["metadata"]["status"] == "current"
