from pathlib import Path
import base64

from app.ai.local_analyzer import LocalAnalyzer
from app.analysis_view import view_of_file


def main():
    analyzer = LocalAnalyzer()
    image_path = Path("data/incoming/IMG_20260911_130107.jpg")

    image_bytes, mime_type = analyzer._prepare_image(view_of_file(image_path))
    image_base64 = base64.b64encode(image_bytes).decode("utf-8")

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": 'Return ONLY this exact JSON: {"test":true}',
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{image_base64}"
                    },
                },
            ],
        }
    ]

    print("SENDING...")
    response = analyzer.client.chat.completions.create(
        model=analyzer.model,
        messages=messages,
    )

    content = response.choices[0].message.content
    print("RAW REPR:")
    print(repr(content))
    print("RAW CONTENT:")
    print(content)


if __name__ == "__main__":
    main()
