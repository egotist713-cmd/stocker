"""
Статистика pipeline по реальным объектам (только чтение БД и событий).

    python scripts/pipeline_stats.py                 # все объекты
    python scripts/pipeline_stats.py --from-id 10    # только выборка с id >= 10
    python scripts/pipeline_stats.py --ids @logs/user_set_ids.json   # отобранный набор
    python scripts/pipeline_stats.py --json          # машиночитаемый вывод

Собирает: QC, Enhancement, Vision, metadata и причины human_review, Stock
Readiness, длительности AI. Ничего не пишет.
"""

import argparse
import json
import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import creative_review, enhancement_decision, stock_readiness  # noqa: E402

from app.database import db  # noqa: E402

DB = db.db_path()  # STOCKER_DATA_DIR


def _json(value):
    try:
        return json.loads(value) if value else None
    except (json.JSONDecodeError, TypeError):
        return None


def _durations(values: list[float]) -> dict | None:
    if not values:
        return None
    return {"n": len(values), "median_s": round(statistics.median(values), 1), "max_s": round(max(values), 1)}


def collect(from_id: int, to_id: int | None, ids: list[int] | None = None) -> dict:
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        query = "SELECT * FROM assets WHERE id >= ?" + (" AND id <= ?" if to_id else "") + " ORDER BY id"
        assets = [dict(r) for r in connection.execute(query, (from_id, to_id) if to_id else (from_id,))]
        if ids is not None:
            assets = [a for a in assets if a["id"] in set(ids)]
        events = {}
        for row in connection.execute("SELECT id, asset_id, stage, status, message FROM processing_events ORDER BY id"):
            events.setdefault(row["asset_id"], []).append(dict(row))
    finally:
        connection.close()

    stats = {
        "assets": len(assets),
        "asset_ids": [a["id"] for a in assets],
        "qc": Counter(), "qc_errors": Counter(), "qc_warnings": Counter(),
        "enhancement": Counter(), "enhancement_decided_by": Counter(), "enhancement_reasons": Counter(),
        "enhancement_notes": Counter(), "enhancement_failed": Counter(),
        "vision": Counter(), "vision_errors": Counter(), "vision_durations": [],
        "metadata_state": Counter(), "metadata_completeness": Counter(), "metadata_ai_failed": Counter(),
        "metadata_validation_errors": Counter(), "metadata_validation_warnings": Counter(), "metadata_durations": [],
        "human_review_reasons": Counter(), "gate_notes": Counter(),
        "readiness": {}, "readiness_checks": Counter(), "categories": Counter(),
        "creative_recommendation": Counter(), "creative_potential": Counter(),
        "per_asset": [],
    }

    for asset in assets:
        history = events.get(asset["id"], [])
        qc = _json(asset["qc_result"]) or {}
        metadata = _json(asset["metadata_json"])
        row = {"id": asset["id"], "file": asset["filename"]}

        # QC
        qc_state = "pending" if not qc else ("passed" if qc.get("passed") else "failed")
        stats["qc"][qc_state] += 1
        stats["qc_errors"].update(e.split(":")[0] for e in qc.get("errors", []))
        stats["qc_warnings"].update(qc.get("warnings", []))
        row["qc"] = qc_state

        # Enhancement
        enh = enhancement_decision.summary_from_events(asset, history)
        decision = enh["decision"] or "not_assessed"
        stats["enhancement"][decision] += 1
        if enh["decided_by"]:
            stats["enhancement_decided_by"][enh["decided_by"]] += 1
        stats["enhancement_reasons"].update(enh["reasons"])
        assessed = next((e for e in reversed(history) if (e["stage"], e["status"]) == ("ENHANCEMENT", "ASSESSED")), None)
        for note in (_json(assessed["message"]) or {}).get("notes", []) if assessed else []:
            stats["enhancement_notes"][note["code"]] += 1
        for event in history:
            if (event["stage"], event["status"]) == ("ENHANCEMENT", "FAILED"):
                message = _json(event["message"]) or {}
                stats["enhancement_failed"][f"{message.get('stage')}:{message.get('error_type')}"] += 1
        row["enhancement"] = decision

        # Vision
        vision_state = "done" if asset["ai_result"] else "not_done"
        for event in history:
            if event["stage"] != "AI":
                continue
            message = _json(event["message"]) or {}
            if event["status"] == "FAILED":
                stats["vision_errors"][message.get("error_type", "?")] += 1
                vision_state = vision_state if asset["ai_result"] else "failed"
            elif event["status"] == "PASSED" and message.get("duration_s") is not None:
                stats["vision_durations"].append(message["duration_s"])
        stats["vision"][vision_state] += 1
        row["vision"] = vision_state

        # Metadata и gate
        state = metadata["state"] if metadata else "none"
        stats["metadata_state"][state] += 1
        row["metadata"] = state
        if metadata:
            stats["metadata_completeness"][metadata.get("completeness")] += 1
            stats["metadata_validation_errors"].update(e["code"] for e in metadata["validation"]["errors"])
            stats["metadata_validation_warnings"].update(w["code"] for w in metadata["validation"]["warnings"])
            gate = metadata.get("review_gate") or {}
            if state == "human_review":
                stats["human_review_reasons"].update(r["code"] for r in gate.get("reasons", []))
                row["review_reasons"] = [r["code"] for r in gate.get("reasons", [])]
            stats["gate_notes"].update(n["code"] for n in gate.get("notes", []))
        for event in history:
            if event["stage"] == "METADATA_AI":
                message = _json(event["message"]) or {}
                if event["status"] == "FAILED":
                    stats["metadata_ai_failed"][message.get("error_type", "?")] += 1
                elif message.get("duration_s") is not None:
                    stats["metadata_durations"].append(message["duration_s"])

        # Stock Readiness
        readiness = stock_readiness.summary(asset, history, metadata)
        for platform, status in readiness["platforms"].items():
            stats["readiness"].setdefault(platform, Counter())[status] += 1
        evaluated = next((e for e in reversed(history) if (e["stage"], e["status"]) == ("READINESS", "EVALUATED")), None)
        result = _json(evaluated["message"]) if evaluated else None
        if result and not readiness["stale"]:
            for platform, data in result["platforms"].items():
                stats["readiness_checks"].update(f"{platform}:{c['code']}:{c['level']}" for c in data["checks"])
                plan = data.get("export_plan") or {}
                for category in ([plan["category"]] if plan.get("category") else plan.get("categories", [])):
                    stats["categories"][f"{platform}:{category}"] += 1
        row["readiness"] = readiness["platforms"]

        # Creative Review (советник, не этап pipeline)
        creative = creative_review.summary_from_events(history)
        stats["creative_recommendation"][creative["recommendation"] or "not_reviewed"] += 1
        stats["creative_potential"][creative["commercial_potential"] or "not_reviewed"] += 1
        row["creative"] = (creative["commercial_score"], creative["recommendation"])
        stats["per_asset"].append(row)

    stats["vision_durations"] = _durations(stats["vision_durations"])
    stats["metadata_durations"] = _durations(stats["metadata_durations"])
    return stats


def _print(stats: dict) -> None:
    print(f"Assets: {stats['assets']} (ids {stats['asset_ids'][0] if stats['asset_ids'] else '-'}..{stats['asset_ids'][-1] if stats['asset_ids'] else '-'})")
    for key, value in stats.items():
        if key in ("assets", "asset_ids", "per_asset"):
            continue
        if isinstance(value, Counter):
            value = dict(value.most_common())
        elif isinstance(value, dict) and value and all(isinstance(v, Counter) for v in value.values()):
            value = {k: dict(v.most_common()) for k, v in value.items()}
        print(f"{key}: {value}")
    print("\nper asset:")
    for row in stats["per_asset"]:
        print(" ", row)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--from-id", type=int, default=1)
    parser.add_argument("--to-id", type=int)
    parser.add_argument("--ids", help="список id через запятую или @файл с JSON-списком (logs/user_set_ids.json)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    ids = None
    if args.ids:
        ids = json.loads(Path(args.ids[1:]).read_text()) if args.ids.startswith("@") else [int(x) for x in args.ids.split(",")]
    stats = collect(args.from_id, args.to_id, ids)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2, default=dict))
    else:
        _print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
