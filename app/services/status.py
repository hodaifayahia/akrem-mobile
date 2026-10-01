"""Status evaluation for monthly installment obligations."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from enum import Enum
from typing import Any, Iterable


class Status(str, Enum):
    """Customer or installment state for a selected month."""

    PAID = "PAID"
    FAILED = "FAILED"
    PENDING = "PENDING"
    NONE = "NONE"


def for_month(
    installments: Iterable[Any],
    year: int,
    month: int,
    today: date,
    grace_days: int = 5,
) -> Status:
    """Evaluate a month's installments, retaining any earlier overdue debt.

    An unpaid installment fails only after its due date plus the grace period.
    A late balance from an earlier month takes precedence even when all
    installments due in the selected month have been paid.
    """
    _validate_month(year, month)
    if not isinstance(today, date):
        raise TypeError("today must be a date")
    if isinstance(grace_days, bool) or not isinstance(grace_days, int) or grace_days < 0:
        raise ValueError("grace_days must be a non-negative integer")

    rows = list(installments)
    month_start = date(year, month, 1)
    month_end = date(year, month, monthrange(year, month)[1])
    due_this_month = [row for row in rows if _due_date(row) and month_start <= _due_date(row) <= month_end]

    for row in rows:
        due_date = _due_date(row)
        if (
            due_date is not None
            and due_date <= month_end
            and not _fully_paid(row)
            and today > due_date + timedelta(days=grace_days)
        ):
            return Status.FAILED

    if not due_this_month:
        return Status.NONE
    if all(_fully_paid(row) for row in due_this_month):
        return Status.PAID
    return Status.PENDING


def combine_statuses(statuses: Iterable[Status | str]) -> Status:
    """Combine per-sale statuses into one customer status."""
    normalized = {_coerce_status(value) for value in statuses}
    if Status.FAILED in normalized:
        return Status.FAILED
    if Status.PENDING in normalized:
        return Status.PENDING
    if Status.PAID in normalized:
        return Status.PAID
    return Status.NONE


def for_customer(
    sales_or_installments: Iterable[Any],
    year: int,
    month: int,
    today: date,
    grace_days: int = 5,
) -> Status:
    """Evaluate all installments from a customer's sales together.

    Accepts either a flat iterable of installment rows or Sale-like objects
    whose ``installments`` attribute contains their rows.
    """
    flattened: list[Any] = []
    for item in sales_or_installments:
        sale_installments = getattr(item, "installments", None)
        if sale_installments is not None:
            flattened.extend(sale_installments)
        else:
            flattened.append(item)
    return for_month(flattened, year, month, today, grace_days)


def combine_customer_status(
    sales_or_installments: Iterable[Any],
    year: int,
    month: int,
    today: date,
    grace_days: int = 5,
) -> Status:
    """Named alias for the per-customer aggregation used by callers."""
    return for_customer(sales_or_installments, year, month, today, grace_days)


def _fully_paid(installment: Any) -> bool:
    due = getattr(installment, "amount_due", 0)
    paid = getattr(installment, "amount_paid", 0)
    payments = getattr(installment, "payments", None)
    if payments:
        payment_sum = sum(getattr(payment, "amount", 0) for payment in payments)
        paid = max(paid, payment_sum)
    return paid >= due


def _due_date(installment: Any) -> date | None:
    due_date = getattr(installment, "due_date", None)
    if hasattr(due_date, "date") and not isinstance(due_date, date):
        due_date = due_date.date()
    return due_date if isinstance(due_date, date) else None


def _validate_month(year: int, month: int) -> None:
    if isinstance(year, bool) or not isinstance(year, int) or isinstance(month, bool) or not isinstance(month, int):
        raise ValueError("year and month must be integers")
    if not 1 <= month <= 12:
        raise ValueError("month must be between 1 and 12")
    try:
        date(year, month, 1)
    except ValueError as error:
        raise ValueError("year is outside the supported date range") from error


def _coerce_status(value: Status | str) -> Status:
    if isinstance(value, Status):
        return value
    return Status(value)
