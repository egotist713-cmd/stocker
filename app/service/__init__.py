"""
Stocker service layer — единая точка входа для внешних вызовов
(docs/SERVICE_CONTRACT.md).

    dispatch("asset.get", {"asset_id": 5}, actor="agent:openclaw") -> envelope

Бизнес-логики здесь нет: реестр операций, валидация параметров, права actor,
конверт ответа. Транспорты (JSON CLI, MCP, HTTP) вызывают только dispatch.
"""

import re
import sys
import traceback

from pydantic import ValidationError

from app.database.db import acting_as
from app.service.errors import ServiceError
from app.service.registry import REVIEW, build_registry

API_VERSION = "1"

HUMAN = "human"
_ACTOR = re.compile(r"^(human|agent:[a-z0-9_.-]+|workflow:[a-z0-9_.-]+)$")

# Операции, которые меняют содержимое metadata и возвращают его в draft → gate.
# Для агентов и workflow они запрещены над решениями человека: иначе правка
# превратила бы rejected в auto_approved в обход человека.
CONTENT_CHANGING = ("metadata.edit", "metadata.rebuild", "metadata.build")
HUMAN_DECISIONS = ("approved", "rejected")


def _human_decision_guard(operation: str, params: dict, actor: str) -> str | None:
    """Сообщение об отказе, если не-человек трогает содержимое после решения человека."""
    if actor == HUMAN or operation not in CONTENT_CHANGING or not isinstance(params, dict):
        return None

    asset_id = params.get("asset_id")
    if not isinstance(asset_id, int):
        return None  # ошибку параметров сообщит валидация

    from app.service.views import asset_view

    view = asset_view(asset_id)
    state = (view or {}).get("pipeline", {}).get("metadata")
    if state in HUMAN_DECISIONS:
        return f"Asset {asset_id} metadata is '{state}' by a human decision; actor '{actor}' cannot change it"
    return None


def _envelope(operation: str, *, ok: bool, asset_id=None, outcome=None, data=None, error=None) -> dict:
    return {
        "api_version": API_VERSION,
        "operation": operation,
        "ok": ok,
        "asset_id": asset_id,
        "outcome": outcome,
        "data": data,
        "error": error,
    }


def _error(operation: str, code: str, message: str, asset_id=None) -> dict:
    return _envelope(operation, ok=False, asset_id=asset_id, error={"code": code, "message": message})


def _allowed_params(spec) -> str:
    """Подсказка для самоисправления агента: допустимые параметры операции из её схемы."""
    schema = spec.params.model_json_schema()
    required = set(schema.get("required", []))
    names = [f"{name} (required)" if name in required else name for name in schema.get("properties", {})]
    return f"Allowed params: {', '.join(names) or 'none'}. Pass them as top-level keys."


def dispatch(operation: str, params: dict | None = None, actor: str = HUMAN) -> dict:
    """Выполнить операцию от имени actor и вернуть envelope. Никогда не бросает исключений."""
    registry = build_registry()
    params = params or {}
    asset_id = params.get("asset_id") if isinstance(params, dict) else None

    if not isinstance(actor, str) or not _ACTOR.match(actor):
        return _error(operation, "INVALID_PARAMS", f"Invalid actor: {actor!r}")

    if operation not in registry:
        return _error(operation, "UNKNOWN_OPERATION", f"Unknown operation: {operation}")

    spec = registry[operation]

    # approve/reject — решения человека. auto_approved ставит только review gate.
    if spec.access == REVIEW and actor != HUMAN:
        return _error(operation, "FORBIDDEN", f"'{operation}' is a human decision; actor '{actor}' is not allowed", asset_id)

    refusal = _human_decision_guard(operation, params, actor)
    if refusal:
        return _error(operation, "FORBIDDEN", refusal, asset_id)

    try:
        parsed = spec.params.model_validate(params)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'params'}: {e['msg']}" for e in exc.errors())
        return _error(operation, "INVALID_PARAMS", f"{details}. {_allowed_params(spec)}", asset_id)

    try:
        with acting_as(actor):
            result = spec.handler(parsed)
    except ServiceError as exc:
        return _error(operation, exc.code, str(exc), asset_id)
    except Exception as exc:  # noqa: BLE001 — граница транспорта: только envelope, трассировка в stderr
        traceback.print_exc(file=sys.stderr)
        return _error(operation, "INTERNAL", f"{type(exc).__name__}: {exc}", asset_id)

    return _envelope(
        operation,
        ok=result["ok"],
        asset_id=result["asset_id"],
        outcome=result["outcome"],
        data=result["data"],
    )


def manifest() -> list[dict]:
    return [operation.manifest() for operation in build_registry().values()]
