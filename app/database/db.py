from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Каталог runtime-данных, привязанных к asset_id: БД, internal-производные, экспорт,
# incoming. STOCKER_DATA_DIR (паспорт §35ZZW) отделяет production-каталог
# (data/prod) от тестового; без переменной — прежний <проект>/data. Путь — внутри
# проекта: source_path и пути производных хранятся относительно корня проекта.
DATA_DIR_ENV = "STOCKER_DATA_DIR"


def resolve_data_dir(value: str | None) -> Path:
    if not value:
        return PROJECT_ROOT / "data"
    path = Path(value)
    path = (path if path.is_absolute() else PROJECT_ROOT / path).resolve()
    if not path.is_relative_to(PROJECT_ROOT):
        raise ValueError(f"{DATA_DIR_ENV} must be inside the project ({PROJECT_ROOT}): {path}")
    return path


load_dotenv()  # переменная может быть задана и в .env — одинаково для всех процессов
DATA_DIR = resolve_data_dir(os.getenv(DATA_DIR_ENV))
DEFAULT_DB_PATH = DATA_DIR / "db" / "stocker.db"


# Функции читают атрибуты модуля при вызове: тесты подменяют DATA_DIR / DEFAULT_DB_PATH.
def data_dir() -> Path:
    return DATA_DIR


def db_path() -> Path:
    return DEFAULT_DB_PATH


def incoming_dir() -> Path:
    return DATA_DIR / "incoming"


def data_dir_of(database: Path | str) -> Path:
    """Каталог данных конкретной БД: <DATA_DIR>/db/stocker.db → <DATA_DIR>."""
    return Path(database).resolve().parents[1]


def internal_dir(data: Path | None = None) -> Path:
    return (data or DATA_DIR) / "internal"


def export_dir() -> Path:
    return DATA_DIR / "export"


def describe() -> str:
    """Строка для начала вывода CLI / worker / check_consistency."""
    return f"DATA_DIR={DATA_DIR}  DB={DEFAULT_DB_PATH}"

# Кто выполняет операцию (service layer: "human", "agent:openclaw", "workflow:n8n").
# Если задан, добавляется ключом "actor" в JSON-сообщения событий. Без него
# (прямые CLI) события не меняются.
_ACTOR: ContextVar[str | None] = ContextVar("stocker_actor", default=None)


@contextmanager
def acting_as(actor: str) -> Iterator[None]:
    token = _ACTOR.set(actor)
    try:
        yield
    finally:
        _ACTOR.reset(token)


def _with_actor(message: str | None) -> str | None:
    actor = _ACTOR.get()
    if actor is None or not message:
        return message

    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return message  # текстовые сообщения (INGEST) не меняются

    if not isinstance(payload, dict):
        return message

    return json.dumps({**payload, "actor": actor}, ensure_ascii=False)


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled."""
    connection = sqlite3.connect(str(db_path or DEFAULT_DB_PATH))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_database(db_path: Path | str | None = None) -> None:
    """Create the Stocker database schema if it does not exist."""
    db_path = Path(db_path or DEFAULT_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                source_path TEXT NOT NULL,
                file_hash TEXT UNIQUE,
                extension TEXT,
                width INTEGER,
                height INTEGER,
                file_size INTEGER,
                status TEXT NOT NULL DEFAULT 'NEW',
                qc_result TEXT,
                ai_result TEXT,
                metadata_json TEXT,
                rejection_reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_assets_status
                ON assets(status);

            CREATE INDEX IF NOT EXISTS idx_assets_file_hash
                ON assets(file_hash);

            CREATE TABLE IF NOT EXISTS processing_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id INTEGER NOT NULL,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(asset_id) REFERENCES assets(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_processing_events_asset
                ON processing_events(asset_id);

            CREATE INDEX IF NOT EXISTS idx_processing_events_stage
                ON processing_events(stage);
            """
        )


def add_asset(
    filename: str,
    source_path: str,
    file_hash: str | None = None,
    extension: str | None = None,
    width: int | None = None,
    height: int | None = None,
    file_size: int | None = None,
    db_path: Path | str | None = None,
) -> int:
    """Add a new asset and return its database ID."""
    with get_connection(db_path) as connection:
        return insert_asset(connection, filename, source_path, file_hash, extension, width, height, file_size)


def insert_asset(
    connection: sqlite3.Connection,
    filename: str,
    source_path: str,
    file_hash: str | None = None,
    extension: str | None = None,
    width: int | None = None,
    height: int | None = None,
    file_size: int | None = None,
) -> int:
    """Insert an asset inside an open transaction (together with its INGEST/DONE event)."""
    cursor = connection.execute(
        """
        INSERT INTO assets (
            filename,
            source_path,
            file_hash,
            extension,
            width,
            height,
            file_size
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (filename, source_path, file_hash, extension, width, height, file_size),
    )
    return int(cursor.lastrowid)


def add_event(
    asset_id: int,
    stage: str,
    status: str,
    message: str | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Record a processing event for an asset."""
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO processing_events (
                asset_id,
                stage,
                status,
                message
            )
            VALUES (?, ?, ?, ?)
            """,
            (asset_id, stage, status, _with_actor(message)),
        )


def save_ai_result(
    asset_id: int,
    ai_result: str,
    db_path: Path | str | None = None,
) -> None:
    """Save the serialized AI analysis for an asset."""
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE assets
            SET ai_result = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (ai_result, asset_id),
        )


def get_asset(
    asset_id: int,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Return one asset as a dictionary."""
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM assets WHERE id = ?",
            (asset_id,),
        ).fetchone()

    return dict(row) if row else None


@contextmanager
def transaction(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Одна транзакция на несколько записей: commit при успехе, rollback при ошибке."""
    connection = get_connection(db_path)
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def insert_event(
    connection: sqlite3.Connection,
    asset_id: int,
    stage: str,
    status: str,
    message: str | None = None,
) -> int:
    """Record a processing event inside an open transaction and return its ID."""
    cursor = connection.execute(
        """
        INSERT INTO processing_events (asset_id, stage, status, message)
        VALUES (?, ?, ?, ?)
        """,
        (asset_id, stage, status, _with_actor(message)),
    )
    return int(cursor.lastrowid)


def update_ai_result(connection: sqlite3.Connection, asset_id: int, ai_result: str) -> None:
    """Save assets.ai_result inside an open transaction (together with its AI/PASSED event)."""
    connection.execute(
        """
        UPDATE assets
        SET ai_result = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (ai_result, asset_id),
    )


def update_metadata(connection: sqlite3.Connection, asset_id: int, metadata_json: str) -> None:
    """Save assets.metadata_json inside an open transaction."""
    connection.execute(
        """
        UPDATE assets
        SET metadata_json = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (metadata_json, asset_id),
    )


def get_last_event(
    asset_id: int,
    stage: str,
    status: str,
    db_path: Path | str | None = None,
) -> dict[str, Any] | None:
    """Return the most recent event of the given stage/status for an asset."""
    connection = get_connection(db_path)
    try:
        row = connection.execute(
            """
            SELECT * FROM processing_events
            WHERE asset_id = ? AND stage = ? AND status = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (asset_id, stage, status),
        ).fetchone()
    finally:
        connection.close()

    return dict(row) if row else None
