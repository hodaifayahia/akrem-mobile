"""Focused tests for calculation, schedule, and monthly status rules."""

from datetime import date
from types import SimpleNamespace

import pytest

from app.services.calc import compute_sale, share_basket_plan, split_amount
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


def test_installment_schedule_defaults_to_the_purchase_day():
    sale = SimpleNamespace(sale_type="installment", months=1, financed=1_000, purchase_date=date(2026, 2, 2))
    assert [row.due_date for row in build(sale, {})] == [date(2026, 3, 2)]
    assert build(sale, None) == build(sale, {"due_mode": "purchase_day"})


def test_first_of_month_schedule_uses_first_of_following_months():
    sale = SimpleNamespace(
        sale_type="installment",
        months=3,
        financed=69_500,
        purchase_date=date(2025, 1, 31),
    )
    rows = build(sale, {"due_mode": "first_of_month"})
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


# ------------------------------------------------- products bought together
def test_split_amount_adds_up_exactly():
    assert split_amount(67_500, [90_000, 30_000]) == [50_625, 16_875]
    assert sum(split_amount(99, [3, 5, 7])) == 99
    assert split_amount(10, [1, 1, 1]) == [4, 3, 3]  # the remainder goes to the first share
    assert split_amount(0, [5, 5]) == [0, 0]
    assert split_amount(7, []) == []
    assert split_amount(7, [0, 0]) == [7, 0]  # nothing to go by: the first line takes it


def test_two_products_bought_together_share_one_plan_and_one_percentage():
    lines = [
        {"sale_type": "installment", "cash_price": 90_000, "rate": 35, "down_payment": 5_000,
         "months": 6, "payment_interval": 1},
        {"sale_type": "installment", "cash_price": 30_000, "rate": 50, "down_payment": 5_000,
         "months": 12, "payment_interval": 3},
    ]

    share_basket_plan(lines)

    # One rate, one duration and one rhythm for the whole visit (the last line's plan).
    assert [(line["rate"], line["months"], line["payment_interval"]) for line in lines] == [
        (50, 12, 3), (50, 12, 3),
    ]
    # The percentage is applied once, to the combined amount, and split between the lines.
    combined = compute_sale(120_000, 50, 0, 12, 0, "installment", interval=3)
    assert sum(line["total"] for line in lines) == combined.total == 180_000
    assert [line["total"] for line in lines] == [135_000, 45_000]
    # A down payment typed once is charged once, so the financed amount is right too.
    assert sum(line["down_payment"] for line in lines) == 5_000
    assert sum(line["total"] - line["down_payment"] for line in lines) == 175_000


def test_one_product_bought_on_its_own_keeps_its_own_plan():
    lines = [{"sale_type": "installment", "cash_price": 30_000, "rate": 35, "down_payment": 2_000,
              "months": 6, "payment_interval": 1}]
    cash = [{"sale_type": "cash", "cash_price": 1_500}]

    share_basket_plan(lines)
    share_basket_plan(cash)
    share_basket_plan([*lines, *cash])  # a cash line changes nothing for the single installment line

    assert lines == [{"sale_type": "installment", "cash_price": 30_000, "rate": 35, "down_payment": 2_000,
                      "months": 6, "payment_interval": 1}]
    assert cash == [{"sale_type": "cash", "cash_price": 1_500}]


def test_an_incomplete_price_leaves_every_line_to_its_own_calculation():
    lines = [
        {"sale_type": "installment", "cash_price": 30_000, "rate": 35, "down_payment": 0, "months": 6},
        {"sale_type": "installment", "cash_price": 0, "rate": 50, "down_payment": 0, "months": 12},
    ]

    share_basket_plan(lines)

    assert "total" not in lines[0] and "total" not in lines[1]
    assert [line["months"] for line in lines] == [6, 12]


def test_a_shared_total_drives_the_installment_amounts():
    result = compute_sale(30_000, 35, 0, 12, 0, "installment", interval=3, total_override=33_750)

    assert (result.total, result.financed) == (33_750, 33_750)
    assert result.payment_count == 4 and sum(result.monthly_list) == 33_750
