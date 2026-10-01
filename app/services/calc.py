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


def add_months(value: date, months: int) -> date:
    """Add calendar months, clamping the day like Excel's EDATE function."""
    month_index = value.year * 12 + (value.month - 1) + months
    year, zero_month = divmod(month_index, 12)
    month = zero_month + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def compute_sale(
    cash_price: int,
    rate: int,
    down_payment: int,
    months: int | None,
    wholesale: int,
    sale_type: str,
    purchase_date: date | None = None,
) -> SaleCalculation:
    """Calculate total, balance, installment amounts, profit and end date.

    Percentages that do not produce a whole dinar are rounded down. Cash and
    credit sales do not receive a monthly schedule; installment sales require
    a positive duration. The final installment absorbs any division remainder.
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
        monthly, remainder = divmod(financed, months)
        monthly_list = [monthly] * months
        monthly_list[-1] += remainder
        if purchase_date is not None:
            end_date = add_months(purchase_date, months)

    return SaleCalculation(
        total=total,
        financed=financed,
        monthly_list=monthly_list,
        profit=profit,
        end_date=end_date,
    )


def _validate_nonnegative_integer(name: str, value: int) -> None:
    """Reject floats and booleans so money and rates remain integral."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
