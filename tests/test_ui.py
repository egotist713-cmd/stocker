"""
Локальный веб-интерфейс v1 (паспорт §35ZZZN): тонкий слой над сервисом — те же операции и события,
что у CLI; POST только с токеном; фото только по asset_id; работает только на data/prod.
"""

import dataclasses
import json
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app import asset_state, export_preparation as ep, worker
from app import readiness as rd
from app.ai.schema import PeopleInfo
from app.service import dispatch
from app.textnorm import normalize_text
from app.ui import server

from tests.conftest import FakeAnalyzer, events, make_image
from tests.test_metadata_service import VISION
from tests.test_publication import SRGB_ICC

TOKEN = "test-token"


class Vision(FakeAnalyzer):
    def __init__(self, analysis):
        super().__init__()
        self.analysis = analysis

    def analyze(self, view):
        return self.analysis


BRAND = VISION.model_copy(update={"brands": ["Acme"]})                       # → human_review (TRADEMARK)
PEOPLE = VISION.model_copy(update={"people": PeopleInfo(present=True, count=2), "subject": "workers far away"})


@pytest.fixture(autouse=True)
def small_images_allowed(monkeypatch):
    monkeypatch.setattr(rd, "PROFILES", {n: dataclasses.replace(p, min_mp=0.001) for n, p in rd.PROFILES.items()})
    monkeypatch.setattr(ep, "PROFILES", {n: dataclasses.replace(p, min_mp=0.001) for n, p in ep.PROFILES.items()})


@pytest.fixture
def client(stocker_root):
    return TestClient(server.create_app(token=TOKEN, require_prod=False))


def asset(root, seed, analysis=VISION, name=None) -> int:
    path = make_image(root, name=name or f"photo{seed}.jpg", seed=seed, icc_profile=SRGB_ICC)
    asset_id = worker.process_file(path, analyzer=Vision(analysis))
    if asset_state.get(asset_id)["state"] == asset_state.PLATFORM_READY:
        dispatch("publication.evaluate", {"asset_id": asset_id})
    return asset_id


def stage_events(root, asset_id, stage, status):
    return [json.loads(m) for s, st, m in events(root, asset_id) if (s, st) == (stage, status)]


def counter(html: str, name: str) -> int:
    marker = f'data-counter="{name}"><b>'
    return int(html.split(marker, 1)[1].split("</b>", 1)[0])


# --- Панель -----------------------------------------------------------------------------


def test_panel_counters_and_collect_button(stocker_root, client):
    approved = asset(stocker_root, 1)
    review = asset(stocker_root, 2, BRAND)
    assert asset_state.get(review)["state"] == asset_state.HUMAN_REVIEW
    html = client.get("/").text
    assert counter(html, "approved") == 1 and counter(html, "attention") == 1
    assert counter(html, "blocked") == 0 and counter(html, "rejected") == 0
    assert 'id="collect" disabled' in html  # есть «требует внимания» — сбор недоступен
    assert client.post("/collect", data={"token": TOKEN}).status_code == 409

    dispatch("metadata.reject", {"asset_id": review, "reason": "brand"}, actor="human")
    html = client.get("/").text
    assert counter(html, "attention") == 0 and counter(html, "rejected") == 1
    assert 'id="collect" disabled' not in html
    assert f"prod #{approved}" in html  # недавние события


def test_ui_works_on_production_catalog_only(stocker_root):
    with pytest.raises(RuntimeError):
        server.create_app()


# --- Требует внимания --------------------------------------------------------------------


def test_attention_screen_shows_reasons_and_readonly_metadata(stocker_root, client):
    review = asset(stocker_root, 1, BRAND)
    response = client.get("/attention", follow_redirects=True)
    assert response.status_code == 200 and f"prod #{review}" in response.text
    assert "Товарный знак или логотип в кадре: Acme" in response.text
    assert f'/photo/{review}/full' in response.text and "Открыть 100%" in response.text
    assert "<textarea" not in response.text  # title и ключевые слова — только чтение


def test_approve_through_ui_writes_the_same_event_as_cli(stocker_root, client):
    by_cli = asset(stocker_root, 1, BRAND)
    by_ui = asset(stocker_root, 2, BRAND)
    dispatch("metadata.approve", {"asset_id": by_cli}, actor="human")  # как app.api / MCP-слой
    response = client.post(f"/attention/{by_ui}/approve", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    (cli_event,) = stage_events(stocker_root, by_cli, "METADATA", "APPROVED")
    (ui_event,) = stage_events(stocker_root, by_ui, "METADATA", "APPROVED")
    assert ui_event == cli_event and ui_event["actor"] == "human"
    # после решения — штатно Readiness (и Publication, если применимо), как в ручном потоке
    assert stage_events(stocker_root, by_ui, "READINESS", "EVALUATED")


def test_reject_requires_reason_and_writes_cli_event(stocker_root, client):
    review = asset(stocker_root, 1, BRAND)
    response = client.post(f"/attention/{review}/reject", data={"token": TOKEN, "reason": "  "}, follow_redirects=False)
    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert stage_events(stocker_root, review, "METADATA", "REJECTED") == []
    client.post(f"/attention/{review}/reject", data={"token": TOKEN, "reason": "логотип — ретушь"})
    (event,) = stage_events(stocker_root, review, "METADATA", "REJECTED")
    assert event["reason"] == normalize_text("логотип — ретушь") and event["actor"] == "human"  # сервис нормализует текст


def test_people_toggle_attests_and_makes_ready(stocker_root, client):
    people = asset(stocker_root, 1, PEOPLE)
    page = client.get(f"/attention/{people}").text
    assert "Люди в кадре не узнаваемы" in page
    client.post(f"/attention/{people}/approve", data={"token": TOKEN, "attest": "on"})
    (attested,) = stage_events(stocker_root, people, "HUMAN", "PEOPLE_ATTESTED")
    assert attested["kind"] == "not_identifiable" and attested["actor"] == "human"
    assert asset_state.get(people)["state"] == asset_state.PUBLICATION_APPROVED


def _with_claim(asset_id: int) -> None:
    """Утверждение без опоры на Vision (UNCONFIRMED_CLAIM): «Berlin» в description."""
    result = dispatch("metadata.edit", {"asset_id": asset_id, "description": "Workers far away in Berlin."}, actor="human")
    assert result["ok"] and "UNCONFIRMED_CLAIM" in {e["code"] for e in result["data"]["metadata"]["validation"]["errors"]}


def test_unconfirmed_claim_toggle_confirms_and_attests_in_one_step(stocker_root, client):
    people = asset(stocker_root, 1, PEOPLE)
    _with_claim(people)
    page = client.get(f"/attention/{people}").text
    assert "Подтверждаю формулировки: Berlin" in page and 'name="claims" value="Berlin"' in page

    # Без подтверждения: одобрение не вызывается, аттестация не записывается (одним шагом).
    response = client.post(f"/attention/{people}/approve", data={"token": TOKEN, "attest": "on"}, follow_redirects=False)
    assert response.status_code == 303
    assert unquote(response.headers["location"]).endswith("error=Не подтверждены формулировки: Berlin"
                                                          " — подтвердите их или отклоните объект")
    assert stage_events(stocker_root, people, "METADATA", "APPROVED") == []
    assert stage_events(stocker_root, people, "HUMAN", "PEOPLE_ATTESTED") == []

    # Подтверждён устаревший список формулировок → тоже ничего не записано.
    client.post(f"/attention/{people}/approve",
                data={"token": TOKEN, "attest": "on", "confirm_claims": "on", "claims": "Paris"})
    assert stage_events(stocker_root, people, "METADATA", "APPROVED") == []
    assert stage_events(stocker_root, people, "HUMAN", "PEOPLE_ATTESTED") == []

    client.post(f"/attention/{people}/approve",
                data={"token": TOKEN, "attest": "on", "confirm_claims": "on", "claims": "Berlin"})
    (approved,) = stage_events(stocker_root, people, "METADATA", "APPROVED")
    assert approved["confirmed_claims"] == ["Berlin"] and approved["actor"] == "human"
    assert stage_events(stocker_root, people, "HUMAN", "PEOPLE_ATTESTED")
    assert asset_state.get(people)["state"] == asset_state.PUBLICATION_APPROVED


def test_failed_approve_leaves_no_attestation(stocker_root, client, monkeypatch):
    people = asset(stocker_root, 1, PEOPLE)
    refused = {"ok": False, "data": None, "error": {"code": "INVALID_TRANSITION", "message": "refused"}}
    real_call = server._call
    monkeypatch.setattr(server, "_call", lambda op, **p: refused if op == "metadata.approve" else real_call(op, **p))
    response = client.post(f"/attention/{people}/approve", data={"token": TOKEN, "attest": "on"}, follow_redirects=False)
    assert "error=refused" in response.headers["location"]
    assert stage_events(stocker_root, people, "HUMAN", "PEOPLE_ATTESTED") == []


def test_toggle_absent_without_claims(stocker_root, client):
    review = asset(stocker_root, 1, BRAND)
    assert "Подтверждаю формулировки" not in client.get(f"/attention/{review}").text


def test_blocked_screen_attests_model_release(stocker_root, client):
    people = asset(stocker_root, 1, PEOPLE)
    client.post(f"/attention/{people}/approve", data={"token": TOKEN})  # без отметки → blocked
    assert asset_state.get(people)["state"] == asset_state.BLOCKED
    page = client.get("/blocked").text
    assert "Узнаваемые люди без релиза" in page and "metadata.approve" not in page
    client.post(f"/blocked/{people}/attest", data={"token": TOKEN})  # без подтверждения — ничего
    assert stage_events(stocker_root, people, "HUMAN", "PEOPLE_ATTESTED") == []
    client.post(f"/blocked/{people}/attest", data={"token": TOKEN, "confirm": "on"})
    assert asset_state.get(people)["state"] == asset_state.PUBLICATION_APPROVED


# --- Одобрено / Отклонено ---------------------------------------------------------------


def test_return_to_review_escalates(stocker_root, client):
    approved = asset(stocker_root, 1)
    assert f"prod #{approved}" in client.get("/approved").text
    client.post(f"/approved/{approved}/return", data={"token": TOKEN})
    (event,) = stage_events(stocker_root, approved, "METADATA", "ESCALATED")
    assert event["reason"] == "вернул пользователь" and asset_state.get(approved)["state"] == asset_state.HUMAN_REVIEW


def test_rejected_screen_lists_reason(stocker_root, client):
    review = asset(stocker_root, 1, BRAND)
    dispatch("metadata.reject", {"asset_id": review, "reason": "заменён"}, actor="human")
    page = client.get("/rejected").text
    assert f"prod #{review}" in page and "заменён" in page and "data/incoming/photo1.jpg" in page


# --- Токен и фото ------------------------------------------------------------------------


def test_post_without_token_is_refused(stocker_root, client):
    review = asset(stocker_root, 1, BRAND)
    before = events(stocker_root, review)
    for path, data in ((f"/attention/{review}/approve", {}), (f"/attention/{review}/reject", {"reason": "x"}),
                       ("/collect", {}), (f"/approved/{review}/return", {"token": "wrong"})):
        assert client.post(path, data=data).status_code == 403
    assert events(stocker_root, review) == before


def test_photos_only_by_asset_id(stocker_root, client):
    asset_id = asset(stocker_root, 1)
    preview = client.get(f"/photo/{asset_id}/preview.jpg")
    assert preview.status_code == 200 and preview.headers["content-type"] == "image/jpeg"
    full = client.get(f"/photo/{asset_id}/full?path=../../.env")
    assert full.status_code == 200 and full.content == (stocker_root / "data/incoming/photo1.jpg").read_bytes()
    assert client.get("/photo/abc/full").status_code == 422
    assert client.get("/photo/999/full").status_code == 404
    assert client.get("/photo/..%2F..%2F.env/full").status_code in (404, 422)


# --- Собрать к загрузке ------------------------------------------------------------------


def test_collect_prepares_and_collects_both_platforms(stocker_root, client):
    first = asset(stocker_root, 1)
    second = asset(stocker_root, 2)
    page = client.post("/collect", data={"token": TOKEN}).text
    assert "Партия 001" in page and "data/publish/adobe/001_" in page and "data/publish/shutterstock/001_" in page
    for asset_id in (first, second):
        assert {e["platform"] for e in stage_events(stocker_root, asset_id, "OUTBOX", "COLLECTED")} == {"adobe", "shutterstock"}
    html = client.get("/").text
    assert counter(html, "approved") == 0  # всё собрано
    assert "Новых файлов нет" in client.post("/collect", data={"token": TOKEN}).text
