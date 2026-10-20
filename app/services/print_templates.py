"""Owner-editable texts of the printed commitment form (تعهد والتزام) and receipts.

The texts are one JSON setting (``print_templates``). Anything missing or
invalid falls back to the default wording of the active language, so a
partly saved or older setting still prints a complete form. Commitment texts
may use placeholders such as ``{customer}`` or ``{financed}``; unknown
placeholders are printed as typed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

from sqlalchemy.orm import Session

from app.i18n import ar
from app.services import auth
from app.services.settings import get_value, set_value

SETTING_KEY = "print_templates"
MAX_TEXT_LENGTH = 600
MAX_LIST_ITEMS = 15
MAX_SIGNATURES = 4

#: Placeholders the commitment texts can use, in the order the editor lists them.
PLACEHOLDERS = (
    "customer", "phone", "product", "total", "down_payment", "financed", "monthly", "months",
    "count", "first_due", "end_date", "purchase_date", "day", "shop", "location", "today",
)
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


@dataclass(frozen=True, slots=True)
class PrintTemplates:
    """Every editable text and option of the printed documents."""

    # Shop identity (commitment header; receipts use the contact line and IDs).
    shop_name: str
    shop_location: str
    header_lines: tuple[str, ...]
    shop_phone: str
    shop_address: str
    business_nis: str
    business_nif: str
    business_rc: str
    # Commitment form.
    commitment_title: str
    commitment_intro: str
    commitment_clauses: tuple[str, ...]
    commitment_signatures: tuple[str, ...]
    commitment_footer: str
    commitment_owner_figures: bool
    # Receipts.
    receipt_footer_lines: tuple[str, ...]
    # Both.
    watermark: bool


def default_templates() -> PrintTemplates:
    """The default wording, in the active language."""
    return PrintTemplates(
        shop_name=ar.PRINT_DEFAULT_SHOP_NAME,
        shop_location=ar.PRINT_DEFAULT_SHOP_LOCATION,
        header_lines=tuple(ar.PRINT_DEFAULT_HEADER_LINES),
        shop_phone="",
        shop_address="",
        business_nis="",
        business_nif="",
        business_rc="",
        commitment_title=ar.PDF_COMMITMENT_TITLE,
        commitment_intro=ar.PRINT_DEFAULT_INTRO,
        commitment_clauses=tuple(ar.PRINT_DEFAULT_CLAUSES),
        commitment_signatures=(
            ar.PDF_SIGNATURE_CUSTOMER, ar.PDF_SIGNATURE_MUNICIPALITY, ar.PDF_SIGNATURE_SELLER,
        ),
        commitment_footer=ar.PRINT_DEFAULT_FOOTER,
        commitment_owner_figures=False,
        receipt_footer_lines=tuple(ar.PRINT_DEFAULT_RECEIPT_FOOTER),
        watermark=True,
    )


def load(session: Session) -> PrintTemplates:
    """The saved texts merged over the defaults.

    A NIS or NIF left blank here falls back to the one typed in Settings
    (older versions kept them only there).
    """
    stored = get_value(session, SETTING_KEY, None)
    templates = normalize(stored if isinstance(stored, Mapping) else {})
    if not templates.business_nis:
        templates = replace(templates, business_nis=_clean_text(get_value(session, "business_nis", "")))
    if not templates.business_nif:
        templates = replace(templates, business_nif=_clean_text(get_value(session, "business_nif", "")))
    return templates


def save_business_ids(session: Session, owner_user_id: int, *, nis: str, nif: str) -> PrintTemplates:
    """Store the NIS and NIF typed in Settings where the printed documents read them (owner only)."""
    auth.require_owner(session, owner_user_id)
    templates = replace(load(session), business_nis=_clean_text(nis), business_nif=_clean_text(nif))
    set_value(session, SETTING_KEY, to_json(templates))
    set_value(session, "business_nis", templates.business_nis)
    set_value(session, "business_nif", templates.business_nif)
    return templates


def save(session: Session, owner_user_id: int, values: Mapping[str, Any] | PrintTemplates) -> PrintTemplates:
    """Validate and store the texts (owner only); returns what was stored."""
    auth.require_owner(session, owner_user_id)
    raw = asdict(values) if isinstance(values, PrintTemplates) else dict(values)
    templates = normalize(raw)
    set_value(session, SETTING_KEY, to_json(templates))
    return templates


def reset(session: Session, owner_user_id: int) -> PrintTemplates:
    """Forget the owner's texts and go back to the default wording (owner only)."""
    auth.require_owner(session, owner_user_id)
    set_value(session, SETTING_KEY, {})
    return load(session)


def normalize(values: Mapping[str, Any]) -> PrintTemplates:
    """Build templates from loose values: blanks dropped, lengths capped, bad types replaced by defaults.

    A required text (shop name, title) left blank keeps its default; an
    emptied list stays empty, except the signatures, which keep at least one box.
    """
    defaults = default_templates()
    result: dict[str, Any] = {}
    for field in fields(PrintTemplates):
        default = getattr(defaults, field.name)
        value = values.get(field.name, default)
        if isinstance(default, bool):
            result[field.name] = value if isinstance(value, bool) else default
        elif isinstance(default, tuple):
            if not isinstance(value, (list, tuple)):
                value = default
            items = tuple(text for text in (_clean_text(item) for item in value) if text)
            limit = MAX_SIGNATURES if field.name == "commitment_signatures" else MAX_LIST_ITEMS
            items = items[:limit]
            if field.name == "commitment_signatures" and not items:
                items = default
            result[field.name] = items
        else:
            text = _clean_text(value) if isinstance(value, str) else default
            if not text and field.name in ("shop_name", "commitment_title"):
                text = default
            result[field.name] = text
    return PrintTemplates(**result)


def to_json(templates: PrintTemplates) -> dict[str, Any]:
    """JSON-ready dict (lists instead of tuples)."""
    return {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in asdict(templates).items()
    }


def fill(text: str, values: Mapping[str, str]) -> str:
    """Replace ``{name}`` placeholders that have a value; leave any other braces as typed."""
    return _PLACEHOLDER_RE.sub(lambda match: values.get(match.group(1), match.group(0)), text)


def _clean_text(value: Any) -> str:
    """One trimmed text of at most ``MAX_TEXT_LENGTH`` characters ("" for non-text)."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_TEXT_LENGTH]
