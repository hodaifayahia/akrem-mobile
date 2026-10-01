"""Sale creation, schedule generation, and safe sale editing workflows."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Customer, Installment, Payment, Sale, Setting, StockItem, User
from app.services import auth, customers as customer_service, products as product_service
from app.services import calc, repositories, schedule, stock

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
    "due_mode": "first_of_month",
    "grace_days": 5,
}


class PaidInstallmentEditError(ValueError):
    """Raised before a sale edit could change installments with payment history."""


def create_sale_for_user(
    session: Session,
    user_id: int,
    **sale_values: Any,
) -> Sale:
    """Create a sale with role checks and seller prices from the owner catalog."""
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
        if unit is not None:
            # A phone from stock is sold at its own price.
            sale_values["product"] = unit.product.name
            sale_values["product_id"] = unit.product_id
            sale_values["wholesale_price"] = unit.wholesale_price
            sale_values["cash_price"] = unit.cash_price
        else:
            catalog_product = product_service.find_active_product(
                session, str(sale_values.get("product", ""))
            ) or product_service.find_by_name(session, str(sale_values.get("product", "")))
            if catalog_product is None or not catalog_product.active:
                raise auth.AuthorizationError("A seller must choose an active catalog product")
            sale_values["product"] = catalog_product.name
            sale_values["product_id"] = catalog_product.id
            sale_values["wholesale_price"] = catalog_product.wholesale_price
            sale_values["cash_price"] = catalog_product.cash_price
    else:
        raise auth.AuthorizationError("Unsupported account role")
    return create_sale(session, **sale_values)


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
    result = calc.compute_sale(
        cash_price=values["cash_price"],
        rate=values["rate"],
        down_payment=values["down_payment"],
        months=values["months"],
        wholesale=values["wholesale_price"],
        sale_type=values["sale_type"],
        purchase_date=values["purchase_date"],
        interval=values["payment_interval"],
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
            **{field: values[field] for field in _DETAIL_FIELDS},
        )
        _insert_schedule(session, sale, resolved_settings)
        if unit is None and (values["imei"] or values["reference"]):
            unit = stock.find_available_unit(session, imei=values["imei"], reference=values["reference"])
        if unit is not None:
            stock.mark_sold(session, unit, sale)
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
