"""
Локальный веб-интерфейс Stocker v1 (паспорт §35ZZZN).

Тонкий слой над сервисным слоем: все чтения и действия — app.service.dispatch(..., actor="human"),
те же операции, что у CLI / MCP; в БД и файлы мимо сервиса не пишет. Пиксели превью — только
через AnalysisView; «100 %» — исходный файл по asset_id (путь из параметров не принимается).
Только 127.0.0.1; POST — с токеном сессии сервера; работает на production-каталоге data/prod.

    scripts\\start_ui.cmd        (STOCKER_DATA_DIR=data/prod, http://127.0.0.1:8780)
"""

from __future__ import annotations

import hmac
import io
import secrets
from urllib.parse import quote
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

from app import analysis_view, ingest
from app.database import db
from app.service import dispatch
from app.ui import humanize

HOST, PORT = "127.0.0.1", 8780
HUMAN = "human"
PLATFORMS = ("adobe", "shutterstock")
APPROVED_STATES = ("publication_approved", "ready_for_export")
PREVIEW_PX, THUMB_PX = 2000, 480
ATTEST_NOTE = "люди в кадре не узнаваемы — решение пользователя в веб-интерфейсе"
RETURN_REASON = "вернул пользователь"

_HERE = Path(__file__).resolve().parent
_MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".tif": "image/tiff",
          ".tiff": "image/tiff", ".avif": "image/avif"}


class Previews:
    """Превью / миниатюры в памяти (по file_hash); строятся из AnalysisView, на диск не пишутся."""

    def __init__(self, limit: int = 64):
        self.cache: OrderedDict = OrderedDict()
        self.limit = limit

    def get(self, asset_id: int, file_hash: str, size: int) -> bytes:
        key = (asset_id, file_hash, size)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        view = analysis_view.open_asset_view(asset_id)
        image = view.full.copy()
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=85)
        self.cache[key] = buffer.getvalue()
        if len(self.cache) > self.limit:
            self.cache.popitem(last=False)
        return self.cache[key]


def _go(path: str, error: str | None = None) -> RedirectResponse:
    """Редирект после POST; текст ошибки — в параметре error (URL-кодирован)."""
    return RedirectResponse(f"{path}?error={quote(error[:300])}" if error else path, status_code=303)


def _call(operation: str, **params) -> dict:
    return dispatch(operation, params, actor=HUMAN)


def _items(state: str) -> list[dict]:
    return _call("asset.list", state=state, limit=500)["data"]["items"]


def _asset(asset_id: int) -> dict:
    envelope = _call("asset.get", asset_id=asset_id)
    if not envelope["ok"]:
        raise HTTPException(404, "asset not found")
    return envelope["data"]


def _approved_for(asset: dict) -> list[str]:
    return list((asset["state"]["stages"].get("publication") or {}).get("approved_for") or [])


def _collection(asset: dict) -> dict:
    """По площадкам: статус экспорта и партия текущего файла (export.get)."""
    out = {}
    for platform in _approved_for(asset):
        data = _call("export.get", asset_id=asset["id"], platform=platform)["data"]
        out[platform] = {"status": data["status"], "collected": data.get("collected"),
                         "filename": (data.get("export") or {}).get("filename")}
    return out


def counters() -> dict:
    summary = _call("review.queue")["data"]["summary"]
    by_state = summary["by_state"]
    approved_ids = [item["id"] for state in APPROVED_STATES for item in _items(state)]
    uncollected = 0
    for asset_id in approved_ids:
        collection = _collection(_asset(asset_id))
        if not collection or any(c["collected"] is None for c in collection.values()):
            uncollected += 1
    return {
        "approved": uncollected,
        "approved_total": len(approved_ids),
        "attention": by_state.get("human_review", 0),
        "blocked": by_state.get("blocked", 0),
        "rejected": by_state.get("rejected", 0),
        "source": by_state.get("source_invalid", 0) + by_state.get("stale", 0),
        "total": summary["total_assets"],
    }


def can_collect(numbers: dict) -> bool:
    """«Собрать к загрузке» — только когда нечего проверять человеку."""
    return numbers["attention"] == 0


def recent_events(limit: int = 15) -> list[dict]:
    ids = [item["id"] for item in _call("asset.list", limit=500)["data"]["items"]]
    events = []
    for asset_id in ids:
        for event in _call("asset.history", asset_id=asset_id)["data"]:
            message = event.get("message")
            if isinstance(message, dict):
                message = message.get("reason") or message.get("outcome") or message.get("platform") or ""
            events.append({**event, "summary": str(message or "")[:120]})
    return sorted(events, key=lambda e: e["id"], reverse=True)[:limit]


def _attention_ids() -> list[int]:
    return [item["id"] for item in _items("human_review")]


def _model_release_needed(asset: dict) -> bool:
    gate = (asset.get("metadata") or {}).get("review_gate") or {}
    codes = {r["code"] for r in gate.get("reasons", [])}
    return "PEOPLE_RECOGNIZABLE" in codes or "MODEL_RELEASE_REQUIRED" in asset["state"]["reasons"]


def _after_human_decision(asset_id: int, attest: bool) -> list[str]:
    """Как в ручном потоке: аттестация (если отмечена) → Readiness → Publication. Возвращает ошибки."""
    errors = []
    if attest:
        result = _call("asset.attest_people", asset_id=asset_id, kind="not_identifiable", note=ATTEST_NOTE)
        if not result["ok"]:
            errors.append(f"asset.attest_people: {result['error']['message']}")
    readiness = _call("readiness.evaluate", asset_id=asset_id)
    if not readiness["ok"] and readiness.get("error"):
        errors.append(f"readiness.evaluate: {readiness['error']['message']}")
    if _asset(asset_id)["state"]["state"] == "platform_ready":
        publication = _call("publication.evaluate", asset_id=asset_id)
        if not publication["ok"] and publication.get("error"):
            errors.append(f"publication.evaluate: {publication['error']['message']}")
    return errors


def collect_all() -> dict:
    """Для каждой площадки: export.prepare одобренным без актуального файла, затем export.collect."""
    report = {}
    approved = [_asset(item["id"]) for state in APPROVED_STATES for item in _items(state)]
    for platform in PLATFORMS:
        prepared, refused = [], []
        for asset in approved:
            if platform not in _approved_for(asset):
                continue
            status = _call("export.get", asset_id=asset["id"], platform=platform)["data"]["status"]
            if status == "ready_for_export":
                continue
            result = _call("export.prepare", asset_id=asset["id"], platform=platform)
            if result["ok"]:
                prepared.append(asset["id"])
            else:
                refused.append({"asset_id": asset["id"],
                                "reason": (result["data"] or {}).get("refused", {}).get("code") or result["error"]["code"]})
        collected = _call("export.collect", platform=platform)
        report[platform] = {"prepared": prepared, "refused": refused,
                            "collect": collected["data"] if collected["ok"] else {"error": collected["error"]}}
    return report


def create_app(token: str | None = None, require_prod: bool = True) -> FastAPI:
    if require_prod and db.data_dir().resolve() != (db.PROJECT_ROOT / "data" / "prod").resolve():
        raise RuntimeError(f"Stocker UI works on the production catalog only (STOCKER_DATA_DIR=data/prod), got {db.data_dir()}")
    app = FastAPI(title="Stocker", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token or secrets.token_urlsafe(24)
    app.state.previews = Previews()
    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    def render(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"token": app.state.token, **context})

    def check_token(value: str) -> None:
        if not hmac.compare_digest(value or "", app.state.token):
            raise HTTPException(403, "invalid token")

    @app.get("/", response_class=HTMLResponse)
    def panel(request: Request):
        numbers = counters()
        return render(request, "panel.html", numbers=numbers, can_collect=can_collect(numbers), events=recent_events())

    @app.get("/attention")
    def attention_first():
        ids = _attention_ids()
        return RedirectResponse(f"/attention/{ids[0]}" if ids else "/", status_code=303)

    @app.get("/attention/{asset_id}", response_class=HTMLResponse)
    def attention(request: Request, asset_id: int, error: str | None = None):
        asset = _asset(asset_id)
        ids = _attention_ids()
        if asset["state"]["state"] != "human_review":
            return RedirectResponse("/attention", status_code=303)
        following = [i for i in ids if i > asset_id] or [i for i in ids if i < asset_id]
        return render(request, "attention.html", asset=asset, fields=(asset["metadata"] or {}).get("fields") or {},
                      gate=humanize.gate_reasons(asset["metadata"]), qc=humanize.qc_reasons(asset["qc"]),
                      people=_model_release_needed(asset), position=ids.index(asset_id) + 1, total=len(ids),
                      next_id=following[0] if following else None, error=error)

    def _next_after(asset_id: int) -> RedirectResponse:
        ids = _attention_ids()
        following = [i for i in ids if i > asset_id] or ids
        return RedirectResponse(f"/attention/{following[0]}" if following else "/", status_code=303)

    @app.post("/attention/{asset_id}/approve")
    def approve(asset_id: int, token: str = Form(""), attest: str = Form("")):
        check_token(token)
        result = _call("metadata.approve", asset_id=asset_id)
        if not result["ok"]:
            return _go(f"/attention/{asset_id}", result["error"]["message"])
        _after_human_decision(asset_id, attest == "on")
        return _next_after(asset_id)

    @app.post("/attention/{asset_id}/reject")
    def reject(asset_id: int, token: str = Form(""), reason: str = Form("")):
        check_token(token)
        if not reason.strip():
            return _go(f"/attention/{asset_id}", "Укажите причину и что делать")
        result = _call("metadata.reject", asset_id=asset_id, reason=reason.strip())
        if not result["ok"]:
            return _go(f"/attention/{asset_id}", result["error"]["message"])
        return _next_after(asset_id)

    @app.get("/approved", response_class=HTMLResponse)
    def approved(request: Request, error: str | None = None):
        assets = [_asset(item["id"]) for state in APPROVED_STATES for item in _items(state)]
        rows = [{"asset": a, "title": ((a["metadata"] or {}).get("fields") or {}).get("title"), "collection": _collection(a)}
                for a in assets]
        return render(request, "approved.html", rows=rows, error=error)

    @app.post("/approved/{asset_id}/return")
    def return_to_review(asset_id: int, token: str = Form("")):
        check_token(token)
        result = _call("metadata.escalate", asset_id=asset_id, reason=RETURN_REASON)
        if not result["ok"]:
            return _go("/approved", f"prod #{asset_id}: {result['error']['message']}")
        return RedirectResponse("/approved", status_code=303)

    @app.get("/blocked", response_class=HTMLResponse)
    def blocked(request: Request, error: str | None = None):
        rows = []
        for item in _items("blocked"):
            asset = _asset(item["id"])
            reasons = [humanize.blocker(code) for code in asset["state"]["reasons"]]
            rows.append({"asset": asset, "reasons": reasons,
                         "attest": "MODEL_RELEASE_REQUIRED" in asset["state"]["reasons"]})
        return render(request, "blocked.html", rows=rows, error=error)

    @app.post("/blocked/{asset_id}/attest")
    def attest_people(asset_id: int, token: str = Form(""), confirm: str = Form("")):
        check_token(token)
        if confirm != "on":
            return _go("/blocked", "Подтвердите, что люди в кадре не узнаваемы")
        errors = _after_human_decision(asset_id, attest=True)
        return _go("/blocked", "; ".join(errors) or None)

    @app.get("/rejected", response_class=HTMLResponse)
    def rejected(request: Request):
        rows = []
        for item in _items("rejected"):
            asset = _asset(item["id"])
            review = (asset["metadata"] or {}).get("review") or {}
            rows.append({"asset": asset, "reason": review.get("reason"), "date": (review.get("decided_at") or "")[:10]})
        return render(request, "rejected.html", rows=rows)

    @app.post("/collect", response_class=HTMLResponse)
    def collect(request: Request, token: str = Form("")):
        check_token(token)
        if not can_collect(counters()):
            raise HTTPException(409, "objects need attention: review them before collecting")
        return render(request, "collect.html", report=collect_all())

    def _photo_asset(asset_id: int) -> dict:
        return _asset(asset_id)  # только по id: путь берётся из записи объекта

    @app.get("/photo/{asset_id}/preview.jpg")
    def preview(asset_id: int):
        asset = _photo_asset(asset_id)
        try:
            data = app.state.previews.get(asset_id, asset["file_hash"], PREVIEW_PX)
        except analysis_view.ViewUnavailable as exc:
            raise HTTPException(404, exc.code) from exc
        return Response(data, media_type="image/jpeg")

    @app.get("/photo/{asset_id}/thumb.jpg")
    def thumb(asset_id: int):
        asset = _photo_asset(asset_id)
        try:
            data = app.state.previews.get(asset_id, asset["file_hash"], THUMB_PX)
        except analysis_view.ViewUnavailable as exc:
            raise HTTPException(404, exc.code) from exc
        return Response(data, media_type="image/jpeg")

    @app.get("/photo/{asset_id}/full")
    def full(asset_id: int):
        asset = _photo_asset(asset_id)
        path = (ingest.ROOT / asset["source_path"]).resolve()
        if not path.is_relative_to(ingest.ROOT.resolve()) or not path.exists():
            raise HTTPException(404, "source file not available")
        return FileResponse(path, media_type=_MEDIA.get(path.suffix.lower(), "application/octet-stream"))

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "data_dir": str(db.data_dir())}

    return app


def main() -> None:
    import uvicorn

    print(db.describe())
    app = create_app()
    print(f"Stocker UI: http://{HOST}:{PORT}/")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
