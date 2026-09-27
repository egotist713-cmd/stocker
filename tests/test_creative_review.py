import json

import pytest
from pydantic import ValidationError

from app import creative_profiles as profiles
from app import creative_review as cr
from app.creative_profiles import INDUSTRIAL_STOCK
from app.ai import creative_advisor as ca
from app.ai.analyzer import AIResponseError
from app.ai.schema import AIAnalysis, CreativeReview
from app.service import dispatch
from app.service.mcp_server import tools

from tests.conftest import OfflineCreativeAdvisor, events, make_image
from tests.test_service import _vision_asset

N8N = "workflow:n8n"


def features(**overrides) -> dict:
    data = {"composition": "good", "uniqueness": "medium", "demand": "high", "commercial_use_cases": ["a", "b"],
            "recommendation": "proceed", "composition_notes": "", "quality_notes": [], "confidence": 0.8}
    return {**data, **overrides}


def _events(root, asset_id):
    return [(st, json.loads(m)) for s, st, m in events(root, asset_id) if s == "CREATIVE_REVIEW"]


# --- Формула creative-score-v1 --------------------------------------------------------


def test_score_formula_and_bands():
    assert cr.commercial_score(features()) == {"score_version": "creative-score-v1", "commercial_score": 84, "commercial_potential": "high"}
    low = features(composition="weak", uniqueness="low", demand="low", commercial_use_cases=[])
    assert cr.commercial_score(low)["commercial_score"] == 15
    assert cr.commercial_score(low)["commercial_potential"] == "low"
    assert cr.commercial_score(features(composition="acceptable", demand="medium", commercial_use_cases=[]))["commercial_potential"] == "medium"


def test_use_cases_points_are_capped():
    many = features(commercial_use_cases=[str(i) for i in range(9)])
    assert cr.commercial_score(many)["commercial_score"] == 35 + 30 + 15 + 10


def test_model_output_schema():
    schema = ca.response_schema()
    assert "commercial_score" not in schema["properties"]  # score считает Python, не модель
    assert schema["properties"]["commercial_use_cases"]["maxItems"] == 5
    with pytest.raises(ValidationError):
        CreativeReview(composition="excellent", uniqueness="high", demand="high", recommendation="proceed")


def test_prompt_contains_vision_summary_profile_and_universal_anchors():
    text = ca.prompt_for(AIAnalysis(subject="Elevator shaft", title="Shaft interior", description="Rails."), INDUSTRIAL_STOCK)
    assert "subject: Elevator shaft" in text and "Rails." in text
    assert INDUSTRIAL_STOCK.audience in text
    assert all(anchor in text for anchor in profiles.UNIVERSAL_ANCHORS)


def test_template_core_has_no_niche():
    # Ядро не знает тематик: всё специфичное — в профиле (паспорт §3A.10).
    core = (ca.PROMPT + " ".join(profiles.UNIVERSAL_ANCHORS)).lower()
    assert not any(word in core for word in ("industrial", "elevator", "engineering", "b2b"))


def test_prompt_version_includes_profile():
    assert ca.prompt_version_for(INDUSTRIAL_STOCK) == "creative-review-v3/industrial_stock-v2"


# --- Профили ------------------------------------------------------------------------------


def test_default_profile_and_env_override(monkeypatch):
    monkeypatch.delenv("STOCKER_CREATIVE_PROFILE", raising=False)
    assert profiles.get_profile().name == "industrial_stock"
    monkeypatch.setenv("STOCKER_CREATIVE_PROFILE", "nature_stock")
    assert profiles.get_profile().name == "nature_stock"
    monkeypatch.setenv("STOCKER_CREATIVE_PROFILE", "travel_stock")
    with pytest.raises(profiles.UnknownProfileError, match="planned"):
        profiles.get_profile()


@pytest.mark.parametrize("name,status", [("travel_stock", "planned"), ("food_stock", "unknown")])
def test_planned_or_unknown_profile_is_refused(stocker_root, name, status):
    asset_id = _vision_asset(stocker_root)

    envelope = dispatch("creative.review", {"asset_id": asset_id, "profile": name})

    assert envelope["error"]["code"] == "UNKNOWN_PROFILE"
    assert status in envelope["error"]["message"] and "industrial_stock" in envelope["error"]["message"]
    assert _events(stocker_root, asset_id) == []


def test_profiles_registry_matches_architecture():
    active = {n for n, p in profiles.PROFILES.items() if p.status == profiles.ACTIVE}
    assert active == {"industrial_stock", "architecture_stock", "nature_stock"}
    assert set(profiles.PLANNED_PROFILES) == {"travel_stock", "product_stock", "lifestyle_stock", "ai_content", "personal_archive"}
    assert all(p.audience and p.anchors and p.route_terms for p in profiles.PROFILES.values() if p.status == profiles.ACTIVE)


@pytest.mark.parametrize("subject,title,keywords,expected", [
    ("Urban residential buildings", "City view", ["apartment", "district"], "architecture_stock"),     # #16
    ("Natural landscape", "Mountain view in winter", ["snow", "hills"], "nature_stock"),              # #66
    ("Electrical Components", "Electrical panel wiring", ["cable", "breaker"], "industrial_stock"),
    ("Concrete wall texture", "Rough concrete surface", ["texture"], "industrial_stock"),             # #117
    ("Gaming mouse", "Computer mouse", ["mouse"], "industrial_stock"),                                # нет совпадений → по умолчанию
])
def test_auto_routing_by_vision_description(subject, title, keywords, expected):
    chosen, selection = profiles.route_profile(subject, title, keywords)
    assert chosen.name == expected and selection["chosen"] == expected and selection["mode"] == "auto"


def test_auto_profile_is_recorded_in_event(stocker_root):
    city = AIAnalysis(subject="Urban residential buildings", title="City skyline", keywords=["city", "apartment"])
    asset_id = _vision_asset(stocker_root, vision=city)

    envelope = dispatch("creative.review", {"asset_id": asset_id, "profile": "auto"})

    assert envelope["ok"] and envelope["data"]["profile"] == "architecture_stock"
    message = _events(stocker_root, asset_id)[-1][1]
    assert message["profile"]["selection"]["mode"] == "auto"
    assert message["profile"]["name"] == "architecture_stock" and message["profile"]["version"] == "1"


# --- События и идемпотентность ---------------------------------------------------------


def test_review_writes_event_with_features_and_score(stocker_root):
    asset_id = _vision_asset(stocker_root)

    result = cr.review_asset(asset_id)

    assert result["outcome"] == cr.REVIEWED
    ((status, message),) = _events(stocker_root, asset_id)
    assert status == "ADVISED"
    assert message["provider"] == "offline" and message["prompt_version"] == "creative-test"
    assert message["profile"] == {"name": "industrial_stock", "version": "2", "selection": {"mode": "default"}}
    assert cr.get(asset_id)["profile"] == "industrial_stock"
    assert message["features"]["composition"] == "good"
    assert message["commercial_score"] == 35 + 30 + 15 + 2 and message["score_version"] == "creative-score-v1"
    assert message["inputs"] == ["image_overview", "vision_summary"]


def test_repeat_is_unchanged(stocker_root):
    asset_id = _vision_asset(stocker_root)
    advisor = OfflineCreativeAdvisor()
    cr.review_asset(asset_id, advisor)

    assert cr.review_asset(asset_id, advisor)["outcome"] == cr.UNCHANGED
    assert advisor.calls == 1


def test_summary_recomputes_score_from_stored_features(stocker_root, monkeypatch):
    asset_id = _vision_asset(stocker_root)
    cr.review_asset(asset_id)

    monkeypatch.setitem(cr.DEMAND_POINTS, "high", 0)  # новая формула — без нового вызова модели
    assert cr.get(asset_id)["commercial_score"] == 35 + 0 + 15 + 2


def test_failure_records_raw_output(stocker_root):
    asset_id = _vision_asset(stocker_root)

    result = cr.review_asset(asset_id, OfflineCreativeAdvisor(error=AIResponseError("bad", raw_output="{oops")))

    assert result["outcome"] == cr.REVIEW_FAILED
    ((status, message),) = _events(stocker_root, asset_id)
    assert status == "FAILED" and message["raw_output"] == "{oops"
    assert cr.get(asset_id)["reviewed"] is False


def test_changed_source_is_not_reviewed(stocker_root):
    asset_id = _vision_asset(stocker_root)
    make_image(stocker_root, seed=7)

    assert cr.review_asset(asset_id)["outcome"] == cr.REVIEW_FAILED


def test_personal_document_is_not_sent_to_model(stocker_root):
    document = AIAnalysis(subject="Passport", keywords=["passport", "identity document"])
    asset_id = _vision_asset(stocker_root, vision=document)
    advisor = OfflineCreativeAdvisor()

    with pytest.raises(cr.CreativeReviewError) as error:
        cr.review_asset(asset_id, advisor)

    assert error.value.code == "PERSONAL_DOCUMENT" and advisor.calls == 0
    assert dispatch("creative.review", {"asset_id": asset_id})["error"]["code"] == "PERSONAL_DOCUMENT"


def test_requires_vision(stocker_root):
    from app.ingest import ingest_file

    asset_id = ingest_file(make_image(stocker_root))
    with pytest.raises(cr.CreativeReviewError) as error:
        cr.review_asset(asset_id)
    assert error.value.code == "VISION_MISSING"


# --- Service layer ----------------------------------------------------------------------


def test_operation_and_asset_view(stocker_root):
    asset_id = _vision_asset(stocker_root)

    envelope = dispatch("creative.review", {"asset_id": asset_id}, actor=N8N)

    assert envelope["ok"] and envelope["outcome"] == "REVIEWED"
    assert envelope["data"]["commercial_potential"] == "high" and envelope["data"]["recommendation"] == "proceed"
    assert _events(stocker_root, asset_id)[0][1]["actor"] == N8N

    view = dispatch("asset.get", {"asset_id": asset_id})["data"]
    assert view["pipeline"]["creative_review"]["commercial_score"] == 82
    assert dispatch("creative.get", {"asset_id": asset_id}, actor="agent:openclaw")["data"]["reviewed"] is True


def test_review_never_changes_metadata_or_readiness(stocker_root):
    asset_id = _vision_asset(stocker_root)
    before = dispatch("asset.get", {"asset_id": asset_id})["data"]

    dispatch("creative.review", {"asset_id": asset_id})

    after = dispatch("asset.get", {"asset_id": asset_id})["data"]
    assert after["metadata"] == before["metadata"]
    assert after["pipeline"]["stock_readiness"] == before["pipeline"]["stock_readiness"]


def test_tools_exposed():
    assert {"creative_review", "creative_get"} <= {tool.name for tool in tools()}
