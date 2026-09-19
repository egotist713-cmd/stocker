from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "db" / "stocker.db"


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled."""
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_database(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Create the Stocker database schema if it does not exist."""
    db_path = Path(db_path)
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
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """Add a new asset and return its database ID."""
    with get_connection(db_path) as connection:
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
            (
                filename,
                source_path,
                file_hash,
                extension,
                width,
                height,
                file_size,
            ),
        )
        return int(cursor.lastrowid)


def add_event(
    asset_id: int,
    stage: str,
    status: str,
    message: str | None = None,
    db_path: Path | str = DEFAULT_DB_PATH,
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
            (asset_id, stage, status, message),
        )


def get_asset(
    asset_id: int,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """Return one asset as a dictionary."""
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM assets WHERE id = ?",
            (asset_id,),
        ).fetchone()

    return dict(row) if row else None
