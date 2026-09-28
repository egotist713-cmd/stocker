"""
Регрессия атомарности (паспорт §3A.14): result state + processing event = одна транзакция.

Полный pipeline запускается многократно; при каждом запуске «пропадает питание»
на N-й записи события. После любого обрыва scripts/check_consistency.py не должен
находить рассогласований (результат без события или событие без результата) —
как было с asset 118 до исправления.

Новый модуль, который пишет состояние assets, обязан попасть в ATOMIC_MODULES —
иначе test_every_state_writer_is_covered упадёт.
"""

import re
from pathlib import Path

import pytest

from app import creative_review, enhancement_decision, ingest, normalization, qc, reprocess, stock_readiness, worker
from app import metadata as metadata_service
from app.service import dispatch
from scripts.check_consistency import find_problems

from tests.conftest import FakeAnalyzer, make_image

ROOT = Path(__file__).resolve().parents[1]

# Модули, которые пишут результат + событие; у каждого подменяется insert_event.
ATOMIC_MODULES = (ingest, normalization, qc, reprocess, worker, metadata_service, enhancement_decision, creative_review, stock_readiness)

MAX_INJECTED = 14


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
    """ingest → normalize → QC → enhancement → Vision → metadata → gate, затем readiness и creative review."""
    try:
        asset_id = worker.process_file(make_image(root), analyzer=FakeAnalyzer())
    except PowerLoss:
        return
    if asset_id:
        for operation in ("readiness.evaluate", "creative.review"):
            dispatch(operation, {"asset_id": asset_id})  # envelope не бросает исключений


def test_pipeline_writes_many_events(stocker_root, monkeypatch):
    counter = _patch_insert_event(monkeypatch, fail_at=None)
    _run_pipeline(stocker_root)
    assert counter["calls"] >= 6  # INGEST, NORMALIZE, QC, ENHANCEMENT, AI, METADATA…
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
