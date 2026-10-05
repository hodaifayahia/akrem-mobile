"""Sale creation, schedule generation, and safe sale editing workflows."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import AuditLog, Customer, Installment, Payment, Sale, Setting, StockItem, User
from app.services import auth, customers as customer_service, products as product_service
from app.services import calc, repositories, schedule, stock
from app.services import settings as settings_service
from app.services.payments import lock_payment_ledger

_SALE_FIELDS = {
    "customer_id",
    "product",
    "sale_type",
    "wholesale_price",
    "cash_price",
    "rate",
    "down_payment",
    "months",
    "payment_interval",
    "purchase_date",
    "expected_pay_date",
    "color",
    "battery_health",
    "is_new",
    "imei",
    "reference",
}
_DETAIL_FIELDS = ("color", "battery_health", "is_new", "imei", "reference")
_SCHEDULE_FIELDS = {
    "sale_type",
    "cash_price",
    "rate",
    "down_payment",
    "months",
    "payment_interval",
    "purchase_date",
}
_PRICING_FIELDS = {
    "sale_type",
    "cash_price",
    "rate",
    "down_payment",
    "months",
    "payment_interval",
}
_DEFAULT_SCHEDULE_SETTINGS: dict[str, object] = {
    "due_mode": schedule.DEFAULT_DUE_MODE,
    "grace_days": 5,
}


class PaidInstallmentEditError(ValueError):
    """Raised before a sale edit could change installments with payment history."""


class DiscountApprovalRequired(ValueError):
    """A seller's price is below the catalog by more than the allowed percentage.

    ``percent`` is the discount asked (rounded up to a whole percent) and
    ``limit`` the owner's setting; saving again with ``approved_by_owner_id``
    (the owner typed their password) records the sale and the approval.
    """

    def __init__(self, percent: int, limit: int) -> None:
        super().__init__(f"A {percent}% discount needs the owner's approval (limit {limit}%)")
        self.percent = percent
        self.limit = limit


def create_sale_for_user(
    session: Session,
    user_id: int,
    *,
    approved_by_owner_id: int | None = None,
    **sale_values: Any,
) -> Sale:
    """Create a sale with role checks; a seller's prices are checked against the owner catalog.

    A seller may lower the price (or, on installments, the rate) by up to the
    owner's ``seller_max_discount`` percent of the catalog total. A bigger
    discount raises ``DiscountApprovalRequired`` unless ``approved_by_owner_id``
    names an active owner, and the approval is written to the audit log.
    The wholesale price always comes from the phone or the catalog.
    """
    user = session.get(User, user_id)
    if user is None or user.disabled:
        raise auth.AuthorizationError("An active account is required")
    unit = _requested_unit(session, sale_values.get("stock_item_id"))
    if user.role == "owner":
        auth.require_owner(session, user_id)
        if unit is not None:
            sale_values["product"] = unit.product.name
            sale_values["product_id"] = unit.product_id
        elif sale_values.get("product_id") is None and str(sale_values.get("product", "")).strip():
            # "Connect" the product: reuse the catalog entry with this name or add it.
            catalog_product, _created = product_service.find_or_create_product(
                session, user_id, str(sale_values["product"]),
                cash_price=max(int(sale_values.get("cash_price") or 0), 1),
                wholesale_price=max(int(sale_values.get("wholesale_price") or 0), 0),
            )
            sale_values["product"] = catalog_product.name
            sale_values["product_id"] = catalog_product.id
    elif user.role == "seller":
        auth.require_seller(session, user_id)
        quantity = sale_values.get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("Quantity must be a whole number >= 1")
        if unit is not None:
            # A phone from stock is priced at its own price (times the quantity).
            sale_values["product"] = unit.product.name
            sale_values["product_id"] = unit.product_id
            catalog_product = unit.product
            unit_wholesale, unit_cash = unit.wholesale_price, unit.cash_price
        else:
            catalog_product = product_service.find_active_product(
                session, str(sale_values.get("product", ""))
            ) or product_service.find_by_name(session, str(sale_values.get("product", "")))
            if catalog_product is None or not catalog_product.active:
                raise auth.AuthorizationError("A seller must choose an active catalog product")
            sale_values["product"] = catalog_product.name
            sale_values["product_id"] = catalog_product.id
            unit_wholesale, unit_cash = catalog_product.wholesale_price, catalog_product.cash_price
        sale_values["wholesale_price"] = unit_wholesale * quantity
        catalog_price = unit_cash * quantity
        asked = sale_values.get("cash_price")
        if isinstance(asked, bool) or not isinstance(asked, int) or asked <= 0:
            asked = catalog_price
        sale_values["cash_price"] = asked
        percent = seller_discount_percent(session, catalog_product, catalog_price, sale_values)
        limit = settings_service.get_seller_max_discount(session)
        approver = None
        if percent > limit:
            approver = _approving_owner(session, approved_by_owner_id)
            if approver is None:
                raise DiscountApprovalRequired(percent, limit)
        sale = create_sale(session, **sale_values)
        if approver is not None:
            session.add(AuditLog(
                user_id=approver.id, action="discount_approved", entity="sale", entity_id=sale.id,
                details=json.dumps({
                    "seller_id": user.id, "percent": percent, "limit": limit,
                    "catalog_price": catalog_price, "cash_price": asked,
                }),
            ))
            session.flush()
        return sale
    else:
        raise auth.AuthorizationError("Unsupported account role")
    return create_sale(session, **sale_values)


def seller_discount_percent(
    session: Session, product: Any, catalog_price: int, values: Mapping[str, Any]
) -> int:
    """How far (whole percent, rounded up) a sale's total is below the catalog total; 0 when not below.

    For an installment the catalog total uses the product's own rate for its
    default duration, else the shop's preset for the chosen months; a duration
    without any rate compares the prices alone.
    """
    asked = int(values.get("cash_price") or 0)
    rate = int(values.get("rate") or 0)
    reference_rate = 0
    if values.get("sale_type") == "installment":
        months = values.get("months")
        plan = product_service.plan_of(product) if product is not None else None
        if plan is not None and plan.rate is not None and plan.months == months:
            reference_rate = plan.rate
        else:
            preset = settings_service.get_rate_presets(session).get(months) if months else None
            reference_rate = preset if preset is not None else rate
    reference = catalog_price * (100 + reference_rate)
    total = asked * (100 + rate)
    if reference <= 0 or total >= reference:
        return 0
    return -(-(reference - total) * 100 // reference)  # rounded up, whole integers only


def _approving_owner(session: Session, owner_id: int | None) -> User | None:
    """The active owner who approved a discount, if any."""
    if owner_id is None:
        return None
    owner = session.get(User, owner_id)
    if owner is None or owner.disabled or owner.role != "owner":
        return None
    return owner


def _requested_unit(session: Session, stock_item_id: Any) -> StockItem | None:
    """The stock unit a sale asks for, which must still be available."""
    if stock_item_id is None:
        return None
    unit = session.get(StockItem, stock_item_id)
    if unit is None:
        raise ValueError("Stock unit not found")
    if unit.status != stock.STATUS_AVAILABLE:
        raise stock.StockError("sold", "This unit was already sold")
    return unit


def create_customer_with_sale(
    session: Session,
    user_id: int,
    *,
    customer: Mapping[str, Any],
    sale: Mapping[str, Any],
    allow_duplicate_phone: bool = False,
) -> tuple[Customer, Sale]:
    """Create a new customer and their first sale together, or neither.

    ``customer`` holds ``customers.create_customer`` keyword arguments and
    ``sale`` holds ``create_sale`` keyword arguments without ``customer_id``.
    Sellers get catalog prices exactly as in ``create_sale_for_user``. Any
    failure, including ``DuplicateCustomerPhoneError``, rolls back the customer.
    """
    _require_sale_operator(session, user_id)
    customer_values = dict(customer)
    sale_values = dict(sale)
    if "customer_id" in sale_values:
        raise ValueError("Sale values cannot include a customer_id")
    if "allow_duplicate_phone" in customer_values:
        raise ValueError("Pass allow_duplicate_phone as its own argument")
    with session.begin_nested():
        new_customer = customer_service.create_customer(
            session, allow_duplicate_phone=allow_duplicate_phone, **customer_values
        )
        new_sale = create_sale_for_user(
            session, user_id, customer_id=new_customer.id, **sale_values
        )
    return new_customer, new_sale


MAX_BASKET_LINES = 50


def create_sales_batch(
    session: Session,
    user_id: int,
    *,
    customer_id: int,
    lines: Iterable[Mapping[str, Any]],
    approved_by_owner_id: int | None = None,
) -> list[Sale]:
    """Save several products bought together by one client: one sale per line, all or none.

    ``approved_by_owner_id`` is the owner's approval of a seller's bigger discount (every line).

    Each line holds ``create_sale_for_user`` keyword arguments without
    ``customer_id``, so every product keeps its own type, price, plan and
    stock unit. Two or more lines share a ``basket_id``, so the client list
    shows them on one row. Lines that picked a specific phone are saved
    first, so a line without a picked phone never takes a unit another line
    asked for.
    """
    _require_sale_operator(session, user_id)
    batch = [dict(line) for line in lines]
    if not batch:
        raise ValueError("A basket needs at least one product")
    if len(batch) > MAX_BASKET_LINES:
        raise ValueError(f"A basket holds at most {MAX_BASKET_LINES} products")
    if any("customer_id" in line for line in batch):
        raise ValueError("Basket lines cannot include a customer_id")
    picked = [line["stock_item_id"] for line in batch if line.get("stock_item_id") is not None]
    if len(picked) != len(set(picked)):
        raise stock.StockError("sold", "The same phone is in the basket twice")
    order = sorted(range(len(batch)), key=lambda index: batch[index].get("stock_item_id") is None)
    calc.share_basket_plan(batch)
    saved: dict[int, Sale] = {}
    with session.begin_nested():
        for index in order:
            saved[index] = create_sale_for_user(
                session, user_id, customer_id=customer_id, approved_by_owner_id=approved_by_owner_id, **batch[index]
            )
        if len(batch) > 1:
            basket_id = uuid.uuid4().hex
            for sale in saved.values():
                sale.basket_id = basket_id
            session.flush()
    return [saved[index] for index in range(len(batch))]


def _require_sale_operator(session: Session, user_id: int) -> User:
    """Return an active owner or seller allowed to record sales."""
    user = session.get(User, user_id)
    if user is None or user.disabled or user.role not in ("owner", "seller"):
        raise auth.AuthorizationError("An active account is required")
    return user


def update_sale_for_user(
    session: Session,
    user_id: int,
    sale_id: int,
    **changes: Any,
) -> Sale:
    """Update a sale after confirming that the active operator is an owner."""
    auth.require_owner(session, user_id)
    return update_sale(session, sale_id, **changes)


def create_sale(
    session: Session,
    *,
    customer_id: int,
    product: str,
    sale_type: str,
    wholesale_price: int,
    cash_price: int,
    rate: int = 0,
    down_payment: int = 0,
    months: int | None = None,
    purchase_date: date | None = None,
    expected_pay_date: date | None = None,
    settings: Mapping[str, object] | None = None,
    payment_interval: int = 1,
    product_id: int | None = None,
    stock_item_id: int | None = None,
    color: str | None = None,
    battery_health: int | None = None,
    is_new: bool = False,
    imei: str | None = None,
    reference: str | None = None,
    quantity: int = 1,
    total: int | None = None,
) -> Sale:
    """Calculate and save a sale with its schedule as one session transaction.

    The caller owns the outer session commit. A nested transaction keeps the sale
    and generated installment rows together if calculation or persistence fails.
    Cash and credit sales have no installment rows; ``expected_pay_date`` is only
    accepted for credit sales. ``payment_interval`` is the number of months
    between installment payments (1 = monthly) and must be 1 for cash/credit.

    The phone's details (colour, battery, IMEI, REF) are stored on the sale.
    ``stock_item_id`` sells that stock unit; otherwise an unsold unit with the
    same IMEI or REF is assigned automatically. Without ``product_id`` the
    sale is linked to the catalog product of the same name when one exists.

    ``quantity`` units leave the stock: the chosen unit first, then the
    oldest available units of the product (see ``stock.units_to_sell``). The
    prices are for the whole quantity. A typed IMEI/REF that matches no unit
    in stock describes a phone outside the stock, so no unit is taken then.
    ``total`` is the amount this sale carries of a basket total already
    calculated on the products bought together (see ``calc.share_basket_plan``).
    """
    if session.get(Customer, customer_id) is None:
        raise ValueError("Customer not found")
    purchase_date = purchase_date or date.today()
    values = _validated_values(
        customer_id=customer_id,
        product=product,
        sale_type=sale_type,
        wholesale_price=wholesale_price,
        cash_price=cash_price,
        rate=rate,
        down_payment=down_payment,
        months=months,
        payment_interval=payment_interval,
        purchase_date=purchase_date,
        expected_pay_date=expected_pay_date,
        color=color,
        battery_health=battery_health,
        is_new=is_new,
        imei=imei,
        reference=reference,
    )
    if product_id is None:
        linked = product_service.find_by_name(session, values["product"])
        product_id = linked.id if linked is not None else None
    unit = _requested_unit(session, stock_item_id)
    identified = bool(values["imei"] or values["reference"])
    if unit is None and identified:
        unit = stock.find_available_unit(session, imei=values["imei"], reference=values["reference"])
    units = (
        []
        if unit is None and identified
        else stock.units_to_sell(session, product_id=product_id, quantity=quantity, first=unit)
    )
    result = calc.compute_sale(
        cash_price=values["cash_price"],
        rate=values["rate"],
        down_payment=values["down_payment"],
        months=values["months"],
        wholesale=values["wholesale_price"],
        sale_type=values["sale_type"],
        purchase_date=values["purchase_date"],
        interval=values["payment_interval"],
        total_override=total,
    )
    resolved_settings = _schedule_settings(session, settings)
    with session.begin_nested():
        sale = repositories.create_sale(
            session,
            customer_id=values["customer_id"],
            product=values["product"],
            sale_type=values["sale_type"],
            wholesale_price=values["wholesale_price"],
            cash_price=values["cash_price"],
            rate=values["rate"],
            down_payment=values["down_payment"],
            months=values["months"],
            payment_interval=values["payment_interval"],
            total=result.total,
            financed=result.financed,
            monthly_amount=result.monthly_list[0] if result.monthly_list else None,
            profit=result.profit,
            purchase_date=values["purchase_date"],
            end_date=result.end_date,
            expected_pay_date=values["expected_pay_date"],
            product_id=product_id,
            quantity=quantity,
            **{field: values[field] for field in _DETAIL_FIELDS},
        )
        _insert_schedule(session, sale, resolved_settings)
        for taken in units:
            stock.mark_sold(session, taken, sale)
        session.flush()
    return sale


def update_sale(
    session: Session,
    sale_id: int,
    *,
    allow_paid_history_rewrite: bool = False,
    settings: Mapping[str, object] | None = None,
    **changes: Any,
) -> Sale:
    """Update a sale and regenerate only unpaid schedule rows.

    If changed calculation fields would rebuild a schedule while paid amounts or
    payment records exist, the default behavior raises ``PaidInstallmentEditError``.
    After the UI obtains confirmation it can retry with
    ``allow_paid_history_rewrite=True``; paid rows are still preserved verbatim.
    """
    sale = session.scalar(
        select(Sale)
        .where(Sale.id == sale_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if sale is None:
        raise ValueError("Sale not found")
    unsupported = sorted(set(changes) - _SALE_FIELDS)
    if unsupported:
        raise ValueError(f"Unsupported sale field: {unsupported[0]}")
    if not changes:
        return sale

    values = {
        "customer_id": sale.customer_id,
        "product": sale.product,
        "sale_type": sale.sale_type,
        "wholesale_price": sale.wholesale_price,
        "cash_price": sale.cash_price,
        "rate": sale.rate,
        "down_payment": sale.down_payment,
        "months": sale.months,
        "payment_interval": sale.payment_interval,
        "purchase_date": sale.purchase_date,
        "expected_pay_date": sale.expected_pay_date,
        **{field: getattr(sale, field) for field in _DETAIL_FIELDS},
    }
    values.update(changes)
    values = _validated_values(**values)
    if session.get(Customer, values["customer_id"]) is None:
        raise ValueError("Customer not found")
    schedule_changed = any(
        values[field] != getattr(sale, field) for field in _SCHEDULE_FIELDS
    )
    pricing_changed = any(
        values[field] != getattr(sale, field) for field in _PRICING_FIELDS
    )
    installment_rows = list(
        session.scalars(
            select(Installment)
            .where(Installment.sale_id == sale_id)
            .order_by(Installment.installment_index)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    paid_ids = _paid_installment_ids(session, installment_rows)
    has_payment_history = bool(paid_ids) or bool(sale.credit_payments)
    calculation_changed = pricing_changed or values["purchase_date"] != sale.purchase_date
    result = (
        calc.compute_sale(
            cash_price=values["cash_price"],
            rate=values["rate"],
            down_payment=values["down_payment"],
            months=values["months"],
            wholesale=values["wholesale_price"],
            sale_type=values["sale_type"],
            purchase_date=values["purchase_date"],
            interval=values["payment_interval"],
        )
        if calculation_changed
        else None
    )
    if pricing_changed and values["sale_type"] == "credit":
        assert result is not None
        credit_paid = sum(payment.amount for payment in sale.credit_payments)
        if result.financed < credit_paid:
            raise ValueError(
                "The new credit balance cannot be lower than payments already recorded"
            )
    if values["sale_type"] != sale.sale_type and has_payment_history:
        raise ValueError("A sale type cannot change after a payment has been recorded")
    if schedule_changed and paid_ids and not allow_paid_history_rewrite:
        raise PaidInstallmentEditError(
            "This sale has payment history. Confirm the edit to regenerate unpaid "
            "installments; recorded payments will be preserved."
        )

    if pricing_changed:
        assert result is not None
        total = result.total
        financed = result.financed
        monthly_amount = result.monthly_list[0] if result.monthly_list else None
        profit = result.profit
    else:
        # Imported legacy rows may intentionally retain totals from a paper
        # ledger. A product/date/expected-date edit must not rewrite those values.
        total = sale.total
        financed = sale.financed
        monthly_amount = sale.monthly_amount
        profit = (
            sale.profit
            if values["wholesale_price"] == sale.wholesale_price
            else sale.total - values["wholesale_price"]
        )
    end_date = result.end_date if calculation_changed and result is not None else sale.end_date
    resolved_settings = _schedule_settings(session, settings)
    with session.begin_nested():
        repositories.update_sale(
            session,
            sale_id,
            **values,
            total=total,
            financed=financed,
            monthly_amount=monthly_amount,
            profit=profit,
            end_date=end_date,
        )
        if schedule_changed:
            for row in installment_rows:
                if row.id not in paid_ids:
                    session.delete(row)
            session.flush()
            _insert_schedule(session, sale, resolved_settings, skip_indexes={
                row.installment_index for row in installment_rows if row.id in paid_ids
            }, preserved_amount=sum(
                row.amount_due for row in installment_rows if row.id in paid_ids
            ))
        session.flush()
        if schedule_changed and sale.sale_type == "installment":
            first_row = session.scalar(
                select(Installment)
                .where(Installment.sale_id == sale_id)
                .order_by(Installment.installment_index)
                .limit(1)
            )
            sale.monthly_amount = first_row.amount_due if first_row is not None else None
            session.flush()
        session.expire(sale, ["installments"])
    return sale


def delete_sales(session: Session, owner_user_id: int, sale_ids: Iterable[int]) -> int:
    """Delete sales with their installments and payments; returns how many were removed.

    Each sale is written to the audit log first (client, product, total and
    what had been paid). A phone that left the stock with the sale goes back
    to stock, since the sale no longer exists.
    """
    owner = auth.require_owner(session, owner_user_id)
    unique_ids = list(dict.fromkeys(int(sale_id) for sale_id in sale_ids))
    if not unique_ids:
        return 0
    lock_payment_ledger(session)
    rows = session.scalars(
        select(Sale)
        .where(Sale.id.in_(unique_ids))
        .options(
            selectinload(Sale.customer),
            selectinload(Sale.installments).selectinload(Installment.payments),
            selectinload(Sale.credit_payments),
            selectinload(Sale.stock_items),
        )
    ).all()
    for sale in rows:
        session.add(AuditLog(
            user_id=owner.id,
            action="delete_sale",
            entity="sale",
            entity_id=sale.id,
            details=json.dumps(_sale_audit_details(sale), ensure_ascii=False, sort_keys=True),
        ))
        for unit in list(sale.stock_items):
            unit.status = stock.STATUS_AVAILABLE
            unit.sale = None
            unit.sold_at = None
        session.delete(sale)
    session.flush()
    return len(rows)


def _sale_audit_details(sale: Sale) -> dict[str, Any]:
    """What the audit log keeps about a deleted sale."""
    paid = sum(payment.amount for row in sale.installments for payment in row.payments)
    paid += sum(payment.amount for payment in sale.credit_payments)
    return {
        "customer_id": sale.customer_id,
        "customer": sale.customer.full_name,
        "product": sale.product,
        "sale_type": sale.sale_type,
        "purchase_date": sale.purchase_date.isoformat(),
        "total": sale.total,
        "down_payment": sale.down_payment,
        "payments_total": paid,
    }


def _validated_values(
    *,
    customer_id: int,
    product: str,
    sale_type: str,
    wholesale_price: int,
    cash_price: int,
    rate: int,
    down_payment: int,
    months: int | None,
    purchase_date: date,
    expected_pay_date: date | None,
    payment_interval: int = 1,
    color: str | None = None,
    battery_health: int | None = None,
    is_new: bool = False,
    imei: str | None = None,
    reference: str | None = None,
) -> dict[str, Any]:
    """Normalize form values and reject invalid sale type/date combinations."""
    clean_product = product.strip()
    if not clean_product:
        raise ValueError("Product cannot be empty")
    if sale_type not in {"cash", "installment", "credit"}:
        raise ValueError("Sale type must be cash, installment, or credit")
    if sale_type != "credit" and expected_pay_date is not None:
        raise ValueError("Expected payment date is only available for credit sales")
    if sale_type == "cash" and (rate != 0 or down_payment != 0 or months is not None):
        raise ValueError("Cash sales cannot have a rate, down payment, or installment months")
    if sale_type == "credit" and months is not None:
        raise ValueError("Credit sales do not have installment months")
    if sale_type != "installment":
        payment_interval = 1  # only installment plans have a payment rhythm
    elif isinstance(payment_interval, bool) or not isinstance(payment_interval, int):
        raise ValueError("Payment interval must be a whole number of months")
    if sale_type == "installment":
        if months is None:
            raise ValueError("Installment sales require months")
        calc.validate_plan(months, payment_interval)
    if not isinstance(purchase_date, date):
        raise ValueError("Purchase date is required")
    for field, value in (
        ("Wholesale price", wholesale_price),
        ("Cash price", cash_price),
        ("Rate", rate),
        ("Down payment", down_payment),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field} must be a whole number")
    return {
        "customer_id": customer_id,
        "product": clean_product,
        "sale_type": sale_type,
        "wholesale_price": wholesale_price,
        "cash_price": cash_price,
        "rate": rate,
        "down_payment": down_payment,
        "months": months,
        "payment_interval": payment_interval,
        "purchase_date": purchase_date,
        "expected_pay_date": expected_pay_date,
        **_detail_values(color, battery_health, is_new, imei, reference),
    }


def _detail_values(
    color: str | None, battery_health: int | None, is_new: bool, imei: str | None, reference: str | None
) -> dict[str, Any]:
    """Validated phone details for a sale (IMEI digits, battery 0-100, trimmed text)."""
    details = stock.UnitDetails(
        color=color, battery_health=battery_health, is_new=bool(is_new), imei=imei, reference=reference,
    ).cleaned()
    return {
        "color": details.color,
        "battery_health": details.battery_health,
        "is_new": details.is_new,
        "imei": details.imei,
        "reference": details.reference,
    }


def _schedule_settings(
    session: Session,
    supplied: Mapping[str, object] | None,
) -> dict[str, object]:
    """Decode persisted settings and apply caller overrides/defaults."""
    resolved = dict(_DEFAULT_SCHEDULE_SETTINGS)
    for row in session.scalars(select(Setting)):
        try:
            resolved[row.key] = json.loads(row.value)
        except (TypeError, json.JSONDecodeError):
            resolved[row.key] = row.value
    if supplied is not None:
        resolved.update(supplied)
    return resolved


def _insert_schedule(
    session: Session,
    sale: Sale,
    settings: Mapping[str, object],
    *,
    skip_indexes: set[int] | None = None,
    preserved_amount: int = 0,
) -> None:
    """Add generated schedule rows, leaving selected historical indexes alone."""
    skipped = skip_indexes or set()
    drafts = [
        draft for draft in schedule.build(sale, settings)
        if draft.installment_index not in skipped
    ]
    if skipped:
        remaining_financed = sale.financed - preserved_amount
        if remaining_financed < 0:
            raise ValueError(
                "New sale total is below the amount already committed to preserved installments"
            )
        if not drafts and remaining_financed:
            raise ValueError("No unpaid installment months remain for the new sale balance")
        if drafts:
            monthly, remainder = divmod(remaining_financed, len(drafts))
            amounts = [monthly] * len(drafts)
            amounts[-1] += remainder
        else:
            amounts = []
    else:
        amounts = [draft.amount_due for draft in drafts]
    for draft, amount in zip(drafts, amounts, strict=True):
        session.add(
            Installment(
                sale_id=sale.id,
                installment_index=draft.installment_index,
                due_date=draft.due_date,
                amount_due=amount,
            )
        )


def _paid_installment_ids(session: Session, rows: list[Installment]) -> set[int]:
    """Return installment IDs with either a paid balance or payment history."""
    if not rows:
        return set()
    row_ids = [row.id for row in rows]
    payment_ids = set(
        session.scalars(
            select(Payment.installment_id).where(Payment.installment_id.in_(row_ids))
        )
    )
    return {
        row.id for row in rows if row.amount_paid > 0 or row.id in payment_ids
    }
