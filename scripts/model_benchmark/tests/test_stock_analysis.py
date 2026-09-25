from ..client import BenchmarkClient
from ..prompts import STOCK_ANALYSIS_PROMPT
from .test_structured import diagnostics


def run(client: BenchmarkClient, image):
    response, latency = client.chat(STOCK_ANALYSIS_PROMPT, [image], response_format={"type": "text"})
    raw = client.content_text(response)
    return response, latency, {"raw_output": raw, **diagnostics(raw)}