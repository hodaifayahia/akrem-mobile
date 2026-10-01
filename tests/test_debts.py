"""Debt book (money owed to / by the shop) and the dashboard's net financial position."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Category, DebtParty
from app.db.session import session_scope
from app.services import auth, customers, debts, finance, products, sales, stock
from app.services.debts import DebtError, DebtTerms, PersonDetails
from app.services.seed import seed_defaults

PASSWORD = "StrongPass123"
TODAY = date(2026, 10, 15)
ALI = PersonDetails("علي", "بن صالح", email="ali@example.com", national_id="109876543210987654", phone="0550123456")


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
def seller_id(session: Session, owner_id: int) -> int:
    return auth.create_seller(
        session, owner_id, username="seller", password=PASSWORD, password_confirmation=PASSWORD
    ).id


def _receivable(session: Session, owner_id: int, person: PersonDetails = ALI, **terms) -> int:
    values = {"amount": 60_000, "debt_date": date(2026, 7, 10)} | terms
    return debts.create_debt(session, owner_id, direction=debts.RECEIVABLE, person=person, terms=DebtTerms(**values)).id


# ------------------------------------------------------------------ people
def test_person_fields_are_validated(session: Session, owner_id: int) -> None:
    for person, code in (
        (PersonDetails("", "x"), "name"),
        (PersonDetails("a", "b", email="not-an-email"), "email"),
        (PersonDetails("a", "b", national_id="12AB34"), "national_id"),
        (PersonDetails("a", "b", phone="123"), "phone"),
    ):
        with pytest.raises(DebtError) as error:
            _receivable(session, owner_id, person)
        assert error.value.code == code


def test_same_national_id_reuses_the_person(session: Session, owner_id: int) -> None:
    first = _receivable(session, owner_id)
    second = _receivable(
        session, owner_id, PersonDetails("علي", "بن صالح", national_id="1098 7654 3210 9876 54", email="new@example.com"),
        amount=5_000,
    )
    assert session.scalar(select(func.count(DebtParty.id))) == 1
    party = debts.get_debt(session, second).party
    assert party.id == debts.get_debt(session, first).party.id and party.email == "new@example.com"
    # Arabic-Indic digits are accepted for the card number.
    third = _receivable(session, owner_id, PersonDetails("سمير", "ب", national_id="١٢٣٤٥٦٧٨٩"), amount=1_000)
    assert debts.get_debt(session, third).party.national_id == "123456789"


def test_only_the_owner_keeps_the_debt_book(session: Session, owner_id: int, seller_id: int) -> None:
    with pytest.raises(auth.AuthorizationError):
        _receivable(session, seller_id)


# ---------------------------------------------------------- plans/payments
def test_debt_paid_back_by_facility_every_two_months(session: Session, owner_id: int) -> None:
    debt_id = _receivable(session, owner_id, plan=debts.PLAN_INSTALLMENTS, months=6, interval=2)
    debt = debts.get_debt(session, debt_id)
    rows = debts.schedule(debt)
    assert [(row.due_date, row.amount) for row in rows] == [
        (date(2026, 9, 1), 20_000), (date(2026, 11, 1), 20_000), (date(2027, 1, 1), 20_000),
    ]
    summary = debts.summarize(debt, TODAY)
    assert (summary.status, summary.overdue_amount, summary.next_due_date) == (debts.STATUS_OVERDUE, 20_000, date(2026, 9, 1))

    debts.record_payment(session, owner_id, debt_id, amount=25_000, payment_date=date(2026, 10, 1))
    summary = debts.summarize(debts.get_debt(session, debt_id), TODAY)
    assert (summary.paid, summary.remaining, summary.overdue_amount) == (25_000, 35_000, 0)
    assert (summary.next_due_date, summary.next_due_amount, summary.status) == (date(2026, 11, 1), 15_000, debts.STATUS_ON_TRACK)
    assert debts.summarize(debts.get_debt(session, debt_id), date(2026, 10, 28)).status == debts.STATUS_DUE_SOON

    with pytest.raises(DebtError) as overpaid:
        debts.record_payment(session, owner_id, debt_id, amount=40_000, payment_date=TODAY)
    assert overpaid.value.code == "overpaid"
    debts.record_payment(session, owner_id, debt_id, amount=35_000, payment_date=TODAY, method="CCP")
    assert debts.summarize(debts.get_debt(session, debt_id), TODAY).status == debts.STATUS_PAID


def test_single_payment_debt_with_and_without_due_date(session: Session, owner_id: int) -> None:
    open_ended = _receivable(session, owner_id, amount=10_000)
    dated = _receivable(session, owner_id, PersonDetails("كريم", "ج"), amount=8_000, due_date=date(2026, 10, 1))
    assert debts.summarize(debts.get_debt(session, open_ended), TODAY).status == debts.STATUS_ON_TRACK
    late = debts.summarize(debts.get_debt(session, dated), TODAY)
    assert (late.status, late.overdue_amount) == (debts.STATUS_OVERDUE, 8_000)
    with pytest.raises(DebtError):
        _receivable(session, owner_id, amount=1_000, due_date=date(2026, 1, 1))  # before the debt date


def test_editing_and_deleting(session: Session, owner_id: int) -> None:
    debt_id = _receivable(session, owner_id)
    payment = debts.record_payment(session, owner_id, debt_id, amount=30_000, payment_date=TODAY)
    with pytest.raises(DebtError) as below:
        debts.update_debt(session, owner_id, debt_id, person=ALI, terms=DebtTerms(20_000, date(2026, 7, 10)))
    assert below.value.code == "below_paid"
    debts.update_debt(
        session, owner_id, debt_id, person=PersonDetails("علي", "صالح", national_id=ALI.national_id),
        terms=DebtTerms(70_000, date(2026, 7, 10), reason="سلفة", plan=debts.PLAN_INSTALLMENTS, months=4),
    )
    summary = debts.summarize(debts.get_debt(session, debt_id), TODAY)
    assert (summary.amount, summary.remaining, summary.last_name, summary.reason) == (70_000, 40_000, "صالح", "سلفة")
    debts.delete_payment(session, owner_id, payment.id)
    assert debts.summarize(debts.get_debt(session, debt_id), TODAY).paid == 0
    debts.delete_debt(session, owner_id, debt_id)
    assert debts.list_debts(session, debts.RECEIVABLE, today=TODAY) == []


def test_lists_totals_filters_and_search(session: Session, owner_id: int) -> None:
    _receivable(session, owner_id, due_date=date(2026, 9, 1))  # overdue
    _receivable(session, owner_id, PersonDetails("سارة", "م", email="sara@shop.dz"), amount=5_000)
    supplier = debts.create_debt(
        session, owner_id, direction=debts.PAYABLE, person=PersonDetails("مورد", "الهواتف"),
        terms=DebtTerms(200_000, date(2026, 9, 1), plan=debts.PLAN_INSTALLMENTS, months=4),
    )
    debts.record_payment(session, owner_id, supplier.id, amount=50_000, payment_date=date(2026, 10, 2))

    owed = debts.list_debts(session, debts.RECEIVABLE, today=TODAY)
    assert [row.full_name for row in owed] == ["علي بن صالح", "سارة م"]  # overdue first
    assert [row.full_name for row in debts.list_debts(session, debts.RECEIVABLE, today=TODAY, search="SHOP.dz")] == ["سارة م"]
    assert [row.full_name for row in debts.list_debts(session, debts.RECEIVABLE, today=TODAY, search="1098")] == ["علي بن صالح"]
    assert len(debts.list_debts(session, debts.RECEIVABLE, today=TODAY, status=debts.STATUS_OVERDUE)) == 1
    mine = debts.totals(session, debts.RECEIVABLE, today=TODAY)
    assert (mine.count, mine.remaining, mine.overdue, mine.overdue_count) == (2, 65_000, 60_000, 1)
    owe = debts.totals(session, debts.PAYABLE, today=TODAY)
    assert (owe.amount, owe.paid, owe.remaining) == (200_000, 50_000, 150_000)
    assert debts.payments_between(session, debts.PAYABLE, date(2026, 10, 1), date(2026, 10, 31)) == 50_000


# ----------------------------------------------------- financial position
def test_financial_position_adds_up_inside_and_outside(session: Session, owner_id: int) -> None:
    category = session.scalar(select(Category).limit(1))
    client = customers.create_customer(session, full_name="زبون", category_id=category.id)
    sales.create_sale(
        session, customer_id=client.id, product="Phone", sale_type="installment", wholesale_price=20_000,
        cash_price=30_000, rate=0, down_payment=6_000, months=4, purchase_date=date(2026, 10, 3),
    )  # 24,000 still owed; 6,000 down payment received this month
    product = products.create_product(session, owner_id, name="Phone X", cash_price=40_000, wholesale_price=35_000)
    stock.add_units(session, owner_id, product_id=product.id, quantity=2)
    debt_id = _receivable(session, owner_id, amount=10_000)
    debts.record_payment(session, owner_id, debt_id, amount=4_000, payment_date=date(2026, 10, 5))
    supplier = debts.create_debt(
        session, owner_id, direction=debts.PAYABLE, person=PersonDetails("مورد", "أ"),
        terms=DebtTerms(50_000, date(2026, 9, 1)),
    )
    debts.record_payment(session, owner_id, supplier.id, amount=15_000, payment_date=date(2026, 10, 9))

    position = finance.financial_position(session, owner_id, year=2026, month=10, today=TODAY)
    assert (position.clients_owe, position.debtors_owe, position.money_inside) == (24_000, 6_000, 30_000)
    assert (position.stock_units, position.stock_value, position.stock_retail_value) == (2, 70_000, 80_000)
    assert position.shop_owes == 35_000
    assert position.net == 30_000 + 70_000 - 35_000
    assert (position.month_sales_received, position.month_debts_received, position.month_paid_out) == (6_000, 4_000, 15_000)
    assert position.month_net == 6_000 + 4_000 - 15_000


def test_financial_position_is_owner_only(session: Session, seller_id: int) -> None:
    with pytest.raises(auth.AuthorizationError):
        finance.financial_position(session, seller_id, year=2026, month=10, today=TODAY)
