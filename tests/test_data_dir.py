"""
STOCKER_DATA_DIR (паспорт §35ZZW): отдельный каталог runtime-данных (production — data/prod).

Без переменной — прежний <проект>/data. Все пути к БД, internal, export, incoming — только
из app.database.db. pytest внешнюю переменную игнорирует и пишет только во временные каталоги.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import ingest, normalization, qc
from app.database import db
from scripts import check_consistency

ROOT = Path(__file__).resolve().parents[1]
PROBE = (
    "import json; from app.database import db; "
    "print(json.dumps({k: str(v) for k, v in {'data': db.data_dir(), 'db': db.db_path(), 'incoming': db.incoming_dir(), "
    "'internal': db.internal_dir(), 'export': db.export_dir()}.items()}))"
)


def _env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k != db.DATA_DIR_ENV}
    return {**env, **extra}


def _probe(env: dict) -> dict:
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_resolve_data_dir():
    assert db.resolve_data_dir(None) == ROOT / "data"
    assert db.resolve_data_dir("") == ROOT / "data"
    assert db.resolve_data_dir("data/prod") == ROOT / "data" / "prod"
    assert db.resolve_data_dir(str(ROOT / "data" / "prod")) == ROOT / "data" / "prod"
    with pytest.raises(ValueError):
        db.resolve_data_dir(str(ROOT.parent / "elsewhere"))  # source_path и производные — относительно проекта


def test_paths_with_variable():
    paths = _probe(_env(STOCKER_DATA_DIR="data/prod"))
    prod = ROOT / "data" / "prod"
    assert paths == {
        "data": str(prod), "db": str(prod / "db" / "stocker.db"), "incoming": str(prod / "incoming"),
        "internal": str(prod / "internal"), "export": str(prod / "export"),
    }


def test_paths_without_variable_are_unchanged():
    from dotenv import dotenv_values

    if dotenv_values(ROOT / ".env").get(db.DATA_DIR_ENV):
        pytest.skip(".env задаёт STOCKER_DATA_DIR — умолчание не проверить")
    paths = _probe(_env())
    assert paths["data"] == str(ROOT / "data")
    assert paths["db"] == str(ROOT / "data" / "db" / "stocker.db")
    assert paths["incoming"] == str(ROOT / "data" / "incoming")


def test_every_module_takes_paths_from_db(stocker_root):
    """Никаких собственных путей: qc, ingest, normalization, check_consistency — через db."""
    assert not hasattr(qc, "DB_PATH") and not hasattr(ingest, "INCOMING")
    assert db.incoming_dir() == stocker_root / "data" / "incoming"
    assert db.db_path() == stocker_root / "data" / "db" / "stocker.db"
    for module in (qc, ingest, normalization, check_consistency):
        source = Path(module.__file__).read_text(encoding="utf-8-sig")
        assert '"stocker.db"' not in source and '"internal"' not in source, module.__name__


def test_pytest_ignores_external_variable():
    assert db.DATA_DIR_ENV not in os.environ
    assert not db.data_dir().is_relative_to(ROOT)  # autouse: временный каталог


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def test_tests_do_not_write_to_production_when_variable_is_set(tmp_path):
    """Прогон тестов при установленной переменной не создаёт production-каталог и не трогает data/db."""
    target = ROOT / "data" / f"prod_guard_{os.getpid()}"
    assert not target.exists()
    real_db = ROOT / "data" / "db" / "stocker.db"
    before = _sha(real_db)
    try:
        run = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_source_paths.py", "tests/test_worker.py"],
            cwd=ROOT, env=_env(STOCKER_DATA_DIR=str(target.relative_to(ROOT))), capture_output=True, text=True,
        )
        assert run.returncode == 0, run.stdout[-2000:]
        assert not target.exists(), "tests wrote into STOCKER_DATA_DIR"
        assert _sha(real_db) == before, "tests changed the default database"
    finally:
        if target.exists():
            import shutil

            shutil.rmtree(target)
