"""Flexible installment plans: duration, payment every N months, product defaults."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Category, Installment, Product, Sale
from app.db.session import session_scope
from app.services import alerts, auth, calc, customers, products, sales, schedule
from app.services.products import ProductPlan
from app.services.seed import seed_defaults
from app.services.status import Status, for_month

PASSWORD = "StrongPass123"
PRESETS = {4: 35, 5: 35, 6: 35, 7: 35, 10: 40, 12: 45}


@pytest.fixture
def session(memory_engine: Engine) -> Iterator[Session]:
    with session_scope(memory_engine) as scoped:
        seed_defaults(scoped)
        yield scoped


@pytest.fixture
def owner_id(session: Session) -> int:
    return auth.create_first_owner(
        session, username="owner", password=PASSWORD, password_confirmation=PASSWORD
    ).id


@pytest.fixture
def customer_id(session: Session) -> int:
    category = session.scalar(select(Category).limit(1))
    return customers.create_customer(
        session, full_name="سمير بن علي", category_id=category.id, phone="0550000001"
    ).id


def _rows(session: Session, sale: Sale) -> list[Installment]:
    return list(session.scalars(
        select(Installment).where(Installment.sale_id == sale.id).order_by(Installment.installment_index)
    ))


# ------------------------------------------------------------ calculation
@pytest.mark.parametrize("months, interval, offsets", [
    (6, 1, [1, 2, 3, 4, 5, 6]),
    (6, 2, [2, 4, 6]),
    (6, 3, [3, 6]),
    (6, 6, [6]),
    (5, 2, [2, 4, 5]),
    (12, 5, [5, 10, 12]),
    (1, 1, [1]),
    (3, 1, [1, 2, 3]),
])
def test_payments_fall_every_interval_and_the_last_at_the_end(months, interval, offsets) -> None:
    assert calc.payment_offsets(months, interval) == offsets


@pytest.mark.parametrize("months, interval", [(0, 1), (6, 0), (6, 7), (61, 1), (6, True)])
def test_invalid_plans_are_rejected(months, interval) -> None:
    with pytest.raises(ValueError):
        calc.payment_offsets(months, interval)


def test_amount_is_split_per_payment_with_remainder_last() -> None:
    result = calc.compute_sale(
        cash_price=100_001, rate=0, down_payment=0, months=6, wholesale=0,
        sale_type="installment", purchase_date=date(2026, 1, 15), interval=2,
    )
    assert result.monthly_list == [33_333, 33_333, 33_335]
    assert result.payment_count == 3 and result.payment_interval == 2
    assert result.end_date == date(2026, 7, 15)  # still purchase date + months


def test_schedule_due_dates_follow_the_interval() -> None:
    sale = type("S", (), dict(
        sale_type="installment", months=6, payment_interval=2, financed=90_000,
        purchase_date=date(2026, 1, 20),
    ))()
    rows = schedule.build(sale, {"due_mode": "first_of_month"})
    assert [(r.due_date, r.amount_due) for r in rows] == [
        (date(2026, 3, 1), 30_000), (date(2026, 5, 1), 30_000), (date(2026, 7, 1), 30_000),
    ]
    by_day = schedule.build(sale, {"due_mode": "purchase_day"})
    assert [r.due_date for r in by_day] == [date(2026, 3, 20), date(2026, 5, 20), date(2026, 7, 20)]


# ------------------------------------------------------------------- sales
def test_installment_sale_every_two_months(session: Session, customer_id: int) -> None:
    sale = sales.create_sale(
        session, customer_id=customer_id, product="iPhone 13", sale_type="installment",
        wholesale_price=90_000, cash_price=110_000, rate=35, down_payment=0, months=6,
        payment_interval=2, purchase_date=date(2026, 1, 10),
    )
    rows = _rows(session, sale)
    assert sale.payment_interval == 2
    assert [(row.installment_index, row.due_date, row.amount_due) for row in rows] == [
        (1, date(2026, 3, 1), 49_500), (2, date(2026, 5, 1), 49_500), (3, date(2026, 7, 1), 49_500),
    ]
    assert sale.monthly_amount == 49_500
    assert sale.end_date == date(2026, 7, 10)


def test_one_month_plan_is_allowed(session: Session, customer_id: int) -> None:
    sale = sales.create_sale(
        session, customer_id=customer_id, product="Cable", sale_type="installment",
        wholesale_price=500, cash_price=1_000, rate=10, months=1, purchase_date=date(2026, 1, 10),
    )
    assert [(r.due_date, r.amount_due) for r in _rows(session, sale)] == [(date(2026, 2, 1), 1_100)]


def test_cash_and_credit_sales_are_always_interval_one(session: Session, customer_id: int) -> None:
    sale = sales.create_sale(
        session, customer_id=customer_id, product="Cable", sale_type="cash",
        wholesale_price=500, cash_price=1_000, payment_interval=3,
    )
    assert sale.payment_interval == 1


def test_interval_longer_than_plan_is_refused(session: Session, customer_id: int) -> None:
    with pytest.raises(ValueError):
        sales.create_sale(
            session, customer_id=customer_id, product="X", sale_type="installment",
            wholesale_price=0, cash_price=1_000, months=3, payment_interval=4,
        )


def test_editing_the_interval_rebuilds_the_schedule(session: Session, customer_id: int) -> None:
    sale = sales.create_sale(
        session, customer_id=customer_id, product="X", sale_type="installment",
        wholesale_price=0, cash_price=60_000, rate=0, months=6, purchase_date=date(2026, 1, 10),
    )
    assert len(_rows(session, sale)) == 6
    sales.update_sale(session, sale.id, payment_interval=3)
    rows = _rows(session, sale)
    assert [(r.due_date, r.amount_due) for r in rows] == [(date(2026, 4, 1), 30_000), (date(2026, 7, 1), 30_000)]
    assert sale.monthly_amount == 30_000


def test_status_and_alerts_only_in_payment_months(session: Session, customer_id: int) -> None:
    """Every-2-months clients are due (and notified) only in their payment months."""
    sale = sales.create_sale(
        session, customer_id=customer_id, product="X", sale_type="installment",
        wholesale_price=0, cash_price=40_000, rate=0, months=4, payment_interval=2,
        purchase_date=date(2026, 1, 10),
    )
    rows = _rows(session, sale)
    assert for_month(rows, 2026, 2, date(2026, 2, 10)) == Status.NONE      # nothing due in February
    assert for_month(rows, 2026, 3, date(2026, 3, 2)) == Status.PENDING    # due 1 March
    assert for_month(rows, 2026, 3, date(2026, 3, 20)) == Status.FAILED    # unpaid after grace
    assert alerts.collection_alerts(session, today=date(2026, 2, 3)) == []
    due_soon = alerts.collection_alerts(session, today=date(2026, 2, 25))
    assert [(a.due_date, a.amount) for a in due_soon] == [(date(2026, 3, 1), 20_000)]


# ---------------------------------------------------------------- products
def test_products_carry_a_default_plan(session: Session, owner_id: int) -> None:
    default = products.create_product(session, owner_id, name="Redmi", cash_price=30_000)
    custom = products.create_product(
        session, owner_id, name="iPhone", cash_price=110_000, plan=ProductPlan(months=10, interval=2),
    )
    own_rate = products.create_product(
        session, owner_id, name="Galaxy", cash_price=21_000, plan=ProductPlan(months=3, interval=1, rate=20),
    )
    no_rate = products.create_product(session, owner_id, name="Nokia", cash_price=5_000, plan=ProductPlan(months=3))

    assert products.plan_of(default) == ProductPlan(6, 1, None)
    entries = {entry.name: entry for entry in products.catalog_entries(session, PRESETS)}
    assert entries["Redmi"][3:] == (6, 1, 35)
    assert entries["iPhone"][3:] == (10, 2, 40)
    assert entries["Galaxy"][3:] == (3, 1, 20)
    assert entries["Nokia"][3:] == (3, 1, None)  # no preset for 3 months
    assert products.plan_of(custom).payment_count() == 5
    assert products.plan_of(no_rate).resolved_rate(PRESETS) is None
    assert own_rate.default_rate == 20


@pytest.mark.parametrize("plan", [ProductPlan(0, 1), ProductPlan(6, 7), ProductPlan(6, 1, 101), ProductPlan(61, 1)])
def test_invalid_product_plans_are_refused(session: Session, owner_id: int, plan: ProductPlan) -> None:
    with pytest.raises(ValueError):
        products.create_product(session, owner_id, name="X", cash_price=1_000, plan=plan)


def test_update_keeps_plan_unless_given(session: Session, owner_id: int) -> None:
    product = products.create_product(
        session, owner_id, name="X", cash_price=1_000, plan=ProductPlan(months=4, interval=2, rate=30),
    )
    products.update_product(session, owner_id, product.id, name="X", cash_price=1_200)
    assert products.plan_of(product) == ProductPlan(4, 2, 30)
    products.update_product(session, owner_id, product.id, name="X", cash_price=1_200, plan=ProductPlan(12, 3))
    assert products.plan_of(product) == ProductPlan(12, 3, None)


def test_product_sheet_can_set_plans(tmp_path: Path, session: Session, owner_id: int) -> None:
    products.create_product(session, owner_id, name="iPhone 13", cash_price=110_000, wholesale_price=90_000)
    path = tmp_path / "plans.xlsx"
    workbook = Workbook()
    for row in (
        ["اسم المنتج", "السعر", "مدة التقسيط بالأشهر", "الدفع كل كم شهر", "نسبة التقسيط %"],
        ["iPhone 13", 110_000, 10, 2, None],     # same price, new plan -> update
        ["Galaxy A05", 21_000, 3, None, "20%"],  # new with own rate
        ["Bad", 5_000, 3, 4, None],              # pays every 4 months in a 3-month plan
        ["Bad 2", 5_000, 0, None, None],
    ):
        workbook.active.append(row)
    workbook.save(path)

    preview = products.preview_product_workbook(session, path)
    rows = {row.name: row for row in preview.rows}
    assert rows["iPhone 13"].action == products.ACTION_UPDATE
    assert rows["Galaxy A05"].action == products.ACTION_CREATE and rows["Galaxy A05"].rate == 20
    assert rows["Bad"].errors == (products.ERROR_INTERVAL_INVALID,)
    assert rows["Bad 2"].errors == (products.ERROR_MONTHS_INVALID,)

    result = products.import_products(session, owner_id, preview.rows)
    assert (result.created, result.updated, result.skipped) == (1, 1, 2)
    catalog = {p.name: p for p in session.scalars(select(Product))}
    assert products.plan_of(catalog["iPhone 13"]) == ProductPlan(10, 2, None)
    assert products.plan_of(catalog["Galaxy A05"]) == ProductPlan(3, 1, 20)
