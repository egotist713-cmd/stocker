import sqlite3
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app import ingest, qc
from app.ai.analyzer import AIAnalyzer
from app.ai.schema import AIAnalysis
from app.database import db


@pytest.fixture
def stocker_root(tmp_path, monkeypatch) -> Path:
    """Изолированный корень проекта с временной БД. Production-БД не затрагивается."""
    db_path = tmp_path / "data" / "db" / "stocker.db"
    (tmp_path / "data" / "incoming").mkdir(parents=True)

    monkeypatch.setattr(ingest, "ROOT", tmp_path)
    monkeypatch.setattr(qc, "ROOT", tmp_path)
    monkeypatch.setattr(qc, "DB_PATH", db_path)
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", db_path)

    # Маленькие тестовые изображения должны проходить QC.
    monkeypatch.setattr(qc, "MIN_WIDTH", 10)
    monkeypatch.setattr(qc, "MIN_HEIGHT", 10)
    monkeypatch.setattr(qc, "MIN_FILE_SIZE", 0)

    db.init_database()
    return tmp_path


def make_image(root: Path, name: str = "photo.jpg", seed: int = 0, size=(64, 48)) -> Path:
    rng = np.random.default_rng(seed)
    pixels = rng.integers(0, 256, size=(size[1], size[0], 3), dtype=np.uint8)
    path = root / "data" / "incoming" / name
    Image.fromarray(pixels).save(path, format="JPEG")
    return path


def events(root: Path, asset_id: int) -> list[tuple[str, str, str]]:
    with sqlite3.connect(root / "data" / "db" / "stocker.db") as connection:
        return connection.execute(
            "SELECT stage, status, message FROM processing_events WHERE asset_id = ? ORDER BY id",
            (asset_id,),
        ).fetchall()


class FakeAnalyzer(AIAnalyzer):
    provider = "fake"
    model = "fake-model"
    prompt_version = "test-v1"

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = 0

    def analyze(self, image_path: Path) -> AIAnalysis:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return AIAnalysis(title="Test title", keywords=["test"], confidence=0.5)
