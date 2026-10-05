"""Owner requests of 2026-10-03: a basket of products, one client-list row per sale,
fresh product prices in stock, the stock quantity on the product, and "my net money"."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Category, Sale
from app.db.session import session_scope
from app.services import auth, customers, debts, finance, payments, products, sales, stock
from app.services.seed import seed_defaults

PASSWORD = "StrongPass123"
TODAY = date(2026, 10, 3)


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


def _customer(session: Session, name: str = "زبون") -> int:
    category = session.scalar(select(Category).limit(1))
    return customers.create_customer(session, full_name=name, category_id=category.id).id


def _product(session: Session, owner_id: int, name: str, units: int, cash: int = 30000) -> int:
    product = products.create_product(session, owner_id, name=name, cash_price=cash, wholesale_price=cash * 4 // 5)
    if units:
        stock.add_units(session, owner_id, product_id=product.id, quantity=units)
    return product.id


def _line(name: str, sale_type: str = "installment", cash: int = 30000, **extra: object) -> dict:
    values = {
        "product": name, "sale_type": sale_type, "wholesale_price": cash * 4 // 5, "cash_price": cash,
        "rate": 35 if sale_type == "installment" else 0, "down_payment": 0,
        "months": 6 if sale_type == "installment" else None, "purchase_date": TODAY,
    }
    values.update(extra)
    return values


# ------------------------------------------------------------------ basket
def test_a_basket_saves_one_sale_per_product(session: Session, owner_id: int) -> None:
    phone = _product(session, owner_id, "13 PRO 2SIM 128GB", 3, cash=90000)
    case = _product(session, owner_id, "Case", 5, cash=1500)
    client = _customer(session)

    saved = sales.create_sales_batch(session, owner_id, customer_id=client, lines=[
        _line("13 PRO 2SIM 128GB", cash=90000),
        _line("Case", "cash", cash=3000, quantity=2),
    ])

    assert [sale.product for sale in saved] == ["13 PRO 2SIM 128GB", "Case"]
    assert [sale.sale_type for sale in saved] == ["installment", "cash"]
    assert len(saved[0].installments) == 6 and saved[1].installments == []
    assert len(stock.available_units(session, phone)) == 2
    assert len(stock.available_units(session, case)) == 3


def test_a_picked_phone_is_kept_for_its_line(session: Session, owner_id: int) -> None:
    product_id = _product(session, owner_id, "SAM A07", 2)
    oldest, newest = stock.available_units(session, product_id)
    client = _customer(session)

    # The first line takes "any" phone; the second picked the oldest one.
    first, second = sales.create_sales_batch(session, owner_id, customer_id=client, lines=[
        _line("SAM A07"), _line("SAM A07", stock_item_id=oldest.id),
    ])

    assert second.stock_items[0].id == oldest.id
    assert first.stock_items[0].id == newest.id


def test_a_failing_line_saves_nothing(session: Session, owner_id: int) -> None:
    product_id = _product(session, owner_id, "SAM A07", 1)
    client = _customer(session)

    with pytest.raises(stock.StockError):
        sales.create_sales_batch(session, owner_id, customer_id=client, lines=[
            _line("SAM A07"), _line("SAM A07", quantity=2),
        ])

    assert session.scalars(select(Sale)).all() == []
    assert len(stock.available_units(session, product_id)) == 1


def test_the_same_phone_cannot_be_in_the_basket_twice(session: Session, owner_id: int) -> None:
    product_id = _product(session, owner_id, "SAM A07", 2)
    unit = stock.available_units(session, product_id)[0]
    client = _customer(session)

    with pytest.raises(stock.StockError):
        sales.create_sales_batch(session, owner_id, customer_id=client, lines=[
            _line("SAM A07", stock_item_id=unit.id), _line("SAM A07", stock_item_id=unit.id),
        ])
    with pytest.raises(ValueError):
        sales.create_sales_batch(session, owner_id, customer_id=client, lines=[])


# ------------------------------------------------------------------ one row per sale
def test_the_client_list_shows_one_row_per_purchase(session: Session, owner_id: int) -> None:
    _product(session, owner_id, "SAM A07", 0)
    buyer = _customer(session, "أحمد")
    _customer(session, "بدون مشتريات")
    sales.create_sales_batch(session, owner_id, customer_id=buyer, lines=[
        _line("SAM A07"), _line("Redmi 13C", "credit", cash=20000, down_payment=5000),
    ])
    sales.create_sales_batch(session, owner_id, customer_id=buyer, lines=[_line("Case", "cash", cash=1500)])

    merged = customers.list_customers(session, year=2026, month=10, today=TODAY)
    rows = customers.list_customers(session, year=2026, month=10, today=TODAY, per_sale=True)

    assert len(merged) == 2
    assert len(rows) == 3
    buyer_rows = [row for row in rows if row.id == buyer]
    # The two products bought together share a row; the later purchase has its own.
    assert [row.products for row in buyer_rows] == [("SAM A07", "Redmi 13C"), ("Case",)]
    assert all(row.sale_id == row.sales[0].id for row in buyer_rows)
    assert buyer_rows[0].sale_types == ("installment", "credit")
    assert sum(row.remaining_balance for row in buyer_rows) == merged[0].remaining_balance
    empty = next(row for row in rows if row.id != buyer)
    assert empty.sale_id is None and empty.sales == ()


def test_a_basket_shares_one_basket_id_and_a_single_sale_has_none(session: Session, owner_id: int) -> None:
    buyer = _customer(session)
    first, second = sales.create_sales_batch(session, owner_id, customer_id=buyer, lines=[
        _line("SAM A07"), _line("Redmi 13C", "cash", cash=20000),
    ])
    (alone,) = sales.create_sales_batch(session, owner_id, customer_id=buyer, lines=[_line("Case", "cash")])

    assert first.basket_id is not None and first.basket_id == second.basket_id
    assert alone.basket_id is None


def test_per_sale_filters_and_search_look_at_each_purchase(session: Session, owner_id: int) -> None:
    buyer = _customer(session, "أحمد")
    sales.create_sales_batch(session, owner_id, customer_id=buyer, lines=[
        _line("SAM A07"), _line("Redmi 13C", "cash", cash=20000),
    ])
    sales.create_sales_batch(session, owner_id, customer_id=buyer, lines=[_line("Case", "cash", cash=1500)])

    cash_rows = customers.list_customers(
        session, year=2026, month=10, today=TODAY, per_sale=True, payment_kind="cash"
    )
    by_product = customers.list_customers(
        session, year=2026, month=10, today=TODAY, per_sale=True, search="redmi"
    )
    by_name = customers.list_customers(
        session, year=2026, month=10, today=TODAY, per_sale=True, search="أحمد"
    )

    assert [row.products for row in cash_rows] == [("Case",)]
    # A search that matches one product of a basket shows the whole basket row.
    assert [row.products for row in by_product] == [("SAM A07", "Redmi 13C")]
    assert len(by_name) == 2


# ------------------------------------------------------------------ fresh prices
def test_editing_a_product_price_updates_its_phones_in_stock(session: Session, owner_id: int) -> None:
    product_id = _product(session, owner_id, "SAM A07", 2, cash=30000)
    own_price = stock.add_units(session, owner_id, product_id=product_id, cash_price=27000)[0]
    client = _customer(session)
    sales.create_sale_for_user(session, owner_id, customer_id=client, **_line("SAM A07", "cash"))

    products.update_product(
        session, owner_id, product_id, name="SAM A07", cash_price=32000, wholesale_price=25000
    )

    units = stock.available_units(session, product_id)
    assert len(units) == 2  # the cash sale took the oldest phone
    assert {unit.cash_price for unit in units if unit.id != own_price.id} == {32000}
    assert {unit.wholesale_price for unit in units if unit.id != own_price.id} == {25000}
    assert session.get(type(own_price), own_price.id).cash_price == 27000
    sold = session.scalars(select(Sale)).one().stock_items[0]
    assert sold.cash_price == 30000  # history keeps the price it was sold at


# ------------------------------------------------------------------ quantity on the product
def test_setting_the_available_quantity_adds_or_removes_phones(session: Session, owner_id: int) -> None:
    product_id = _product(session, owner_id, "SAM A07", 2)
    with_imei = stock.add_units(
        session, owner_id, product_id=product_id, details=stock.UnitDetails(imei="356789012345678")
    )[0]

    assert stock.set_available_quantity(session, owner_id, product_id=product_id, quantity=5) == (2, 0)
    assert len(stock.available_units(session, product_id)) == 5
    assert stock.set_available_quantity(session, owner_id, product_id=product_id, quantity=1) == (0, 4)
    remaining = stock.available_units(session, product_id)
    assert [unit.id for unit in remaining] == [with_imei.id]  # the identified phone is the last to go
    assert stock.set_available_quantity(session, owner_id, product_id=product_id, quantity=1) == (0, 0)
    with pytest.raises(ValueError):
        stock.set_available_quantity(session, owner_id, product_id=product_id, quantity=-1)


def test_only_the_owner_sets_the_quantity(session: Session, owner_id: int) -> None:
    product_id = _product(session, owner_id, "SAM A07", 1)
    seller = auth.create_user(
        session, owner_id, username="seller", password=PASSWORD, password_confirmation=PASSWORD, role="seller"
    )
    with pytest.raises(auth.AuthorizationError):
        stock.set_available_quantity(session, seller.id, product_id=product_id, quantity=3)


# ------------------------------------------------------------------ my net money
def test_net_money_is_what_people_owe_me_minus_what_i_owe(session: Session, owner_id: int) -> None:
    _product(session, owner_id, "SAM A07", 3)
    client = _customer(session)
    installment, credit = sales.create_sales_batch(session, owner_id, customer_id=client, lines=[
        _line("SAM A07", cash=30000, rate=0, months=3),
        _line("Redmi 13C", "credit", cash=20000, down_payment=5000),
    ])
    payments.record_credit_payment_for_user(session, owner_id, credit.id, 3000, TODAY, "cash", None)
    person = debts.PersonDetails(first_name="Ali", last_name="B")
    debts.create_debt(session, owner_id, direction=debts.RECEIVABLE, person=person,
                      terms=debts.DebtTerms(amount=7000, debt_date=TODAY))
    debts.create_debt(session, owner_id, direction=debts.PAYABLE, person=debts.PersonDetails("Sup", "Plier"),
                      terms=debts.DebtTerms(amount=10000, debt_date=TODAY))

    position = finance.financial_position(session, owner_id, year=2026, month=10, today=TODAY)

    assert (position.installments_owe, position.credit_owe) == (30000, 12000)
    assert position.clients_owe == 42000
    assert position.net_money == 30000 + 12000 + 7000 - 10000
    assert position.net == position.net_money + position.stock_value
