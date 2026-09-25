import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.model_benchmark.client import BenchmarkClient
from scripts.model_benchmark.config import BenchmarkConfig
from scripts.model_benchmark.logger import JsonlLogger, utc_timestamp
from scripts.model_benchmark.tests import test_multi_image, test_stock_analysis, test_structured, test_tools, test_vision


def record(logger, config, name, started, response=None, extra=None, error=None):
    extra = extra or {}
    usage = BenchmarkClient.usage(response) if response is not None else {"input_tokens": None, "output_tokens": None}
    success = error is None and not extra.get("unsupported_reason") and not extra.get("validation_error") and extra.get("schema_valid", True) is not False
    data = {"timestamp": utc_timestamp(), "model": config.model, "test": name, "success": success, "latency_sec": round(time.perf_counter() - started, 4), "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"), "raw_output": extra.get("raw_output", ""), "parsed_result": extra.get("parsed_result"), "error": error, "details": {k: v for k, v in extra.items() if k not in {"raw_output", "parsed_result"}}}
    for field in ("strict_json", "json_parse_success", "schema_valid", "number_of_images", "tool_call_requested", "tool_call_received", "tool_name", "tool_arguments", "unsupported_reason", "capability"):
        if field in extra:
            data[field] = extra[field]
    if "usage" in usage:
        data["usage"] = usage["usage"]
    logger.write(data)
    return data


def main():
    parser = argparse.ArgumentParser(description="Run isolated LM Studio model benchmark")
    parser.add_argument("--model", default=BenchmarkConfig.model)
    parser.add_argument("--base-url", default=BenchmarkConfig.base_url)
    parser.add_argument("--image", type=Path, default=BenchmarkConfig.image)
    args = parser.parse_args()
    config = BenchmarkConfig(model=args.model, base_url=args.base_url, image=args.image)
    if not config.image.exists():
        raise SystemExit(f"Image not found: {config.image}")
    image_paths = sorted(p for p in config.image.parent.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    output = config.results_dir / f"{config.model}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    logger = JsonlLogger(output)
    client = BenchmarkClient(config.base_url, config.model)
    cases = [("basic_vision", lambda: test_vision.run(client, config.image)), ("structured_output", lambda: test_structured.run(client, config.image)), ("stock_analysis", lambda: test_stock_analysis.run(client, config.image)), ("tool_calling", lambda: test_tools.run(client, config.image))]
    for name, case in cases:
        started = time.perf_counter()
        try:
            response, _latency, extra = case()
            result = record(logger, config, name, started, response, extra)
        except Exception as exc:
            result = record(logger, config, name, started, error=f"{type(exc).__name__}: {exc}")
        print(json.dumps({"test": name, "success": result["success"], "latency_sec": result["latency_sec"], "error": result["error"]}, ensure_ascii=False))
    for number_of_images in range(1, min(3, len(image_paths)) + 1):
        name = f"multi_image_{number_of_images}"
        started = time.perf_counter()
        try:
            response, _latency, extra = test_multi_image.run(client, image_paths[:number_of_images])
            result = record(logger, config, name, started, response, extra)
        except Exception as exc:
            result = record(logger, config, name, started, error=f"{type(exc).__name__}: {exc}", extra={"number_of_images": number_of_images})
        print(json.dumps({"test": name, "success": result["success"], "number_of_images": number_of_images, "latency_sec": result["latency_sec"], "error": result["error"]}, ensure_ascii=False))
    print(f"RESULT_FILE={output}")


if __name__ == "__main__":
    main()