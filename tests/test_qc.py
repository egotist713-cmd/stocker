import pytest
from PIL import Image

from app import qc


@pytest.fixture
def platform_minimum(monkeypatch):
    """Настоящие пороги QC (conftest снижает минимум для маленьких тестовых картинок)."""
    monkeypatch.setattr(qc, "MIN_MEGAPIXELS", 4.0)
    monkeypatch.setattr(qc, "RECOMMENDED_MEGAPIXELS", 12.0)


def _check(root, size):
    path = root / "data" / "incoming" / "img.jpg"
    Image.new("RGB", size, "gray").save(path, "JPEG")
    return qc.check_asset({"source_path": "data/incoming/img.jpg"})


@pytest.mark.parametrize("size,passed,errors,warnings", [
    ((1999, 1999), False, ["RESOLUTION_TOO_LOW"], []),                     # 3.996 MP — ниже площадок
    ((2000, 2000), True, [], ["RESOLUTION_BELOW_RECOMMENDED"]),             # 4 MP — минимум площадок
    ((3000, 2000), True, [], ["RESOLUTION_BELOW_RECOMMENDED"]),             # 6 MP — только warning
    ((4000, 3000), True, [], []),                                           # 12 MP — рекомендация
])
def test_qc_blocks_only_below_platform_minimum(stocker_root, platform_minimum, size, passed, errors, warnings):
    result = _check(stocker_root, size)

    assert result["passed"] is passed
    assert [e for e in result["errors"] if e.startswith("RESOLUTION")] == errors
    assert [w for w in result["warnings"] if w.startswith("RESOLUTION")] == warnings
