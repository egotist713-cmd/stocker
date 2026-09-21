from pathlib import Path

from app.ai.openai_analyzer import OpenAIAnalyzer


IMAGE = Path(
    "F:/stock/stocker/data/incoming/IMG_20260911_130107.jpg"
)


analyzer = OpenAIAnalyzer()

result = analyzer.analyze(IMAGE)

print("=== AI ANALYSIS ===")
print(result.model_dump_json(indent=2, ensure_ascii=False))