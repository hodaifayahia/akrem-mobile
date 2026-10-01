"""Pure sale calculation helpers; all amounts are integer dinars."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class SaleCalculation:
    """Calculated values for a sale before it is persisted."""

    total: int
    financed: int
    monthly_list: list[int]
    profit: int
    end_date: date | None
    payment_interval: int = 1

    @property
    def payment_count(self) -> int:
        """Number of scheduled payments (``len(monthly_list)``)."""
        return len(self.monthly_list)


def add_months(value: date, months: int) -> date:
    """Add calendar months, clamping the day like Excel's EDATE function."""
    month_index = value.year * 12 + (value.month - 1) + months
    year, zero_month = divmod(month_index, 12)
    month = zero_month + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


MAX_PLAN_MONTHS = 60


def payment_offsets(months: int, interval: int = 1) -> list[int]:
    """Month offsets from the purchase date at which installments fall due.

    A plan of ``months`` paid every ``interval`` months has one payment every
    ``interval`` months; the last payment is always at ``months``, so a
    5-month plan paid every 2 months is due at months 2, 4 and 5.
    """
    validate_plan(months, interval)
    offsets = list(range(interval, months + 1, interval))
    if not offsets or offsets[-1] != months:
        offsets.append(months)
    return offsets


def validate_plan(months: int, interval: int) -> None:
    """Reject durations and payment intervals outside 1..60 months or longer than the plan."""
    for name, value in (("months", months), ("payment_interval", interval)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a whole number >= 1")
    if months > MAX_PLAN_MONTHS:
        raise ValueError(f"months cannot exceed {MAX_PLAN_MONTHS}")
    if interval > months:
        raise ValueError("payment_interval cannot be longer than the plan")


def compute_sale(
    cash_price: int,
    rate: int,
    down_payment: int,
    months: int | None,
    wholesale: int,
    sale_type: str,
    purchase_date: date | None = None,
    interval: int = 1,
) -> SaleCalculation:
    """Calculate total, balance, installment amounts, profit and end date.

    Percentages that do not produce a whole dinar are rounded down. Cash and
    credit sales do not receive a schedule; installment sales require a
    positive duration. ``interval`` is the number of months between payments
    (1 = monthly); ``monthly_list`` holds one amount per payment and the final
    payment absorbs any division remainder.
    """
    _validate_nonnegative_integer("cash_price", cash_price)
    _validate_nonnegative_integer("rate", rate)
    _validate_nonnegative_integer("down_payment", down_payment)
    _validate_nonnegative_integer("wholesale", wholesale)
    if rate > 100:
        raise ValueError("rate must be between 0 and 100")
    if sale_type not in {"cash", "installment", "credit"}:
        raise ValueError("sale_type must be 'cash', 'installment', or 'credit'")
    if purchase_date is not None and not isinstance(purchase_date, date):
        raise TypeError("purchase_date must be a date or None")

    applied_rate = 0 if sale_type == "cash" else rate
    total = cash_price * (100 + applied_rate) // 100
    if down_payment > total:
        raise ValueError("down_payment cannot exceed total")
    financed = total - down_payment
    profit = total - wholesale

    monthly_list: list[int] = []
    end_date: date | None = None
    if sale_type == "installment":
        if months is None or isinstance(months, bool) or not isinstance(months, int) or months < 1:
            raise ValueError("installment sales require months >= 1")
        count = len(payment_offsets(months, interval))
        monthly, remainder = divmod(financed, count)
        monthly_list = [monthly] * count
        monthly_list[-1] += remainder
        if purchase_date is not None:
            end_date = add_months(purchase_date, months)

    return SaleCalculation(
        total=total,
        financed=financed,
        monthly_list=monthly_list,
        profit=profit,
        end_date=end_date,
        payment_interval=interval if sale_type == "installment" else 1,
    )


def _validate_nonnegative_integer(name: str, value: int) -> None:
    """Reject floats and booleans so money and rates remain integral."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
