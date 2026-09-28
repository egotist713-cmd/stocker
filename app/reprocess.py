"""
Контролируемый пересчёт (docs/ASSET_STATE_CONTRACT.md §6, этап C).

asset.reprocess пересчитывает указанную стадию и её downstream через те же функции,
что и pipeline, — только стадии, которые сейчас не актуальны. Upstream не трогается;
стадия не запускается на неактуальном / невалидном upstream; source проверяется
SHA256 до любой записи. dry_run (по умолчанию) — только план, без записи.

Решения человека не перезаписываются: metadata в approved / rejected или с правками
человека пересчётом не пересобирается (нужен человек).
"""

import json

from app import analysis_view, asset_state, creative_review, enhancement_decision, normalization, qc, stock_readiness, worker
from app import metadata as metadata_service
from app import metadata_builder as mb
from app.ai.enhancement_advisor import advisor_enabled
from app.ai.local_analyzer import LocalAnalyzer
from app.database.db import get_asset, insert_event, transaction

STAGE = "REPROCESS"

# Стадии, которые reprocess умеет выполнять (подключены к pipeline), в порядке обработки.
STAGES = ("normalize", "qc", "enhancement", "vision", "metadata", "readiness", "creative_review")

# Что пересчитывается от стадии: порядок pipeline (QC пропускает к Vision), а не только
# зависимость по данным. Enhancement — советующая, от неё никто не зависит.
# view — производный шаг (AnalysisView в памяти из representation): ничего не пишет.
# Readiness — после metadata (правила площадок; только для одобренной metadata).
PIPELINE_ORDER = ("normalize", "view", "qc", "enhancement", "vision", "metadata", "readiness")
DOWNSTREAM = {
    "normalize": ("normalize", "view", "qc", "enhancement", "vision", "metadata", "readiness"),
    "qc": ("qc", "enhancement", "vision", "metadata", "readiness"),
    "enhancement": ("enhancement",),
    "vision": ("vision", "metadata", "readiness"),
    "metadata": ("metadata", "readiness"),
    "readiness": ("readiness",),
    # Creative Review — советник по запросу (решение 26.09): в цепочки по умолчанию не входит,
    # только явный reprocess_from=creative_review; модель вызывается осознанно.
    "creative_review": ("creative_review",),
}

# Что должно быть актуально, чтобы стадию можно было запускать.
UPSTREAM = {
    "normalize": (),
    "view": ("normalize",),
    "qc": ("normalize", "view"),
    "enhancement": ("normalize", "view"),
    "vision": ("normalize", "view", "qc"),
    "metadata": ("normalize", "view", "qc", "vision"),
    "readiness": ("normalize", "view", "qc", "vision", "metadata"),
    "creative_review": ("normalize", "view", "qc", "vision", "metadata", "readiness"),
}

# Не подключены к pipeline — reprocess их не запускает (устаревание остаётся видно в state).
NOT_CONNECTED = ("publication",)

WRITES = {
    "normalize": ["NORMALIZE/EVALUATED", "NORMALIZE/PASSED"],
    "view": [],  # в памяти
    "qc": ["assets.qc_result", "QC/PASSED|FAILED"],
    "enhancement": ["ENHANCEMENT/ASSESSED", "ENHANCEMENT/ADVISED (только disputed)"],
    "vision": ["assets.ai_result", "AI/PASSED|FAILED"],
    "metadata": ["METADATA_AI/PASSED|FAILED", "assets.metadata_json", "METADATA/DRAFTED", "METADATA/GATED"],
    "readiness": ["READINESS/EVALUATED|FAILED"],
    "creative_review": ["CREATIVE_REVIEW/ADVISED|FAILED (модель)"],
}

# Исходы.
DRY_RUN = "DRY_RUN"
REPROCESSED = "REPROCESSED"
NOTHING_TO_DO = "NOTHING_TO_DO"
REFUSED = "REFUSED"
STOPPED = "STOPPED"

DERIVED = "DERIVED"  # view построен заново в памяти (ничего не записано)
NOT_APPLICABLE = "SKIPPED_NOT_APPLICABLE"  # Readiness при неодобренной metadata — не оценивается


class ReprocessError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _metadata_policy(asset: dict) -> str | None:
    """Причина, по которой metadata нельзя пересобрать автоматически (решение / правки человека)."""
    raw = asset["metadata_json"]
    metadata = json.loads(raw) if raw else None
    if metadata is None:
        return None
    if metadata["state"] in (mb.APPROVED, mb.REJECTED):
        return "HUMAN_DECISION"
    if metadata.get("edited_fields"):
        return "HUMAN_EDITS"
    return None


def _refusal(code: str, message: str, **extra) -> dict:
    return {"code": code, "message": message, **extra}


def plan(asset_id: int, reprocess_from: str | None = None, through: str | None = None) -> dict:
    """План без записи: состояние, причина, стадии, что будет заменено. Source — SHA256."""
    asset = get_asset(asset_id)
    if asset is None:
        raise ReprocessError("ASSET_NOT_FOUND", f"Asset not found: {asset_id}")
    state = asset_state.get(asset_id, verify_source=True)
    stages = state["stages"]
    start = reprocess_from or state["reprocess_from"]
    result = {
        "asset_id": asset_id,
        "state": state["state"],
        "reasons": state["reasons"],
        "suggested_from": state["reprocess_from"],
        "reprocess_from": start,
        "through": through,
        "steps": [],
        "not_run": [{"stage": s, "status": stages[s]["status"], "reason": stages[s].get("reason"),
                     "why": "not connected to the pipeline"} for s in NOT_CONNECTED],
        "refused": None,
    }

    if state["state"] == asset_state.REJECTED:
        result["refused"] = _refusal("REJECTED", "Rejected by a human: terminal, not reprocessed")
    elif state["state"] == asset_state.SOURCE_INVALID:
        result["refused"] = _refusal("SOURCE_INVALID", "Source is missing or changed (SHA256); restore it first",
                                     source=stages["source"])
    elif start is None:
        result["refused"] = _refusal("NOTHING_STALE", f"No stale required stage (state '{state['state']}')")
    elif start not in STAGES:
        result["refused"] = _refusal("NOT_CONNECTED", f"'{start}' is not a pipeline stage reprocess can run")
    elif through is not None and through not in DOWNSTREAM[start]:
        result["refused"] = _refusal("INVALID_THROUGH", f"'{through}' is not downstream of '{start}'")
    if result["refused"]:
        return result

    bad = [u for u in UPSTREAM[start] if stages[u]["status"] != asset_state.CURRENT]
    if bad:
        u = bad[0]
        result["refused"] = _refusal("UPSTREAM_NOT_CURRENT", f"Upstream '{u}' is {stages[u]['status']}; reprocess from it first",
                                     stage=u, status=stages[u]["status"], reason=stages[u].get("reason"))
        return result

    chain = DOWNSTREAM[start]
    if through is not None:
        chain = chain[: chain.index(through) + 1]
    will_run = set()
    for name in chain:
        stage = stages[name]
        # Станет неактуальной, если пересчитывается то, от чего она зависит по данным.
        upstream_rerun = [d for d in asset_state.DEPENDS_ON[name] if d in will_run]
        if name in ("readiness", "creative_review") and stage["status"] == asset_state.NOT_APPLICABLE \
                and "metadata" not in will_run:
            action, why = "skip", f"not_applicable:{stage.get('reason')}"
        elif name == "readiness" and "metadata" in will_run:
            action, why = "run", "UPSTREAM_RERUN:metadata (if metadata stays approved)"
        elif stage["status"] == asset_state.CURRENT and not upstream_rerun:
            action, why = "skip", "current"
        else:
            action = "run"
            why = stage.get("reason") if stage["status"] != asset_state.CURRENT else f"UPSTREAM_RERUN:{upstream_rerun[0]}"
            if name == "metadata" and (policy := _metadata_policy(asset)):
                action, why = "stop", policy
            elif name == "view":
                action = "derive"  # заново в памяти из representation, без записи
        if action in ("run", "derive"):
            will_run.add(name)
        result["steps"].append({"stage": name, "action": action, "why": why, "status": stage["status"],
                                "writes": WRITES[name] if action == "run" else []})
        if action == "stop":
            break  # дальше выполнение не пойдёт — план не обещает того, чего не будет
    return result


def _write_summary(asset_id: int, message: dict) -> None:
    with transaction() as connection:
        insert_event(connection, asset_id, STAGE, "DONE", json.dumps(message, ensure_ascii=False))


def run(
    asset_id: int,
    reprocess_from: str | None = None,
    through: str | None = None,
    dry_run: bool = True,
    analyzer=None,
    metadata_analyzer=None,
    creative_advisor=None,
) -> dict:
    planned = plan(asset_id, reprocess_from, through)
    if planned["refused"]:
        return {**planned, "outcome": REFUSED, "executed": []}
    if dry_run:
        return {**planned, "outcome": DRY_RUN, "executed": []}

    executed, stopped, view = [], None, None
    for step in planned["steps"]:
        name = step["stage"]
        current = asset_state.get(asset_id)["stages"]
        normalized_now = any(e["stage"] == "normalize" and e["outcome"] != "SKIPPED_CURRENT" for e in executed)
        if name == "view" and normalized_now:
            pass  # representation только что пересоздана — view строится заново (ниже)
        elif name in ("readiness", "creative_review") and current[name]["status"] == asset_state.NOT_APPLICABLE:
            executed.append({"stage": name, "outcome": NOT_APPLICABLE})  # metadata не одобрена / Readiness blocked
            continue
        elif current[name]["status"] == asset_state.CURRENT:
            executed.append({"stage": name, "outcome": "SKIPPED_CURRENT"})
            continue
        # Защита: стадия не запускается на неактуальном upstream (например, QC не прошёл).
        bad = [u for u in UPSTREAM[name] if current[u]["status"] != asset_state.CURRENT]
        if bad:
            stopped = {"stage": name, "code": "UPSTREAM_NOT_CURRENT", "upstream": bad[0],
                       "status": current[bad[0]]["status"], "reason": current[bad[0]].get("reason")}
            break
        if name == "metadata" and (policy := _metadata_policy(get_asset(asset_id))):
            stopped = {"stage": name, "code": policy}
            break

        if name == "view":
            try:
                view = analysis_view.open_asset_view(asset_id)
            except analysis_view.ViewUnavailable as exc:
                stopped = {"stage": name, "code": f"VIEW_FAILED:{exc.code}"}
                break
            executed.append({"stage": name, "outcome": DERIVED})
            continue

        if name in ("qc", "enhancement", "vision", "creative_review") and view is None:
            try:
                view = analysis_view.open_asset_view(asset_id)  # один view на все стадии пересчёта
            except analysis_view.ViewUnavailable as exc:
                stopped = {"stage": name, "code": f"VIEW_FAILED:{exc.code}"}
                break

        outcome = _execute(name, asset_id, view, analyzer, metadata_analyzer, creative_advisor)
        executed.append({"stage": name, "outcome": outcome})
        if outcome in ("NORMALIZE_FAILED", "QC_FAILED", worker.AI_FAILED, "METADATA_AI_FAILED", "READINESS_FAILED",
                       creative_review.REVIEW_FAILED) or outcome.startswith("REFUSED:"):
            stopped = {"stage": name, "code": outcome}
            break

    after = asset_state.get(asset_id)
    # REPROCESS/DONE — только если хотя бы одна стадия реально пересчитана (контракт §6):
    # пропуски актуальных и производный view событием не считаются.
    ran = [e for e in executed if e["outcome"] not in ("SKIPPED_CURRENT", DERIVED, NOT_APPLICABLE)]
    outcome = STOPPED if stopped else (REPROCESSED if ran else NOTHING_TO_DO)
    if ran:  # no-op пересчёт событий не пишет
        _write_summary(asset_id, {
            "reprocess_from": planned["reprocess_from"], "through": through,
            "state_before": planned["state"], "reasons_before": planned["reasons"],
            "executed": executed, "stopped": stopped,
            "state_after": after["state"], "reasons_after": after["reasons"],
        })
    return {**planned, "outcome": outcome, "executed": executed, "stopped": stopped,
            "state_after": after["state"], "reasons_after": after["reasons"],
            "reprocess_from_after": after["reprocess_from"]}


def _execute(name: str, asset_id: int, view, analyzer, metadata_analyzer, creative_advisor=None) -> str:
    if name == "normalize":
        return normalization.run_asset(asset_id)["outcome"]
    if name == "qc":
        result = qc.check_asset(get_asset(asset_id), view)
        qc.save_qc_result(asset_id, result)
        return "QC_PASSED" if result["passed"] else "QC_FAILED"
    if name == "enhancement":
        result = enhancement_decision.assess_asset(asset_id, view=view)
        decision = enhancement_decision.effective_decision(result["assessment"])
        if decision == enhancement_decision.DISPUTED and advisor_enabled():
            enhancement_decision.advise_asset(asset_id, view=view)
        return result["outcome"]
    if name == "vision":
        return worker.run_ai(asset_id, view, analyzer or LocalAnalyzer())
    if name == "metadata":
        # Самая дешёвая операция, которая снимает причину: смена политики gate — только gate;
        # смена правил builder — пересборка правилами; иначе (новый Vision, нет metadata) — Metadata AI.
        stage = asset_state.get(asset_id)["stages"]["metadata"]
        reason = stage.get("own_reason") or stage.get("reason")
        if stage["status"] == asset_state.S_STALE and reason == "GATE_POLICY_CHANGED":
            return metadata_service.gate(asset_id)["outcome"]
        if stage["status"] == asset_state.S_STALE and reason == "BUILDER_CHANGED":
            return metadata_service.rebuild(asset_id)["outcome"]
        return metadata_service.build(asset_id, force=True, analyzer=metadata_analyzer)["outcome"]
    if name == "readiness":
        return stock_readiness.evaluate_asset(asset_id)["outcome"]
    if name == "creative_review":
        try:
            return creative_review.review_asset(asset_id, advisor=creative_advisor, profile=_previous_profile(asset_id),
                                                view=view)["outcome"]
        except creative_review.CreativeReviewError as exc:  # защита стадии сработала — без записи
            return f"REFUSED:{exc.code}"
    raise ReprocessError("UNKNOWN_STAGE", name)


def _previous_profile(asset_id: int) -> str | None:
    """Тот же выбор профиля, что у прежней оценки: auto → auto, явный → тот же, иначе по умолчанию."""
    from app.creative_profiles import AUTO

    last = creative_review._stored(creative_review._last_event(asset_id, "ADVISED")) or {}
    profile = last.get("profile") or {}
    mode = (profile.get("selection") or {}).get("mode")
    if mode == "auto":
        return AUTO
    if mode == "explicit":
        return profile.get("name")
    return None
