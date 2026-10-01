"""Cell helpers shared by the Excel importers (sales sheet and product sheet)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
import unicodedata


def normalize_header(value: str) -> str:
    """Normalize an Arabic Excel header for matching and sale type parsing."""
    result: list[str] = []
    for char in unicodedata.normalize("NFKD", value).casefold():
        if unicodedata.category(char).startswith("M") or char == "ـ":
            continue
        if char in "أإآٱء":
            result.append("ا")
        elif char == "ة":
            result.append("ه")
        else:
            result.append(char)
    return "".join(result).strip()


def western_digits(value: str) -> str:
    """Translate Arabic and Persian decimal digits to Western digits."""
    return "".join(str(unicodedata.digit(char)) if char.isdecimal() else char for char in value)


def is_blank(value: Any) -> bool:
    """Treat spreadsheet blanks, NaN, and whitespace-only strings uniformly."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, Decimal):
        return value.is_nan()
    try:
        if value != value:
            return True
    except (TypeError, ValueError):
        pass
    return bool(getattr(value, "__class__", None) and value.__class__.__name__ == "NAType")
