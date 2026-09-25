import json

from ..client import BenchmarkClient
from ..prompts import TOOL_PROMPT

TOOLS = [
    {"type": "function", "function": {"name": "get_asset_info", "description": "Get existing metadata for an asset.", "parameters": {"type": "object", "properties": {"asset_id": {"type": "string"}}, "required": ["asset_id"]}}},
    {"type": "function", "function": {"name": "run_qc", "description": "Run quality control for an image.", "parameters": {"type": "object", "properties": {"asset_id": {"type": "string"}, "checks": {"type": "array", "items": {"type": "string"}}}, "required": ["asset_id"]}}},
    {"type": "function", "function": {"name": "save_metadata", "description": "Save metadata after review.", "parameters": {"type": "object", "properties": {"asset_id": {"type": "string"}, "metadata": {"type": "object"}}, "required": ["asset_id", "metadata"]}}},
]


def run(client: BenchmarkClient, image):
    details = {"capability": "not_tested", "tool_call_requested": True, "tool_call_received": False, "tool_name": None, "tool_arguments": None, "unsupported_reason": None}
    try:
        response, latency = client.chat(TOOL_PROMPT, [image], tools=TOOLS, tool_choice="auto")
    except Exception as exc:
        details["unsupported_reason"] = f"request_error_at_endpoint_or_client: {type(exc).__name__}: {exc}"
        return None, 0.0, {**details, "error": str(exc), "raw_output": ""}
    calls = []
    for call in (getattr(client.tool_message(response), "tool_calls", None) or []):
        function = call.function
        try:
            arguments = json.loads(function.arguments or "{}")
            argument_error = None
        except Exception as exc:
            arguments = function.arguments
            argument_error = str(exc)
        calls.append({"name": function.name, "arguments": arguments, "argument_error": argument_error})
    if calls:
        details.update({"capability": "tested", "tool_call_received": True, "tool_name": [call["name"] for call in calls], "tool_arguments": [call["arguments"] for call in calls]})
    else:
        details["unsupported_reason"] = "request_succeeded_but_response_contained_no_structured_tool_call"
    return response, latency, {**details, "tool_calls": calls, "raw_output": client.content_text(response)}