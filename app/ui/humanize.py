"""Причины gate / QC / Readiness человеческим языком (только отображение, правила не меняются)."""

SIDES = {"top": "сверху", "bottom": "снизу", "left": "слева", "right": "справа"}

GATE = {
    "TRADEMARK": "Товарный знак или логотип в кадре: {detail}",
    "TEXT_BRAND_OR_LEGAL": "Надпись с брендом или юридическим наименованием: {detail}",
    "LEGAL_CLAIM": "Юридически значимое слово в описании или ключевых словах: {detail}",
    "PEOPLE_RECOGNIZABLE": "Узнаваемые люди ({detail}) — нужен релиз или ваше решение «люди не узнаваемы»",
    "PERSONAL_DOCUMENT": "Документ с персональными данными ({detail}) — не публикуется",
    "EDITORIAL_RISK": "Редакционный риск: {detail}",
    "AI_GENERATED": "Vision считает изображение сгенерированным",
    "MANUAL_ESCALATION": "Отправлено на ручную проверку: {detail}",
    "VALIDATION_ERRORS": "Ошибки в metadata: {detail}",
    "UNCONFIRMED_CLAIM": "Утверждение в metadata не подтверждено Vision: {detail}",
    "AUTO_APPROVE_DISABLED": "Автоодобрение выключено — решение за человеком",
}

QC_WARNINGS = {
    "RESOLUTION_BELOW_RECOMMENDED": "Разрешение ниже рекомендованного (12 MP)",
    "HIGH_DARK_PIXEL_RATIO": "Много почти чёрных пикселей",
    "HIGH_BRIGHT_PIXEL_RATIO": "Много пересвеченных пикселей",
}

# Блокеры Readiness и состояния blocked: (что случилось, что делать).
BLOCKERS = {
    "MODEL_RELEASE_REQUIRED": ("Узнаваемые люди без релиза",
                               "Если люди в кадре не узнаваемы — отметьте это переключателем; иначе нужен релиз или другой кадр"),
    "COLOR_SPACE_UNDECLARED": ("Цвет файла не объявлен (нет ICC-профиля)",
                               "Пересохраните файл со встроенным sRGB и положите в incoming под новым именем"),
    "DOMINANT_BRAND": ("Бренд или логотип — главный объект кадра", "Нужна ретушь или другой кадр"),
    "TRADEMARK_IN_METADATA": ("Бренд в описании или ключевых словах", "Отклоните или пересоберите metadata"),
    "PERSONAL_DOCUMENT": ("Документ с персональными данными", "Такие кадры не публикуются — отклоните"),
    "EDITORIAL_ONLY": ("Только для редакционного использования", "Редакционная публикация не поддерживается — отклоните"),
    "AI_GENERATED": ("Vision считает изображение сгенерированным", "Проверьте источник; при ошибке — отклоните и загрузите заново"),
    "RESOLUTION_TOO_LOW": ("Разрешение меньше 4 MP", "Нужен файл большего размера"),
    "TEXT_EMPTY": ("Пустое описание", "Отклоните или пересоберите metadata"),
    "TEXT_TOO_LONG": ("Описание длиннее допустимого", "Отклоните или пересоберите metadata"),
    "KEYWORDS_TOO_FEW": ("Мало ключевых слов", "Отклоните или пересоберите metadata"),
    "KEYWORDS_TOO_MANY": ("Слишком много ключевых слов", "Отклоните или пересоберите metadata"),
    "METADATA_PARTIAL": ("Metadata неполная (Metadata AI не ответил)", "Повторите обработку позже"),
    "QC_NOT_PASSED": ("Файл не прошёл технический контроль", "Нужен другой файл"),
    "SOURCE_NOT_OK": ("Исходный файл изменён или пропал", "Верните исходный файл на место"),
    "READINESS_BLOCKED": ("Не проходит правила площадок", "См. причины ниже"),
    "QC_FAILED": ("Файл не прошёл технический контроль", "Нужен другой файл"),
}


def edge_border(borders: list[dict] | None) -> list[str]:
    return [f"Чёрная полоса по краю {SIDES.get(b['side'], b['side'])}: {b['width']} px "
            f"(доля {round(b['share'] * 100)} %) — обрезать {b['width'] + 3} px" for b in borders or []]


def gate_reason(code: str, detail) -> str:
    detail = detail if detail is not None else ""
    if code == "MANUAL_ESCALATION" and str(detail).startswith("EDGE_BORDER"):
        text = str(detail).split(":", 1)[1].strip()
        for side, ru in SIDES.items():
            text = text.replace(side, ru)
        return f"Чёрные полосы по краям: {text.replace('чёрные полосы - ', '').replace('чёрные полосы — ', '')}"
    return GATE.get(code, "{code}: {detail}").format(code=code, detail=detail)


def gate_reasons(metadata: dict | None) -> list[str]:
    gate = (metadata or {}).get("review_gate") or {}
    return [gate_reason(r["code"], r.get("detail")) for r in gate.get("reasons", [])]


def qc_reasons(qc: dict | None) -> list[str]:
    qc = qc or {}
    lines = [QC_WARNINGS[w] for w in qc.get("warnings", []) if w in QC_WARNINGS]
    return lines + edge_border(qc.get("edge_border"))


def blocker(code: str) -> tuple[str, str]:
    return BLOCKERS.get(code, (code, "Проверьте объект; при необходимости отклоните"))
