from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = "http://192.168.1.104:1234/v1"
DEFAULT_MODEL = "qwen2.5-vl-7b-instruct"
DEFAULT_IMAGE = ROOT / "data" / "incoming" / "IMG_20260911_130107.jpg"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


@dataclass(frozen=True)
class BenchmarkConfig:
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    image: Path = DEFAULT_IMAGE
    results_dir: Path = RESULTS_DIR
    timeout: float = 180.0

