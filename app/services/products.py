"""Owner-managed product price book for sales entry.

A product needs only a name and a selling (cash) price; the wholesale price
is optional and stored as 0 when unknown. Each product also carries a default
installment plan (duration, months between payments, optional rate) that the
sale forms start from; every sale can still use its own plan.

Physical units (battery, colour, IMEI, REF) are in :mod:`app.services.stock`;
the Excel template, upload and export are in :mod:`app.services.product_sheet`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Product, Sale
from app.services import auth, calc


class ProductNameTakenError(ValueError):
    """Another catalog product already has this name."""


DEFAULT_PLAN_MONTHS = 6


@dataclass(frozen=True)
class ProductPlan:
    """A product's default installment plan.

    ``interval`` is the number of months between payments (1 = monthly).
    ``rate`` None means "use the shop's rate preset for this duration".
    """

    months: int = DEFAULT_PLAN_MONTHS
    interval: int = 1
    rate: int | None = None

    def resolved_rate(self, presets: dict[int, int]) -> int | None:
        """The product's own rate, else the preset for its duration, else None."""
        return self.rate if self.rate is not None else presets.get(self.months)

    def payment_count(self) -> int:
        """Number of payments in this plan."""
        return len(calc.payment_offsets(self.months, self.interval))


class CatalogEntry(NamedTuple):
    """What a sale form needs to offer one product with its default plan."""

    name: str
    cash_price: int
    wholesale_price: int
    months: int = DEFAULT_PLAN_MONTHS
    interval: int = 1
    rate: int | None = None


def plan_of(product: Product) -> ProductPlan:
    """The default plan stored on a product."""
    return ProductPlan(
        months=product.default_months or DEFAULT_PLAN_MONTHS,
        interval=product.payment_interval or 1,
        rate=product.default_rate,
    )


def catalog_entries(session: Session, presets: dict[int, int]) -> list[CatalogEntry]:
    """Active products with their default plan and the rate that plan resolves to."""
    entries = []
    for product in list_products(session):
        plan = plan_of(product)
        entries.append(CatalogEntry(
            product.name, product.cash_price, product.wholesale_price,
            plan.months, plan.interval, plan.resolved_rate(presets),
        ))
    return entries


def _validate_plan(plan: ProductPlan) -> ProductPlan:
    """Check duration, interval and optional rate of a default plan."""
    calc.validate_plan(plan.months, plan.interval)
    if plan.rate is not None and (
        isinstance(plan.rate, bool) or not isinstance(plan.rate, int) or not 0 <= plan.rate <= 100
    ):
        raise ValueError("Plan rate must be a whole number between 0 and 100")
    return plan


def list_products(session: Session, *, include_inactive: bool = False) -> list[Product]:
    """Return catalog items alphabetically, hiding archived entries by default."""
    query = select(Product)
    if not include_inactive:
        query = query.where(Product.active.is_(True))
    return list(session.scalars(query.order_by(Product.name, Product.id)))


def create_product(
    session: Session,
    owner_user_id: int,
    *,
    name: str,
    cash_price: int,
    wholesale_price: int = 0,
    plan: ProductPlan | None = None,
) -> Product:
    """Create a catalog product after verifying the active owner account.

    Name and selling price are required; the wholesale price defaults to 0
    and the plan to 6 months paid monthly at the preset rate.
    """
    auth.require_owner(session, owner_user_id)
    clean_name = _validate_name(name)
    _validate_amount("wholesale price", wholesale_price)
    _validate_price(cash_price)
    clean_plan = _validate_plan(plan or ProductPlan())
    product = Product(
        name=clean_name,
        wholesale_price=wholesale_price,
        cash_price=cash_price,
        default_months=clean_plan.months,
        payment_interval=clean_plan.interval,
        default_rate=clean_plan.rate,
        active=True,
    )
    session.add(product)
    try:
        session.flush()
    except IntegrityError as error:
        raise ProductNameTakenError("A catalog product already uses this name") from error
    return product


def update_product(
    session: Session,
    owner_user_id: int,
    product_id: int,
    *,
    name: str,
    cash_price: int,
    wholesale_price: int | None = None,
    plan: ProductPlan | None = None,
) -> Product:
    """Update the active catalog price while old sales keep their stored snapshot.

    ``wholesale_price=None`` and ``plan=None`` keep the stored values.
    """
    auth.require_owner(session, owner_user_id)
    product = session.get(Product, product_id)
    if product is None:
        raise ValueError("Catalog product not found")
    product.name = _validate_name(name)
    if wholesale_price is not None:
        product.wholesale_price = _validate_amount("wholesale price", wholesale_price)
    product.cash_price = _validate_price(cash_price)
    if plan is not None:
        _set_plan(product, _validate_plan(plan))
    try:
        session.flush()
    except IntegrityError as error:
        raise ProductNameTakenError("A catalog product already uses this name") from error
    return product


def _set_plan(product: Product, plan: ProductPlan) -> None:
    product.default_months = plan.months
    product.payment_interval = plan.interval
    product.default_rate = plan.rate


def set_product_active(
    session: Session,
    owner_user_id: int,
    product_id: int,
    active: bool,
) -> Product:
    """Activate or archive a catalog item without changing historical sales."""
    auth.require_owner(session, owner_user_id)
    if not isinstance(active, bool):
        raise ValueError("Product active state must be boolean")
    product = session.get(Product, product_id)
    if product is None:
        raise ValueError("Catalog product not found")
    product.active = active
    session.flush()
    return product


def find_active_product(session: Session, name: str) -> Product | None:
    """Find an active catalog entry by its trimmed exact product name."""
    clean_name = name.strip() if isinstance(name, str) else ""
    if not clean_name:
        return None
    return session.scalar(
        select(Product).where(Product.name == clean_name, Product.active.is_(True))
    )


def name_key(name: str) -> str:
    """Catalog name identity: case- and spacing-insensitive ("iPhone  13" == "IPHONE 13")."""
    return " ".join(str(name).split()).casefold()


def find_by_name(session: Session, name: str) -> Product | None:
    """The catalog product with this name (case and spacing ignored), preferring an active one."""
    key = name_key(name)
    if not key:
        return None
    matches = [
        product for product in session.scalars(
            select(Product).where(func.lower(Product.name) == key.lower()).order_by(Product.active.desc(), Product.id)
        )
    ]
    if not matches:
        # Spacing differences ("IP 8  PLUS") are not visible to SQL lower(); check in Python.
        matches = [product for product in session.scalars(select(Product)) if name_key(product.name) == key]
        matches.sort(key=lambda product: (not product.active, product.id))
    return matches[0] if matches else None


def find_or_create_product(
    session: Session,
    owner_user_id: int,
    name: str,
    *,
    cash_price: int,
    wholesale_price: int = 0,
    plan: ProductPlan | None = None,
) -> tuple[Product, bool]:
    """Link to the product with this name, or add it to the catalog; ``(product, created)``.

    This is how a sale or a stock entry "connects" a product: an existing
    name is reused (an archived product is restored), a new one is created
    with the given prices. Creating requires the owner.
    """
    product = find_by_name(session, name)
    if product is not None:
        if not product.active:
            auth.require_owner(session, owner_user_id)
            product.active = True
            session.flush()
        return product, False
    product = create_product(
        session, owner_user_id, name=name, cash_price=cash_price,
        wholesale_price=wholesale_price, plan=plan,
    )
    return product, True


def _validate_name(name: str) -> str:
    """Trim a non-empty product name to the database length limit."""
    if not isinstance(name, str):
        raise ValueError("Product name is required")
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Product name cannot be empty")
    if len(clean_name) > 180:
        raise ValueError("Product name cannot exceed 180 characters")
    return clean_name


def _validate_amount(label: str, value: int) -> int:
    """Validate whole-dinar prices and return the normalized number."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label.capitalize()} must be a non-negative whole number")
    return value


def _validate_price(value: int) -> int:
    """The selling price is required: a whole number of dinars above zero."""
    _validate_amount("cash price", value)
    if value == 0:
        raise ValueError("Cash price is required")
    return value


def sales_by_product(session: Session) -> dict[str, tuple[int, date | None]]:
    """Return ``{product name: (units sold, last sale date)}`` from recorded sales.

    Sales keep a copy of the product name, so renamed catalog entries only
    count sales made under their current name.
    """
    rows = session.execute(
        select(Sale.product, func.count(Sale.id), func.max(Sale.purchase_date)).group_by(Sale.product)
    )
    return {name: (int(count), last_date) for name, count, last_date in rows}


# ------------------------------------------------------------ Excel sheet
# Template, upload and export live in app/services/product_sheet.py; these
# names stay reachable here for existing callers (loaded lazily, PEP 562,
# because product_sheet itself imports this module).
_SHEET_NAMES = frozenset({
    "MAX_IMPORT_ROWS", "ProductWorkbookError", "ProductImportRow", "ProductImportPreview",
    "ProductImportResult", "write_product_template", "preview_product_workbook", "import_products",
    "export_stock_workbook",
    "ERROR_NAME_MISSING", "ERROR_NAME_TOO_LONG", "ERROR_NAME_INVALID", "ERROR_PRICE_MISSING",
    "ERROR_PRICE_INVALID", "ERROR_WHOLESALE_INVALID", "ERROR_DUPLICATE", "ERROR_MONTHS_INVALID",
    "ERROR_INTERVAL_INVALID", "ERROR_RATE_INVALID", "ERROR_QUANTITY_INVALID", "ERROR_IMEI_DUPLICATE",
    "WARNING_NO_WHOLESALE", "WARNING_BELOW_WHOLESALE", "WARNING_BATTERY_TEXT", "WARNING_STATUS_TEXT",
    "WARNING_REF_DUPLICATE",
    "ACTION_CREATE", "ACTION_UPDATE", "ACTION_RESTORE", "ACTION_UNCHANGED", "ACTION_EXISTING", "ACTION_INVALID",
    "UNIT_ADD", "UNIT_UPDATE", "UNIT_IN_STOCK", "UNIT_SOLD",
})


def __getattr__(name: str) -> Any:
    if name in _SHEET_NAMES:
        from app.services import product_sheet

        return getattr(product_sheet, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
