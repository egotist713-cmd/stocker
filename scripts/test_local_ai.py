from app.ai.local_analyzer import LocalAnalyzer


def test_local_ai() -> None:
    """Test local AI analysis on a sample image."""
    analyzer = LocalAnalyzer()
    result = analyzer.analyze("data/incoming/IMG_20260911_130107.jpg")
    print(repr(result.model_dump_json(indent=2)))


if __name__ == "__main__":
    test_local_ai()