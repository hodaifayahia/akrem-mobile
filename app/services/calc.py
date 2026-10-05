"""Pure sale calculation helpers; all amounts are integer dinars."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any


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
    total_override: int | None = None,
) -> SaleCalculation:
    """Calculate total, balance, installment amounts, profit and end date.

    Percentages that do not produce a whole dinar are rounded down. Cash and
    credit sales do not receive a schedule; installment sales require a
    positive duration. ``interval`` is the number of months between payments
    (1 = monthly); ``monthly_list`` holds one amount per payment and the final
    payment absorbs any division remainder.

    ``total_override`` replaces the total this function would compute from the
    price and the rate: products bought together share the one total their
    combined amount gives, split by ``split_amount`` (see
    ``shared_basket_plan``). Everything else follows from that total.
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
    if total_override is not None:
        _validate_nonnegative_integer("total", total_override)
        total = total_override
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


def split_amount(total: int, weights: Sequence[int]) -> list[int]:
    """Split ``total`` across ``weights`` by largest remainder; the parts add up exactly.

    Used for products bought together: the percentage is applied once to the
    combined selling price and each product keeps its share of the result, so
    the lines of one visit never add up to more (or less) than the amount the
    shop agreed. A zero or empty weight list gives everything to the first part.
    """
    _validate_nonnegative_integer("total", total)
    parts = [max(0, _whole(weight) or 0) for weight in weights]
    if not parts:
        return []
    weight_total = sum(parts)
    if weight_total <= 0:
        return [total, *([0] * (len(parts) - 1))]
    base = [total * weight // weight_total for weight in parts]
    remainder = total - sum(base)
    # The lines left over a dinar get it, the biggest share of it first.
    order = sorted(range(len(parts)), key=lambda index: (-(total * parts[index] % weight_total), index))
    for index in order[:remainder]:
        base[index] += 1
    return base


def shared_basket_plan(lines: Sequence[Mapping[str, Any]]) -> tuple[int, int | None, int]:
    """The one rate, duration and payment interval of the products bought together.

    Two or more products added to one basket are one purchase: they share the
    plan of the last installment line, which is the plan sitting in the sale
    form when the visit is saved. Cash and credit lines carry no plan.
    """
    plan = [line for line in lines if line.get("sale_type") == "installment"]
    if not plan:
        return (0, None, 1)
    last = plan[-1]
    return (
        _whole(last.get("rate")) or 0,
        last.get("months"),
        _whole(last.get("payment_interval")) or 1,
    )


def share_basket_plan(lines: list[dict[str, Any]]) -> None:
    """Give the products of one visit one rate, one duration and one percentage.

    Every line keeps its own product, price, quantity, type and phone, but the
    installment lines of a basket are one purchase: they share the plan
    (``shared_basket_plan``), so their schedules fall on the same dates and over
    the same months. The percentage is applied ONCE, to the combined selling
    price, and that total is split across the lines by their share of it, so the
    lines add up to exactly what one calculation on the combined amount gives.
    A down payment typed once is charged once, on the first line. Fewer than two
    installment lines are left untouched, so a single-product sale is unchanged.
    """
    plan = [index for index, line in enumerate(lines) if line.get("sale_type") == "installment"]
    if len(plan) < 2:
        return
    rate, months, interval = shared_basket_plan(lines)
    prices = [_whole(lines[index].get("cash_price")) or 0 for index in plan]
    if any(price <= 0 for price in prices):
        return  # a price the seller still has to check: every line is calculated on its own
    down = max(_whole(lines[index].get("down_payment")) or 0 for index in plan)
    combined = compute_sale(
        cash_price=sum(prices), rate=rate, down_payment=0, months=months, wholesale=0,
        sale_type="installment", interval=interval,
    )
    parts = split_amount(combined.total, prices)
    for position, index in enumerate(plan):
        lines[index].update(
            rate=rate,
            months=months,
            payment_interval=interval,
            down_payment=down if position == 0 else 0,
            total=parts[position],
        )


def _whole(value: Any) -> int | None:
    """The value when it is a whole number, else ``None`` (the sale validation reports it)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _validate_nonnegative_integer(name: str, value: int) -> None:
    """Reject floats and booleans so money and rates remain integral."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
