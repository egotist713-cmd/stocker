from ..client import BenchmarkClient
from ..prompts import VISION_PROMPT


def run(client: BenchmarkClient, image):
    response, latency = client.chat(VISION_PROMPT, [image])
    return response, latency, {"raw_output": client.content_text(response)}

