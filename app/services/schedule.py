"""Build installment due dates and amounts from a sale."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping

from app.services.calc import add_months, payment_offsets


@dataclass(frozen=True, slots=True)
class ScheduledInstallment:
    """One calculated installment row, ready to persist or display."""

    installment_index: int
    due_date: date
    amount_due: int
    amount_paid: int = 0


def build(sale: Any, settings: Mapping[str, Any] | None = None) -> list[ScheduledInstallment]:
    """Build schedule rows for a Sale ORM object or compatible sale record.

    ``due_mode`` may be ``first_of_month`` (the default) or ``purchase_day``.
    The schedule is derived from ``financed``, ``months`` and
    ``payment_interval`` (months between payments, default 1) so persisted
    Sale rows do not need to store each amount.
    """
    if getattr(sale, "sale_type", None) != "installment":
        return []

    months = getattr(sale, "months", None)
    if isinstance(months, bool) or not isinstance(months, int) or months < 1:
        raise ValueError("installment sale requires months >= 1")
    financed = getattr(sale, "financed", None)
    if isinstance(financed, bool) or not isinstance(financed, int) or financed < 0:
        raise ValueError("installment sale requires non-negative integer financed amount")
    purchase_date = _as_date(getattr(sale, "purchase_date", None))
    if purchase_date is None:
        raise ValueError("installment sale requires purchase_date")

    due_mode = _setting(settings or {}, "due_mode", "first_of_month")
    if due_mode not in {"first_of_month", "purchase_day"}:
        raise ValueError("due_mode must be 'first_of_month' or 'purchase_day'")

    interval = getattr(sale, "payment_interval", None) or 1
    offsets = payment_offsets(months, interval)
    amounts = _monthly_amounts(sale, financed, len(offsets))
    result: list[ScheduledInstallment] = []
    for index, (offset, amount) in enumerate(zip(offsets, amounts, strict=True), start=1):
        target_month = add_months(purchase_date, offset)
        due_date = (
            target_month.replace(day=1)
            if due_mode == "first_of_month"
            else target_month
        )
        result.append(
            ScheduledInstallment(
                installment_index=index,
                due_date=due_date,
                amount_due=amount,
            )
        )
    return result


def _monthly_amounts(sale: Any, financed: int, count: int) -> list[int]:
    """Use a supplied calculation list when valid; otherwise derive one amount per payment."""
    supplied = getattr(sale, "monthly_list", None)
    if supplied is not None:
        values = list(supplied)
        if (
            len(values) != count
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values)
            or sum(values) != financed
        ):
            raise ValueError("monthly_list must contain one non-negative integer amount per payment and sum to financed")
        return values

    base, remainder = divmod(financed, count)
    values = [base] * count
    values[-1] += remainder
    return values


def _setting(settings: Mapping[str, Any], key: str, default: Any) -> Any:
    """Read a plain settings dict or a Setting-like key/value record."""
    value = settings.get(key, default)
    if hasattr(value, "value"):
        value = value.value
    if isinstance(value, str) and key == "due_mode":
        return value.strip('"')
    return value


def _as_date(value: date | datetime | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None
