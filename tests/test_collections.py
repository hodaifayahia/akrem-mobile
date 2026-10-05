"""The Payments table's rows: one per installment or credit balance, with filters."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Category, Customer, Installment, Payment, Sale
from app.db.session import session_scope
from app.services import collections, reports, repositories
from app.services.collections import CollectionFilter
from app.services.seed import seed_defaults

TODAY = date(2026, 10, 10)


@pytest.fixture
def session(memory_engine: Engine) -> Iterator[Session]:
    """Seeded session (grace_days = 5)."""
    with session_scope(memory_engine) as scoped:
        seed_defaults(scoped)
        yield scoped


def _customer(session: Session, name: str, phone: str | None = "0550000001", category: str = "أساتذة") -> Customer:
    category_row = session.scalar(select(Category).where(Category.name == category))
    assert category_row is not None
    return repositories.create_customer(session, full_name=name, category_id=category_row.id, phone=phone)


def _sale(session: Session, customer: Customer, *, sale_type: str = "installment", financed: int = 30_000,
          expected_pay_date: date | None = None, product: str = "Phone") -> Sale:
    return repositories.create_sale(
        session, customer_id=customer.id, product=product, sale_type=sale_type, wholesale_price=20_000,
        cash_price=financed, rate=0, down_payment=0, months=3 if sale_type == "installment" else None,
        total=financed, financed=0 if sale_type == "cash" else financed, monthly_amount=None,
        profit=financed - 20_000, purchase_date=date(2026, 6, 1), expected_pay_date=expected_pay_date,
    )


def _installment(session: Session, sale: Sale, due: date, *, amount_paid: int = 0) -> Installment:
    row = Installment(
        sale_id=sale.id, installment_index=len(sale.installments) + 1, due_date=due,
        amount_due=10_000, amount_paid=amount_paid,
    )
    session.add(row)
    session.flush()
    session.refresh(sale)
    return row


def _rows(session: Session) -> list[collections.CollectionRow]:
    session.expire_all()
    return collections.collection_rows(session, today=TODAY)


@pytest.fixture
def shop_rows(session: Session) -> list[collections.CollectionRow]:
    """Ali: late, in grace, due this month and next month; Sara: paid; Omar: credit."""
    ali = _customer(session, "علي بن صالح", "0550000001")
    sale = _sale(session, ali, product="Galaxy A15")
    _installment(session, sale, date(2026, 9, 1))  # 39 days late
    _installment(session, sale, date(2026, 10, 7))  # within the 5 grace days
    _installment(session, sale, date(2026, 10, 25))  # later this month
    _installment(session, sale, date(2026, 11, 1))  # next month
    sara = _customer(session, "سارة", None, category="عسكري")
    paid_sale = _sale(session, sara, product="iPhone 13")
    _installment(session, paid_sale, date(2026, 9, 1), amount_paid=10_000)
    omar = _customer(session, "عمر", "0660000002")
    credit = _sale(session, omar, sale_type="credit", financed=50_000, expected_pay_date=date(2026, 9, 20))
    session.add(Payment(sale_id=credit.id, amount=20_000, payment_date=date(2026, 9, 25), method="cash"))
    session.flush()
    return _rows(session)


def test_row_status_follows_grace_days() -> None:
    assert collections.row_status(date(2026, 10, 5), 1, today=TODAY, grace_days=5) == "due"
    assert collections.row_status(date(2026, 10, 4), 1, today=TODAY, grace_days=5) == "overdue"
    assert collections.row_status(date(2026, 10, 31), 1, today=TODAY, grace_days=5) == "due"
    assert collections.row_status(date(2026, 11, 1), 1, today=TODAY, grace_days=5) == "upcoming"
    assert collections.row_status(date(2026, 9, 1), 0, today=TODAY, grace_days=5) == "paid"
    assert collections.row_status(None, 1, today=TODAY, grace_days=5) == "due"


def test_one_row_per_installment_and_credit_balance(shop_rows) -> None:
    assert [(row.kind, row.status) for row in shop_rows] == [
        ("installment", "overdue"),  # 39 days late comes first
        ("credit", "overdue"),  # expected 20/09 + 5 grace days
        ("installment", "due"),
        ("installment", "due"),
        ("installment", "upcoming"),
        ("installment", "paid"),
    ]
    late = shop_rows[0]
    assert (late.customer_name, late.number, late.count, late.days_late) == ("علي بن صالح", 1, 4, 39)
    credit = shop_rows[1]
    assert (credit.amount_due, credit.paid, credit.remaining) == (50_000, 20_000, 30_000)
    assert credit.last_payment_date == date(2026, 9, 25) and credit.method == "cash"


def test_filters_search_status_kind_category_and_period(shop_rows) -> None:
    def keep(**kwargs) -> list[tuple[str, str]]:
        rows = collections.filter_rows(shop_rows, CollectionFilter(**kwargs), today=TODAY)
        return [(row.customer_name, row.status) for row in rows]

    assert keep(search="سارة") == [("سارة", "paid")]
    assert keep(search="0660") == [("عمر", "overdue")]  # phone
    assert keep(search="galaxy") == [("علي بن صالح", s) for s in ("overdue", "due", "due", "upcoming")]
    assert len(keep(status="unpaid")) == 5
    assert keep(status="paid") == [("سارة", "paid")]
    assert keep(kind="credit") == [("عمر", "overdue")]
    teachers = shop_rows[0].category_id
    assert {name for name, _ in keep(category_id=teachers)} == {"علي بن صالح", "عمر"}
    assert len(keep(period="until_this_month")) == 5  # everything but November
    assert keep(period="this_month") == [("علي بن صالح", "due"), ("علي بن صالح", "due")]
    assert keep(period="next_month") == [("علي بن صالح", "upcoming")]
    assert keep(period="month", month=date(2026, 9, 15)) == [
        ("علي بن صالح", "overdue"), ("عمر", "overdue"), ("سارة", "paid"),
    ]
    with pytest.raises(ValueError):
        collections.filter_rows(shop_rows, CollectionFilter(period="month"), today=TODAY)


def test_totals_count_left_to_collect_and_collected(shop_rows) -> None:
    summary = collections.totals(shop_rows)
    assert (summary.overdue_count, summary.overdue_amount) == (2, 10_000 + 30_000)
    assert (summary.due_count, summary.due_amount) == (2, 20_000)
    assert (summary.upcoming_count, summary.paid_count, summary.paid_amount) == (1, 1, 10_000)
    assert summary.remaining == 70_000


def test_payments_export_writes_the_listed_rows(shop_rows, tmp_path) -> None:
    path = reports.export_collections_xlsx(shop_rows[:2], tmp_path / "payments.xlsx")
    sheet = load_workbook(path).active
    rows = list(sheet.iter_rows(values_only=True))
    assert len(rows) == 3
    assert rows[1][1] == "علي بن صالح" and rows[2][1] == "عمر"
    assert rows[2][8] == 30_000  # remaining
