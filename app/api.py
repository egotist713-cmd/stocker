"""
JSON CLI service layer (docs/SERVICE_CONTRACT.md §6).

    python -m app.api operations.list
    python -m app.api asset.get --params '{"asset_id": 5}'
    echo '{"asset_id": 5}' | python -m app.api asset.history --params -
    python -m app.api metadata.gate --params '{"asset_id": 5}' --actor workflow:n8n

stdout — только envelope JSON (UTF-8). Прогресс worker'а уходит в stderr.
Exit code: 0 — ok, 1 — not ok, 2 — ошибка использования CLI.
Существующие CLI (app.worker, app.metadata) не меняются.
"""

import argparse
import contextlib
import json
import sys

from app.service import HUMAN, dispatch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.api", description="Stocker service layer (JSON).")
    parser.add_argument("operation", help="operation name, e.g. asset.get; see operations.list")
    parser.add_argument("--params", default=None, help="JSON object, or '-' to read it from stdin")
    parser.add_argument("--actor", default=HUMAN, help="human (default), agent:<name>, workflow:<name>")
    parser.add_argument("--pretty", action="store_true", help="indent JSON output")
    args = parser.parse_args(argv)

    if args.params is None:
        params = {}
    else:
        raw = sys.stdin.read() if args.params == "-" else args.params
        try:
            params = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            parser.error(f"--params is not valid JSON: {exc}")
        if not isinstance(params, dict):
            parser.error("--params must be a JSON object")

    # stdout — только для envelope: print() из worker/ingest уходит в stderr.
    with contextlib.redirect_stdout(sys.stderr):
        envelope = dispatch(args.operation, params, actor=args.actor)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False, indent=2 if args.pretty else None) + "\n")

    return 0 if envelope["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
