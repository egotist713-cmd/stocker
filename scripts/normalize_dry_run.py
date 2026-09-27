"""
Internal Normalization Engine на наборе — без БД, без регистрации и без изменения файлов
(валидация normalize-v1 на независимых наборах; паспорт §35ZQ).

    python scripts/normalize_dry_run.py data/samples/2026-09-27
    python scripts/normalize_dry_run.py --registered

Для каждого файла: факты → plan → (derivative во временную папку, удаляется) → views
full / preview / overview в памяти. Хеш source до и после сравнивается.
"""

import argparse
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import normalizer  # noqa: E402
from app.ingest import sha256_file  # noqa: E402
from scripts.scan_source_facts import registered_sources, scan  # noqa: E402


def run(rows: list[dict], sources: dict[str, Path], work: Path) -> list[dict]:
    results = []
    for row in rows:
        result = {"file": row["file"], "unchanged": row["unchanged"]}
        results.append(result)
        if "failed" in row:
            result["failed"] = row["failed"]["error_type"]
            continue
        facts = row["facts"]
        try:
            planned = normalizer.plan(facts)
        except normalizer.NormalizationRefused as exc:
            result["failed"] = exc.code
            continue
        source = sources[row["file"]]
        before = sha256_file(source)
        result["representation"] = planned["representation"]
        path = source
        if planned["representation"] == normalizer.DERIVATIVE:
            built = normalizer.build_derivative(source, work / row["file"].replace("#", "").replace(" ", "_"))
            result["derivative"] = {k: built[k] for k in ("width", "height", "mode", "icc_embedded")}
            path = built["path"]
        result["views"] = {}
        for variant in normalizer.PARAMS["views"]:
            try:
                image, info = normalizer.open_view(path, variant, facts["color_profile"], planned["representation"])
                result["views"][variant] = {**info, "size": image.size}
            except normalizer.NormalizationRefused as exc:
                result["views"][variant] = {"refused": exc.code}
        result["unchanged"] = result["unchanged"] and sha256_file(source) == before
    return results


def summarize(results: list[dict]) -> dict:
    ok = [r for r in results if "representation" in r]
    full = [r["views"]["full"] for r in ok]
    return {
        "files": len(results),
        "normalized": len(ok),
        "failed": Counter(r["failed"] for r in results if "failed" in r),
        "files_changed": [r["file"] for r in results if not r["unchanged"]],
        "representation": Counter(r["representation"] for r in ok),
        "view_color_space": Counter(v.get("color_space", v.get("refused")) for v in full),
        "view_color_converted": sum(bool(v.get("color_converted")) for v in full),
        "view_alpha_composited": sum(bool(v.get("alpha_composited")) for v in full),
        "view_refused": [(r["file"], r["views"]["full"]["refused"]) for r in ok if "refused" in r["views"]["full"]],
        "derivatives": [(r["file"], r["derivative"]) for r in ok if "derivative" in r],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Normalization engine dry run (no DB, no file changes)")
    parser.add_argument("folder", nargs="?")
    parser.add_argument("--registered", action="store_true", help="исходники объектов Stocker (только чтение БД)")
    args = parser.parse_args(argv)
    if args.registered:
        items = registered_sources()
    else:
        folder = (ROOT / args.folder).resolve()
        items = [(p.name, p) for p in sorted(q for q in folder.rglob("*") if q.is_file())]
    rows = scan(items=items)
    with tempfile.TemporaryDirectory(prefix="stocker-normalize-") as work:
        results = run(rows, dict(items), Path(work))
    for key, value in summarize(results).items():
        print(f"{key}: {dict(value) if isinstance(value, Counter) else value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
