import base64
import io
import json
import sqlite3

import pytest
from PIL import Image
from pydantic import ValidationError

from app import enhancement as en
from app import enhancement_decision as ed
from app import worker
from app.ai import enhancement_advisor as adv
from app.ai.analyzer import AIResponseError
from app.ai.schema import EnhancementAdvice
from app.ingest import ingest_file
from app.service import dispatch
from app.service.mcp_server import tools

from tests.conftest import FakeAnalyzer, OfflineEnhancementAdvisor, events, make_image

N8N = "workflow:n8n"
RECOMMENDED = EnhancementAdvice(
    decision="enhancement_recommended",
    reasons=[{"reason": "artifacts", "detail": "visible blocks"}],
    operations=["remove_compression_artifacts"],
    confidence=0.8,
)


def metrics(**overrides) -> dict:
    data = {"megapixels": 12.6, "sharpness": 800.0, "noise_sigma": 1.0, "blockiness": 1.0, "detail_ratio": 0.7}
    return {**data, **overrides}


@pytest.fixture
def measured(monkeypatch):
    """Подмена метрик: тестовые картинки 64×48 не дают пограничных значений."""
    holder = {"metrics": metrics(blockiness=1.3)}  # только borderline → disputed
    monkeypatch.setattr(en, "read_metrics", lambda path: holder["metrics"])
    return holder


@pytest.fixture
def asset_id(stocker_root) -> int:
    return ingest_file(make_image(stocker_root))


def _events(root, asset_id):
    return [(st, json.loads(m)) for s, st, m in events(root, asset_id) if s == "ENHANCEMENT"]


# --- Ответ модели -------------------------------------------------------------------


def test_advice_schema_rules():
    with pytest.raises(ValidationError):
        EnhancementAdvice(decision="enhancement_recommended", reasons=[])
    with pytest.raises(ValidationError):
        EnhancementAdvice(decision="enhancement_risky", reasons=[{"reason": "other", "detail": " "}])
    with pytest.raises(ValidationError):
        EnhancementAdvice(decision="enhancement_not_needed", operations=["sharpen"])
    with pytest.raises(ValidationError):
        EnhancementAdvice(decision="topaz_now")
    assert EnhancementAdvice(decision="enhancement_not_needed").reasons == []


def test_strict_schema_has_no_service_fields():
    schema = adv.response_schema()
    assert set(schema["properties"]) == {"decision", "reasons", "operations", "confidence"}
    assert schema["properties"]["decision"]["enum"] == [
        "enhancement_not_needed", "enhancement_recommended", "enhancement_risky",
    ]


def test_prompt_contains_rule_findings():
    assessment = en.assess(metrics(blockiness=1.3), "h")
    text = adv.prompt_for(assessment)
    assert "artifacts: blockiness=1.3 (borderline)" in text


def test_prepare_images_overview_and_100_percent_crop(tmp_path):
    path = tmp_path / "big.jpg"
    Image.new("RGB", (3000, 2000), "gray").save(path, "JPEG")

    overview, crop = adv.prepare_images(path)

    def size(url):
        return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).size

    assert size(overview) == (1536, 1024)
    assert size(crop) == (1024, 1024)


def test_advisor_switch(monkeypatch):
    monkeypatch.delenv("STOCKER_ENHANCEMENT_ADVISOR", raising=False)
    assert adv.advisor_enabled()
    monkeypatch.setenv("STOCKER_ENHANCEMENT_ADVISOR", "0")
    assert not adv.advisor_enabled()


# --- Только спорные случаи, правила не отменяются -------------------------------------


def test_disputed_case_gets_advice_event(stocker_root, asset_id, measured):
    ed.assess_asset(asset_id)
    advisor = OfflineEnhancementAdvisor(RECOMMENDED)

    result = ed.advise_asset(asset_id, advisor)

    assert result["outcome"] == ed.ADVISED and advisor.calls == 1
    (_, assessed), (status, advised) = _events(stocker_root, asset_id)
    assert status == "ADVISED"
    assert advised["provider"] == "offline" and advised["prompt_version"] == "enhancement-test"
    assert advised["inputs"] == ["image_overview", "image_crop_100", "rules_metrics"]
    assert advised["advice"]["decision"] == "enhancement_recommended"

    data = ed.get(asset_id)
    assert data["decision"] == "enhancement_recommended"
    assert data["decided_by"] == "advisor" and data["confidence"] == 0.8
    assert data["reasons"] == ["artifacts"]


def test_repeat_advice_is_unchanged(stocker_root, asset_id, measured):
    ed.assess_asset(asset_id)
    advisor = OfflineEnhancementAdvisor(RECOMMENDED)
    ed.advise_asset(asset_id, advisor)

    assert ed.advise_asset(asset_id, advisor)["outcome"] == ed.UNCHANGED
    assert advisor.calls == 1


@pytest.mark.parametrize("override,decision", [
    ({}, en.NOT_NEEDED),                       # всё ok
    ({"blockiness": 2.0}, en.RECOMMENDED),     # issue — решено правилами
    ({"sharpness": 5.0}, en.RISKY),            # severe — решено правилами
])
def test_rules_decision_is_never_sent_to_model(stocker_root, asset_id, measured, override, decision):
    measured["metrics"] = metrics(**override)
    ed.assess_asset(asset_id)
    advisor = OfflineEnhancementAdvisor(RECOMMENDED)

    result = ed.advise_asset(asset_id, advisor)

    assert result["outcome"] == ed.NOT_DISPUTED and advisor.calls == 0
    data = ed.get(asset_id)
    assert data["decision"] == decision and data["decided_by"] == "rules"


def test_advisor_failure_keeps_disputed_and_records_raw_output(stocker_root, asset_id, measured):
    ed.assess_asset(asset_id)
    advisor = OfflineEnhancementAdvisor(error=AIResponseError("bad json", raw_output="{broken"))

    result = ed.advise_asset(asset_id, advisor)

    assert result["outcome"] == ed.ADVISOR_FAILED
    status, message = _events(stocker_root, asset_id)[-1]
    assert status == "FAILED" and message["stage"] == "advisor"
    assert message["raw_output"] == "{broken" and message["provider"] == "offline"
    assert ed.get(asset_id)["decision"] == ed.DISPUTED


def test_advice_requires_current_assessment(stocker_root, asset_id, measured):
    with pytest.raises(ed.EnhancementError) as error:
        ed.advise_asset(asset_id, OfflineEnhancementAdvisor())
    assert error.value.code == "ENHANCEMENT_NOT_ASSESSED"

    ed.assess_asset(asset_id)
    with sqlite3.connect(stocker_root / "data" / "db" / "stocker.db") as connection:
        connection.execute("UPDATE assets SET file_hash = 'other' WHERE id = ?", (asset_id,))
    with pytest.raises(ed.EnhancementError) as error:
        ed.advise_asset(asset_id, OfflineEnhancementAdvisor())
    assert error.value.code == "ENHANCEMENT_STALE"


def test_advice_for_old_assessment_is_ignored(stocker_root, asset_id, measured, monkeypatch):
    ed.assess_asset(asset_id)
    ed.advise_asset(asset_id, OfflineEnhancementAdvisor(RECOMMENDED))

    # Новая версия правил — новая оценка; старая рекомендация к ней не относится.
    monkeypatch.setattr(en, "RULES_VERSION", "enhancement-rules-next")
    assert ed.assess_asset(asset_id)["outcome"] == ed.ASSESSED

    data = ed.get(asset_id)
    assert data["advice"] is None and data["decision"] == ed.DISPUTED


# --- Service layer и worker -----------------------------------------------------------


def test_operation_advise_via_service(stocker_root, asset_id, measured):
    envelope = dispatch("enhancement.advise", {"asset_id": asset_id}, actor=N8N)

    assert envelope["ok"] and envelope["outcome"] == "ADVISED"
    assert envelope["data"]["decision"] == "enhancement_not_needed"
    assert envelope["data"]["decided_by"] == "advisor"
    assert _events(stocker_root, asset_id)[-1][1]["actor"] == N8N


def test_operation_advise_not_disputed(stocker_root, asset_id, measured):
    measured["metrics"] = metrics()
    envelope = dispatch("enhancement.advise", {"asset_id": asset_id})
    assert envelope["ok"] and envelope["outcome"] == "NOT_DISPUTED"
    assert envelope["data"]["decided_by"] == "rules"


def test_tool_is_exposed():
    assert "enhancement_advise" in {tool.name for tool in tools()}


def test_worker_asks_model_only_for_disputed(stocker_root, measured, monkeypatch):
    monkeypatch.delenv("STOCKER_ENHANCEMENT_ADVISOR", raising=False)
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())

    stages = [(s, st) for s, st, _ in events(stocker_root, asset_id)]
    assert stages.index(("ENHANCEMENT", "ADVISED")) < stages.index(("AI", "PASSED"))


def test_worker_advisor_switch_off(stocker_root, measured, monkeypatch):
    monkeypatch.setenv("STOCKER_ENHANCEMENT_ADVISOR", "0")
    asset_id = worker.process_file(make_image(stocker_root), analyzer=FakeAnalyzer())

    stages = [(s, st) for s, st, _ in events(stocker_root, asset_id)]
    assert ("ENHANCEMENT", "ASSESSED") in stages and ("ENHANCEMENT", "ADVISED") not in stages
    assert ("AI", "PASSED") in stages
