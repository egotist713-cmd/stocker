"""
Факты об исходниках в папке — без регистрации в Stocker и без изменения файлов
(валидация контракта Normalization на независимых наборах; паспорт §35ZP).

    python scripts/scan_source_facts.py data/samples/2026-09-27 [--json out.json]

Для каждого файла: source_facts.read_facts или классификация отказа так же, как
normalization.evaluate_asset (MISSING_CODEC / DECODE_ERROR / UNSUPPORTED_FORMAT).
Хеш файла до и после чтения сравнивается — файл не должен меняться.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ingest import sha256_file  # noqa: E402
from app.source_facts import detect_container, read_facts  # noqa: E402

EXTENSIONS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".tif": "TIFF", ".tiff": "TIFF",
              ".heic": "HEIF", ".heif": "HEIF", ".avif": "AVIF", ".webp": "WEBP"}


def registered_sources() -> list[tuple[str, Path]]:
    """Исходники всех объектов Stocker (набор «existing»), без записи событий."""
    import sqlite3

    from app.ingest import source_file

    db = ROOT / "data" / "db" / "stocker.db"
    connection = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [(f"#{r['id']} {r['filename']}", source_file(dict(r))) for r in connection.execute("SELECT id, filename, source_path FROM assets ORDER BY id")]
    finally:
        connection.close()


def scan(folder: Path | None = None, items: list[tuple[str, Path]] | None = None) -> list[dict]:
    rows = []
    items = items or [(p.name, p) for p in sorted(q for q in folder.rglob("*") if q.is_file())]
    for label, path in items:
        if not path.exists():
            rows.append({"file": label, "extension": path.suffix.lower(), "container": None, "extension_matches": True,
                         "failed": {"error_type": "SOURCE_MISSING", "error": "file not found"}, "unchanged": True})
            continue
        before = sha256_file(path)
        container = detect_container(path.read_bytes()[:16])
        row = {"file": label, "extension": path.suffix.lower(), "container": container,
               "extension_matches": EXTENSIONS.get(path.suffix.lower()) == container}
        try:
            row["facts"] = read_facts(path, path.name)
        except Exception as exc:  # noqa: BLE001 — та же классификация, что в normalization
            error_type = "MISSING_CODEC" if container == "HEIF" else "DECODE_ERROR" if container else "UNSUPPORTED_FORMAT"
            row["failed"] = {"error_type": error_type, "error": f"{type(exc).__name__}: {exc}"[:200]}
        row["unchanged"] = sha256_file(path) == before
        rows.append(row)
    return rows


def summarize(rows: list[dict]) -> dict:
    ok = [r["facts"] for r in rows if "facts" in r]
    return {
        "files": len(rows),
        "evaluated": len(ok),
        "failed": [(r["file"], r["failed"]["error_type"]) for r in rows if "failed" in r],
        "files_changed": [r["file"] for r in rows if not r["unchanged"]],
        "extension_mismatch": [(r["file"], r["extension"], r["container"]) for r in rows if not r["extension_matches"]],
        "container": Counter(r["container"] for r in rows),
        "format": Counter(f["format"] for f in ok),
        "color_mode": Counter(f["color_mode"] for f in ok),
        "bit_depth": Counter(f["bit_depth"] for f in ok),
        "alpha": Counter(f["has_alpha"] for f in ok),
        "color_profile": Counter(f["color_profile"]["kind"] for f in ok),
        "hdr": Counter(str(f["hdr"]) for f in ok),
        "orientation": Counter(f["orientation"] for f in ok),
        "frames": Counter(f"{f['frames']['readable']}/{f['frames']['declared']}" for f in ok),
        "mpf": Counter(f"{f['mpf']['within_file']}/{f['mpf']['declared']}" if f["mpf"] else None for f in ok),
        "orientation_raw": Counter(f["orientation_raw"] for f in ok),
        "alpha_used": Counter(f["alpha_used"] for f in ok),
        "color_source": Counter(f["color_profile"]["source"] for f in ok),
        "digital_source_type": Counter(t for f in ok for t in f["provenance"]["digital_source_type"]),
        "c2pa": sum(f["provenance"]["c2pa"] for f in ok),
        "software": Counter((f["software"] or "-").split(" ")[0] for f in ok),
        "motion_video": sum(f["embedded"]["motion_video"] is not None for f in ok),
        "auxiliary": Counter(a for f in ok for a in f["embedded"]["auxiliary"]),
        "jpeg_quality": Counter((f["jpeg_quality"] // 10 * 10) if f["jpeg_quality"] else None for f in ok),
        "megapixels": Counter("<1" if f["megapixels"] < 1 else "1-4" if f["megapixels"] < 4 else "4-12" if f["megapixels"] < 12 else "12+" for f in ok),
        "metadata": {key: sum(f["metadata_present"][key] for f in ok) for key in (ok[0]["metadata_present"] if ok else {})},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Source facts for a folder (read only)")
    parser.add_argument("folder", nargs="?")
    parser.add_argument("--registered", action="store_true", help="исходники объектов Stocker вместо папки")
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    rows = scan(items=registered_sources()) if args.registered else scan((ROOT / args.folder).resolve())
    summary = summarize(rows)
    for key, value in summary.items():
        print(f"{key}: {dict(value) if isinstance(value, Counter) else value}")
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
