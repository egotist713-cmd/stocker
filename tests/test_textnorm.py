import pytest

from app.textnorm import normalize_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("shaft’s length", "shaft's length"),
        ("“quoted” and «guillemets»", '"quoted" and "guillemets"'),
        ("steel—concrete", "steel-concrete"),
        ("2020–2024", "2020-2024"),
        ("minus − sign", "minus - sign"),
        ("non‑breaking", "non-breaking"),
        ("wait…", "wait..."),
        ("ﬁne", "fine"),  # лигатура ﬁ
        ("ＡＢＣ", "ABC"),  # полноширинные буквы
        ("soft­hyphen", "softhyphen"),
        ("zero​width", "zerowidth"),
        ("﻿bom", "bom"),
        ("a b", "a b"),  # неразрывный пробел
        ("  many   spaces\n\tand\r\nlines  ", "many spaces and lines"),
        ("bell\x07char", "bell char"),
    ],
)
def test_normalize_text(raw, expected):
    assert normalize_text(raw) == expected


def test_case_and_meaning_are_preserved():
    assert normalize_text("Elevator Shaft in Berlin") == "Elevator Shaft in Berlin"


def test_empty_and_whitespace_only():
    assert normalize_text("") == ""
    assert normalize_text(" ​  ") == ""


def test_idempotent():
    raw = "  “Steel” — rails’ detail… "
    once = normalize_text(raw)

    assert normalize_text(once) == once
