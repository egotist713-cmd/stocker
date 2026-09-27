"""
AnalysisView — единственный вход пикселей для стадий анализа
(docs/INTERNAL_IMAGE_REPRESENTATION_CONTRACT.md §4, §14).

    SOURCE / INTERNAL DERIVATIVE → open_asset_view() → AnalysisView → QC, Enhancement, Vision, …

Стадии не открывают source / derivative сами и сами не решают ни ориентацию, ни
цвет: view строится один раз по манифесту NORMALIZE/PASSED (normalizer.open_view),
варианты preview / overview — уменьшения того же full. Без действительного
манифеста view нет (ViewUnavailable) — обратного пути к «открыть source как есть» нет.

Readiness пикселей не читает: он работает по фактам источника (не через view).
"""

import base64
from io import BytesIO
from pathlib import Path

from PIL import Image

from app import ingest, normalization, normalizer
from app.database.db import get_asset

# Коды отказа (стадия, запросившая view, записывает их как свою причину).
NORMALIZE_NOT_PASSED = "NORMALIZE_NOT_PASSED"
NORMALIZE_STALE = "NORMALIZE_STALE"
REPRESENTATION_INVALID = "REPRESENTATION_INVALID"


class ViewUnavailable(Exception):
    """View нельзя построить; code — причина (выше или код normalizer §7)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class AnalysisView:
    """
    Подготовленное изображение объекта: ориентировано, 8 бит RGB, альфа на белом,
    цвет — sRGB (объявленный и конвертированный) или undeclared (как есть). ICC нет.
    """

    def __init__(self, full: Image.Image, info: dict, *, source_sha256: str, representation: str,
                 asset_id: int | None = None, source_format: str | None = None):
        self.full = full
        self.asset_id = asset_id
        self.source_sha256 = source_sha256
        self.source_format = source_format
        self.representation = representation
        self.color_space = info["color_space"]
        self.color_converted = info["color_converted"]
        self.alpha_composited = info["alpha_composited"]
        self.fingerprint = normalizer.view_fingerprint(source_sha256)
        self._variants = {"full": full}

    @property
    def size(self) -> tuple[int, int]:
        return self.full.size

    def variant(self, name: str) -> Image.Image:
        """full / preview (2048) / overview (1536): уменьшение того же full, без увеличения."""
        if name not in self._variants:
            limit = normalizer.PARAMS["views"][name]
            image = self.full.copy()
            if limit and max(image.size) > limit:
                image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
            self._variants[name] = image
        return self._variants[name]

    def jpeg(self, name: str, quality: int = 90) -> bytes:
        output = BytesIO()
        self.variant(name).save(output, format="JPEG", quality=quality, optimize=True)
        return output.getvalue()

    def data_url(self, name: str, quality: int = 90) -> str:
        return "data:image/jpeg;base64," + base64.b64encode(self.jpeg(name, quality)).decode("ascii")

    def identity(self) -> dict:
        """Для provenance событий стадий: какой именно view видела стадия."""
        return {
            "view_version": normalizer.VIEW_VERSION,
            "fingerprint": self.fingerprint,
            "representation": self.representation,
            "color_space": self.color_space,
            "color_converted": self.color_converted,
            "alpha_composited": self.alpha_composited,
            "size": list(self.size),
        }


def from_file(path: Path, color: dict, representation: str, source_sha256: str, **extra) -> AnalysisView:
    """View файла по цвету манифеста (тесты и dry-run; стадии используют open_asset_view)."""
    try:
        image, info = normalizer.open_view(Path(path), "full", color, representation)
    except normalizer.NormalizationRefused as exc:
        raise ViewUnavailable(exc.code, str(exc)) from exc
    return AnalysisView(image, info, source_sha256=source_sha256, representation=representation, **extra)


def view_of_file(path: Path) -> AnalysisView:
    """
    View незарегистрированного файла — для отладочных скриптов и dry-run (не для стадий).
    Те же факты, plan и правила views; derivative не пишется: декодирование в памяти
    то же, что у lossless derivative (§3).
    """
    from app.ingest import sha256_file
    from app.source_facts import read_facts

    path = Path(path)
    try:
        planned = normalizer.plan(read_facts(path, path.name))
    except normalizer.NormalizationRefused as exc:
        raise ViewUnavailable(exc.code, str(exc)) from exc
    return from_file(path, planned["preserved"]["color"], normalizer.SOURCE, sha256_file(path))


def open_asset_view(asset_id: int) -> AnalysisView:
    """View объекта по действительному манифесту NORMALIZE/PASSED; файл representation сверяется по хешу."""
    asset = get_asset(asset_id)
    if asset is None:
        raise ViewUnavailable("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")
    state = normalization.get(asset_id)
    manifest = state["manifest"]
    if manifest is None:
        raise ViewUnavailable(NORMALIZE_NOT_PASSED, f"Asset {asset_id} has no NORMALIZE/PASSED; run normalize.run first")
    if manifest.get("fingerprint") != normalizer.fingerprint(asset["file_hash"]):
        raise ViewUnavailable(NORMALIZE_STALE, f"Normalization of asset {asset_id} is stale; run normalize.run first")

    path = normalization.representation_path(asset, manifest)
    expected = asset["file_hash"] if manifest["representation"] == normalizer.SOURCE else manifest["derivative"]["sha256"]
    if not path.exists() or ingest.sha256_file(path) != expected:
        raise ViewUnavailable(REPRESENTATION_INVALID, f"Representation file of asset {asset_id} is missing or changed")

    facts = state.get("facts") or {}
    return from_file(path, manifest["preserved"]["color"], manifest["representation"], asset["file_hash"],
                     asset_id=asset_id, source_format=facts.get("format"))
