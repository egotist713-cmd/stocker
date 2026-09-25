import re
import unicodedata

# Типографские символы → ASCII-эквиваленты. Стоки и CSV-экспорт надёжнее
# работают с простыми кавычками и дефисами.
_TYPOGRAPHY = str.maketrans(
    {
        "‘": "'",  # ‘
        "’": "'",  # ’
        "‚": "'",  # ‚
        "‛": "'",  # ‛
        "′": "'",  # ′
        "“": '"',  # “
        "”": '"',  # ”
        "„": '"',  # „
        "‟": '"',  # ‟
        "″": '"',  # ″
        "«": '"',  # «
        "»": '"',  # »
        "‐": "-",  # hyphen
        "‑": "-",  # non-breaking hyphen
        "‒": "-",  # figure dash
        "–": "-",  # en dash
        "—": "-",  # em dash
        "―": "-",  # horizontal bar
        "−": "-",  # minus sign
        # Невидимые символы удаляются.
        "­": None,  # soft hyphen
        "​": None,  # zero width space
        "‌": None,  # zero width non-joiner
        "‍": None,  # zero width joiner
        "⁠": None,  # word joiner
        "﻿": None,  # BOM / zero width no-break space
    }
)

_WHITESPACE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """
    Нормализовать текст metadata: Unicode NFKC, ASCII-кавычки и дефисы,
    удаление невидимых и управляющих символов, схлопывание пробелов.

    Смысл и регистр не меняются.
    """
    text = unicodedata.normalize("NFKC", value).translate(_TYPOGRAPHY)
    text = "".join(" " if unicodedata.category(char) == "Cc" else char for char in text)
    return _WHITESPACE.sub(" ", text).strip()
