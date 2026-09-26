"""
Удаление распознанного текста документов с персональными данными из уже
сохранённых данных (gate-v1.2, паспорт §35ZG).

    python scripts/redact_personal_documents.py           # пробный прогон: только счётчики
    python scripts/redact_personal_documents.py --apply   # изменить БД

Для asset, у которых Vision описывает документ (review_gate.personal_document):
- assets.ai_result: text_visible → [];
- assets.metadata_json и сообщения событий этого asset: строки, содержащие
  распознанный текст, → "[redacted]"; review_gate.text_items → [];
- событие PRIVACY/REDACTED с перечнем изменённого (без самого текста).

Это единственное место, где меняются прошлые события: защита персональных
данных важнее неизменности истории. Изображения не трогаются.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import review_gate as rg  # noqa: E402
from app.ai.schema import AIAnalysis  # noqa: E402
from app.database.db import acting_as, get_connection, insert_event  # noqa: E402
from app.textnorm import normalize_text  # noqa: E402

REDACTED = "[redacted]"
MIN_TEXT_LENGTH = 3


def _redact(value, texts: set[str]) -> tuple[object, int]:
    """Заменить строки, содержащие распознанный текст; вернуть (значение, число замен)."""
    if isinstance(value, str):
        folded = value.casefold()
        return (REDACTED, 1) if any(t in folded for t in texts) else (value, 0)
    if isinstance(value, list):
        items = [_redact(v, texts) for v in value]
        return [v for v, _ in items], sum(n for _, n in items)
    if isinstance(value, dict):
        items = {k: _redact(v, texts) for k, v in value.items()}
        return {k: v for k, (v, _) in items.items()}, sum(n for _, n in items.values())
    return value, 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Redact recognized text of personal documents")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor", default="agent:claude-code")
    args = parser.parse_args(argv)

    connection = get_connection()
    try:
        assets = connection.execute("SELECT id, ai_result, metadata_json FROM assets WHERE ai_result IS NOT NULL").fetchall()
        for asset in assets:
            vision = AIAnalysis.model_validate_json(asset["ai_result"])
            terms = rg.personal_document(vision)
            metadata = json.loads(asset["metadata_json"]) if asset["metadata_json"] else None
            text_items = ((metadata or {}).get("review_gate") or {}).get("text_items") or []
            if not terms or not (vision.text_visible or text_items):
                continue

            texts = {normalize_text(t).casefold() for t in vision.text_visible} | {t["text"].casefold() for t in text_items}
            texts = {t for t in texts if len(t) >= MIN_TEXT_LENGTH}

            new_vision = vision.model_copy(update={"text_visible": []})
            new_metadata, metadata_hits = _redact(metadata, texts) if metadata else (None, 0)
            if new_metadata and new_metadata.get("review_gate"):
                new_metadata["review_gate"]["text_items"] = []

            event_updates = []
            for event in connection.execute(
                "SELECT id, message FROM processing_events WHERE asset_id = ? AND message LIKE '{%'", (asset["id"],)
            ).fetchall():
                message = json.loads(event["message"])
                redacted, hits = _redact(message, texts)
                if hits:
                    event_updates.append((event["id"], json.dumps(redacted, ensure_ascii=False), hits))

            summary = {
                "reason": "PERSONAL_DOCUMENT",
                "terms": terms,
                "text_visible_removed": len(vision.text_visible),
                "text_items_removed": len(text_items),
                "metadata_strings_redacted": metadata_hits,
                "events_updated": [event_id for event_id, _, _ in event_updates],
                "event_strings_redacted": sum(hits for _, _, hits in event_updates),
            }
            print(f"asset {asset['id']}: {summary}")

            if args.apply:
                connection.execute("UPDATE assets SET ai_result = ? WHERE id = ?", (new_vision.model_dump_json(), asset["id"]))
                if new_metadata is not None:
                    connection.execute(
                        "UPDATE assets SET metadata_json = ? WHERE id = ?",
                        (json.dumps(new_metadata, ensure_ascii=False), asset["id"]),
                    )
                for event_id, message, _ in event_updates:
                    connection.execute("UPDATE processing_events SET message = ? WHERE id = ?", (message, event_id))
                with acting_as(args.actor):
                    insert_event(connection, asset["id"], "PRIVACY", "REDACTED", json.dumps(summary, ensure_ascii=False))
        if args.apply:
            connection.commit()
            print("applied")
        else:
            print("dry run: nothing changed (use --apply)")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
