"""Everything the shop has to collect, as one list for the Payments table.

Each installment of an installment sale is one row; each credit sale is one
row for its open balance. Statuses follow the business rules used by the
alerts: an unpaid amount is late only after its due date plus the grace days.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db.models import Installment, Payment, Sale
from app.services.alerts import credit_remaining, installment_remaining
from app.services.customers import normalize_search_text
from app.services.settings import get_grace_days

RowStatus = Literal["overdue", "due", "upcoming", "paid"]
#: Status filter values; ``"unpaid"`` keeps overdue, due and upcoming rows.
STATUS_FILTERS = ("overdue", "due", "upcoming", "paid", "unpaid")
KIND_FILTERS = ("installment", "credit")
#: ``until_this_month`` keeps everything due by the end of the current month.
PERIOD_FILTERS = ("all", "until_this_month", "this_month", "next_month", "month")


@dataclass(frozen=True, slots=True)
class CollectionRow:
    """One installment, or one credit balance, with its collection status."""

    kind: str
    sale_id: int
    installment_id: int | None
    customer_id: int
    customer_name: str
    customer_phone: str | None
    category_id: int
    product: str
    number: int | None
    count: int | None
    due_date: date | None
    amount_due: int
    paid: int
    remaining: int
    status: RowStatus
    days_late: int
    last_payment_date: date | None
    method: str | None
    search_key: str = field(default="", compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class CollectionFilter:
    """What the Payments table shows; the defaults show everything."""

    search: str = ""
    status: str | None = None
    kind: str | None = None
    category_id: int | None = None
    period: str = "all"
    month: date | None = None


@dataclass(frozen=True, slots=True)
class CollectionTotals:
    """Row counts and unpaid (or collected) dinars per status."""

    overdue_count: int = 0
    overdue_amount: int = 0
    due_count: int = 0
    due_amount: int = 0
    upcoming_count: int = 0
    upcoming_amount: int = 0
    paid_count: int = 0
    paid_amount: int = 0

    @property
    def remaining(self) -> int:
        """Dinars still to collect on the listed rows."""
        return self.overdue_amount + self.due_amount + self.upcoming_amount


def row_status(due_date: date | None, remaining: int, *, today: date, grace_days: int) -> RowStatus:
    """Paid, overdue (after due date + grace), due (by the end of this month) or upcoming."""
    if remaining <= 0:
        return "paid"
    if due_date is None:
        return "due"
    if today > due_date + timedelta(days=grace_days):
        return "overdue"
    if due_date <= _month_end(today):
        return "due"
    return "upcoming"


def collection_rows(session: Session, *, today: date) -> list[CollectionRow]:
    """Return every installment and credit balance, late first, then by due date."""
    grace_days = get_grace_days(session)
    rows = [
        *_installment_rows(session, today, grace_days),
        *_credit_rows(session, today, grace_days),
    ]
    return sorted(rows, key=_sort_key)


def filter_rows(rows: Iterable[CollectionRow], wanted: CollectionFilter, *, today: date) -> list[CollectionRow]:
    """Keep the rows matching the search text and every chosen filter."""
    if wanted.status not in (None, "", *STATUS_FILTERS):
        raise ValueError(f"Unsupported status filter: {wanted.status}")
    if wanted.kind not in (None, "", *KIND_FILTERS):
        raise ValueError(f"Unsupported kind filter: {wanted.kind}")
    if wanted.period not in PERIOD_FILTERS:
        raise ValueError(f"Unsupported period filter: {wanted.period}")
    terms = normalize_search_text(wanted.search).split()
    window = _period_window(wanted.period, wanted.month, today)
    kept: list[CollectionRow] = []
    for row in rows:
        if terms and not all(term in row.search_key for term in terms):
            continue
        if wanted.status == "unpaid":
            if row.status == "paid":
                continue
        elif wanted.status and row.status != wanted.status:
            continue
        if wanted.kind and row.kind != wanted.kind:
            continue
        if wanted.category_id is not None and row.category_id != wanted.category_id:
            continue
        if window is not None and not _in_window(row, window, wanted.period):
            continue
        kept.append(row)
    return kept


def totals(rows: Iterable[CollectionRow]) -> CollectionTotals:
    """Count rows per status; amounts are what is left to collect (collected for paid)."""
    counts = {status: 0 for status in ("overdue", "due", "upcoming", "paid")}
    amounts = dict(counts)
    for row in rows:
        counts[row.status] += 1
        amounts[row.status] += row.paid if row.status == "paid" else row.remaining
    return CollectionTotals(
        counts["overdue"], amounts["overdue"],
        counts["due"], amounts["due"],
        counts["upcoming"], amounts["upcoming"],
        counts["paid"], amounts["paid"],
    )


# ---------------------------------------------------------------- building
def _installment_rows(session: Session, today: date, grace_days: int) -> list[CollectionRow]:
    installments = session.scalars(
        select(Installment)
        .join(Installment.sale)
        .where(Sale.sale_type == "installment")
        .options(
            selectinload(Installment.payments),
            joinedload(Installment.sale).joinedload(Sale.customer),
            joinedload(Installment.sale).selectinload(Sale.installments),
        )
    ).all()
    rows: list[CollectionRow] = []
    for installment in installments:
        sale = installment.sale
        remaining = max(0, installment_remaining(installment))
        paid = installment.amount_due - remaining
        last = _last_payment(installment.payments)
        rows.append(_row(
            "installment", sale,
            installment_id=installment.id,
            number=installment.installment_index,
            count=len(sale.installments),
            due_date=installment.due_date,
            amount_due=installment.amount_due,
            paid=paid,
            remaining=remaining,
            today=today,
            grace_days=grace_days,
            last_payment_date=installment.paid_date or (last.payment_date if last else None),
            method=installment.method or (last.method if last else None),
        ))
    return rows


def _credit_rows(session: Session, today: date, grace_days: int) -> list[CollectionRow]:
    sales = session.scalars(
        select(Sale)
        .where(Sale.sale_type == "credit")
        .options(selectinload(Sale.credit_payments), joinedload(Sale.customer))
    ).all()
    rows: list[CollectionRow] = []
    for sale in sales:
        remaining = max(0, credit_remaining(sale))
        last = _last_payment(sale.credit_payments)
        rows.append(_row(
            "credit", sale,
            installment_id=None,
            number=None,
            count=None,
            due_date=sale.expected_pay_date,
            amount_due=sale.financed,
            paid=sale.financed - remaining,
            remaining=remaining,
            today=today,
            grace_days=grace_days,
            last_payment_date=last.payment_date if last else None,
            method=last.method if last else None,
        ))
    return rows


def _row(
    kind: str,
    sale: Sale,
    *,
    installment_id: int | None,
    number: int | None,
    count: int | None,
    due_date: date | None,
    amount_due: int,
    paid: int,
    remaining: int,
    today: date,
    grace_days: int,
    last_payment_date: date | None,
    method: str | None,
) -> CollectionRow:
    customer = sale.customer
    status = row_status(due_date, remaining, today=today, grace_days=grace_days)
    days_late = (today - due_date).days if status == "overdue" and due_date is not None else 0
    return CollectionRow(
        kind=kind,
        sale_id=sale.id,
        installment_id=installment_id,
        customer_id=customer.id,
        customer_name=customer.full_name,
        customer_phone=customer.phone,
        category_id=customer.category_id,
        product=sale.product,
        number=number,
        count=count,
        due_date=due_date,
        amount_due=amount_due,
        paid=paid,
        remaining=remaining,
        status=status,
        days_late=days_late,
        last_payment_date=last_payment_date,
        method=method,
        search_key=normalize_search_text(
            " ".join(part for part in (customer.full_name, customer.phone, sale.product) if part)
        ),
    )


def _last_payment(payments: Iterable[Payment]) -> Payment | None:
    ordered = sorted(payments, key=lambda payment: (payment.payment_date, payment.id or 0))
    return ordered[-1] if ordered else None


_STATUS_ORDER = {"overdue": 0, "due": 1, "upcoming": 2, "paid": 3}


def _sort_key(row: CollectionRow) -> tuple[int, int, int, str, int]:
    """Late rows (most days late first), then due and upcoming by date, paid last."""
    due = row.due_date.toordinal() if row.due_date is not None else date.max.toordinal()
    if row.status == "overdue":
        return (0, -row.days_late, due, row.customer_name, row.sale_id)
    if row.status == "paid":
        return (3, -due, 0, row.customer_name, row.sale_id)
    return (_STATUS_ORDER[row.status], due, 0, row.customer_name, row.sale_id)


# ---------------------------------------------------------------- periods
def _month_end(day: date) -> date:
    return date(day.year, day.month, monthrange(day.year, day.month)[1])


def _month_window(day: date) -> tuple[date, date]:
    return date(day.year, day.month, 1), _month_end(day)


def _period_window(period: str, month: date | None, today: date) -> tuple[date | None, date] | None:
    """``(first, last)`` due dates for a period; ``first`` is open for ``until_this_month``."""
    if period == "all":
        return None
    if period == "until_this_month":
        return None, _month_end(today)
    if period == "this_month":
        return _month_window(today)
    if period == "next_month":
        return _month_window(_month_end(today) + timedelta(days=1))
    if month is None:
        raise ValueError("A month is required for the month period")
    return _month_window(month)


def _in_window(row: CollectionRow, window: tuple[date | None, date], period: str) -> bool:
    first, last = window
    if row.due_date is None:
        # An open credit with no expected date is due now, not in a later month.
        return period == "until_this_month"
    return (first is None or row.due_date >= first) and row.due_date <= last
