"""Customer workflows and validation."""

from __future__ import annotations

import re
import json
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db.models import Category, Customer, Installment, Payment, Sale, Setting
from app.services import repositories
from app.services.settings import get_grace_days
from app.services.status import Status, for_customer, for_month

_MOBILE_RE = re.compile(r"^0[567][0-9]{8}$")
PAYMENT_KINDS = ("cash", "installment", "credit")
PAYMENT_FILTERS = ("cash", "facilities", "installment", "credit")
BALANCE_FILTERS = ("open", "settled")
_CUSTOMER_FIELDS = {
    "full_name",
    "category_id",
    "phone",
    "id_number",
    "id_issue_date",
    "id_issue_place",
    "address",
    "profession",
    "ccp_number",
    "cheques_count",
    "notes",
}


@dataclass(frozen=True, slots=True)
class CustomerSummary:
    """Customer-list row with sale, status, and balance summaries."""

    id: int
    full_name: str
    phone: str | None
    category_id: int
    category_name: str
    products: tuple[str, ...]
    purchase_dates: tuple[date, ...]
    end_dates: tuple[date | None, ...]
    monthly_amount: int
    remaining_balance: int
    status: Status
    customer: Customer
    sales: tuple[Sale, ...]

    @property
    def purchase_date(self) -> date | None:
        """Return the latest purchase date, useful for a single table cell."""
        return max(self.purchase_dates, default=None)

    @property
    def end_date(self) -> date | None:
        """Return the latest known installment end date."""
        return max((value for value in self.end_dates if value is not None), default=None)

    @property
    def sale_types(self) -> tuple[str, ...]:
        """Distinct sale types of this customer, in cash/installment/credit order."""
        present = {sale.sale_type for sale in self.sales}
        return tuple(kind for kind in PAYMENT_KINDS if kind in present)

    @property
    def payment_profile(self) -> str:
        """``"cash"`` when every purchase was paid in full, ``"facilities"`` when any
        purchase is paid over time (installment or credit), ``"none"`` without sales."""
        return _payment_profile(self.sale_types)

    @property
    def credit_score(self) -> int:
        """Calculate a 0-100 credit reliability index based on payment punctuality and defaults."""
        if not self.sales:
            return 100
        if self.status == Status.FAILED:
            return 35
        if self.status == Status.PENDING:
            return 80
        if self.status == Status.PAID:
            return 98
        return 90

    @property
    def credit_badge(self) -> str:
        """Human-readable executive credit reliability rating."""
        score = self.credit_score
        if score >= 90:
            return "⭐⭐⭐⭐⭐ ممتاز"
        elif score >= 75:
            return "⭐⭐⭐⭐ جيد جداً"
        elif score >= 50:
            return "⭐⭐⭐ متوسط"
        return "⚠️ عالي المخاطر"


def _payment_profile(sale_types: tuple[str, ...] | set[str]) -> str:
    """Classify sale types as ``"none"``, all-``"cash"``, or ``"facilities"``."""
    kinds = set(sale_types)
    if not kinds:
        return "none"
    return "cash" if kinds == {"cash"} else "facilities"


@dataclass(frozen=True, slots=True)
class PurchaseRecord:
    """One sale in a customer's purchase history, with paid and remaining amounts."""

    sale_id: int
    purchase_date: date
    product: str
    sale_type: str
    cash_price: int
    wholesale_price: int
    rate: int
    total: int
    down_payment: int
    months: int | None
    monthly_amount: int | None
    end_date: date | None
    expected_pay_date: date | None
    paid: int
    remaining: int
    profit: int
    status: Status
    overdue_installments: int
    payment_interval: int = 1
    payment_count: int = 0


@dataclass(frozen=True, slots=True)
class PaymentRecord:
    """One received payment (installment or credit) in a customer's history."""

    payment_id: int
    payment_date: date
    amount: int
    method: str
    sale_id: int
    product: str
    sale_type: str
    installment_index: int | None
    note: str | None


@dataclass(frozen=True, slots=True)
class CustomerHistory:
    """All purchases and payments of one customer, newest first, with totals."""

    customer_id: int
    full_name: str
    purchases: tuple[PurchaseRecord, ...]
    payments: tuple[PaymentRecord, ...]
    purchase_count: int
    total_bought: int
    total_paid: int
    remaining: int
    cash_purchases: int
    installment_purchases: int
    credit_purchases: int
    first_purchase: date | None
    last_purchase: date | None
    last_payment: date | None
    overdue_installments: int

    @property
    def payment_profile(self) -> str:
        """``"cash"``, ``"facilities"``, or ``"none"``, as for ``CustomerSummary``."""
        return _payment_profile({purchase.sale_type for purchase in self.purchases})


def customer_history(session: Session, customer_id: int, *, today: date) -> CustomerHistory:
    """Build a customer's purchase and payment history in three eager queries.

    Paid amounts follow the ledger rules: cash sales are fully paid; installment
    sales count the down payment plus each installment's ``max(amount_paid,
    sum(payments))``; credit sales count the down payment plus credit payments.
    Each purchase status is evaluated for ``today``'s month with the configured
    grace days.
    """
    customer = session.scalar(
        select(Customer)
        .where(Customer.id == customer_id)
        .options(
            selectinload(Customer.sales).selectinload(Sale.installments).selectinload(
                Installment.payments
            ),
            selectinload(Customer.sales).selectinload(Sale.credit_payments),
        )
        # Refresh collections already loaded in this session so new sales/payments show.
        .execution_options(populate_existing=True)
    )
    if customer is None:
        raise ValueError("Customer not found")
    grace_days = get_grace_days(session)
    ordered_sales = sorted(
        customer.sales, key=lambda sale: (sale.purchase_date, sale.id), reverse=True
    )
    purchases = tuple(_purchase_record(sale, today, grace_days) for sale in ordered_sales)
    payments = tuple(sorted(
        (record for sale in ordered_sales for record in _payment_records(sale)),
        key=lambda record: (record.payment_date, record.payment_id),
        reverse=True,
    ))
    purchase_dates = [purchase.purchase_date for purchase in purchases]
    return CustomerHistory(
        customer_id=customer.id,
        full_name=customer.full_name,
        purchases=purchases,
        payments=payments,
        purchase_count=len(purchases),
        total_bought=sum(purchase.total for purchase in purchases),
        total_paid=sum(purchase.paid for purchase in purchases),
        remaining=sum(purchase.remaining for purchase in purchases),
        cash_purchases=sum(1 for purchase in purchases if purchase.sale_type == "cash"),
        installment_purchases=sum(
            1 for purchase in purchases if purchase.sale_type == "installment"
        ),
        credit_purchases=sum(1 for purchase in purchases if purchase.sale_type == "credit"),
        first_purchase=min(purchase_dates, default=None),
        last_purchase=max(purchase_dates, default=None),
        last_payment=payments[0].payment_date if payments else None,
        overdue_installments=sum(purchase.overdue_installments for purchase in purchases),
    )


def _installment_paid(installment: Installment) -> int:
    """Return an installment's paid amount using the ledger's max rule."""
    return max(installment.amount_paid, sum(payment.amount for payment in installment.payments))


def _sale_paid(sale: Sale) -> int:
    """Return everything received for a sale, including any down payment."""
    if sale.sale_type == "cash":
        return sale.total
    if sale.sale_type == "credit":
        return sale.down_payment + sum(payment.amount for payment in sale.credit_payments)
    return sale.down_payment + sum(_installment_paid(row) for row in sale.installments)


def _overdue_count(sale: Sale, today: date, grace_days: int) -> int:
    """Count unpaid installments whose due date plus grace days has passed."""
    cutoff = today - timedelta(days=grace_days)
    return sum(
        1
        for row in sale.installments
        if row.due_date < cutoff and _installment_paid(row) < row.amount_due
    )


def _purchase_record(sale: Sale, today: date, grace_days: int) -> PurchaseRecord:
    """Summarize one eagerly loaded sale for the history view."""
    paid = _sale_paid(sale)
    return PurchaseRecord(
        sale_id=sale.id,
        purchase_date=sale.purchase_date,
        product=sale.product,
        sale_type=sale.sale_type,
        cash_price=sale.cash_price,
        wholesale_price=sale.wholesale_price,
        rate=sale.rate,
        total=sale.total,
        down_payment=sale.down_payment,
        months=sale.months,
        monthly_amount=sale.monthly_amount,
        end_date=sale.end_date,
        expected_pay_date=sale.expected_pay_date,
        paid=paid,
        remaining=max(0, sale.total - paid),
        profit=sale.profit,
        status=for_month(sale.installments, today.year, today.month, today, grace_days),
        overdue_installments=_overdue_count(sale, today, grace_days),
        payment_interval=sale.payment_interval or 1,
        payment_count=len(sale.installments),
    )


def _payment_records(sale: Sale) -> list[PaymentRecord]:
    """List a sale's installment and credit payments as history rows."""
    rows: list[tuple[Payment, int | None]] = [
        (payment, installment.installment_index)
        for installment in sale.installments
        for payment in installment.payments
    ]
    rows.extend((payment, None) for payment in sale.credit_payments)
    return [
        PaymentRecord(
            payment_id=payment.id,
            payment_date=payment.payment_date,
            amount=payment.amount,
            method=payment.method,
            sale_id=sale.id,
            product=sale.product,
            sale_type=sale.sale_type,
            installment_index=index,
            note=payment.note,
        )
        for payment, index in rows
    ]


def normalize_search_text(value: str | None) -> str:
    """Normalize Arabic text for search, ignoring diacritics and common variants.

    Hamza-bearing alifs and standalone hamza become alif, taa marbuta becomes
    haa, and Arabic-Indic digits become Western digits. NFKD also reduces
    hamza-on-waw/yaa to their base letter before combining marks are removed.
    """
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKD", value).casefold()
    result: list[str] = []
    for char in normalized:
        if unicodedata.category(char).startswith("M") or char == "ـ":
            continue
        if char in "أإآٱء":
            result.append("ا")
        elif char == "ة":
            result.append("ه")
        elif char.isdecimal():
            try:
                result.append(str(unicodedata.digit(char)))
            except (TypeError, ValueError):
                result.append(char)
        else:
            result.append(char)
    return "".join(result).strip()


def list_customers(
    session: Session,
    *,
    year: int,
    month: int,
    today: date,
    category_id: int | None = None,
    status: Status | str | None = None,
    search: str | None = None,
    payment_kind: str | None = None,
    balance: str | None = None,
) -> list[CustomerSummary]:
    """Return customer rows with selected-month status and outstanding balances.

    ``payment_kind``: ``"cash"`` keeps customers who paid every purchase in
    full; ``"facilities"`` keeps customers paying at least one purchase over
    time; ``"installment"`` / ``"credit"`` keep customers with such a sale. ``balance`` keeps customers who still
    owe money (``"open"``) or have nothing left to pay (``"settled"``).
    The search text matches names, phone numbers and product names.

    Customer names and phone numbers are narrowed in a lightweight first query
    when searching. The final query eagerly loads categories, sales, schedules,
    and payment rows, avoiding per-customer database queries for large lists.
    """
    selected_status = _coerce_customer_status(status)
    if payment_kind not in (None, "", *PAYMENT_FILTERS):
        raise ValueError(f"Unsupported payment kind: {payment_kind}")
    if balance not in (None, "", *BALANCE_FILTERS):
        raise ValueError(f"Unsupported balance filter: {balance}")
    search_key = normalize_search_text(search)
    grace_days = _grace_days(session)
    # Validate the selected period even when the query has no matching rows.
    for_customer((), year, month, today, grace_days=grace_days)

    base_query = select(Customer)
    if category_id is not None:
        base_query = base_query.where(Customer.category_id == category_id)

    matching_ids: list[int] | None = None
    if search_key:
        candidates_query = select(Customer.id, Customer.full_name, Customer.phone)
        if category_id is not None:
            candidates_query = candidates_query.where(Customer.category_id == category_id)
        matching = {
            customer_id
            for customer_id, full_name, phone in session.execute(candidates_query)
            if search_key in normalize_search_text(full_name)
            or search_key in normalize_search_text(phone)
        }
        product_query = select(Sale.customer_id, Sale.product)
        if category_id is not None:
            product_query = product_query.join(Customer).where(Customer.category_id == category_id)
        matching.update(
            customer_id
            for customer_id, product in session.execute(product_query)
            if search_key in normalize_search_text(product)
        )
        matching_ids = sorted(matching)
        if not matching_ids:
            return []

    eager_options = (
        joinedload(Customer.category),
        selectinload(Customer.sales).selectinload(Sale.installments).selectinload(
            Installment.payments
        ),
        selectinload(Customer.sales).selectinload(Sale.credit_payments),
    )
    if matching_ids is not None and len(matching_ids) > 900:
        # Keep the customer ID filter under older SQLite bind-parameter limits.
        rows = []
        for offset in range(0, len(matching_ids), 900):
            query = (
                select(Customer)
                .where(Customer.id.in_(matching_ids[offset : offset + 900]))
                .options(*eager_options)
                .order_by(Customer.full_name, Customer.id)
            )
            rows.extend(session.scalars(query).all())
        rows.sort(key=lambda customer: (customer.full_name, customer.id))
    else:
        if matching_ids is not None:
            base_query = base_query.where(Customer.id.in_(matching_ids))
        rows = session.scalars(
            base_query.options(*eager_options).order_by(Customer.full_name, Customer.id)
        ).all()
    result: list[CustomerSummary] = []
    for customer in rows:
        customer_sales = tuple(sorted(
            customer.sales,
            key=lambda sale: (sale.purchase_date, sale.id),
        ))
        customer_status = for_customer(
            customer_sales, year, month, today, grace_days=grace_days
        )
        if selected_status is not None and customer_status != selected_status:
            continue
        if payment_kind and not _matches_payment_kind(customer_sales, payment_kind):
            continue

        monthly_amount = sum(
            installment.amount_due
            for sale in customer_sales
            for installment in sale.installments
            if installment.due_date.year == year and installment.due_date.month == month
        )
        remaining_balance = 0
        for sale in customer_sales:
            if sale.sale_type == "credit":
                paid = sum(payment.amount for payment in sale.credit_payments)
                remaining_balance += max(0, sale.financed - paid)
            elif sale.sale_type == "installment":
                for installment in sale.installments:
                    paid = max(
                        installment.amount_paid,
                        sum(payment.amount for payment in installment.payments),
                    )
                    remaining_balance += max(0, installment.amount_due - paid)

        if balance == "open" and remaining_balance <= 0:
            continue
        if balance == "settled" and remaining_balance > 0:
            continue

        summary = CustomerSummary(
            id=customer.id,
            full_name=customer.full_name,
            phone=customer.phone,
            category_id=customer.category_id,
            category_name=customer.category.name,
            products=tuple(sale.product for sale in customer_sales),
            purchase_dates=tuple(sale.purchase_date for sale in customer_sales),
            end_dates=tuple(sale.end_date for sale in customer_sales),
            monthly_amount=monthly_amount,
            remaining_balance=remaining_balance,
            status=customer_status,
            customer=customer,
            sales=customer_sales,
        )
        result.append(summary)
    return result


def _matches_payment_kind(sales: tuple[Sale, ...], payment_kind: str) -> bool:
    kinds = {sale.sale_type for sale in sales}
    if payment_kind == "cash":
        return kinds == {"cash"}
    if payment_kind == "facilities":
        return bool(kinds & {"installment", "credit"})
    return payment_kind in kinds


def get_customer_details(session: Session, customer_id: int) -> Customer:
    """Load a customer and all sale/payment relationships for its detail view."""
    customer = session.scalar(
        select(Customer)
        .where(Customer.id == customer_id)
        .options(
            joinedload(Customer.category),
            selectinload(Customer.sales).selectinload(Sale.installments).selectinload(
                Installment.payments
            ),
            selectinload(Customer.sales).selectinload(Sale.credit_payments),
        )
    )
    if customer is None:
        raise ValueError("Customer not found")
    return customer


def count_overdue_installments(
    session: Session,
    customer_id: int,
    *,
    today: date,
) -> int:
    """Count unpaid installment months past the configured grace period."""
    if session.get(Customer, customer_id) is None:
        raise ValueError("Customer not found")
    cutoff = today - timedelta(days=_grace_days(session))
    rows = session.scalars(
        select(Installment)
        .join(Sale, Sale.id == Installment.sale_id)
        .options(selectinload(Installment.payments))
        .where(
            Sale.customer_id == customer_id,
            Installment.due_date < cutoff,
        )
    ).all()
    return sum(
        1
        for installment in rows
        if max(
            installment.amount_paid,
            sum(payment.amount for payment in installment.payments),
        ) < installment.amount_due
    )


def _coerce_customer_status(status: Status | str | None) -> Status | None:
    """Normalize an optional status filter and reject unknown values."""
    if status is None or status == "":
        return None
    if isinstance(status, Status):
        return status
    try:
        return Status(status.upper())
    except (AttributeError, ValueError) as error:
        raise ValueError(f"Unsupported customer status: {status}") from error


def _grace_days(session: Session) -> int:
    """Read the configured grace period, using the business default of five days."""
    setting = session.get(Setting, "grace_days")
    if setting is None:
        return 5
    try:
        value = json.loads(setting.value)
    except (TypeError, json.JSONDecodeError):
        return 5
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 5
    return value


class DuplicateCustomerPhoneError(ValueError):
    """Raised when a phone is already used and explicit confirmation is needed."""

    def __init__(self, phone: str, matches: list[Customer]) -> None:
        self.phone = phone
        self.matches = tuple(matches)
        names = ", ".join(customer.full_name for customer in matches)
        super().__init__(f"Phone number {phone} is already used by: {names}")


def normalize_phone(phone: str | None) -> str | None:
    """Return a canonical local mobile number, or ``None`` for an empty value.

    Algerian mobile numbers are stored as 10 digits beginning with 05, 06, or 07.
    Arabic-Indic digits are converted to Western digits before validation.
    """
    if phone is None:
        return None
    clean_phone = "".join(
        str(unicodedata.digit(char)) if char.isdecimal() else char for char in phone
    ).strip()
    if not clean_phone:
        return None
    if not _MOBILE_RE.fullmatch(clean_phone):
        raise ValueError("Phone must be an Algerian mobile number: 10 digits starting 05, 06, or 07")
    return clean_phone


def find_duplicate_phones(
    session: Session,
    phone: str | None,
    *,
    exclude_customer_id: int | None = None,
) -> list[Customer]:
    """Find customers already using a valid mobile number.

    Use this before saving to display a non-blocking duplicate warning. Create and
    update workflows repeat the check and require ``allow_duplicate_phone=True``
    before accepting a duplicate, avoiding a race between warning and save.
    """
    clean_phone = normalize_phone(phone)
    if clean_phone is None:
        return []
    query = select(Customer).where(Customer.phone == clean_phone).order_by(Customer.id)
    if exclude_customer_id is not None:
        query = query.where(Customer.id != exclude_customer_id)
    return list(session.scalars(query))


def create_customer(
    session: Session,
    *,
    full_name: str,
    category_id: int,
    phone: str | None = None,
    allow_duplicate_phone: bool = False,
    **details: Any,
) -> Customer:
    """Validate and create a customer; duplicate phone use needs confirmation."""
    clean_name = full_name.strip()
    if not clean_name:
        raise ValueError("Customer name cannot be empty")
    if session.get(Category, category_id) is None:
        raise ValueError("Customer category not found")
    _validate_fields(details)
    clean_phone = normalize_phone(phone)
    if clean_phone and not allow_duplicate_phone:
        matches = find_duplicate_phones(session, clean_phone)
        if matches:
            raise DuplicateCustomerPhoneError(clean_phone, matches)
    if "cheques_count" in details:
        _validate_cheques_count(details["cheques_count"])
    return repositories.create_customer(
        session,
        full_name=clean_name,
        category_id=category_id,
        phone=clean_phone,
        **details,
    )


def update_customer(
    session: Session,
    customer_id: int,
    *,
    allow_duplicate_phone: bool = False,
    **changes: Any,
) -> Customer:
    """Validate and update supplied customer fields.

    Pass ``allow_duplicate_phone=True`` after the UI has shown the duplicate
    warning and the user has chosen to continue.
    """
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise ValueError("Customer not found")
    _validate_fields(changes)
    clean_changes = dict(changes)
    if "full_name" in clean_changes:
        clean_changes["full_name"] = clean_changes["full_name"].strip()
        if not clean_changes["full_name"]:
            raise ValueError("Customer name cannot be empty")
    if "category_id" in clean_changes and session.get(Category, clean_changes["category_id"]) is None:
        raise ValueError("Customer category not found")
    if "phone" in clean_changes:
        clean_phone = normalize_phone(clean_changes["phone"])
        clean_changes["phone"] = clean_phone
        if clean_phone and not allow_duplicate_phone:
            matches = find_duplicate_phones(
                session, clean_phone, exclude_customer_id=customer_id
            )
            if matches:
                raise DuplicateCustomerPhoneError(clean_phone, matches)
    if "cheques_count" in clean_changes:
        _validate_cheques_count(clean_changes["cheques_count"])
    return repositories.update_customer(session, customer_id, **clean_changes)


def _validate_fields(fields: dict[str, Any]) -> None:
    """Reject accidental writes to relationships and generated model fields."""
    unsupported = sorted(set(fields) - _CUSTOMER_FIELDS)
    if unsupported:
        raise ValueError(f"Unsupported customer field: {unsupported[0]}")


def _validate_cheques_count(value: int | None) -> None:
    """Check that an optional cheque count is a nonnegative integer."""
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError("Cheque count must be a nonnegative whole number")


def delete_customer(session: Session, customer_id: int) -> None:
    """Delete a customer who has no sales; financial history is never removed."""
    repositories.delete_customer(session, customer_id)
