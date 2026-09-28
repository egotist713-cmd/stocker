"""
Unified Asset State и Staleness (docs/ASSET_STATE_CONTRACT.md, asset-state-v1).

Read-model: вычисляет из результатов стадий и их отпечатков итоговое состояние
объекта, статусы стадий, problems и allowed_actions. Ничего не пишет и не
пересчитывает; файл source только проверяется на существование и размер
(SHA256 — только при verify=True).
"""

import json

from app import creative_review, enhancement_decision, ingest, normalization, normalizer, stock_readiness
from app import metadata_builder as mb
from app import review_gate as rg
from app import qc as qc_rules
from app.ai.analyzer import input_fingerprint, vision_inputs
from app.ai.creative_advisor import prompt_version_for
from app.ai.local_analyzer import LocalAnalyzer
from app.creative_profiles import UnknownProfileError, get_profile
from app.database.db import get_asset, get_connection

STATE_VERSION = "asset-state-v1"

# Итоговые состояния (контракт §3.1).
REJECTED = "rejected"
SOURCE_INVALID = "source_invalid"
BLOCKED = "blocked"
ERROR = "error"
STALE = "stale"
PROCESSING = "processing"
HUMAN_REVIEW = "human_review"
METADATA_APPROVED = "metadata_approved"
PLATFORM_READY = "platform_ready"
# Будущие (Publication gate, Export preparation) — пока недостижимы.
PUBLICATION_APPROVED = "publication_approved"
READY_FOR_EXPORT = "ready_for_export"

TERMINAL_STATES = frozenset({REJECTED})

# Статусы стадий (§4).
CURRENT = "current"
S_STALE = "stale"
MISSING = "missing"
FAILED = "failed"
NOT_APPLICABLE = "not_applicable"
NOT_IMPLEMENTED = "not_implemented"

# Обязательные стадии в порядке обработки и советующие.
REQUIRED = ("normalize", "view", "qc", "vision", "metadata", "readiness")
ADVISORY = ("enhancement", "creative_review")

# Зависимости по данным (граф §2): устаревание наследуется вниз.
DEPENDS_ON = {
    "normalize": (),
    "view": ("normalize",),
    "qc": ("view",),
    "enhancement": ("view",),
    "vision": ("view",),
    "metadata": ("vision",),
    "readiness": ("normalize", "qc", "metadata"),
    # Данные: view (overview) и Vision; применимость и порядок — после актуального Readiness.
    "creative_review": ("view", "vision", "readiness"),
}

# Текущая идентичность Vision по провайдеру (модель, промпт, схема, кодирование, параметры).
# Неизвестный провайдер — не «актуально молча», а STALE.
VISION_IDENTITIES = {LocalAnalyzer.provider: LocalAnalyzer.current_identity}

# Отказ стадии → blocked (детерминированный) или error (техническая ошибка, повтор).
_FAILURE_KIND = {"normalize": BLOCKED, "view": BLOCKED, "qc": BLOCKED, "vision": ERROR, "readiness": ERROR}
_FAILURE_CODE = {"normalize": "NORMALIZE_FAILED", "view": "VIEW_FAILED", "qc": "QC_FAILED",
                 "vision": "VISION_FAILED", "readiness": "READINESS_FAILED"}

_SOURCE_NEGATIVE_CODES = ("SOURCE_MISSING", "SOURCE_CHANGED")


def _json(value):
    try:
        return json.loads(value) if value else None
    except (json.JSONDecodeError, TypeError):
        return None


def _last(events: list[dict], stage: str, status: str | None = None) -> dict | None:
    return next((e for e in reversed(events) if e["stage"] == stage and (status is None or e["status"] == status)), None)


def _newer(a: dict | None, b: dict | None) -> bool:
    """a существует и новее b (или b нет)."""
    return a is not None and (b is None or a["id"] > b["id"])


def _stage(status: str, reason: str | None = None, **extra) -> dict:
    return {"status": status, "reason": reason, **extra}


def _changed(stored: dict | None, current: dict) -> list[str]:
    """Какие входы изменились (верхний уровень состава отпечатка) — для объяснения STALE."""
    stored = stored or {}
    return sorted(key for key in set(stored) | set(current) if stored.get(key) != current.get(key))


# --- Source integrity (§3.5) ------------------------------------------------------------

def source_status(asset: dict, events: list[dict], verify: bool = False) -> dict:
    """
    Уровни проверки (контракт §3.5a): fast (exists + size + свидетельства) может только
    опровергнуть source; идентичность доказывает только sha256 (verify=True).
    """
    level = "sha256" if verify else "fast"
    path = ingest.source_file(asset)
    if not path.exists():
        return _stage(MISSING, "SOURCE_MISSING", verification=level)
    if asset.get("file_size") and path.stat().st_size != asset["file_size"]:
        return _stage("changed", "SOURCE_CHANGED", verification=level)
    if verify:
        same = ingest.sha256_file(path) == asset["file_hash"]
        return _stage("ok", verification=level) if same else _stage("changed", "SOURCE_CHANGED", verification=level)

    negatives = [e for e in events if (e["stage"], e["status"]) == ("SOURCE", "INVALID")
                 or ((e["stage"], e["status"]) == ("NORMALIZE", "FAILED")
                     and (_json(e["message"]) or {}).get("error_type") in _SOURCE_NEGATIVE_CODES)]
    positives = [e for e in events if (e["stage"], e["status"]) in (("NORMALIZE", "EVALUATED"), ("NORMALIZE", "PASSED"))
                 or e["stage"] == "QC"]
    negative, positive = (negatives or [None])[-1], (positives or [None])[-1]
    if _newer(negative, positive):
        # Файл на месте, но последнее свидетельство — отрицательное: без проверки не «ok».
        return _stage("changed", "SOURCE_CHANGED", verification=level)
    # «ok» при fast — признаков изменения нет, а не «идентичен».
    return _stage("ok", verification=level)


# --- Собственные статусы стадий -----------------------------------------------------------

def _normalize(asset: dict, events: list[dict]) -> dict:
    file_hash = asset["file_hash"]
    evaluated, passed, failed = (_last(events, "NORMALIZE", s) for s in ("EVALUATED", "PASSED", "FAILED"))
    current_facts, current_rep = normalization.fingerprint(file_hash), normalizer.fingerprint(file_hash)

    success = max((e for e in (evaluated, passed) if e), key=lambda e: e["id"], default=None)
    if _newer(failed, success):
        message = _json(failed["message"]) or {}
        code = message.get("error_type")
        expected = current_rep if message.get("stage") == "engine" else current_facts
        if code in _SOURCE_NEGATIVE_CODES or message.get("fingerprint") is None:
            return _stage(S_STALE, "NO_FINGERPRINT" if message.get("fingerprint") is None else "SOURCE_RECHECK")
        if message["fingerprint"] != expected:
            return _stage(S_STALE, "FINGERPRINT_CHANGED")
        return _stage(FAILED, f"NORMALIZE_FAILED:{code}", code=code)

    if passed is None:
        return _stage(MISSING, "NOT_NORMALIZED")
    manifest = _json(passed["message"]) or {}
    facts = _json(evaluated["message"]) if evaluated else None
    if manifest.get("fingerprint") != current_rep or (facts or {}).get("fingerprint") != current_facts:
        return _stage(S_STALE, "FINGERPRINT_CHANGED", stored_fp=manifest.get("fingerprint"), current_fp=current_rep)
    return _stage(CURRENT, representation=manifest.get("representation"), current_fp=current_rep)


def _view(asset: dict, events: list[dict]) -> dict:
    failed, passed = _last(events, "VIEW", "FAILED"), _last(events, "NORMALIZE", "PASSED")
    current = normalizer.view_fingerprint(asset["file_hash"])
    if _newer(failed, passed):
        code = (_json(failed["message"]) or {}).get("error_type")
        return _stage(FAILED, f"VIEW_FAILED:{code}", code=code, current_fp=current)
    if passed is None:
        return _stage(MISSING, "NOT_NORMALIZED", current_fp=current)
    return _stage(CURRENT, current_fp=current)


def _qc(asset: dict, events: list[dict]) -> dict:
    """Отпечаток QC = view + правила и пороги (qc.fingerprint); без отпечатка — наследие, STALE."""
    result = _json(asset["qc_result"])
    if not result:
        return _stage(MISSING, "NOT_CHECKED")
    view_fp = normalizer.view_fingerprint(asset["file_hash"])
    current = qc_rules.fingerprint(view_fp)
    stored = result.get("input_fingerprint")
    if stored is None:
        return _stage(S_STALE, "NO_FINGERPRINT", current_fp=current)
    if stored != current:
        return _stage(S_STALE, "FINGERPRINT_CHANGED", stored_fp=stored, current_fp=current,
                      changed=_changed(result.get("inputs"), qc_rules.inputs(view_fp)))
    if not result.get("passed"):
        return _stage(FAILED, "QC_FAILED", errors=result.get("errors", []))
    return _stage(CURRENT, stored_fp=stored, current_fp=current)


def _vision(asset: dict, events: list[dict]) -> dict:
    """Отпечаток Vision = view + provider / model / промпт / схема / кодирование / параметры."""
    passed = _last(events, "AI", "PASSED")
    if not asset["ai_result"]:
        last = _last(events, "AI")
        if last is not None and last["status"] == "FAILED":
            return _stage(FAILED, "VISION_FAILED")
        return _stage(MISSING, "NOT_ANALYZED")
    message = (_json(passed["message"]) if passed else None) or {}
    stored = message.get("input_fingerprint")
    base = {"stored_fp": stored, "event_id": passed["id"] if passed else None}
    if stored is None:
        return _stage(S_STALE, "NO_FINGERPRINT", **base)
    identity_of = VISION_IDENTITIES.get(message.get("provider"))
    if identity_of is None:
        return _stage(S_STALE, "UNKNOWN_PROVIDER", **base)
    view_fp, identity = normalizer.view_fingerprint(asset["file_hash"]), identity_of()
    current = input_fingerprint(view_fp, identity)
    if stored != current:
        return _stage(S_STALE, "FINGERPRINT_CHANGED", current_fp=current,
                      changed=_changed(message.get("inputs"), vision_inputs(view_fp, identity)), **base)
    return _stage(CURRENT, current_fp=current, **base)


def _metadata(asset: dict, events: list[dict], metadata: dict | None) -> dict:
    if metadata is None:
        return _stage(MISSING, "NO_METADATA")
    state = metadata["state"]
    passed = _last(events, "AI", "PASSED")
    vision_event = ((metadata.get("sources") or {}).get("vision") or {}).get("event_id")
    extra = {"state": state, "completeness": metadata.get("completeness")}
    if state == mb.REJECTED:
        return _stage(CURRENT, **extra)
    if passed is None or vision_event != passed["id"]:
        return _stage(S_STALE, "VISION_CHANGED", **extra)
    if state in rg.GATEABLE_STATES:  # решения человека сменой правил не отменяются
        builder = (metadata.get("sources") or {}).get("builder_version")
        policy = (metadata.get("review_gate") or {}).get("policy_version")
        if builder != mb.BUILDER_VERSION or (state != mb.DRAFT and policy != rg.POLICY_VERSION):
            return _stage(S_STALE, "RULES_CHANGED", **extra)
    return _stage(CURRENT, **extra)


def _readiness(asset: dict, events: list[dict], metadata: dict | None) -> dict:
    state = (metadata or {}).get("state")
    if state not in (mb.AUTO_APPROVED, mb.APPROVED):
        return _stage(NOT_APPLICABLE, "METADATA_NOT_APPROVED")
    evaluated, failed = _last(events, "READINESS", "EVALUATED"), _last(events, "READINESS", "FAILED")
    if _newer(failed, evaluated):
        return _stage(FAILED, "READINESS_FAILED")
    summary = stock_readiness.summary(asset, events, metadata)
    if not summary["evaluated"]:
        return _stage(MISSING, "NOT_EVALUATED")
    if summary["stale"]:
        return _stage(S_STALE, "FINGERPRINT_CHANGED")
    result = _json(evaluated["message"]) or {}
    blockers = sorted({c["code"] for p in (result.get("platforms") or {}).values()
                       for c in p.get("checks", []) if c.get("level") == "blocker"})
    if not summary["ready_for"]:
        return _stage(CURRENT, "READINESS_BLOCKED", ready_for=[], blockers=blockers)
    return _stage(CURRENT, ready_for=summary["ready_for"], blockers=blockers)


def _enhancement(asset: dict, events: list[dict]) -> dict:
    summary = enhancement_decision.summary_from_events(asset, events)
    if not summary["assessed"]:
        return _stage(MISSING, "NOT_ASSESSED")
    if summary["stale"]:
        return _stage(S_STALE, "FINGERPRINT_CHANGED", decision=summary["decision"])
    return _stage(CURRENT, decision=summary["decision"])


def _creative(asset: dict, events: list[dict]) -> dict:
    event = _last(events, "CREATIVE_REVIEW", "ADVISED")
    if event is None:
        return _stage(MISSING, "NOT_REVIEWED")
    message = _json(event["message"]) or {}
    try:
        profile = get_profile((message.get("profile") or {}).get("name"))
        prompt_version = prompt_version_for(profile)
    except (UnknownProfileError, TypeError):
        return _stage(S_STALE, "UNKNOWN_PROFILE")
    # Входы (view, Vision) — по отпечатку с той версией, с которой оценивали; шаблон / профиль — отдельно.
    current = creative_review.fingerprint(asset["file_hash"], asset["ai_result"], message.get("prompt_version"))
    if message.get("fingerprint") != current:
        return _stage(S_STALE, "FINGERPRINT_CHANGED", stored_fp=message.get("fingerprint"), current_fp=current)
    if message.get("prompt_version") != prompt_version:
        return _stage(S_STALE, "PROMPT_CHANGED", stored=message.get("prompt_version"), current=prompt_version)
    return _stage(CURRENT, current_fp=current)


def _creative_applicability(creative: dict, readiness: dict) -> dict:
    """
    Creative Review — только для объектов, которые технически можно продавать (STOCK_READINESS
    §4.3): metadata одобрена и актуальный Readiness готов хотя бы для одной площадки.
    Иначе не применим; сохранённая оценка остаётся историей (has_result).
    """
    has_result = creative["status"] != MISSING
    if readiness["status"] == NOT_APPLICABLE:
        return _stage(NOT_APPLICABLE, "METADATA_NOT_APPROVED", has_result=has_result)
    if readiness["status"] == MISSING:
        return _stage(NOT_APPLICABLE, "READINESS_NOT_EVALUATED", has_result=has_result)
    if readiness["status"] == CURRENT and readiness.get("reason") == "READINESS_BLOCKED":
        return _stage(NOT_APPLICABLE, "READINESS_BLOCKED", has_result=has_result)
    return creative  # Readiness готов (или устарел — тогда устареет и Creative, по наследованию)


# --- Наследование устаревания (§2.3) -------------------------------------------------------

def _effective(stages: dict) -> dict:
    """Результат ниже устаревшей / несостоявшейся обязательной стадии тоже не актуален."""
    for name in (*REQUIRED, *ADVISORY):
        stage = stages[name]
        if stage["status"] in (MISSING, NOT_APPLICABLE):
            continue
        bad = next((dep for dep in DEPENDS_ON[name]
                    if stages[dep]["status"] not in (CURRENT, NOT_APPLICABLE)), None)
        if bad is not None and stage["status"] != S_STALE:
            stages[name] = {**stage, "status": S_STALE, "reason": f"UPSTREAM_STALE:{bad}",
                            "own_status": stage["status"], "own_reason": stage["reason"]}
        elif bad is not None:
            stages[name] = {**stage, "reason": f"UPSTREAM_STALE:{bad}", "own_status": S_STALE, "own_reason": stage["reason"]}
    return stages


# --- Итоговое состояние (§3) ----------------------------------------------------------------

def derive(asset: dict, events: list[dict], verify_source: bool = False) -> dict:
    metadata = _json(asset["metadata_json"])
    stages = {
        "source": source_status(asset, events, verify_source),
        "normalize": _normalize(asset, events),
        "view": _view(asset, events),
        "qc": _qc(asset, events),
        "enhancement": _enhancement(asset, events),
        "vision": _vision(asset, events),
        "metadata": _metadata(asset, events, metadata),
        "readiness": _readiness(asset, events, metadata),
        "creative_review": _creative(asset, events),
        "publication": _stage(NOT_IMPLEMENTED),
    }
    source_ok = stages["source"]["status"] == "ok"
    if not source_ok:  # без source ничего ниже не может быть актуальным
        stages["normalize"] = {**stages["normalize"], "status": S_STALE, "reason": "UPSTREAM_STALE:source"} \
            if stages["normalize"]["status"] != MISSING else stages["normalize"]
    stages["creative_review"] = _creative_applicability(stages["creative_review"], stages["readiness"])
    stages = _effective(stages)

    state, reasons, reprocess_from = _state(stages, metadata)
    result = {
        "state_version": STATE_VERSION,
        "asset_id": asset["id"],
        "state": state,
        "reasons": reasons,
        "reprocess_from": reprocess_from,
        "terminal": state in TERMINAL_STATES,
        "metadata_state": (metadata or {}).get("state"),
        "metadata_approved": (metadata or {}).get("state") in (mb.AUTO_APPROVED, mb.APPROVED),
        "ready_for": stages["readiness"].get("ready_for", []) if state == PLATFORM_READY else [],
        "stages": stages,
    }
    result["problems"] = problems(result)
    result["allowed_actions"] = allowed_actions(result, metadata)
    return result


def _state(stages: dict, metadata: dict | None) -> tuple[str, list[str], str | None]:
    if (metadata or {}).get("state") == mb.REJECTED:
        return REJECTED, [], None
    if stages["source"]["status"] != "ok":
        return SOURCE_INVALID, [stages["source"]["reason"]], None

    for name in REQUIRED:
        stage = stages[name]
        status = stage["status"]
        if status == NOT_APPLICABLE:
            break
        if status == FAILED:
            return _FAILURE_KIND[name], [stage["reason"]], None
        if status == S_STALE:
            return STALE, [f"STALE:{name}:{stage['reason']}"], name
        if status == MISSING:
            if name == "readiness":
                return METADATA_APPROVED, [], None
            return PROCESSING, [f"NOT_PROCESSED:{name}"], None
        if name == "metadata":
            state = stage["state"]
            if state == mb.HUMAN_REVIEW:
                codes = [r["code"] for r in (metadata.get("review_gate") or {}).get("reasons", [])]
                return HUMAN_REVIEW, codes or ["HUMAN_REVIEW"], None
            if state == mb.DRAFT:
                partial = metadata.get("completeness") == mb.PARTIAL
                return PROCESSING, ["METADATA_PARTIAL" if partial else "METADATA_NOT_GATED"], None
        if name == "readiness":
            if stage.get("reason") == "READINESS_BLOCKED":
                return BLOCKED, ["READINESS_BLOCKED", *stage.get("blockers", [])], None
            return PLATFORM_READY, [], None
    return PROCESSING, ["NOT_PROCESSED:metadata"], None


# --- problems и allowed_actions (§5) ----------------------------------------------------------

def problems(result: dict) -> list[str]:
    state, stages = result["state"], result["stages"]
    codes = []
    if state in (SOURCE_INVALID, BLOCKED, ERROR, STALE):
        codes.extend(result["reasons"])
    elif state == PROCESSING and result["reasons"][0].startswith("METADATA_"):
        codes.extend(result["reasons"])
    if state not in (REJECTED, SOURCE_INVALID):
        for name in ADVISORY:
            if stages[name]["status"] == S_STALE:
                codes.append(f"STALE:{name}:{stages[name]['reason']}")
    return codes


def _action(operation: str, access: str, **params) -> dict:
    return {"operation": operation, "access": access, **({"params": params} if params else {})}


_REPROCESS = {
    "normalize": _action("asset.reprocess", "pipeline", reprocess_from="normalize"),
    "view": _action("asset.reprocess", "pipeline", reprocess_from="normalize"),
    "qc": _action("asset.reprocess", "pipeline", reprocess_from="qc"),
    "vision": _action("asset.reprocess", "pipeline", reprocess_from="vision"),
    "metadata": _action("asset.reprocess", "pipeline", reprocess_from="metadata"),
    "readiness": _action("asset.reprocess", "pipeline", reprocess_from="readiness"),
}
_NEXT = {
    "normalize": _action("normalize.run", "pipeline"),
    "view": _action("normalize.run", "pipeline"),
    "qc": _action("asset.process", "pipeline"),
    "vision": _action("asset.process", "pipeline"),
    "metadata": _action("metadata.build", "pipeline"),
}


def allowed_actions(result: dict, metadata: dict | None) -> list[dict]:
    """Подсказка агенту из состояния; права и переходы проверяет сама операция."""
    state, stages, reasons = result["state"], result["stages"], result["reasons"]
    actions = []
    if state in (REJECTED, SOURCE_INVALID):
        return actions  # source: восстановить файл вне Stocker; никаких approve
    if state == BLOCKED:
        code = reasons[0]
        if code.startswith("NORMALIZE_FAILED"):
            actions.append(_action("normalize.get", "read"))
        elif code.startswith("VIEW_FAILED"):
            actions.append(_action("normalize.run", "pipeline"))
        elif code == "READINESS_BLOCKED":
            actions += [_action("metadata.edit", "pipeline"), _action("metadata.reject", "review")]
    elif state == ERROR:
        actions.append(_action("asset.process", "pipeline") if reasons[0] == "VISION_FAILED"
                       else _action("readiness.evaluate", "pipeline"))
    elif state == STALE:
        actions.append(_REPROCESS[result["reprocess_from"]])
    elif state == PROCESSING:
        code = reasons[0]
        if code.startswith("NOT_PROCESSED:"):
            actions.append(_NEXT[code.split(":", 1)[1]])
        elif code == "METADATA_PARTIAL" and not (metadata or {}).get("edited_fields"):
            actions.append(_action("metadata.build", "pipeline"))
        if code.startswith("METADATA_"):
            actions += [_action("metadata.edit", "pipeline"), _action("metadata.reject", "review")]
    elif state == HUMAN_REVIEW:
        actions += [_action("metadata.edit", "pipeline"), _action("metadata.rebuild", "pipeline"),
                    _action("metadata.escalate", "pipeline")]
        if not ((metadata or {}).get("validation") or {}).get("errors"):
            actions.append(_action("metadata.approve", "review"))
        actions.append(_action("metadata.reject", "review"))
    elif state == METADATA_APPROVED:
        actions += [_action("readiness.evaluate", "pipeline"), _action("metadata.reject", "review")]
    elif state == PLATFORM_READY:
        if stages["creative_review"]["status"] != CURRENT:
            actions.append(_action("creative.review", "pipeline"))
        actions.append(_action("metadata.reject", "review"))

    if state not in (STALE,) and stages["enhancement"]["status"] == S_STALE and stages["view"]["status"] == CURRENT:
        actions.append(_action("enhancement.assess", "pipeline"))
    return actions


# --- Загрузка -------------------------------------------------------------------------------

def _events(asset_id: int) -> list[dict]:
    connection = get_connection()
    try:
        rows = connection.execute(
            "SELECT id, stage, status, message FROM processing_events WHERE asset_id = ? ORDER BY id", (asset_id,)
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def get(asset_id: int, verify_source: bool = False) -> dict | None:
    asset = get_asset(asset_id)
    if asset is None:
        return None
    return derive(asset, _events(asset_id), verify_source)
