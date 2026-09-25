"""
Review gate gate-v1: можно ли пропустить metadata без человека.

Контракт: docs/METADATA_CONTRACT.md §6A (metadata-v2). Чистые функции без БД
и AI. Builder, валидация и grounding (app/metadata_builder.py) не меняются:
gate читает их результат и решает только маршрут.
"""

import copy
import re
from datetime import datetime, timezone

from app import metadata_builder as mb
from app.ai.schema import AIAnalysis
from app.textnorm import normalize_text


POLICY_VERSION = "gate-v1"
METADATA_VERSION = "2"

AUTO_APPROVED = "auto_approved"
HUMAN_REVIEW = "human_review"
DEFERRED = "deferred"

# Состояния, которые gate может менять. Решения человека gate не трогает.
GATEABLE_STATES = (mb.DRAFT, AUTO_APPROVED, HUMAN_REVIEW)
ESCALATABLE_STATES = GATEABLE_STATES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _contains(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


# --- text_visible (§6A.4) -------------------------------------------------------

LEGAL_FORMS = frozenset(
    {
        "inc", "inc.", "ltd", "ltd.", "llc", "gmbh", "ag", "corp", "corp.", "plc", "s.a.", "srl", "bv", "b.v.",
        "ооо", "оао", "зао", "пао", "ао", "нпо", "фгуп", "гуп", "муп", "ип",
    }
)
LEGAL_SYMBOLS = ("®", "™", "©")
LEGAL_WORDS = ("copyright", "patent", "patented", "trademark", "all rights reserved")

WARNING_WORDS = (
    "warning", "caution", "danger", "high voltage", "notice", "exit", "emergency", "fire", "stop",
    "no entry", "keep out",
    "внимание", "осторожно", "опасно", "высокое напряжение", "не включать", "запрещено", "выход", "стоп",
)

_TOKEN = re.compile(r"[\w.]+")
_URL = re.compile(r"(https?://|www\.)|\b[\w-]+\.(com|net|org|ru|de|io|рф)\b", re.IGNORECASE)
_EMAIL = re.compile(r"\S+@\S+\.\S+")
_PHONE = re.compile(r"\+?\d[\d\s()\-]{8,}\d")
_CODE = re.compile(r"(?<!\w)[A-ZА-ЯЁ]{1,4}-[A-ZА-ЯЁ0-9]{1,4}(?!\w)")


def _brand_terms(vision: AIAnalysis) -> list[str]:
    terms = (normalize_text(name).casefold() for name in [*vision.brands, *vision.logos])
    return [term for term in terms if term]


def classify_text_item(text: str, vision: AIAnalysis) -> dict:
    """Категория одной надписи: brand_or_legal → technical → descriptive (первое совпадение)."""
    normalized = normalize_text(text)
    folded = normalized.casefold()
    tokens = [token.casefold() for token in _TOKEN.findall(normalized)]

    def result(category: str, rule: str) -> dict:
        return {"text": normalized, "category": category, "rule": rule}

    if any(_contains(folded, brand) for brand in _brand_terms(vision)):
        return result("brand_or_legal", "BRAND")
    if any(token in LEGAL_FORMS or token.rstrip(".") in LEGAL_FORMS for token in tokens):
        return result("brand_or_legal", "LEGAL_FORM")
    if any(symbol in normalized for symbol in LEGAL_SYMBOLS):
        return result("brand_or_legal", "LEGAL_SYMBOL")
    if any(_contains(folded, word) for word in LEGAL_WORDS):
        return result("brand_or_legal", "LEGAL_WORD")
    if _URL.search(normalized) or _EMAIL.search(normalized):
        return result("brand_or_legal", "CONTACT")
    phone = _PHONE.search(normalized)
    if phone and sum(char.isdigit() for char in phone.group()) >= 10:
        return result("brand_or_legal", "CONTACT")

    if any(char.isdigit() for char in normalized):
        return result("technical", "DIGITS")
    if _CODE.search(normalized):
        return result("technical", "CODE")
    if any(_contains(folded, word) for word in WARNING_WORDS):
        return result("technical", "WARNING_WORD")

    return result("descriptive", "DEFAULT")


# --- Люди (§6A.4a) --------------------------------------------------------------

RECOGNIZABLE_MARKERS = (
    "face", "faces", "facial", "portrait", "headshot", "smiling", "looking at camera",
    "looking at the camera", "eyes",
)
PARTIAL_MARKERS = (
    "hand", "hands", "glove", "gloves", "gloved", "arm", "arms", "finger", "fingers", "legs", "feet",
    "from behind", "back view", "rear view", "silhouette", "silhouetted", "faceless", "face not visible",
    "face is not visible", "face obscured", "face hidden", "face covered", "unrecognizable", "anonymous",
    "blurred figure",
)
FACE_NEGATIONS = (
    "face not visible", "face is not visible", "face obscured", "face hidden", "face covered", "faceless",
)
PERSON_SUBJECT_WORDS = (
    "person", "people", "man", "men", "woman", "women", "worker", "workers", "engineer", "technician",
    "operator", "electrician", "builder", "welder", "mechanic",
)
NON_HUMAN_PHRASES = (
    "hand tool", "hand tools", "hand rail", "hand rails", "hand truck", "hand wheel", "robotic arm",
    "robot arm", "crane arm", "mechanical arm", "swing arm",
)


def _without(text: str, phrases) -> str:
    for phrase in phrases:
        text = re.sub(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ", text)
    return text


def people_risk(vision: AIAnalysis) -> dict:
    if not vision.people.present and vision.people.count == 0:
        return {"level": "none", "markers": []}

    parts = [
        vision.title, vision.description, vision.subject, vision.commercial_context,
        *vision.technical_subjects, *vision.keywords,
    ]
    text = _without(normalize_text(" ".join(parts)).casefold(), NON_HUMAN_PHRASES)
    subject = _without(normalize_text(vision.subject).casefold(), NON_HUMAN_PHRASES)

    partial = [marker for marker in PARTIAL_MARKERS if _contains(text, marker)]
    recognizable = [marker for marker in RECOGNIZABLE_MARKERS if _contains(_without(text, FACE_NEGATIONS), marker)]

    if recognizable:
        return {"level": "recognizable", "markers": recognizable}

    if partial:
        return {"level": "partial", "markers": partial}

    person_subject = [word for word in PERSON_SUBJECT_WORDS if _contains(subject, word)]
    if person_subject:
        return {"level": "recognizable", "markers": [f"subject:{word}" for word in person_subject]}

    return {"level": "unclear", "markers": []}


# --- Юридически значимые утверждения (§6A.5) -------------------------------------

LEGAL_CLAIM_PATTERNS = (
    r"certified", r"certification", r"compliant", r"compliance", r"approved", r"patented", r"patent",
    r"trademark(?:ed)?", r"licensed", r"official", r"guaranteed", r"guarantee", r"warranty",
    r"iso\s?\d{3,5}", r"ul listed", r"ce marked", r"meets\b.{0,40}?\bstandards?",
)
_LEGAL_CLAIM = re.compile(r"(?<!\w)(?:" + "|".join(LEGAL_CLAIM_PATTERNS) + r")(?!\w)", re.IGNORECASE)


def legal_claims(fields: dict) -> list[dict]:
    found = []
    texts = [("title", fields["title"]), ("description", fields["description"])]
    texts += [("keywords", keyword) for keyword in fields["keywords"]]

    for field, text in texts:
        for match in _LEGAL_CLAIM.finditer(text):
            found.append({"field": field, "term": match.group()})

    return found


# --- Решение (§6A.3) ------------------------------------------------------------


def evaluate(metadata: dict, vision: AIAnalysis, auto_approve_enabled: bool = True) -> dict:
    """Вычислить блок review_gate. Не меняет state (это делает apply_gate)."""
    reasons, notes = [], []

    def reason(code: str, detail=None):
        reasons.append({"code": code, "detail": detail})

    def note(code: str, detail=None):
        notes.append({"code": code, "detail": detail})

    errors = metadata["validation"]["errors"]
    other_errors = sorted({e["code"] for e in errors if e["code"] != "UNCONFIRMED_CLAIM"})
    if other_errors:
        reason("VALIDATION_ERRORS", ", ".join(other_errors))

    for claim in metadata["grounding"]["specific_claims"]:
        if not claim["confirmed"]:
            reason("UNCONFIRMED_CLAIM", claim["term"])

    if vision.brands or vision.logos:
        reason("TRADEMARK", ", ".join([*vision.brands, *vision.logos]))

    text_items = [classify_text_item(text, vision) for text in vision.text_visible]
    for item in text_items:
        if item["category"] == "brand_or_legal":
            reason("TEXT_BRAND_OR_LEGAL", item["text"])
        elif item["category"] == "technical":
            note("TEXT_TECHNICAL", item["text"])
        else:
            note("TEXT_DESCRIPTIVE", item["text"])

    for claim in legal_claims(metadata["fields"]):
        reason("LEGAL_CLAIM", f"{claim['field']}: {claim['term']}")

    people = people_risk(vision)
    if people["level"] == "recognizable":
        reason("PEOPLE_RECOGNIZABLE", ", ".join(people["markers"]))
    elif people["level"] == "unclear":
        reason("PEOPLE_UNCLEAR", f"people.count={vision.people.count}")
    elif people["level"] == "partial":
        note("PEOPLE_PARTIAL", ", ".join(people["markers"]))

    if vision.editorial_risk:
        reason("EDITORIAL_RISK", ", ".join(vision.editorial_risk))

    if vision.ai_generated:
        reason("AI_GENERATED")

    escalation = (metadata.get("review_gate") or {}).get("escalation")
    if escalation:
        reason("MANUAL_ESCALATION", escalation["reason"])

    if metadata["completeness"] == mb.PARTIAL and not escalation:
        decision = DEFERRED
    elif reasons:
        decision = HUMAN_REVIEW
    elif not auto_approve_enabled:
        reason("AUTO_APPROVE_DISABLED")
        decision = HUMAN_REVIEW
    else:
        decision = AUTO_APPROVED

    return {
        "policy_version": POLICY_VERSION,
        "decision": decision,
        "reasons": reasons,
        "notes": notes,
        "text_items": text_items,
        "people_risk": people,
        "escalation": escalation,
        "auto_approve_enabled": auto_approve_enabled,
        "evaluated_at": _now(),
    }


def _state_for(decision: str) -> str:
    return mb.DRAFT if decision == DEFERRED else decision


def apply_gate(
    metadata: dict,
    vision: AIAnalysis,
    *,
    auto_approve_enabled: bool = True,
) -> tuple[dict, dict | None]:
    """
    Применить gate. Возвращает (metadata, gate) — gate is None, если состояние
    — решение человека (approved/rejected) и gate его не трогает.
    """
    if metadata["state"] not in GATEABLE_STATES:
        return metadata, None

    result = copy.deepcopy(metadata)
    gate = evaluate(result, vision, auto_approve_enabled)
    result["review_gate"] = gate
    result["state"] = _state_for(gate["decision"])
    result["metadata_version"] = METADATA_VERSION
    return result, gate


def escalate(metadata: dict, vision: AIAnalysis, reason: str, *, auto_approve_enabled: bool = True) -> dict:
    """Поднять риск: → human_review с MANUAL_ESCALATION до решения человека."""
    if metadata["state"] not in ESCALATABLE_STATES:
        raise mb.MetadataTransitionError(f"Cannot escalate from state '{metadata['state']}'")

    reason = normalize_text(reason or "")
    if not reason:
        raise mb.MetadataTransitionError("Escalate requires a reason")

    result = copy.deepcopy(metadata)
    result.setdefault("review_gate", {})
    result["review_gate"] = {**(result["review_gate"] or {}), "escalation": {"reason": reason, "at": _now()}}
    gated, _ = apply_gate(result, vision, auto_approve_enabled=auto_approve_enabled)
    return gated


def clear_escalation(metadata: dict) -> dict:
    """Решение человека (approve/reject) снимает ручную эскалацию."""
    if not (metadata.get("review_gate") or {}).get("escalation"):
        return metadata

    result = copy.deepcopy(metadata)
    result["review_gate"]["escalation"] = None
    return result
