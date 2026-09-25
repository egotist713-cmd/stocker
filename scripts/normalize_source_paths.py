"""
Привести assets.source_path к POSIX-виду ("data/incoming/x.jpg").

Старые записи, созданные из Windows, содержат "\\". Скрипт идемпотентен:
по умолчанию только показывает изменения, с --apply записывает их и добавляет
событие SOURCE/NORMALIZED со старым и новым значением.

    python -m scripts.normalize_source_paths
    python -m scripts.normalize_source_paths --apply
"""

import argparse
import json

from app.database.db import get_connection


def find_changes() -> list[tuple[int, str, str]]:
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT id, source_path FROM assets WHERE instr(source_path, '\\') > 0 ORDER BY id"
        ).fetchall()

    return [(row["id"], row["source_path"], row["source_path"].replace("\\", "/")) for row in rows]


def apply_changes(changes: list[tuple[int, str, str]]) -> None:
    with get_connection() as connection:
        for asset_id, old, new in changes:
            connection.execute(
                "UPDATE assets SET source_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new, asset_id),
            )
            connection.execute(
                "INSERT INTO processing_events (asset_id, stage, status, message) VALUES (?, ?, ?, ?)",
                (asset_id, "SOURCE", "NORMALIZED", json.dumps({"old": old, "new": new}, ensure_ascii=False)),
            )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.normalize_source_paths")
    parser.add_argument("--apply", action="store_true", help="write changes to the database")
    args = parser.parse_args(argv)

    changes = find_changes()

    for asset_id, old, new in changes:
        print(f"asset {asset_id}: {old} -> {new}")

    if not changes:
        print("Nothing to normalize.")
    elif args.apply:
        apply_changes(changes)
        print(f"Normalized: {len(changes)}")
    else:
        print(f"Dry run: {len(changes)} change(s). Use --apply to write.")


if __name__ == "__main__":
    main()
