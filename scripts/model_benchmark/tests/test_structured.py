import json
import re

from ..client import BenchmarkClient
from ..prompts import STRUCTURED_PROMPT
from app.ai.schema import AIAnalysis


def diagnostics(raw: str) -> dict:
    candidate = raw.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.DOTALL | re.IGNORECASE)
    strict_json = match is None
    if match:
        candidate = match.group(1).strip()
    json_parse_success = False
    json_result = None
    json_error = None
    try:
        json_result = json.loads(candidate)
        json_parse_success = True
    except Exception as exc:
        json_error = str(exc)
    schema_valid = False
    parsed = None
    validation_error = None
    if json_parse_success:
        try:
            parsed = AIAnalysis.model_validate(json_result).model_dump()
            schema_valid = True
        except Exception as exc:
            validation_error = str(exc)
    return {"strict_json": strict_json, "json_parse_success": json_parse_success, "schema_valid": schema_valid, "parsed_result": parsed, "json_error": json_error, "validation_error": validation_error}


def run(client: BenchmarkClient, image):
    response, latency = client.chat(STRUCTURED_PROMPT, [image], response_format={"type": "text"})
    raw = client.content_text(response)
    return response, latency, {"raw_output": raw, **diagnostics(raw)}