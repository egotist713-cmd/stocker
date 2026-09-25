from ..client import BenchmarkClient
from ..prompts import MULTI_IMAGE_PROMPT


def run(client: BenchmarkClient, images):
    response, latency = client.chat(MULTI_IMAGE_PROMPT, images, image_max_edge=768)
    return response, latency, {"raw_output": client.content_text(response), "number_of_images": len(images)}