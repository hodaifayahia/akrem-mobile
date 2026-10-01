"""Dashboard totals and status counts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Installment, Sale, Setting
from app.services.status import Status, for_month


@dataclass(frozen=True, slots=True)
class DashboardSummary:
    """Values displayed on the dashboard for a selected month."""

    total_wholesale: int | None
    failed_operations: int
    completed_operations: int
    pending_operations: int
    expected_collections: int
    collected_this_month: int
    total_profit: int | None
    remaining_balance: int
    collection_rate: int = 0
    today_collected: int = 0
    today_sales_count: int = 0
    today_profit: int = 0
    healthy_count: int = 0
    overdue_count: int = 0
    monthly_trends: tuple[tuple[str, int, int], ...] = ()
    month_cash_sales: int = 0
    month_installment_sales: int = 0
    month_credit_sales: int = 0
    cash_customers: int = 0
    facility_customers: int = 0


_ARABIC_MONTHS = (
    "", "جانفي", "فيفري", "مارس", "أفريل", "ماي", "جوان",
    "جويلية", "أوت", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"
)


def get_dashboard_summary(
    session: Session,
    *,
    year: int,
    month: int,
    today: date,
    role: str,
) -> DashboardSummary:
    """Calculate dashboard values, withholding owner-only figures from sellers."""
    if not 1 <= month <= 12:
        raise ValueError("month must be between 1 and 12")
    grace_days = _grace_days(session)
    sales = session.scalars(
        select(Sale).options(
            selectinload(Sale.installments).selectinload(Installment.payments),
            selectinload(Sale.credit_payments),
        )
    ).all()

    counts = {Status.FAILED: 0, Status.PAID: 0, Status.PENDING: 0}
    expected = 0
    collected = 0
    # Only installment and credit payments count toward the collection rate;
    # cash sales and down payments were never part of "expected".
    collected_on_schedule = 0
    remaining = 0

    today_collected = 0
    today_sales_count = 0
    today_profit = 0

    # 6-Month Trend Buckets: map of (y, m) -> [expected, collected]
    trend_months: list[tuple[int, int]] = []
    for i in range(5, -1, -1):
        m_index = year * 12 + (month - 1) - i
        y, m0 = divmod(m_index, 12)
        trend_months.append((y, m0 + 1))
    trends_map: dict[tuple[int, int], list[int]] = {ym: [0, 0] for ym in trend_months}

    month_sales_by_type = {"cash": 0, "installment": 0, "credit": 0}
    customer_types: dict[int, set[str]] = {}
    for sale in sales:
        customer_types.setdefault(sale.customer_id, set()).add(sale.sale_type)
        if sale.purchase_date.year == year and sale.purchase_date.month == month:
            month_sales_by_type[sale.sale_type] = month_sales_by_type.get(sale.sale_type, 0) + 1
        sale_status = for_month(sale.installments, year, month, today, grace_days)
        if sale_status in counts:
            counts[sale_status] += 1

        # Today's sales performance
        if sale.purchase_date == today:
            today_sales_count += 1
            today_profit += sale.profit
            if sale.sale_type == "cash":
                today_collected += sale.total
            else:
                today_collected += sale.down_payment

        # Handle credit sales
        if sale.sale_type == "credit":
            credit_payments = sale.credit_payments
            remaining += max(0, sale.financed - sum(payment.amount for payment in credit_payments))
            if (
                sale.expected_pay_date is not None
                and sale.expected_pay_date.year == year
                and sale.expected_pay_date.month == month
            ):
                expected += max(
                    0,
                    sale.financed - sum(payment.amount for payment in credit_payments),
                )
            for payment in credit_payments:
                if payment.payment_date == today:
                    today_collected += payment.amount
                if payment.payment_date.year == year and payment.payment_date.month == month:
                    collected += payment.amount
                    collected_on_schedule += payment.amount
                ym = (payment.payment_date.year, payment.payment_date.month)
                if ym in trends_map:
                    trends_map[ym][1] += payment.amount

            if sale.expected_pay_date is not None:
                exp_ym = (sale.expected_pay_date.year, sale.expected_pay_date.month)
                if exp_ym in trends_map:
                    trends_map[exp_ym][0] += max(
                        0,
                        sale.financed - sum(p.amount for p in credit_payments),
                    )

        # Handle spot collections at purchase date
        sale_ym = (sale.purchase_date.year, sale.purchase_date.month)
        if sale_ym in trends_map:
            if sale.sale_type == "cash":
                trends_map[sale_ym][1] += sale.total
            else:
                trends_map[sale_ym][1] += sale.down_payment

        if sale.purchase_date.year == year and sale.purchase_date.month == month:
            if sale.sale_type == "cash":
                collected += sale.total
            else:
                collected += sale.down_payment

        # Handle installment schedule rows & payments
        for installment in sale.installments:
            remaining += max(
                0,
                installment.amount_due - max(
                    installment.amount_paid,
                    sum(payment.amount for payment in installment.payments),
                ),
            )
            if installment.due_date.year == year and installment.due_date.month == month:
                expected += installment.amount_due
            inst_ym = (installment.due_date.year, installment.due_date.month)
            if inst_ym in trends_map:
                trends_map[inst_ym][0] += installment.amount_due

            for payment in installment.payments:
                if payment.payment_date == today:
                    today_collected += payment.amount
                if payment.payment_date.year == year and payment.payment_date.month == month:
                    collected += payment.amount
                    collected_on_schedule += payment.amount
                p_ym = (payment.payment_date.year, payment.payment_date.month)
                if p_ym in trends_map:
                    trends_map[p_ym][1] += payment.amount

    # Build monthly trend points
    trend_points: list[tuple[str, int, int]] = []
    for ym in trend_months:
        lbl = _ARABIC_MONTHS[ym[1]]
        trend_points.append((lbl, trends_map[ym][0], trends_map[ym][1]))

    # Compute collection rate percentage
    if expected > 0:
        rate_val = min(100, round((collected_on_schedule / expected) * 100))
    elif collected_on_schedule > 0:
        rate_val = 100
    else:
        rate_val = 0

    owner = role == "owner"
    return DashboardSummary(
        total_wholesale=sum(sale.wholesale_price for sale in sales) if owner else None,
        failed_operations=counts[Status.FAILED],
        completed_operations=counts[Status.PAID],
        pending_operations=counts[Status.PENDING],
        expected_collections=expected,
        collected_this_month=collected,
        total_profit=sum(sale.profit for sale in sales) if owner else None,
        remaining_balance=remaining,
        collection_rate=rate_val,
        today_collected=today_collected,
        today_sales_count=today_sales_count,
        today_profit=today_profit if owner else 0,
        healthy_count=counts[Status.PAID],
        overdue_count=counts[Status.FAILED],
        monthly_trends=tuple(trend_points),
        month_cash_sales=month_sales_by_type["cash"],
        month_installment_sales=month_sales_by_type["installment"],
        month_credit_sales=month_sales_by_type["credit"],
        cash_customers=sum(1 for kinds in customer_types.values() if kinds == {"cash"}),
        facility_customers=sum(1 for kinds in customer_types.values() if kinds - {"cash"}),
    )


def _grace_days(session: Session) -> int:
    """Read the grace-days setting, falling back to the documented default."""
    value = session.get(Setting, "grace_days")
    if value is None:
        return 5
    try:
        grace_days = json.loads(value.value)
    except (json.JSONDecodeError, TypeError):
        return 5
    if isinstance(grace_days, bool) or not isinstance(grace_days, int) or grace_days < 0:
        return 5
    return grace_days
