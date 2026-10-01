"""
Регрессия атомарности (паспорт §3A.14): result state + processing event = одна транзакция.

Полный pipeline запускается многократно; при каждом запуске «пропадает питание»
на N-й записи события. После любого обрыва scripts/check_consistency.py не должен
находить рассогласований (результат без события или событие без результата) —
как было с asset 118 до исправления.

Новый модуль, который пишет состояние assets, обязан попасть в ATOMIC_MODULES —
иначе test_every_state_writer_is_covered упадёт.
"""

import dataclasses
import re
from pathlib import Path

import pytest

from app import attestation, creative_review, enhancement_decision, export_preparation, ingest, normalization, outbox, qc, reprocess, stock_readiness, worker
from app import metadata as metadata_service
from app import readiness as rd
from app.service import dispatch
from scripts.check_consistency import find_problems

from tests.conftest import make_image
from tests.test_publication import SRGB_ICC, StockVision

ROOT = Path(__file__).resolve().parents[1]

# Модули, которые пишут результат + событие; у каждого подменяется insert_event.
ATOMIC_MODULES = (ingest, normalization, qc, reprocess, worker, metadata_service, enhancement_decision, creative_review,
                  stock_readiness, export_preparation, attestation, outbox)

MAX_INJECTED = 19


@pytest.fixture(autouse=True)
def small_images_reach_export(monkeypatch):
    """Тестовое изображение 64×48 проходит Readiness и Publication — цепочка доходит до Export."""
    monkeypatch.setattr(rd, "PROFILES", {n: dataclasses.replace(p, min_mp=0.001) for n, p in rd.PROFILES.items()})
    monkeypatch.setattr(export_preparation, "PROFILES",
                        {n: dataclasses.replace(p, min_mp=0.001) for n, p in export_preparation.PROFILES.items()})


class PowerLoss(RuntimeError):
    pass


def _patch_insert_event(monkeypatch, fail_at: int | None) -> dict:
    counter = {"calls": 0}
    for module in ATOMIC_MODULES:
        original = module.insert_event

        def crashing(*args, _original=original, **kwargs):
            counter["calls"] += 1
            if counter["calls"] == fail_at:
                raise PowerLoss(f"power loss at event write #{fail_at}")
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, "insert_event", crashing)
    return counter


def _run_pipeline(root: Path) -> None:
    """ingest → normalize → QC → enhancement → Vision → metadata → gate → readiness, затем creative review,
    Publication и Export (файл площадки + DERIVATIVE/CREATED)."""
    try:
        asset_id = worker.process_file(make_image(root, icc_profile=SRGB_ICC), analyzer=StockVision())
    except PowerLoss:
        return
    if asset_id:
        dispatch("asset.attest_people", {"asset_id": asset_id, "kind": "not_identifiable"})  # HUMAN/PEOPLE_ATTESTED
        for operation in ("readiness.evaluate", "creative.review", "publication.evaluate", "export.prepare"):
            dispatch(operation, {"asset_id": asset_id})  # envelope не бросает исключений
        dispatch("export.prepare", {"asset_id": asset_id, "platform": "shutterstock"})
        try:
            outbox.collect("adobe")  # файлы партии, затем OUTBOX/COLLECTED одной транзакцией
        except PowerLoss:
            pass


def test_pipeline_writes_many_events(stocker_root, monkeypatch):
    counter = _patch_insert_event(monkeypatch, fail_at=None)
    _run_pipeline(stocker_root)
    assert counter["calls"] >= 6  # INGEST, NORMALIZE, QC, ENHANCEMENT, AI, METADATA…
    derivative = [m for m in ATOMIC_MODULES if m is export_preparation]
    assert derivative and export_preparation.get(1)["status"] == export_preparation.READY_FOR_EXPORT
    assert not any(find_problems(stocker_root / "data" / "db" / "stocker.db").values())


@pytest.mark.parametrize("fail_at", range(1, MAX_INJECTED + 1))
def test_power_loss_at_any_event_leaves_consistent_state(stocker_root, monkeypatch, fail_at):
    _patch_insert_event(monkeypatch, fail_at)

    _run_pipeline(stocker_root)

    problems = {name: ids for name, ids in find_problems(stocker_root / "data" / "db" / "stocker.db").items() if ids}
    assert problems == {}, f"power loss at event #{fail_at} left inconsistent state: {problems}"


def test_every_state_writer_is_covered():
    """Любой модуль app/, который пишет в assets, должен быть в ATOMIC_MODULES."""
    writes_state = re.compile(r"UPDATE assets|INSERT INTO assets|update_ai_result|update_metadata|insert_asset|save_qc_result")
    covered = {Path(module.__file__).name for module in ATOMIC_MODULES} | {"db.py", "__init__.py"}
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "app").rglob("*.py")
        if path.name not in covered and writes_state.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"modules writing asset state without atomicity coverage: {offenders}"
