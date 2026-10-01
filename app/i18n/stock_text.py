"""Localized wording for stock units (battery, sale status)."""

from __future__ import annotations

from app.i18n import ar


_RTL_RANGES = ((0x0590, 0x08FF), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF))


def ltr_text(text: str) -> str:
    """Keep a Latin product name, IMEI or REF in reading order inside right-to-left tables.

    In an Arabic (RTL) cell "13 pm blue" would otherwise show as "pm blue 13".
    Text that contains Arabic letters is returned unchanged.
    """
    if not text or any(start <= ord(char) <= end for char in text for start, end in _RTL_RANGES):
        return text
    return f"\u2066{text}\u2069"  # LEFT-TO-RIGHT ISOLATE ... POP DIRECTIONAL ISOLATE


def battery_text(is_new: bool, health: int | None) -> str:
    """"جديد" for a new phone, "92%" for a used one, a dash when unknown."""
    if is_new:
        return ar.STOCK_BATTERY_NEW
    return "—" if health is None else f"{health}%"


def status_text(status: str) -> str:
    """Available / sold."""
    return ar.STOCK_STATUS_SOLD if status == "sold" else ar.STOCK_STATUS_AVAILABLE


def unit_label(unit: object, *, show_price: bool = True) -> str:
    """One line describing a unit in pickers: ``REF-00012 · Black · 92% · 356789… · 30,500 دج``."""
    parts = [getattr(unit, "reference", None) or "—"]
    color = getattr(unit, "color", None)
    if color:
        parts.append(color)
    parts.append(battery_text(bool(getattr(unit, "is_new", False)), getattr(unit, "battery_health", None)))
    imei = getattr(unit, "imei", None)
    if imei:
        parts.append(imei)
    if show_price:
        parts.append(f"{getattr(unit, 'cash_price', 0):,} {ar.CURRENCY_SUFFIX}")
    return " · ".join(parts)
