"""
Обработчики операций service layer. Бизнес-логики здесь нет: вызываются
существующие worker и app.metadata, результат упаковывается в
{asset_id, outcome, ok, data} (envelope собирает app.service.dispatch).
"""

from pathlib import Path

from app import ingest
from app import metadata as metadata_service
from app import worker
from app.service import views
from app.service.errors import ServiceError
from app.textnorm import normalize_text

# Исходы, при которых операция отработала, но результата нет (ok: false, error: null).
NOT_OK_OUTCOMES = {worker.AI_FAILED, worker.SOURCE_INVALID, metadata_service.METADATA_AI_FAILED}


def _result(asset_id: int | None, outcome: str | None, data, ok: bool = True) -> dict:
    return {"asset_id": asset_id, "outcome": outcome, "ok": ok, "data": data}


def _view_or_404(asset_id: int) -> dict:
    view = views.asset_view(asset_id)
    if view is None:
        raise ServiceError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")
    return view


def _changed(asset_id: int, outcome: str) -> dict:
    return _result(asset_id, outcome, _view_or_404(asset_id), ok=outcome not in NOT_OK_OUTCOMES)


def _metadata_call(function, asset_id: int, *args, **kwargs) -> dict:
    try:
        result = function(asset_id, *args, **kwargs)
    except metadata_service.MetadataError as exc:
        raise ServiceError(exc.code, str(exc)) from exc
    return _changed(asset_id, result["outcome"])


# --- чтение ------------------------------------------------------------------------


def asset_get(params) -> dict:
    return _result(params.asset_id, None, _view_or_404(params.asset_id))


def asset_list(params) -> dict:
    return _result(None, None, views.list_assets(**params.model_dump()))


def asset_history(params) -> dict:
    _view_or_404(params.asset_id)
    return _result(params.asset_id, None, views.history(params.asset_id, params.stage))


def review_queue(params) -> dict:
    return _result(None, None, views.review_queue(params.limit, params.offset))


def metadata_get(params) -> dict:
    view = _view_or_404(params.asset_id)
    if view["metadata"] is None:
        raise ServiceError("METADATA_MISSING", f"Asset {params.asset_id} has no metadata")
    return _result(params.asset_id, None, view["metadata"])


def operations_list(params) -> dict:
    from app.service import registry

    return _result(None, None, [operation.manifest() for operation in registry.build_registry().values()])


# --- pipeline ----------------------------------------------------------------------


def asset_process_file(params) -> dict:
    path = Path(params.path)
    if not path.is_absolute():
        path = ingest.ROOT / path

    asset_id, outcome = worker.ingest_and_process(path)
    if asset_id is None:
        raise ServiceError("FILE_NOT_ADDED", f"File was not added (duplicate, unsupported, invalid or outside project): {params.path}")

    return _changed(asset_id, outcome)


def asset_process(params) -> dict:
    outcome = worker.process_asset(params.asset_id, force=params.force)
    if outcome == worker.ASSET_NOT_FOUND:
        raise ServiceError("ASSET_NOT_FOUND", f"Asset not found: {params.asset_id}")
    return _changed(params.asset_id, outcome)


def metadata_build(params) -> dict:
    return _metadata_call(metadata_service.build, params.asset_id, force=params.force)


def metadata_rebuild(params) -> dict:
    return _metadata_call(metadata_service.rebuild, params.asset_id)


def metadata_edit(params) -> dict:
    changes = {}
    if params.title is not None:
        changes["title"] = params.title
    if params.description is not None:
        changes["description"] = params.description

    if params.keywords is not None or params.add_keywords or params.remove_keywords:
        view = _view_or_404(params.asset_id)
        if view["metadata"] is None:
            raise ServiceError("METADATA_MISSING", f"Asset {params.asset_id} has no metadata")
        keywords = list(params.keywords) if params.keywords is not None else list(view["metadata"]["fields"]["keywords"])
        keywords += params.add_keywords
        removed = {normalize_text(k).lower() for k in params.remove_keywords}
        changes["keywords"] = [k for k in keywords if normalize_text(k).lower() not in removed]

    return _metadata_call(metadata_service.edit, params.asset_id, changes)


def metadata_gate(params) -> dict:
    return _metadata_call(metadata_service.gate, params.asset_id)


def metadata_escalate(params) -> dict:
    return _metadata_call(metadata_service.escalate, params.asset_id, params.reason)


# --- review (только человек; права проверяет dispatch) --------------------------------


def metadata_approve(params) -> dict:
    return _metadata_call(
        metadata_service.approve,
        params.asset_id,
        allow_partial=params.allow_partial,
        confirm_claims=params.confirm_claims,
    )


def metadata_reject(params) -> dict:
    return _metadata_call(metadata_service.reject, params.asset_id, params.reason)
