"""Stock: one row per physical phone, as in the shop's "Gros et Détail" sheet.

A catalog :class:`~app.db.models.Product` is the model ("SAM A07 4/64") with
its default prices and plan. Each :class:`~app.db.models.StockItem` is one
unit of it with its own wholesale and selling price, battery (a percentage
for a used phone, or *new*, written ``*`` in the sheet), colour, IMEI,
reference (``REF-00001``) and a free note. Selling a unit marks it sold and
links it to the sale.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.db.models import Product, Sale, StockItem
from app.services import auth
from app.services.spreadsheet import is_blank, western_digits

STATUS_AVAILABLE = "available"
STATUS_SOLD = "sold"
CONDITION_NEW = "new"
CONDITION_USED = "used"
CONDITION_UNKNOWN = "unknown"
CONDITION_LOW_BATTERY = "low_battery"
LOW_BATTERY_BELOW = 85
REFERENCE_PREFIX = "REF-"
_NEW_WORDS = {"*", "new", "neuf", "nouveau", "جديد", "جديدة", "neu"}
_REFERENCE_NUMBER = re.compile(r"^REF-(\d+)$")


class StockError(ValueError):
    """A stock operation was refused; ``code`` says why (``imei_taken``, ``reference_taken``, ``sold``, ``not_enough``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class UnitDetails:
    """The descriptive fields of one phone (all optional)."""

    color: str | None = None
    battery_health: int | None = None
    is_new: bool = False
    imei: str | None = None
    reference: str | None = None
    note: str | None = None

    def cleaned(self) -> UnitDetails:
        """Trimmed values; empty text becomes None; IMEI digits only; REF upper-case."""
        health = self.battery_health
        if health is not None and (isinstance(health, bool) or not isinstance(health, int) or not 0 <= health <= 100):
            raise ValueError("Battery health must be a whole percentage from 0 to 100")
        imei = normalize_imei(self.imei) if self.imei else None
        if self.imei and imei is None:
            raise ValueError("IMEI must contain 14 to 17 digits")
        return UnitDetails(
            color=_clean(self.color, 60),
            battery_health=None if self.is_new else health,
            is_new=bool(self.is_new),
            imei=imei,
            reference=normalize_reference(self.reference),
            note=_clean(self.note, 500),
        )


# ------------------------------------------------------------------ parsing
def parse_battery(value: Any) -> tuple[bool, int | None, str | None]:
    """Read a battery cell: ``(is_new, health %, leftover text)``.

    ``*``/new → new phone; ``92``, ``92%`` or ``0.92`` (Excel percentage) →
    92 %; blank → unknown. Anything else is returned as leftover text so the
    caller can keep it as a note instead of losing it.
    """
    if is_blank(value) or isinstance(value, bool):
        return False, None, None
    if isinstance(value, (int, float)):
        return _health_from_number(float(value), str(value))
    text = western_digits(str(value)).strip()
    if text.casefold() in _NEW_WORDS:
        return True, None, None
    number_text = text.replace("%", "").replace("٪", "").replace(",", ".").strip()
    try:
        number = float(Decimal(number_text))
    except (InvalidOperation, ValueError):
        return False, None, text
    percent_sign = "%" in text or "٪" in text
    if percent_sign:
        return _health_from_number(number if number > 1 else number * 100, text)
    return _health_from_number(number, text)


def _health_from_number(number: float, original: str) -> tuple[bool, int | None, str | None]:
    if 0 < number <= 1:
        number *= 100
    if 0 <= number <= 100:
        return False, int(round(number)), None
    return False, None, original


def normalize_imei(value: Any) -> str | None:
    """Digits of an IMEI (14 to 17 digits, spaces/dashes allowed), else None."""
    if is_blank(value):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = western_digits(str(value)).strip()
    digits = re.sub(r"[\s\-/.]", "", text)
    return digits if digits.isdigit() and 14 <= len(digits) <= 17 else None


def normalize_reference(value: Any) -> str | None:
    """A trimmed, upper-case reference such as ``REF-00012``; None when blank."""
    if is_blank(value):
        return None
    return _clean(western_digits(str(value)).upper(), 40)


def _clean(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] or None


# ------------------------------------------------------------------ queries
def next_reference(session: Session, taken: set[str] | None = None) -> str:
    """The next free ``REF-00001`` style reference."""
    highest = 0
    for (reference,) in session.execute(select(StockItem.reference).where(StockItem.reference.is_not(None))):
        match = _REFERENCE_NUMBER.match(reference or "")
        if match:
            highest = max(highest, int(match.group(1)))
    for reference in taken or ():
        match = _REFERENCE_NUMBER.match(reference)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{REFERENCE_PREFIX}{highest + 1:05d}"


def list_units(
    session: Session,
    *,
    status: str | None = None,
    product_id: int | None = None,
    color: str | None = None,
    condition: str | None = None,
    search: str | None = None,
) -> list[StockItem]:
    """Stock units in the order they were entered (like the Excel sheet), with product and sale loaded.

    ``condition`` is ``new``, ``used``, ``unknown`` or ``low_battery`` (used,
    under 85 %). ``search`` matches the product name, IMEI, reference, colour
    and note.
    """
    query = (
        select(StockItem)
        .join(Product)
        .options(joinedload(StockItem.product), joinedload(StockItem.sale).joinedload(Sale.customer))
    )
    if status is not None:
        query = query.where(StockItem.status == status)
    if product_id is not None:
        query = query.where(StockItem.product_id == product_id)
    if color:
        query = query.where(func.lower(StockItem.color) == color.casefold())
    if condition == CONDITION_NEW:
        query = query.where(StockItem.is_new.is_(True))
    elif condition == CONDITION_USED:
        query = query.where(StockItem.is_new.is_(False), StockItem.battery_health.is_not(None))
    elif condition == CONDITION_UNKNOWN:
        query = query.where(StockItem.is_new.is_(False), StockItem.battery_health.is_(None))
    elif condition == CONDITION_LOW_BATTERY:
        query = query.where(StockItem.is_new.is_(False), StockItem.battery_health < LOW_BATTERY_BELOW)
    if search and search.strip():
        pattern = f"%{western_digits(search.strip()).casefold()}%"
        query = query.where(or_(
            func.lower(Product.name).like(pattern),
            func.lower(func.coalesce(StockItem.imei, "")).like(pattern),
            func.lower(func.coalesce(StockItem.reference, "")).like(pattern),
            func.lower(func.coalesce(StockItem.color, "")).like(pattern),
            func.lower(func.coalesce(StockItem.note, "")).like(pattern),
        ))
    return list(session.scalars(query.order_by(StockItem.id)).unique())


def available_units(session: Session, product_id: int) -> list[StockItem]:
    """Units of a product that can still be sold, oldest first."""
    return list(session.scalars(
        select(StockItem)
        .where(StockItem.product_id == product_id, StockItem.status == STATUS_AVAILABLE)
        .order_by(StockItem.id)
    ))


MAX_SALE_QUANTITY = 500


def units_to_sell(
    session: Session, *, product_id: int | None, quantity: int, first: StockItem | None = None
) -> list[StockItem]:
    """The stock units a sale of ``quantity`` takes, so the stock goes down by that many.

    ``first`` is the phone the user picked (or matched by IMEI/REF); the rest
    are the oldest available units of the same product. A product with no
    phone in stock sells without touching the stock, but asking for several
    phones when fewer are available is refused (``not_enough``).
    """
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_SALE_QUANTITY:
        raise ValueError(f"Quantity must be between 1 and {MAX_SALE_QUANTITY}")
    units = [first] if first is not None else []
    product_id = first.product_id if first is not None else product_id
    if product_id is not None and len(units) < quantity:
        others = [unit for unit in available_units(session, product_id) if first is None or unit.id != first.id]
        units.extend(others[: quantity - len(units)])
    if quantity > 1 and len(units) < quantity:
        raise StockError("not_enough", f"Only {len(units)} unit(s) of this product are in stock")
    return units


def find_available_unit(
    session: Session, *, imei: str | None = None, reference: str | None = None
) -> StockItem | None:
    """An unsold unit with this IMEI or reference."""
    clean_imei = normalize_imei(imei) if imei else None
    clean_reference = normalize_reference(reference)
    conditions = []
    if clean_imei:
        conditions.append(StockItem.imei == clean_imei)
    if clean_reference:
        conditions.append(StockItem.reference == clean_reference)
    if not conditions:
        return None
    return session.scalar(
        select(StockItem).where(StockItem.status == STATUS_AVAILABLE, or_(*conditions)).limit(1)
    )


def find_by_scan(session: Session, code: Any) -> StockItem | None:
    """The unsold phone a barcode scan (or a typed code) points at: its IMEI or its REF.

    Scanners type the code and press Enter; ``REF-12``, ``ref-00012`` and
    ``00012`` all find ``REF-00012``.
    """
    if is_blank(code):
        return None
    text = western_digits(str(code)).strip()
    unit = find_available_unit(session, imei=text if normalize_imei(text) else None, reference=text)
    if unit is not None:
        return unit
    number = re.sub(r"^REF-?", "", text.upper())
    if number.isdigit():
        return find_available_unit(session, reference=f"{REFERENCE_PREFIX}{int(number):05d}")
    return None


def scanned_imeis(text: str) -> tuple[list[str], list[str]]:
    """Split scanner input (one code per line) into ``(valid IMEIs, rejected lines)``.

    Repeated scans of the same phone count once.
    """
    valid: list[str] = []
    rejected: list[str] = []
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        imei = normalize_imei(line)
        if imei is None:
            rejected.append(line.strip())
        elif imei not in valid:
            valid.append(imei)
    return valid, rejected


def add_scanned_units(
    session: Session,
    owner_user_id: int,
    *,
    product_id: int,
    imeis: Iterable[str],
    details: UnitDetails = UnitDetails(),
    cash_price: int | None = None,
    wholesale_price: int | None = None,
) -> list[StockItem]:
    """Add one phone per scanned IMEI (same product, prices and details); all or none; owner only."""
    codes = list(dict.fromkeys(imeis))
    if not codes:
        raise ValueError("Scan at least one IMEI")
    if len(codes) > MAX_SALE_QUANTITY:
        raise ValueError(f"At most {MAX_SALE_QUANTITY} phones at once")
    created: list[StockItem] = []
    with session.begin_nested():
        for code in codes:
            unit_details = UnitDetails(
                color=details.color, battery_health=details.battery_health, is_new=details.is_new,
                imei=code, reference=None, note=details.note,
            )
            created.extend(add_units(
                session, owner_user_id, product_id=product_id, details=unit_details,
                cash_price=cash_price, wholesale_price=wholesale_price,
            ))
    return created


def stock_counts(session: Session) -> dict[int, tuple[int, int]]:
    """``{product_id: (available units, sold units)}``."""
    counts: dict[int, list[int]] = {}
    rows = session.execute(
        select(StockItem.product_id, StockItem.status, func.count(StockItem.id))
        .group_by(StockItem.product_id, StockItem.status)
    )
    for product_id, status, count in rows:
        pair = counts.setdefault(product_id, [0, 0])
        pair[0 if status == STATUS_AVAILABLE else 1] += int(count)
    return {product_id: (pair[0], pair[1]) for product_id, pair in counts.items()}


def colors(session: Session) -> list[str]:
    """Distinct colours in stock, alphabetically."""
    values = session.scalars(select(StockItem.color).where(StockItem.color.is_not(None)).distinct())
    unique = {value.strip(): None for value in values if value and value.strip()}
    return sorted(unique, key=str.casefold)


# ------------------------------------------------------------------ changes
def add_units(
    session: Session,
    owner_user_id: int,
    *,
    product_id: int,
    details: UnitDetails = UnitDetails(),
    cash_price: int | None = None,
    wholesale_price: int | None = None,
    quantity: int = 1,
) -> list[StockItem]:
    """Add ``quantity`` identical units (each gets its own reference); owner only.

    Prices default to the product's catalog prices. An IMEI or a typed
    reference identifies one phone, so they need ``quantity == 1``.
    """
    auth.require_owner(session, owner_user_id)
    product = session.get(Product, product_id)
    if product is None:
        raise ValueError("Catalog product not found")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 500:
        raise ValueError("Quantity must be between 1 and 500")
    clean = details.cleaned()
    if quantity > 1 and (clean.imei or clean.reference):
        raise ValueError("An IMEI or reference belongs to a single unit")
    cash = product.cash_price if cash_price is None else _amount(cash_price)
    wholesale = product.wholesale_price if wholesale_price is None else _amount(wholesale_price)
    created = [
        _new_unit(session, product, clean, cash, wholesale, taken=set())
        for _ in range(quantity)
    ]
    _flush(session)
    return created


def set_available_quantity(
    session: Session, owner_user_id: int, *, product_id: int, quantity: int
) -> tuple[int, int]:
    """Make the product's unsold phones number exactly ``quantity``; returns ``(added, removed)``; owner only.

    Missing phones are added at the catalog prices (each gets its own REF).
    Extra phones are removed newest first, preferring the ones with no IMEI,
    so a phone typed in with its details is the last to go. Sold phones are
    history and never touched.
    """
    auth.require_owner(session, owner_user_id)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 0 <= quantity <= MAX_SALE_QUANTITY:
        raise ValueError(f"Quantity must be between 0 and {MAX_SALE_QUANTITY}")
    if session.get(Product, product_id) is None:
        raise ValueError("Catalog product not found")
    units = available_units(session, product_id)
    if quantity > len(units):
        added = add_units(session, owner_user_id, product_id=product_id, quantity=quantity - len(units))
        return len(added), 0
    extra = sorted(units, key=lambda unit: (unit.imei is not None, -unit.id))[: len(units) - quantity]
    for unit in extra:
        session.delete(unit)
    session.flush()
    return 0, len(extra)


def _new_unit(
    session: Session, product: Product, details: UnitDetails, cash: int, wholesale: int, *, taken: set[str]
) -> StockItem:
    _check_unique(session, details)
    reference = details.reference or next_reference(session, taken)
    taken.add(reference)
    unit = StockItem(
        product_id=product.id,
        cash_price=cash,
        wholesale_price=wholesale,
        color=details.color,
        battery_health=details.battery_health,
        is_new=details.is_new,
        imei=details.imei,
        reference=reference,
        note=details.note,
        status=STATUS_AVAILABLE,
    )
    session.add(unit)
    session.flush()
    return unit


def update_unit(
    session: Session,
    owner_user_id: int,
    unit_id: int,
    *,
    details: UnitDetails,
    cash_price: int,
    wholesale_price: int,
    product_id: int | None = None,
) -> StockItem:
    """Edit a unit's prices and details; owner only."""
    auth.require_owner(session, owner_user_id)
    unit = session.get(StockItem, unit_id)
    if unit is None:
        raise ValueError("Stock unit not found")
    clean = details.cleaned()
    _check_unique(session, clean, ignore_id=unit.id)
    if product_id is not None and session.get(Product, product_id) is None:
        raise ValueError("Catalog product not found")
    unit.product_id = product_id or unit.product_id
    unit.cash_price = _amount(cash_price)
    unit.wholesale_price = _amount(wholesale_price)
    unit.color = clean.color
    unit.battery_health = clean.battery_health
    unit.is_new = clean.is_new
    unit.imei = clean.imei
    unit.reference = clean.reference or unit.reference or next_reference(session)
    unit.note = clean.note
    _flush(session)
    return unit


def delete_unit(session: Session, owner_user_id: int, unit_id: int) -> None:
    """Remove an unsold unit (entered by mistake); sold units are history."""
    auth.require_owner(session, owner_user_id)
    unit = session.get(StockItem, unit_id)
    if unit is None:
        raise ValueError("Stock unit not found")
    if unit.status == STATUS_SOLD:
        raise StockError("sold", "A sold unit cannot be deleted")
    session.delete(unit)
    session.flush()


def delete_units(session: Session, owner_user_id: int, unit_ids: Iterable[int]) -> tuple[int, int]:
    """Remove several unsold units; sold ones are history and stay.

    Returns ``(deleted, sold_kept)``.
    """
    auth.require_owner(session, owner_user_id)
    unique_ids = list(dict.fromkeys(int(unit_id) for unit_id in unit_ids))
    rows = session.scalars(select(StockItem).where(StockItem.id.in_(unique_ids))).all() if unique_ids else []
    deleted = 0
    for unit in rows:
        if unit.status == STATUS_SOLD:
            continue
        session.delete(unit)
        deleted += 1
    session.flush()
    return deleted, len(rows) - deleted


def mark_sold(session: Session, unit: StockItem, sale: Sale, sold_on: date | None = None) -> None:
    """Attach a unit to the sale that took it and copy its details onto the sale."""
    if unit.status == STATUS_SOLD and unit.sale_id not in (None, sale.id):
        raise StockError("sold", "This unit was already sold")
    unit.status = STATUS_SOLD
    unit.sale_id = sale.id
    unit.sold_at = sold_on or sale.purchase_date
    sale.product_id = unit.product_id
    sale.color = sale.color or unit.color
    if not (sale.is_new or sale.battery_health is not None):
        sale.is_new = unit.is_new
        sale.battery_health = unit.battery_health
    sale.imei = sale.imei or unit.imei
    sale.reference = sale.reference or unit.reference
    session.flush()


def _check_unique(session: Session, details: UnitDetails, *, ignore_id: int | None = None) -> None:
    for column, value, code in (
        (StockItem.imei, details.imei, "imei_taken"),
        (StockItem.reference, details.reference, "reference_taken"),
    ):
        if not value:
            continue
        query = select(StockItem.id).where(column == value)
        if ignore_id is not None:
            query = query.where(StockItem.id != ignore_id)
        if session.scalar(query.limit(1)) is not None:
            raise StockError(code, f"Another unit already uses {value}")


def _amount(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Prices must be non-negative whole numbers")
    return value


def _flush(session: Session) -> None:
    try:
        session.flush()
    except IntegrityError as error:
        raise StockError("reference_taken", "IMEI or reference already used") from error
