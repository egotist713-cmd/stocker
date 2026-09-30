"""
Этап B: явные отпечатки входов QC и Vision (docs/ASSET_STATE_CONTRACT.md §2.1a).

Отпечаток — только из входов, от которых результат реально зависит; старые результаты
без отпечатка остаются STALE (история не переписывается).
"""

import json
from types import SimpleNamespace

import pytest
from PIL import ImageCms

from app import analysis_view, asset_state, normalizer, qc, worker
from app.ai import local_analyzer
from app.ai.analyzer import input_fingerprint
from app.ai.local_analyzer import LocalAnalyzer
from app.database.db import get_asset

from tests.conftest import FakeAnalyzer, events, make_image
from tests.test_service import VISION

SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def stub_lmstudio(analyzer: LocalAnalyzer) -> dict:
    """Настоящий LocalAnalyzer, подменён только HTTP-вызов; captured — аргументы запроса."""
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=VISION.model_dump_json()))])

    analyzer.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return captured


@pytest.fixture
def lmstudio_asset(stocker_root, monkeypatch):
    monkeypatch.delenv("LMSTUDIO_MODEL", raising=False)
    analyzer = LocalAnalyzer()
    captured = stub_lmstudio(analyzer)
    asset_id = worker.process_file(make_image(stocker_root, icc_profile=SRGB_ICC), analyzer=analyzer)
    return asset_id, captured


def ai_passed(root, asset_id) -> dict:
    return [json.loads(m) for s, st, m in events(root, asset_id) if (s, st) == ("AI", "PASSED")][-1]


def vision(asset_id) -> dict:
    return asset_state.get(asset_id)["stages"]["vision"]


def qc_stage(asset_id) -> dict:
    return asset_state.get(asset_id)["stages"]["qc"]


# --- QC ------------------------------------------------------------------------------------


def test_qc_records_its_input_fingerprint(stocker_root):
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())
    result = json.loads(get_asset(asset_id)["qc_result"])
    view_fp = analysis_view.open_asset_view(asset_id).fingerprint

    assert result["input_fingerprint"] == qc.fingerprint(view_fp)
    assert result["inputs"] == {"view": view_fp, "rules": qc.rules()}
    assert result["inputs"]["rules"]["version"] == qc.QC_RULES_VERSION
    assert qc_stage(asset_id)["status"] == "current"


def test_rules_cover_every_qc_threshold():
    # Любой числовой порог модуля QC должен входить в отпечаток, иначе его смена прошла бы незаметно.
    thresholds = {name for name, value in vars(qc).items()
                  if name.isupper() and isinstance(value, (int, float)) and not isinstance(value, bool)}
    assert {name.lower() for name in thresholds} <= set(qc.rules())


@pytest.mark.parametrize("name, value", [("MIN_MEGAPIXELS", 3.0), ("EXTREME_RATIO", 0.3), ("QC_RULES_VERSION", "qc-rules-v4")])
def test_qc_rule_change_makes_qc_stale(stocker_root, monkeypatch, name, value):
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())
    monkeypatch.setattr(qc, name, value)
    stage = qc_stage(asset_id)
    assert stage["reason"] == "FINGERPRINT_CHANGED" and stage["changed"] == ["rules"]
    result = asset_state.get(asset_id)
    assert result["reprocess_from"] == "qc"
    assert result["stages"]["vision"]["status"] == "current"  # Vision от правил QC не зависит


def test_view_change_makes_qc_stale_with_explanation(stocker_root, monkeypatch):
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())
    monkeypatch.setattr(normalizer, "VIEW_VERSION", "analysis-view-v2")
    assert qc_stage(asset_id)["changed"] == ["view"]


# --- Vision -------------------------------------------------------------------------------


def test_vision_records_full_input_identity(stocker_root, lmstudio_asset):
    asset_id, captured = lmstudio_asset
    message = ai_passed(stocker_root, asset_id)
    view = analysis_view.open_asset_view(asset_id)
    inputs = message["inputs"]

    assert inputs["view"] == view.fingerprint
    assert inputs["provider"] == "lmstudio" and inputs["model"] == local_analyzer.DEFAULT_MODEL
    assert inputs["prompt_version"] == LocalAnalyzer.prompt_version
    assert inputs["image"] == {"variant": "preview", "format": "jpeg", "quality": 90}
    assert inputs["params"] == {"temperature": None, "max_tokens": None}
    assert set(inputs) == {"view", "provider", "model", "prompt_version", "prompt_sha256", "schema_sha256", "image", "params"}
    assert message["input_fingerprint"] == input_fingerprint(view.fingerprint, LocalAnalyzer.current_identity())
    assert vision(asset_id)["status"] == "current"

    # Отпечаток описывает то, что реально ушло в модель.
    assert captured["model"] == inputs["model"]
    assert captured["messages"][0]["content"][0]["text"] == local_analyzer.PROMPT
    assert "temperature" not in captured and "max_tokens" not in captured
    assert captured["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_image_encoding_in_identity_is_what_is_sent(stocker_root, lmstudio_asset):
    asset_id, captured = lmstudio_asset
    view = analysis_view.open_asset_view(asset_id)
    sent, _ = LocalAnalyzer._prepare_image(view)
    assert sent == view.jpeg(local_analyzer.IMAGE["variant"], quality=local_analyzer.IMAGE["quality"])


@pytest.mark.parametrize("change, patch", [
    ("model", lambda mp: mp.setenv("LMSTUDIO_MODEL", "other-model")),
    ("prompt_sha256", lambda mp: mp.setattr(local_analyzer, "PROMPT", local_analyzer.PROMPT + "\n- New rule.")),
    ("prompt_version", lambda mp: mp.setattr(LocalAnalyzer, "prompt_version", "local-v3")),
    ("schema_sha256", lambda mp: mp.setattr(local_analyzer, "response_schema", lambda: {"type": "object"})),
    ("image", lambda mp: mp.setattr(local_analyzer, "IMAGE", {**local_analyzer.IMAGE, "quality": 95})),
    ("params", lambda mp: mp.setattr(local_analyzer, "REQUEST_PARAMS", {"temperature": 0, "max_tokens": None})),
])
def test_each_vision_input_change_makes_vision_stale(stocker_root, lmstudio_asset, monkeypatch, change, patch):
    asset_id, _ = lmstudio_asset
    patch(monkeypatch)
    stage = vision(asset_id)
    assert stage["status"] == "stale" and stage["reason"] == "FINGERPRINT_CHANGED"
    assert stage["changed"] == [change]
    result = asset_state.get(asset_id)
    assert result["reprocess_from"] == "vision" and result["stages"]["qc"]["status"] == "current"
    assert result["stages"]["metadata"]["reason"] == "UPSTREAM_STALE:vision"


def test_view_change_makes_vision_stale(stocker_root, lmstudio_asset, monkeypatch):
    asset_id, _ = lmstudio_asset
    monkeypatch.setattr(normalizer, "VIEW_VERSION", "analysis-view-v2")
    assert vision(asset_id)["changed"] == ["view"]


def test_same_inputs_same_fingerprint(stocker_root, lmstudio_asset):
    asset_id, _ = lmstudio_asset
    first = ai_passed(stocker_root, asset_id)["input_fingerprint"]
    analyzer = LocalAnalyzer()
    stub_lmstudio(analyzer)
    worker.run_ai(asset_id, analysis_view.open_asset_view(asset_id), analyzer)
    assert ai_passed(stocker_root, asset_id)["input_fingerprint"] == first


def test_failed_vision_records_inputs_too(stocker_root):
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer(error=RuntimeError("down")))
    failed = [json.loads(m) for s, st, m in events(stocker_root, asset_id) if (s, st) == ("AI", "FAILED")][-1]
    assert failed["input_fingerprint"] and failed["inputs"]["provider"] == "fake"


def test_history_is_not_rewritten(stocker_root):
    # Старое AI/PASSED без отпечатка остаётся как есть и остаётся STALE; новое пишется рядом.
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())
    from tests.test_asset_state import _db

    with _db(stocker_root) as connection:
        connection.execute("UPDATE processing_events SET message = ? WHERE asset_id = ? AND stage = 'AI'",
                           (json.dumps({"provider": "fake", "prompt_version": "test-v1"}), asset_id))
    assert vision(asset_id)["reason"] == "NO_FINGERPRINT"
    legacy = [m for s, st, m in events(stocker_root, asset_id) if s == "AI"]
    asset_state.get(asset_id)
    assert [m for s, st, m in events(stocker_root, asset_id) if s == "AI"] == legacy
