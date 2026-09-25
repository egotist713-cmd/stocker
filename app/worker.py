from pathlib import Path

from app.ai.local_analyzer import LocalAnalyzer
from app.database.db import add_event, get_asset, save_ai_result
from app.ingest import ingest_file
from app.qc import check_asset, save_qc_result


def process_file(path: Path) -> int | None:
    print(f"WORKER: {path}")

    asset_id = ingest_file(path)

    if asset_id is None:
        print("Worker stopped: file was not added")
        return None

    asset = get_asset(asset_id)

    if asset is None:
        raise RuntimeError(f"Asset not found after ingest: asset_id {asset_id}")

    qc_result = check_asset(asset)
    save_qc_result(asset_id, qc_result)

    print(f"QC: {qc_result['passed']}")

    if not qc_result["passed"]:
        print("AI: skipped because QC failed")
        print(f"Asset ID: {asset_id}")
        return asset_id

    ai_result = LocalAnalyzer().analyze(path)
    save_ai_result(asset_id, ai_result.model_dump_json())
    add_event(asset_id, "AI", "PASSED", "AI analysis completed")

    print("AI: PASSED")
    print(f"Asset ID: {asset_id}")

    return asset_id


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python -m app.worker IMAGE_PATH")
        raise SystemExit(1)

    process_file(Path(sys.argv[1]))
