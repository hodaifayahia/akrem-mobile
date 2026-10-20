"""Customer workflows and validation."""

from __future__ import annotations

import re
import json
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db.models import Category, Customer, Installment, Payment, Sale, Setting
from app.services import auth, repositories
from app.services.calc import add_months
from app.services.settings import get_grace_days
from app.services.status import Status, for_customer, for_month

_MOBILE_RE = re.compile(r"^0[567][0-9]{8}$")
PAYMENT_KINDS = ("cash", "installment", "credit")
RETURNED = "returned"  # shown instead of the payment method of a product brought back


def display_kind(sale: Sale) -> str:
    """The sale's payment method as shown on screen, or ``"returned"`` (see ``sales.return_sales``)."""
    return RETURNED if sale.returned_at is not None else sale.sale_type
PAYMENT_FILTERS = ("cash", "facilities", "installment", "credit")
BALANCE_FILTERS = ("open", "settled")
PURCHASE_FILTERS = ("this_month", "last_month", "last_3_months", "this_year")
DEDUCTION_FILTERS = ("ends_this_month", "ends_next_month", "finished", "ongoing")
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
    total: int
    remaining_balance: int
    status: Status
    customer: Customer
    sales: tuple[Sale, ...]
    # Set on the one-row-per-sale list: the (first) sale this row shows (None for a client without sales).
    sale_id: int | None = None

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
    def display_kinds(self) -> tuple[str, ...]:
        """The row's payment methods as shown: ``"returned"`` for a product brought back."""
        present = {display_kind(sale) for sale in self.sales}
        return tuple(kind for kind in (*PAYMENT_KINDS, RETURNED) if kind in present)

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
    kinds = {"cash" if kind == RETURNED else kind for kind in sale_types}  # a return is settled
    if not kinds:
        return "none"
    return "cash" if kinds == {"cash"} else "facilities"


@dataclass(frozen=True, slots=True)
class TransactionRecord:
    """One amount received for a sale, with what was still owed after it.

    ``kind`` is ``"full_payment"`` (a cash sale), ``"down_payment"``,
    ``"payment"`` (a recorded installment or credit payment) or ``"earlier"``
    (an installment amount marked paid without payment rows, e.g. imported).
    One amount received is one record, even when it was spread over several
    installments or several products of a basket: ``amount`` is what the
    client handed over and ``installment_indexes`` lists every installment it paid.
    """

    kind: str
    transaction_date: date
    amount: int
    remaining_after: int
    method: str | None = None
    installment_index: int | None = None
    note: str | None = None
    payment_id: int | None = None
    product: str | None = None
    batch_id: str | None = None
    installment_indexes: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ReturnRecord:
    """A product the client brought back: when, what the shop gave back, and why."""

    product: str
    returned_at: date
    refund: int
    reason: str | None = None


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
    color: str | None = None
    battery_health: int | None = None
    is_new: bool = False
    imei: str | None = None
    reference: str | None = None
    quantity: int = 1
    basket_id: str | None = None
    products: tuple[str, ...] = ()
    # Every amount received for this sale, oldest first.
    transactions: tuple[TransactionRecord, ...] = ()
    # Products of this purchase the client brought back.
    returns: tuple[ReturnRecord, ...] = ()


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
    purchase_groups = list(reversed(group_basket_sales(ordered_sales)))
    purchases = tuple(_purchase_record(group, today, grace_days) for group in purchase_groups)
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
        cash_purchases=sum(1 for group in purchase_groups if any(sale.sale_type == "cash" for sale in group)),
        installment_purchases=sum(
            1 for group in purchase_groups if any(sale.sale_type == "installment" for sale in group)
        ),
        credit_purchases=sum(1 for group in purchase_groups if any(sale.sale_type == "credit" for sale in group)),
        first_purchase=min(purchase_dates, default=None),
        last_purchase=max(purchase_dates, default=None),
        last_payment=payments[0].payment_date if payments else None,
        overdue_installments=sum(purchase.overdue_installments for purchase in purchases),
    )


def _installment_paid(installment: Installment) -> int:
    """Return an installment's paid amount using the ledger's max rule."""
    return max(installment.amount_paid, sum(payment.amount for payment in installment.payments))


def sale_paid(sale: Sale) -> int:
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


def group_basket_sales(sales: Iterable[Sale]) -> list[tuple[Sale, ...]]:
    """Group every product bought in one basket into a single displayed purchase.

    The database keeps a sale row per basket line so stock and payments retain
    their own records. Purchases made outside a basket always stay separate.
    """
    groups: dict[tuple[object, ...], list[Sale]] = {}
    for sale in sorted(sales, key=lambda item: (item.purchase_date, item.id)):
        if sale.basket_id is None:
            key = ("sale", sale.id)
        else:
            key = ("basket", sale.basket_id)
        groups.setdefault(key, []).append(sale)
    return [tuple(group) for group in groups.values()]


def basket_product_label(sales: Iterable[Sale]) -> str:
    """Return the basket's item list, combining repeated product names and quantities."""
    quantities: dict[str, int] = {}
    for sale in sales:
        quantities[sale.product] = quantities.get(sale.product, 0) + (sale.quantity or 1)
    return " + ".join(
        f"{product} ×{quantity}" if quantity > 1 else product
        for product, quantity in quantities.items()
    )


def _purchase_record(sales: tuple[Sale, ...], today: date, grace_days: int) -> PurchaseRecord:
    """Summarize one basket, including every item and transaction in it."""
    sale = sales[0]
    installment_sales = [item for item in sales if item.sale_type == "installment"]
    sale_types = {display_kind(item) for item in sales}
    returned = [item for item in sales if item.returned_at is not None]
    months = {item.months for item in installment_sales}
    rates = {item.rate for item in installment_sales}
    intervals = {item.payment_interval or 1 for item in installment_sales}
    total = sum(item.total for item in sales)
    paid = sum(sale_paid(item) for item in sales)
    transactions = _group_transactions(sales, total)
    return PurchaseRecord(
        sale_id=sale.id,
        purchase_date=sale.purchase_date,
        product=basket_product_label(sales),
        sale_type=next(iter(sale_types)) if len(sale_types) == 1 else "mixed",
        cash_price=sum(item.cash_price for item in sales),
        wholesale_price=sum(item.wholesale_price for item in sales),
        rate=next(iter(rates)) if len(rates) == 1 else 0,
        total=total,
        down_payment=sum(item.down_payment for item in sales),
        months=next(iter(months)) if len(months) == 1 else None,
        monthly_amount=(sum(item.monthly_amount or 0 for item in installment_sales) if installment_sales else None),
        end_date=max((item.end_date for item in sales if item.end_date is not None), default=None),
        expected_pay_date=max((item.expected_pay_date for item in sales if item.expected_pay_date is not None), default=None),
        paid=paid,
        remaining=max(0, total - paid),
        profit=sum(item.profit for item in sales),
        status=for_customer(sales, today.year, today.month, today, grace_days),
        overdue_installments=sum(_overdue_count(item, today, grace_days) for item in sales),
        payment_interval=next(iter(intervals)) if len(intervals) == 1 else 1,
        payment_count=max((len(item.installments) for item in installment_sales), default=0),
        color=sale.color if len(sales) == 1 else None,
        battery_health=sale.battery_health if len(sales) == 1 else None,
        is_new=sale.is_new if len(sales) == 1 else False,
        imei=sale.imei if len(sales) == 1 else None,
        reference=sale.reference if len(sales) == 1 else None,
        quantity=1,
        basket_id=sale.basket_id,
        products=tuple(dict.fromkeys(item.product for item in sales)),
        transactions=transactions,
        returns=tuple(
            ReturnRecord(item.product, item.returned_at, item.refund_amount or 0, item.return_reason)
            for item in returned
        ),
    )


def _group_transactions(sales: tuple[Sale, ...], total: int) -> tuple[TransactionRecord, ...]:
    """One purchase's transactions with one running balance: one row per amount received.

    A payment spread over several installments (or over the products of a
    basket) shares a ``batch_id`` and becomes one row of the amount the client
    paid. The down payments and cash payments taken at the purchase are one row too.
    """
    records = [
        (sale.id, index, transaction)
        for sale in sales
        for index, transaction in enumerate(_transactions(sale))
    ]
    records.sort(key=lambda item: (
        item[2].transaction_date, item[2].payment_id or 0, item[0], item[1],
    ))
    merged: dict[tuple[object, ...], list[TransactionRecord]] = {}
    for sale_id, index, transaction in records:
        merged.setdefault(_merge_key(sale_id, index, transaction), []).append(transaction)
    remaining = total
    result: list[TransactionRecord] = []
    for parts in merged.values():
        amount = sum(part.amount for part in parts)
        remaining = max(0, remaining - amount)
        result.append(_merged_transaction(parts, amount, remaining))
    return tuple(result)


def _merge_key(sale_id: int, index: int, transaction: TransactionRecord) -> tuple[object, ...]:
    """Rows with the same key are one amount received."""
    if transaction.batch_id:
        return ("batch", transaction.batch_id)
    if transaction.kind in ("down_payment", "full_payment"):
        return ("purchase", transaction.kind, transaction.transaction_date)
    return ("row", sale_id, index)


def _merged_transaction(parts: list[TransactionRecord], amount: int, remaining: int) -> TransactionRecord:
    """One record for the parts of one amount received (the first part gives date and method)."""
    first = parts[0]
    indexes = tuple(sorted({number for part in parts for number in part.installment_indexes}))
    products = tuple(dict.fromkeys(part.product for part in parts if part.product))
    return TransactionRecord(
        kind=first.kind,
        transaction_date=first.transaction_date,
        amount=amount,
        remaining_after=remaining,
        method=first.method,
        installment_index=indexes[0] if len(indexes) == 1 else None,
        note=next((part.note for part in parts if part.note), None),
        payment_id=max((part.payment_id for part in parts if part.payment_id is not None), default=None),
        product=" + ".join(products) if products else None,
        batch_id=first.batch_id,
        installment_indexes=indexes,
    )


def _transactions(sale: Sale) -> tuple[TransactionRecord, ...]:
    """Every amount received for an eagerly loaded sale, oldest first, with a running balance.

    The amounts add up to the ledger's paid amount (see ``sale_paid``).
    """
    # (date, order within the sale, kind, amount, method, installment index, note, payment id, batch id)
    rows: list[tuple[date, int, str, int, str | None, int | None, str | None, int | None, str | None]] = []
    if sale.sale_type == "cash":
        if sale.total or sale.returned_at is None:  # a return fully refunded kept nothing
            rows.append((sale.purchase_date, 0, "full_payment", sale.total, None, None, None, None, None))
    elif sale.down_payment:
        rows.append((sale.purchase_date, 0, "down_payment", sale.down_payment, None, None, None, None, None))
    if sale.sale_type == "credit":
        for payment in sale.credit_payments:
            rows.append((payment.payment_date, payment.id, "payment", payment.amount,
                         payment.method, None, payment.note, payment.id, payment.batch_id))
    elif sale.sale_type == "installment":
        for installment in sale.installments:
            recorded = 0
            for payment in installment.payments:
                recorded += payment.amount
                rows.append((payment.payment_date, payment.id, "payment", payment.amount, payment.method,
                             installment.installment_index, payment.note, payment.id, payment.batch_id))
            earlier = installment.amount_paid - recorded
            if earlier > 0:
                rows.append((installment.paid_date or installment.due_date, 0, "earlier", earlier,
                             installment.method, installment.installment_index, installment.note, None, None))
    rows.sort(key=lambda row: (row[0], row[1], row[5] or 0))
    remaining = sale.total
    result: list[TransactionRecord] = []
    for when, _order, kind, amount, method, index, note, payment_id, batch_id in rows:
        remaining = max(0, remaining - amount)
        result.append(TransactionRecord(
            kind=kind, transaction_date=when, amount=amount, remaining_after=remaining, method=method,
            installment_index=index, note=note, payment_id=payment_id,
            product=sale.product, batch_id=batch_id,
            installment_indexes=(index,) if index is not None else (),
        ))
    return tuple(result)


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
    purchased: str | None = None,
    deduction: str | None = None,
    per_sale: bool = False,
) -> list[CustomerSummary]:
    """Return customer rows with selected-month status and outstanding balances.

    With ``per_sale`` every sale is its own row (a client who bought three
    phones on three visits shows three rows, each with that sale's status,
    amounts and dates), except that products bought together in one basket
    share a row; a client without sales still shows one row. Filters then
    apply to each row on its own, and a search keeps a basket row when one of
    its products matches.

    ``payment_kind``: ``"cash"`` keeps customers who paid every purchase in
    full; ``"facilities"`` keeps customers paying at least one purchase over
    time; ``"installment"`` / ``"credit"`` keep customers with such a sale. ``balance`` keeps customers who still
    owe money (``"open"``) or have nothing left to pay (``"settled"``).
    ``purchased`` keeps customers who bought something in that period
    (``this_month``, ``last_month``, ``last_3_months``, ``this_year``,
    relative to ``today``). ``deduction`` looks at installment end dates (the
    sheet's "تاريخ انتهاء الاقتطاع"): ``ends_this_month``, ``ends_next_month``,
    ``finished`` (every plan ended) or ``ongoing`` (a plan still running).
    The search text matches names, phone numbers, product names, IMEI and REF.

    Customer names and phone numbers are narrowed in a lightweight first query
    when searching. The final query eagerly loads categories, sales, schedules,
    and payment rows, avoiding per-customer database queries for large lists.
    """
    selected_status = _coerce_customer_status(status)
    if payment_kind not in (None, "", *PAYMENT_FILTERS):
        raise ValueError(f"Unsupported payment kind: {payment_kind}")
    if balance not in (None, "", *BALANCE_FILTERS):
        raise ValueError(f"Unsupported balance filter: {balance}")
    if purchased not in (None, "", *PURCHASE_FILTERS):
        raise ValueError(f"Unsupported purchase period: {purchased}")
    if deduction not in (None, "", *DEDUCTION_FILTERS):
        raise ValueError(f"Unsupported deduction filter: {deduction}")
    search_key = normalize_search_text(search)
    grace_days = _grace_days(session)
    # Validate the selected period even when the query has no matching rows.
    for_customer((), year, month, today, grace_days=grace_days)

    base_query = select(Customer)
    if category_id is not None:
        base_query = base_query.where(Customer.category_id == category_id)

    matching_ids: list[int] | None = None
    # Searching by product/IMEI/REF on the per-sale list keeps only the matching sales.
    name_matches: set[int] = set()
    sale_matches: set[int] = set()
    if search_key:
        candidates_query = select(Customer.id, Customer.full_name, Customer.phone)
        if category_id is not None:
            candidates_query = candidates_query.where(Customer.category_id == category_id)
        name_matches = {
            customer_id
            for customer_id, full_name, phone in session.execute(candidates_query)
            if search_key in normalize_search_text(full_name)
            or search_key in normalize_search_text(phone)
        }
        matching = set(name_matches)
        product_query = select(Sale.id, Sale.customer_id, Sale.product, Sale.imei, Sale.reference)
        if category_id is not None:
            product_query = product_query.join(Customer).where(Customer.category_id == category_id)
        for sale_id, customer_id, product, imei, reference in session.execute(product_query):
            if (
                search_key in normalize_search_text(product)
                or search_key in normalize_search_text(imei or "")
                or search_key in normalize_search_text(reference or "")
            ):
                matching.add(customer_id)
                sale_matches.add(sale_id)
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
        if not per_sale:
            groups: list[tuple[tuple[Sale, ...], int | None]] = [(customer_sales, None)]
        else:
            rows_of_sales = _basket_rows(customer_sales)
            if search_key and customer.id not in name_matches:
                rows_of_sales = [
                    group for group in rows_of_sales if any(sale.id in sale_matches for sale in group)
                ]
            groups = [(group, group[0].id) for group in rows_of_sales] or [((), None)]
        for group, sale_id in groups:
            summary = _summary_row(
                customer, group, sale_id,
                year=year, month=month, today=today, grace_days=grace_days,
                status=selected_status, payment_kind=payment_kind, balance=balance,
                purchased=purchased, deduction=deduction,
            )
            if summary is not None:
                result.append(summary)
    return result


def _basket_rows(sales: tuple[Sale, ...]) -> list[tuple[Sale, ...]]:
    """Split a client's sales into list rows: one per basket, one per sale bought on its own.

    Rows keep the order of their first sale.
    """
    rows: dict[object, list[Sale]] = {}
    for sale in sales:
        rows.setdefault(sale.basket_id or ("sale", sale.id), []).append(sale)
    return [tuple(group) for group in rows.values()]


def _summary_row(
    customer: Customer,
    customer_sales: tuple[Sale, ...],
    sale_id: int | None,
    *,
    year: int,
    month: int,
    today: date,
    grace_days: int,
    status: Status | None,
    payment_kind: str | None,
    balance: str | None,
    purchased: str | None,
    deduction: str | None,
) -> CustomerSummary | None:
    """One list row for ``customer_sales`` (all of a client's sales, or one), or None when filtered out."""
    customer_status = for_customer(customer_sales, year, month, today, grace_days=grace_days)
    if status is not None and customer_status != status:
        return None
    if payment_kind and not _matches_payment_kind(customer_sales, payment_kind):
        return None
    if purchased and not _bought_in(customer_sales, purchased, today):
        return None
    if deduction and not _matches_deduction(customer_sales, deduction, today):
        return None

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
        return None
    if balance == "settled" and remaining_balance > 0:
        return None

    return CustomerSummary(
        id=customer.id,
        full_name=customer.full_name,
        phone=customer.phone,
        category_id=customer.category_id,
        category_name=customer.category.name,
        products=tuple(sale.product for sale in customer_sales),
        purchase_dates=tuple(sale.purchase_date for sale in customer_sales),
        end_dates=tuple(sale.end_date for sale in customer_sales),
        monthly_amount=monthly_amount,
        total=sum(sale.total for sale in customer_sales),
        remaining_balance=remaining_balance,
        status=customer_status,
        customer=customer,
        sales=customer_sales,
        sale_id=sale_id,
    )


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


def purchase_period(period: str, today: date) -> tuple[date, date]:
    """First and last day of a ``PURCHASE_FILTERS`` period, relative to ``today``."""
    month_start = today.replace(day=1)
    if period == "this_month":
        return month_start, add_months(month_start, 1) - timedelta(days=1)
    if period == "last_month":
        start = add_months(month_start, -1)
        return start, month_start - timedelta(days=1)
    if period == "last_3_months":
        return add_months(month_start, -2), today
    if period == "this_year":
        return today.replace(month=1, day=1), today.replace(month=12, day=31)
    raise ValueError(f"Unsupported purchase period: {period}")


def _bought_in(customer_sales: tuple[Sale, ...], period: str, today: date) -> bool:
    start, end = purchase_period(period, today)
    return any(start <= sale.purchase_date <= end for sale in customer_sales)


def _matches_deduction(customer_sales: tuple[Sale, ...], kind: str, today: date) -> bool:
    """Match installment end dates against the end-of-deduction filter."""
    ends = [sale.end_date for sale in customer_sales if sale.sale_type == "installment" and sale.end_date]
    if not ends:
        return False
    if kind in ("ends_this_month", "ends_next_month"):
        target = today if kind == "ends_this_month" else add_months(today.replace(day=1), 1)
        return any((end.year, end.month) == (target.year, target.month) for end in ends)
    if kind == "finished":
        return all(end < today for end in ends)
    return any(end >= today for end in ends)  # ongoing


def delete_customer(session: Session, customer_id: int) -> None:
    """Delete a customer who has no sales; financial history is never removed."""
    repositories.delete_customer(session, customer_id)


@dataclass(frozen=True, slots=True)
class CustomerDeleteResult:
    """Outcome of deleting several customers at once."""

    deleted: int
    sales_deleted: int
    kept: tuple[str, ...]


def delete_customers(
    session: Session,
    owner_user_id: int,
    customer_ids: Iterable[int],
    *,
    with_sales: bool = False,
) -> CustomerDeleteResult:
    """Delete customers chosen by the owner.

    A customer who has sales is kept (and named in ``kept``) unless
    ``with_sales`` is set; then their sales, installments and payments are
    deleted first, with an audit entry for each sale.
    """
    from app.services.sales import delete_sales  # here: the sales service imports this module

    auth.require_owner(session, owner_user_id)
    unique_ids = list(dict.fromkeys(int(customer_id) for customer_id in customer_ids))
    rows = session.scalars(
        select(Customer).where(Customer.id.in_(unique_ids)).options(selectinload(Customer.sales))
    ).all() if unique_ids else []
    kept: list[str] = []
    deleted = sales_deleted = 0
    for customer in rows:
        if customer.sales:
            if not with_sales:
                kept.append(customer.full_name)
                continue
            sales_deleted += delete_sales(session, owner_user_id, [sale.id for sale in customer.sales])
            session.expire(customer, ["sales"])
        session.delete(customer)
        deleted += 1
    session.flush()
    return CustomerDeleteResult(deleted=deleted, sales_deleted=sales_deleted, kept=tuple(kept))
