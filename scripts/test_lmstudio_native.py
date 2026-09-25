import base64
import json
import urllib.request
from pathlib import Path

from app.ai.local_analyzer import LocalAnalyzer

analyzer = LocalAnalyzer()

path = Path("data/incoming/IMG_20260911_130107.jpg")
image_bytes, mime_type = analyzer._prepare_image(path, "image/jpeg")
image_base64 = base64.b64encode(image_bytes).decode()

data = {
    "model": "qwen2.5-vl-7b-instruct",
    "input": [
        {
            "type": "text",
            "content": "Describe this image in one sentence.",
        },
        {
            "type": "image",
            "data_url": f"data:{mime_type};base64,{image_base64}",
        },
    ],
    "store": False,
}

request = urllib.request.Request(
    "http://192.168.1.104:1234/api/v1/chat",
    data=json.dumps(data).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)

with urllib.request.urlopen(request) as response:
    print(response.read().decode())