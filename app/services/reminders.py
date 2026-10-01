"""Customer reminder links (WhatsApp) shared by every screen that offers them."""

from __future__ import annotations

from urllib.parse import quote

ALGERIA_COUNTRY_CODE = "213"


def international_number(phone: str | None) -> str | None:
    """Convert a local Algerian mobile number to WhatsApp's digits-only format.

    ``0661 23 45 67`` becomes ``213661234567``. Numbers already in
    international form are kept; anything too short to dial returns ``None``.
    """
    if not phone:
        return None
    digits = "".join(str(int(char)) for char in phone if char.isdecimal())
    if phone.strip().startswith("+"):
        return digits if len(digits) >= 9 else None
    if digits.startswith("00"):
        digits = digits[2:]
    elif len(digits) == 10 and digits.startswith("0"):
        digits = ALGERIA_COUNTRY_CODE + digits[1:]
    return digits if len(digits) >= 9 else None


def whatsapp_link(phone: str | None, message: str) -> str | None:
    """Return a ``wa.me`` link with a drafted message, or ``None`` without a phone."""
    number = international_number(phone)
    if number is None:
        return None
    return f"https://wa.me/{number}?text={quote(message)}"
