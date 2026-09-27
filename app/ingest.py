from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.database.db import get_connection, init_database, insert_asset, insert_event, transaction


ROOT = Path(__file__).resolve().parents[1]
INCOMING = ROOT / "data" / "incoming"

# AVIF (28.09.2026): стадии читают пиксели только через AnalysisView — lossless internal
# derivative. HEIC / HEIF не принимаются: кодека в runtime нет (MISSING_CODEC).
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".avif"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def to_source_path(path: Path) -> str:
    """
    Путь для assets.source_path: относительно ROOT, всегда с разделителем "/".

    Одинаковая запись из Windows и WSL позволяет открыть asset в обеих средах.
    """
    return path.resolve().relative_to(ROOT).as_posix()


def source_file(asset) -> Path:
    """Абсолютный путь к исходному файлу asset: source_path хранится относительно ROOT."""
    # Старые записи из Windows содержат "\": под WSL такой путь не разрешится.
    return ROOT / asset["source_path"].replace("\\", "/")


def already_registered(file_hash: str) -> bool:
    db_path = ROOT / "data" / "db" / "stocker.db"

    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT id, filename FROM assets WHERE file_hash = ?",
            (file_hash,),
        ).fetchone()

    if row:
        print(f"SKIP duplicate: {row['filename']} (ID {row['id']})")
        return True

    return False


def ingest_file(path: Path) -> int | None:
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        print(f"SKIP unsupported: {path.name}")
        return None

    # source_path хранится относительно ROOT, поэтому файл должен лежать в проекте.
    if not path.resolve().is_relative_to(ROOT):
        print(f"SKIP outside project: {path}")
        return None

    print(f"Processing: {path.name}")

    file_hash = sha256_file(path)

    if already_registered(file_hash):
        return None

    try:
        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format

    except UnidentifiedImageError:
        print(f"SKIP invalid image: {path.name}")
        return None

    except Exception as exc:
        print(f"SKIP image error: {path.name}: {exc}")
        return None

    file_size = path.stat().st_size

    db_path = ROOT / "data" / "db" / "stocker.db"

    # Asset и событие регистрации — одной транзакцией (аудит атомарности 27.09.2026):
    # сбой между ними оставлял бы asset без INGEST/DONE.
    with transaction(db_path) as connection:
        asset_id = insert_asset(
            connection,
            filename=path.name,
            source_path=to_source_path(path),
            file_hash=file_hash,
            extension=path.suffix.lower(),
            width=width,
            height=height,
            file_size=file_size,
        )
        insert_event(connection, asset_id, "INGEST", "DONE", f"Registered {image_format} {width}x{height}")

    print(f"ADDED: ID={asset_id}")
    print(f"  File:   {path.name}")
    print(f"  Format: {image_format}")
    print(f"  Size:   {width}x{height}")
    print(f"  Bytes:  {file_size}")
    print(f"  SHA256: {file_hash}")

    return asset_id


def main() -> None:
    init_database()

    files = sorted(
        path
        for path in INCOMING.iterdir()
        if path.is_file()
    )

    if not files:
        print("No files in incoming.")
        return

    print(f"Found files: {len(files)}")
    print()

    added = 0

    for path in files:
        if ingest_file(path) is not None:
            added += 1

        print()

    print("=== INGEST COMPLETE ===")
    print(f"Added: {added}")


if __name__ == "__main__":
    main()
