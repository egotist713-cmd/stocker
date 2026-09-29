"""
Regression fixture export_metadata_contamination_case (паспорт §35ZQ).

Сейчас проверяется сама фикстура и то, что факты об исходнике её видят. Тест
экспорта включится вместе с Export preparation: из готового файла площадки не
должна попасть НИКАКАЯ неразрешённая исходная metadata (inventory ⊆ whitelist),
а не только несколько известных тегов.
"""

import pytest

from app.source_facts import read_facts

from tests.fixtures.export_metadata_contamination import REQUIRED, build_contaminated_jpeg, inventory


@pytest.fixture
def contaminated(tmp_path):
    return build_contaminated_jpeg(tmp_path / "contaminated.jpg")


def test_fixture_contains_every_contamination_class(contaminated):
    missing = REQUIRED - inventory(contaminated)
    assert missing == set(), f"fixture lost contamination classes: {sorted(missing)}"


def test_source_facts_record_provenance_but_not_personal_values(contaminated):
    facts = read_facts(contaminated)
    present = facts["metadata_present"]
    assert present["gps"] and present["device"] and present["serial_numbers"] and present["iptc"] and present["xmp"]
    assert facts["software"] == "Topaz Gigapixel 1.3.6 (Windows)"
    assert facts["provenance"] == {"digital_source_type": ["compositeWithTrainedAlgorithmicMedia"], "c2pa": True}
    assert facts["embedded"]["motion_video"] is not None
    stored = str(facts)
    assert "SN-123456" not in stored and "53.0" not in stored and "SourceCam" not in stored


def test_export_contains_only_platform_whitelist(contaminated, tmp_path):
    """Включён вместе с export.prepare (EXPORT_PREPARATION_CONTRACT §4.5, паспорт §35ZZW)."""
    import dataclasses

    from app import export_preparation

    for profile in export_preparation.PROFILES.values():
        profile = dataclasses.replace(profile, min_mp=0.01)  # фикстура 320×240
        exported = export_preparation.prepare_file(contaminated, profile, tmp_path)
        for scan in (inventory, export_preparation.inventory):  # и инвентарь фикстуры, и инвентарь шага 8
            leftovers = scan(exported) - profile.metadata_whitelist
            assert leftovers == set(), f"{profile.name}: source metadata leaked into export: {sorted(leftovers)}"
