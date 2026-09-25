"""
Детерминированный metadata-слой без БД и без AI-вызовов.

Контракт: docs/METADATA_CONTRACT.md (metadata-v1). Все функции чистые:
принимают metadata (dict) и возвращают новый dict, не изменяя входной.
Сохранение и события — в app/metadata.py.
"""

import copy
import re
from datetime import datetime, timezone

from app.ai.schema import AIAnalysis, MetadataSuggestion
from app.textnorm import normalize_text


METADATA_VERSION = "1"
BUILDER_VERSION = "metadata-v1"

# Внутренние лимиты (§4.5). Сверить с правилами площадок на этапе экспорта;
# при изменении увеличить BUILDER_VERSION.
TITLE_MAX = 200
TITLE_RECOMMENDED = 70
DESCRIPTION_MAX = 200
KEYWORDS_MIN = 7
KEYWORDS_RECOMMENDED = 25
KEYWORDS_MAX = 49
KEYWORD_MAX_LENGTH = 50

STOPWORDS = frozenset({"photo", "image", "picture", "stock", "stock photo"})

EDITABLE_FIELDS = ("title", "description", "keywords")

DRAFT = "draft"
APPROVED = "approved"
REJECTED = "rejected"

# metadata-v2 (§6A): состояния review gate. Их устанавливает app/review_gate.py.
AUTO_APPROVED = "auto_approved"
HUMAN_REVIEW = "human_review"

# Решения человека допустимы и после gate.
APPROVABLE_STATES = (DRAFT, AUTO_APPROVED, HUMAN_REVIEW)
REJECTABLE_STATES = (DRAFT, AUTO_APPROVED, HUMAN_REVIEW, APPROVED)

FULL = "full"
PARTIAL = "partial"


class MetadataTransitionError(ValueError):
    """Недопустимый переход review или недопустимая правка."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_review() -> dict:
    return {"decided_at": None, "reason": None, "allow_partial": False}


# --- Нормализация (§4.2, §4.3) ------------------------------------------------

_EDGE_PUNCTUATION = re.compile(r"^[\W_]+|[\W_]+$")


def normalize_title(value: str) -> str:
    return normalize_text(value).rstrip(".").rstrip()


def normalize_description(value: str) -> str:
    return normalize_text(value)


def _contains_term(text: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None


def _brand_terms(vision: AIAnalysis) -> list[str]:
    terms = (normalize_text(name).lower() for name in [*vision.brands, *vision.logos])
    return [term for term in terms if term]


def normalize_keywords(raw: list[str], vision: AIAnalysis) -> tuple[list[str], list[dict]]:
    """Нормализовать форму keywords. Количество и длину не ограничивает (это валидация)."""
    brands = _brand_terms(vision)
    keywords: list[str] = []
    seen: set[str] = set()
    dropped: list[dict] = []

    for item in raw:
        for part in re.split(r"[,;]", item):
            keyword = normalize_text(_EDGE_PUNCTUATION.sub("", normalize_text(part).lower()))

            if not keyword:
                reason = "EMPTY"
            elif any(_contains_term(keyword, brand) for brand in brands):
                reason = "BRAND"
            elif keyword in STOPWORDS:
                reason = "STOPWORD"
            elif keyword in seen:
                reason = "DUPLICATE"
            else:
                keywords.append(keyword)
                seen.add(keyword)
                continue

            dropped.append({"keyword": keyword or normalize_text(part), "reason": reason})

    return keywords, dropped


def merge_keywords(primary: list[str], vision_keywords: list[str]) -> list[str]:
    """Порядок Metadata AI, затем Vision-keywords, которых там нет."""
    present = {normalize_text(keyword).lower() for keyword in primary}
    return [*primary, *(k for k in vision_keywords if normalize_text(k).lower() not in present)]


# --- Флаги (§4.4) ---------------------------------------------------------------


def compute_flags(vision: AIAnalysis) -> dict:
    trademark_risk = bool(vision.brands or vision.logos)
    return {
        "model_release_required": vision.people.present or vision.people.count > 0,
        "trademark_risk": trademark_risk,
        "visible_text": bool(vision.text_visible),
        "editorial_risk": bool(vision.editorial_risk) or trademark_risk,
        "ai_generated": vision.ai_generated,
    }


# --- Опора на Vision (§4.7) -----------------------------------------------------

_TOKEN = re.compile(r"[a-z0-9]+")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _stem(token: str) -> str:
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def _tokens(text: str) -> list[str]:
    return [_stem(token) for token in _TOKEN.findall(normalize_text(text).lower())]


def vision_corpus(vision: AIAnalysis) -> set[str]:
    parts = [
        vision.title,
        vision.description,
        vision.subject,
        vision.commercial_context,
        *vision.technical_subjects,
        *vision.keywords,
        *vision.text_visible,
        *vision.brands,
        *vision.logos,
        *vision.editorial_risk,
    ]
    return set(_tokens(" ".join(parts)))


def _grounded(text: str, corpus: set[str]) -> bool:
    tokens = _tokens(text)
    return bool(tokens) and all(token in corpus for token in tokens)


def _is_title_case(text: str) -> bool:
    # Первое слово заглавное всегда, поэтому не учитывается.
    long_words = [word for word in _WORD.findall(text)[1:] if len(word) >= 4]
    if not long_words:
        return False
    capitalized = sum(1 for word in long_words if word[0].isupper())
    return capitalized / len(long_words) >= 0.8


def _text_claims(text: str, field: str, corpus: set[str], proper_nouns: bool) -> list[dict]:
    claims = []

    for sentence in _SENTENCE_END.split(text):
        for index, match in enumerate(_WORD.finditer(sentence)):
            word = re.sub(r"'s$", "", match.group()).strip("'-")
            if not word or _grounded(word, corpus):
                continue

            is_acronym = len(word) >= 2 and word.isupper() and word.isalpha()

            if any(char.isdigit() for char in word):
                reason = "NUMBER"
            elif proper_nouns and ((index > 0 and word[0].isupper()) or is_acronym):
                reason = "PROPER_NOUN"
            else:
                continue

            claims.append({"term": word, "field": field, "reason": reason})

    return claims


def analyze_grounding(fields: dict, vision: AIAnalysis, previous: dict | None = None) -> dict:
    corpus = vision_corpus(vision)
    confirmed = {
        (claim["term"].lower(), claim["field"])
        for claim in (previous or {}).get("specific_claims", [])
        if claim.get("confirmed")
    }

    claims = [
        *_text_claims(fields["description"], "description", corpus, proper_nouns=True),
        *_text_claims(fields["title"], "title", corpus, proper_nouns=not _is_title_case(fields["title"])),
    ]
    proper_noun_tokens = {
        token for claim in claims if claim["reason"] == "PROPER_NOUN" for token in _tokens(claim["term"])
    }

    vision_keywords, concept_keywords = [], []

    for keyword in fields["keywords"]:
        tokens = _tokens(keyword)
        # Утверждением считается только цифра без опоры на Vision:
        # "ip20 rating" при "IP20" в Vision — концепт, а не утверждение.
        ungrounded_numbers = [t for t in tokens if any(c.isdigit() for c in t) and t not in corpus]
        if _grounded(keyword, corpus):
            vision_keywords.append(keyword)
        elif ungrounded_numbers:
            claims.append({"term": keyword, "field": "keywords", "reason": "NUMBER"})
        elif proper_noun_tokens.intersection(tokens):
            claims.append({"term": keyword, "field": "keywords", "reason": "PROPER_NOUN"})
        else:
            concept_keywords.append(keyword)

    unique_claims, seen = [], set()
    for claim in claims:
        key = (claim["term"].lower(), claim["field"])
        if key in seen:
            continue
        seen.add(key)
        unique_claims.append({**claim, "confirmed": key in confirmed})

    return {
        "vision_keywords": vision_keywords,
        "concept_keywords": concept_keywords,
        "specific_claims": unique_claims,
    }


# --- Валидация (§4.5) -----------------------------------------------------------


def _issue(code: str, field: str | None, message: str) -> dict:
    return {"code": code, "field": field, "message": message}


def validate(fields: dict, flags: dict, grounding: dict, completeness: str, vision: AIAnalysis) -> dict:
    errors, warnings = [], []
    title, description, keywords = fields["title"], fields["description"], fields["keywords"]

    if not title:
        errors.append(_issue("TITLE_EMPTY", "title", "Title is empty"))
    elif len(title) > TITLE_MAX:
        errors.append(_issue("TITLE_TOO_LONG", "title", f"Title is {len(title)} characters (max {TITLE_MAX})"))
    elif len(title) > TITLE_RECOMMENDED:
        warnings.append(
            _issue("TITLE_LONG", "title", f"Title is {len(title)} characters (recommended <= {TITLE_RECOMMENDED})")
        )

    if not description:
        errors.append(_issue("DESCRIPTION_EMPTY", "description", "Description is empty"))
    elif len(description) > DESCRIPTION_MAX:
        errors.append(
            _issue(
                "DESCRIPTION_TOO_LONG",
                "description",
                f"Description is {len(description)} characters (max {DESCRIPTION_MAX})",
            )
        )

    if len(keywords) < KEYWORDS_MIN:
        errors.append(
            _issue("TOO_FEW_KEYWORDS", "keywords", f"{len(keywords)} keywords (min {KEYWORDS_MIN})")
        )
    elif len(keywords) < KEYWORDS_RECOMMENDED:
        warnings.append(
            _issue("FEW_KEYWORDS", "keywords", f"{len(keywords)} keywords (recommended >= {KEYWORDS_RECOMMENDED})")
        )

    if len(keywords) > KEYWORDS_MAX:
        errors.append(
            _issue("TOO_MANY_KEYWORDS", "keywords", f"{len(keywords)} keywords (max {KEYWORDS_MAX})")
        )

    for keyword in keywords:
        if len(keyword) > KEYWORD_MAX_LENGTH:
            errors.append(
                _issue(
                    "KEYWORD_TOO_LONG",
                    "keywords",
                    f"Keyword is {len(keyword)} characters (max {KEYWORD_MAX_LENGTH}): '{keyword}'",
                )
            )

    for field in ("title", "description"):
        lowered = fields[field].lower()
        for brand in _brand_terms(vision):
            if _contains_term(lowered, brand):
                errors.append(_issue("BRAND_IN_TEXT", field, f"Brand or logo in {field}: '{brand}'"))

    for claim in grounding["specific_claims"]:
        if not claim["confirmed"]:
            errors.append(
                _issue(
                    "UNCONFIRMED_CLAIM",
                    claim["field"],
                    f"Unconfirmed specific claim: '{claim['term']}' ({claim['reason']})",
                )
            )

    if completeness == PARTIAL:
        warnings.append(_issue("PARTIAL_DRAFT", None, "Metadata AI result is missing; draft built from Vision"))

    flag_warnings = {
        "model_release_required": "MODEL_RELEASE_REQUIRED",
        "trademark_risk": "TRADEMARK_RISK",
        "visible_text": "VISIBLE_TEXT",
        "editorial_risk": "EDITORIAL_RISK",
        "ai_generated": "AI_GENERATED",
    }
    for flag, code in flag_warnings.items():
        if flags[flag]:
            warnings.append(_issue(code, None, f"Flag {flag} is set"))

    return {"errors": errors, "warnings": warnings}


def _derive(metadata: dict, vision: AIAnalysis, dropped: list[dict]) -> dict:
    """Пересчитать flags, grounding и validation по текущим fields."""
    metadata["flags"] = compute_flags(vision)
    metadata["grounding"] = analyze_grounding(metadata["fields"], vision, metadata.get("grounding"))
    metadata["validation"] = {
        **validate(metadata["fields"], metadata["flags"], metadata["grounding"], metadata["completeness"], vision),
        "dropped_keywords": dropped,
    }
    return metadata


def _source_values(vision: AIAnalysis, suggestion: MetadataSuggestion | None) -> dict:
    if suggestion is None:
        return {"title": vision.title, "description": vision.description, "keywords": list(vision.keywords)}

    return {
        "title": suggestion.title,
        "description": suggestion.description,
        "keywords": merge_keywords(suggestion.keywords, vision.keywords),
    }


# --- Сборка и переходы (§6) -----------------------------------------------------


def build_draft(
    vision: AIAnalysis,
    suggestion: MetadataSuggestion | None,
    *,
    vision_source: dict,
    metadata_ai_source: dict | None = None,
    metadata_ai_failure_event_id: int | None = None,
) -> dict:
    """Новый draft: full из MetadataSuggestion или partial из Vision (§4.1, §4.6)."""
    values = _source_values(vision, suggestion)
    keywords, dropped = normalize_keywords(values["keywords"], vision)

    metadata = {
        "metadata_version": METADATA_VERSION,
        "state": DRAFT,
        "completeness": FULL if suggestion is not None else PARTIAL,
        "fields": {
            "title": normalize_title(values["title"]),
            "description": normalize_description(values["description"]),
            "keywords": keywords,
        },
        "generated": suggestion.model_dump() if suggestion is not None else None,
        "edited_fields": [],
        "review": _empty_review(),
        "sources": {
            "vision": vision_source,
            "metadata_ai": metadata_ai_source if suggestion is not None else None,
            "metadata_ai_failure_event_id": metadata_ai_failure_event_id if suggestion is None else None,
            "builder_version": BUILDER_VERSION,
            "built_at": _now(),
        },
    }
    return _derive(metadata, vision, dropped)


def rebuild(metadata: dict, vision: AIAnalysis) -> dict:
    """Применить текущие Python-правила к generated (или Vision), сохранив правки человека."""
    result = copy.deepcopy(metadata)
    suggestion = MetadataSuggestion.model_validate(result["generated"]) if result["generated"] else None
    values = _source_values(vision, suggestion)
    edited = set(result["edited_fields"])

    if "title" not in edited:
        result["fields"]["title"] = normalize_title(values["title"])
    if "description" not in edited:
        result["fields"]["description"] = normalize_description(values["description"])

    dropped = result["validation"]["dropped_keywords"]
    if "keywords" not in edited:
        result["fields"]["keywords"], dropped = normalize_keywords(values["keywords"], vision)

    if result["state"] != DRAFT:
        result["state"] = DRAFT
        result["review"] = _empty_review()

    result["sources"]["builder_version"] = BUILDER_VERSION
    result["sources"]["built_at"] = _now()
    return _derive(result, vision, dropped)


def edit(metadata: dict, vision: AIAnalysis, changes: dict) -> tuple[dict, list[dict]]:
    """
    Правка человека. Возвращает новый metadata и список фактических изменений
    ({field, old, new, state_before}); пустой список — изменений нет.
    """
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise MetadataTransitionError(f"Unknown fields: {sorted(unknown)}")

    result = copy.deepcopy(metadata)
    state_before = result["state"]
    dropped = result["validation"]["dropped_keywords"]
    applied = []

    for field in EDITABLE_FIELDS:
        if field not in changes:
            continue

        if field == "keywords":
            new_value, dropped = normalize_keywords(list(changes[field]), vision)
        elif field == "title":
            new_value = normalize_title(changes[field])
        else:
            new_value = normalize_description(changes[field])

        old_value = result["fields"][field]
        if new_value == old_value:
            continue

        result["fields"][field] = new_value
        if field not in result["edited_fields"]:
            result["edited_fields"].append(field)
        applied.append({"field": field, "old": old_value, "new": new_value, "state_before": state_before})

    if not applied:
        return metadata, []

    if result["state"] != DRAFT:
        result["state"] = DRAFT
        result["review"] = _empty_review()

    return _derive(result, vision, dropped), applied


def approve(
    metadata: dict,
    vision: AIAnalysis,
    *,
    allow_partial: bool = False,
    confirm_claims: bool = False,
) -> tuple[dict, list[str]]:
    """Одобрить draft. Возвращает новый metadata и список подтверждённых этой командой утверждений."""
    if metadata["state"] not in APPROVABLE_STATES:
        raise MetadataTransitionError(f"Cannot approve from state '{metadata['state']}'")

    if metadata["completeness"] == PARTIAL and not allow_partial:
        raise MetadataTransitionError("Partial draft requires --allow-partial")

    result = copy.deepcopy(metadata)
    confirmed = []

    if confirm_claims:
        for claim in result["grounding"]["specific_claims"]:
            if not claim["confirmed"]:
                claim["confirmed"] = True
                confirmed.append(claim["term"])
        result = _derive(result, vision, result["validation"]["dropped_keywords"])

    errors = result["validation"]["errors"]
    if errors:
        codes = sorted({error["code"] for error in errors})
        raise MetadataTransitionError(f"Cannot approve with validation errors: {', '.join(codes)}")

    result["state"] = APPROVED
    result["review"] = {"decided_at": _now(), "reason": None, "allow_partial": allow_partial}
    return result, confirmed


def reject(metadata: dict, reason: str) -> dict:
    if metadata["state"] not in REJECTABLE_STATES:
        raise MetadataTransitionError(f"Cannot reject from state '{metadata['state']}'")

    reason = normalize_text(reason or "")
    if not reason:
        raise MetadataTransitionError("Reject requires a reason")

    result = copy.deepcopy(metadata)
    result["state"] = REJECTED
    result["review"] = {"decided_at": _now(), "reason": reason, "allow_partial": False}
    return result
