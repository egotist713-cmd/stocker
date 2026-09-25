import base64
import mimetypes
import time
from io import BytesIO
from pathlib import Path
from typing import Any

from openai import OpenAI
from PIL import Image


class BenchmarkClient:
    def __init__(self, base_url: str, model: str, timeout: float = 180.0):
        self.model = model
        self.client = OpenAI(api_key="lm-studio", base_url=base_url, timeout=timeout)

    @staticmethod
    def image_data_url(path: Path, max_edge: int = 1024) -> str:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with Image.open(path) as image:
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            output = BytesIO()
            image_format = "JPEG" if mime in {"image/jpeg", "image/jpg"} else image.format or "PNG"
            if image_format.upper() == "JPEG":
                image = image.convert("RGB")
            image.save(output, format=image_format)
            encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def chat(self, prompt: str, images: list[Path] | None = None, image_max_edge: int = 1024, **kwargs: Any) -> tuple[Any, float]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in images or []:
            content.append({"type": "image_url", "image_url": {"url": self.image_data_url(image, image_max_edge)}})
        started = time.perf_counter()
        response = self.client.chat.completions.create(model=self.model, messages=[{"role": "user", "content": content}], **kwargs)
        return response, time.perf_counter() - started

    @staticmethod
    def content_text(response: Any) -> str:
        content = response.choices[0].message.content or ""
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content)

    @staticmethod
    def usage(response: Any) -> dict[str, Any]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {"input_tokens": None, "output_tokens": None}
        data = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
        return {"input_tokens": data.get("prompt_tokens", data.get("input_tokens")), "output_tokens": data.get("completion_tokens", data.get("output_tokens")), "usage": data}

    @staticmethod
    def tool_message(response: Any) -> Any:
        return response.choices[0].message