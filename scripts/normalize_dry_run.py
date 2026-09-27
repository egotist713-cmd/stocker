"""
Internal Normalization Engine на наборе — без БД, без регистрации и без изменения файлов
(валидация normalize-v1 на независимых наборах; паспорт §35ZQ).

    python scripts/normalize_dry_run.py data/samples/2026-09-27
    python scripts/normalize_dry_run.py --registered

Для каждого файла: факты → plan → (derivative во временную папку, удаляется) →
AnalysisView (тот же, что получают стадии) → Enhancement правилами и цветовой вердикт
Readiness по фактам. Хеш source до и после сравнивается.
"""

import argparse
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import analysis_view, normalizer  # noqa: E402
from app import enhancement as en  # noqa: E402
from app import readiness as rd  # noqa: E402
from app.ingest import sha256_file  # noqa: E402
from scripts.scan_source_facts import registered_sources, scan  # noqa: E402


def _color_verdict(color: dict) -> str:
    """Цветовая проверка Readiness (по фактам, без пикселей) для профиля Adobe."""
    facts = {"source": "ok", "qc_passed": True, "format": "JPEG", "width": 9000, "height": 9000, "file_size": 1,
             "color": {k: color.get(k) for k in ("kind", "description", "source", "declared")}}
    checks, _ = rd.file_checks(rd.PROFILES["adobe"], facts)
    return next((c["code"] for c in checks if c["code"].startswith("COLOR")), "-")


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
        try:
            view = analysis_view.from_file(path, planned["preserved"]["color"], planned["representation"], before)
        except analysis_view.ViewUnavailable as exc:
            result["view"] = {"refused": exc.code}
        else:
            result["view"] = {"color_space": view.color_space, "color_converted": view.color_converted,
                              "alpha_composited": view.alpha_composited, "size": view.size}
            assessment = en.assess(en.measure_view(view, facts["jpeg_quality"]), before)
            result["enhancement"] = assessment["decision"] or ("disputed" if assessment["disputed"] else None)
        result["readiness_color"] = _color_verdict(facts["color_profile"])
        result["unchanged"] = result["unchanged"] and sha256_file(source) == before
    return results


def summarize(results: list[dict]) -> dict:
    ok = [r for r in results if "representation" in r]
    views = [r["view"] for r in ok]
    return {
        "files": len(results),
        "normalized": len(ok),
        "failed": Counter(r["failed"] for r in results if "failed" in r),
        "files_changed": [r["file"] for r in results if not r["unchanged"]],
        "representation": Counter(r["representation"] for r in ok),
        "view_color_space": Counter(v.get("color_space", v.get("refused")) for v in views),
        "view_color_converted": sum(bool(v.get("color_converted")) for v in views),
        "view_alpha_composited": sum(bool(v.get("alpha_composited")) for v in views),
        "view_refused": [(r["file"], r["view"]["refused"]) for r in ok if "refused" in r["view"]],
        "enhancement": Counter(r.get("enhancement") for r in ok),
        "readiness_color": Counter(r["readiness_color"] for r in ok),
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
