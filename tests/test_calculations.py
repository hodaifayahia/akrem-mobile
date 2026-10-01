"""Focused tests for calculation, schedule, and monthly status rules."""

from datetime import date
from types import SimpleNamespace

import pytest

from app.services.calc import compute_sale
from app.services.schedule import ScheduledInstallment, build
from app.services.status import Status, combine_statuses, for_customer, for_month


def test_excel_installment_examples():
    first = compute_sale(62_800, 40, 10_500, 10, 56_500, "installment", date(2025, 1, 15))
    assert (first.total, first.financed, first.monthly_list, first.profit) == (
        87_920,
        77_420,
        [7_742] * 10,
        31_420,
    )
    assert first.end_date == date(2025, 11, 15)

    second = compute_sale(47_000, 35, 12_300, 6, 41_500, "installment")
    assert (second.total, second.financed, second.monthly_list, second.profit) == (
        63_450,
        51_150,
        [8_525] * 6,
        21_950,
    )

    third = compute_sale(145_000, 10, 90_000, 3, 130_000, "installment")
    assert (third.total, third.financed, third.monthly_list, third.profit) == (
        159_500,
        69_500,
        [23_166, 23_166, 23_168],
        29_500,
    )


def test_cash_sale_has_no_schedule_and_rate_is_ignored():
    sale = compute_sale(32_000, 40, 0, None, 29_900, "cash")
    assert sale.total == 32_000
    assert sale.financed == 32_000
    assert sale.monthly_list == []
    assert sale.profit == 2_100
    assert sale.end_date is None


def test_credit_down_payment_is_already_paid_and_has_no_schedule():
    sale = compute_sale(47_000, 35, 12_300, None, 41_500, "credit")
    assert sale.total == 63_450
    assert sale.financed == 51_150
    assert sale.monthly_list == []
    assert sale.end_date is None


def test_whole_dinar_rounding_remainder_goes_to_last_month():
    sale = compute_sale(100, 0, 1, 3, 70, "installment")
    assert sale.monthly_list == [33, 33, 33]
    assert sum(sale.monthly_list) == sale.financed


@pytest.mark.parametrize(
    "purchase_date,months,expected",
    [
        (date(2024, 1, 31), 1, date(2024, 2, 29)),
        (date(2024, 1, 31), 2, date(2024, 3, 31)),
        (date(2024, 2, 29), 12, date(2025, 2, 28)),
    ],
)
def test_end_date_uses_edate_month_clamping(purchase_date, months, expected):
    sale = compute_sale(1_000, 0, 0, months, 500, "installment", purchase_date)
    assert sale.end_date == expected


def test_installment_schedule_defaults_to_first_of_following_months():
    sale = SimpleNamespace(
        sale_type="installment",
        months=3,
        financed=69_500,
        purchase_date=date(2025, 1, 31),
    )
    rows = build(sale, {})
    assert rows == [
        ScheduledInstallment(1, date(2025, 2, 1), 23_166),
        ScheduledInstallment(2, date(2025, 3, 1), 23_166),
        ScheduledInstallment(3, date(2025, 4, 1), 23_168),
    ]


def test_purchase_day_schedule_clamps_short_months():
    sale = SimpleNamespace(
        sale_type="installment",
        months=3,
        financed=300,
        purchase_date=date(2025, 1, 31),
    )
    rows = build(sale, {"due_mode": "purchase_day"})
    assert [row.due_date for row in rows] == [
        date(2025, 2, 28),
        date(2025, 3, 31),
        date(2025, 4, 30),
    ]


def test_schedule_accepts_sales_without_installment_type_and_orm_shape():
    assert build(SimpleNamespace(sale_type="cash", months=None, financed=0)) == []
    orm_sale = SimpleNamespace(
        sale_type="installment",
        months=2,
        financed=101,
        monthly_amount=50,
        purchase_date=date(2025, 6, 12),
        installments=[],
    )
    rows = build(orm_sale, {"due_mode": "first_of_month"})
    assert [row.amount_due for row in rows] == [50, 51]


def test_status_on_due_date_is_pending_and_paid_when_fully_paid():
    row = SimpleNamespace(due_date=date(2025, 4, 1), amount_due=100, amount_paid=0)
    assert for_month([row], 2025, 4, date(2025, 4, 1)) is Status.PENDING
    row.amount_paid = 100
    assert for_month([row], 2025, 4, date(2025, 4, 1)) is Status.PAID


def test_status_is_pending_through_grace_and_failed_one_day_after():
    row = SimpleNamespace(due_date=date(2025, 4, 1), amount_due=100, amount_paid=0)
    assert for_month([row], 2025, 4, date(2025, 4, 6), 5) is Status.PENDING
    assert for_month([row], 2025, 4, date(2025, 4, 7), 5) is Status.FAILED


def test_partial_payment_remains_pending_then_failed_after_grace():
    row = SimpleNamespace(due_date=date(2025, 4, 1), amount_due=100, amount_paid=99)
    assert for_month([row], 2025, 4, date(2025, 4, 5), 5) is Status.PENDING
    assert for_month([row], 2025, 4, date(2025, 4, 7), 5) is Status.FAILED


def test_past_overdue_installment_keeps_customer_failed_after_current_payment():
    old_late = SimpleNamespace(due_date=date(2025, 3, 1), amount_due=100, amount_paid=0)
    current_paid = SimpleNamespace(due_date=date(2025, 4, 1), amount_due=100, amount_paid=100)
    sale_one = SimpleNamespace(installments=[old_late])
    sale_two = SimpleNamespace(installments=[current_paid])
    assert for_customer([sale_one, sale_two], 2025, 4, date(2025, 4, 7), 5) is Status.FAILED


def test_previous_overdue_is_not_failed_before_grace_has_expired():
    old_not_late = SimpleNamespace(due_date=date(2025, 4, 2), amount_due=100, amount_paid=0)
    current_paid = SimpleNamespace(due_date=date(2025, 4, 1), amount_due=100, amount_paid=100)
    assert for_month([old_not_late, current_paid], 2025, 4, date(2025, 4, 7), 5) is Status.PENDING


def test_status_without_due_rows_is_none_unless_past_debt_is_late():
    assert for_month([], 2025, 4, date(2025, 4, 7)) is Status.NONE
    late = SimpleNamespace(due_date=date(2025, 3, 1), amount_due=10, amount_paid=0)
    assert for_month([late], 2025, 4, date(2025, 4, 7)) is Status.FAILED


def test_payment_records_can_represent_partial_payments():
    row = SimpleNamespace(
        due_date=date(2025, 4, 1),
        amount_due=100,
        amount_paid=0,
        payments=[SimpleNamespace(amount=40), SimpleNamespace(amount=60)],
    )
    assert for_month([row], 2025, 4, date(2025, 4, 1)) is Status.PAID


def test_customer_status_combiner_precedence():
    assert combine_statuses([Status.PAID, Status.NONE]) is Status.PAID
    assert combine_statuses([Status.PAID, Status.PENDING]) is Status.PENDING
    assert combine_statuses([Status.PAID, Status.FAILED]) is Status.FAILED
    assert combine_statuses([]) is Status.NONE


@pytest.mark.parametrize(
    "args",
    [
        (1, 101, 0, 1, 0, "installment"),
        (1, 0, 2, 1, 0, "installment"),
        (1.5, 0, 0, 1, 0, "installment"),
        (1, 0, 0, 0, 0, "installment"),
        (1, 0, 0, None, 0, "unsupported"),
    ],
)
def test_calculation_rejects_invalid_inputs(args):
    with pytest.raises((TypeError, ValueError)):
        compute_sale(*args)


def test_schedule_rejects_unknown_due_mode():
    sale = SimpleNamespace(
        sale_type="installment", months=1, financed=50, purchase_date=date(2025, 1, 1)
    )
    with pytest.raises(ValueError, match="due_mode"):
        build(sale, {"due_mode": "purchase_week"})
