import json

import pytest

from app import metadata as service
from tests.test_metadata_service import FakeMetadataAnalyzer, asset_id, stored  # noqa: F401 (fixture)


@pytest.fixture
def fake_ai(monkeypatch):
    monkeypatch.setattr(service, "LMStudioMetadataAnalyzer", FakeMetadataAnalyzer)


def test_full_review_flow(asset_id, fake_ai, capsys):
    assert service.main(["build", str(asset_id)]) == 0
    assert "Outcome: DRAFTED" in capsys.readouterr().out

    assert service.main(["edit", str(asset_id), "--title", "Reviewed elevator shaft"]) == 0
    assert service.main(["approve", str(asset_id)]) == 0

    metadata = stored(asset_id)
    assert metadata["state"] == "approved"
    assert metadata["fields"]["title"] == "Reviewed elevator shaft"


def test_show_prints_metadata_json(asset_id, fake_ai, capsys):
    service.main(["build", str(asset_id)])
    capsys.readouterr()

    assert service.main(["show", str(asset_id)]) == 0

    assert json.loads(capsys.readouterr().out) == stored(asset_id)


def test_add_and_remove_keywords(asset_id, fake_ai):
    service.main(["build", str(asset_id)])

    service.main(
        [
            "edit", str(asset_id),
            "--add-keyword", "Steel Beam, rivet",
            "--add-keyword", "girder",
            "--remove-keyword", "CONSTRUCTION",
        ]
    )

    keywords = stored(asset_id)["fields"]["keywords"]
    assert keywords[-3:] == ["steel beam", "rivet", "girder"]
    assert "construction" not in keywords


def test_replace_keywords(asset_id, fake_ai):
    service.main(["build", str(asset_id)])

    service.main(["edit", str(asset_id), "--keywords", "a, b, c"])

    assert stored(asset_id)["fields"]["keywords"] == ["a", "b", "c"]


def test_edit_without_arguments_is_usage_error(asset_id, fake_ai):
    service.main(["build", str(asset_id)])

    with pytest.raises(SystemExit) as info:
        service.main(["edit", str(asset_id)])

    assert info.value.code == 2


def test_invalid_transition_returns_1(asset_id, fake_ai, capsys):
    service.main(["build", str(asset_id)])
    service.main(["edit", str(asset_id), "--keywords", "a, b"])  # TOO_FEW_KEYWORDS
    capsys.readouterr()

    assert service.main(["approve", str(asset_id)]) == 1
    assert "ERROR INVALID_TRANSITION" in capsys.readouterr().err
    assert stored(asset_id)["state"] == "draft"


def test_missing_asset_returns_1(stocker_root, capsys):
    assert service.main(["show", "999"]) == 1
    assert "ASSET_NOT_FOUND" in capsys.readouterr().err


def test_partial_flow_and_exit_codes(asset_id, monkeypatch):
    monkeypatch.setattr(service, "LMStudioMetadataAnalyzer", lambda: FakeMetadataAnalyzer(error=ConnectionError("down")))

    assert service.main(["build", str(asset_id)]) == 0  # partial draft создан
    assert stored(asset_id)["completeness"] == "partial"

    assert service.main(["build", str(asset_id)]) == 1  # повтор AI не удался
    assert service.main(["approve", str(asset_id)]) == 1
    assert service.main(["approve", str(asset_id), "--allow-partial"]) == 0


def test_reject_requires_reason_argument(asset_id, fake_ai):
    service.main(["build", str(asset_id)])

    with pytest.raises(SystemExit):
        service.main(["reject", str(asset_id)])

    assert service.main(["reject", str(asset_id), "--reason", "Not stock-worthy"]) == 0
    assert stored(asset_id)["review"]["reason"] == "Not stock-worthy"
