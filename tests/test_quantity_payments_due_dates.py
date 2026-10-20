"""Owner requests of 2026-10-02: stock quantity, free-amount payments, due dates on the purchase day."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Category, Installment, Payment, Sale, StockItem
from app.db.session import session_scope
from app.services import auth, customers, payments, products, sales, schedule, settings, stock
from app.services.seed import seed_defaults

PASSWORD = "StrongPass123"


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


def _stocked_product(session: Session, owner_id: int, count: int = 5) -> int:
    product = products.create_product(
        session, owner_id, name="SAM A07 4/64", cash_price=30000, wholesale_price=24000
    )
    stock.add_units(session, owner_id, product_id=product.id, quantity=count)
    return product.id


def _available(session: Session, product_id: int) -> int:
    return len(stock.available_units(session, product_id))


# ------------------------------------------------------------------ quantity
def test_selling_a_quantity_takes_that_many_phones_out_of_stock(session: Session, owner_id: int) -> None:
    product_id = _stocked_product(session, owner_id, 5)
    client = _customer(session)

    sale = sales.create_sale_for_user(
        session, owner_id, customer_id=client, product="SAM A07 4/64", sale_type="cash",
        wholesale_price=48000, cash_price=60000, quantity=2,
    )
    assert sale.quantity == 2
    assert _available(session, product_id) == 3
    assert {unit.sale_id for unit in session.scalars(select(StockItem).where(StockItem.status == "sold"))} == {sale.id}

    # No phone picked: the oldest one in stock is taken, so the count still goes down.
    sales.create_sale_for_user(
        session, owner_id, customer_id=client, product="SAM A07 4/64", sale_type="installment",
        wholesale_price=24000, cash_price=30000, rate=35, months=6,
    )
    assert _available(session, product_id) == 2


def test_picked_phone_is_sold_first_then_the_oldest_ones(session: Session, owner_id: int) -> None:
    product_id = _stocked_product(session, owner_id, 4)
    units = stock.available_units(session, product_id)
    picked = units[-1]

    sale = sales.create_sale(
        session, customer_id=_customer(session), product="SAM A07 4/64", sale_type="cash",
        wholesale_price=48000, cash_price=60000, stock_item_id=picked.id, quantity=2,
    )
    sold = {unit.id for unit in sale.stock_items}
    assert sold == {picked.id, units[0].id}
    assert sale.reference == picked.reference


def test_more_phones_than_in_stock_is_refused(session: Session, owner_id: int) -> None:
    product_id = _stocked_product(session, owner_id, 2)
    with pytest.raises(stock.StockError) as error:
        sales.create_sale(
            session, customer_id=_customer(session), product="SAM A07 4/64", sale_type="cash",
            wholesale_price=72000, cash_price=90000, quantity=3,
        )
    assert error.value.code == "not_enough"
    assert _available(session, product_id) == 2


def test_typed_imei_outside_stock_still_takes_a_phone_of_the_product(session: Session, owner_id: int) -> None:
    """Owner's request 2026-10-20: every sale leaves the stock, even with an IMEI typed wrong."""
    product = products.create_product(session, owner_id, name="SAM A07 4/64", cash_price=30000)
    recorded = stock.add_units(
        session, owner_id, product_id=product.id, details=stock.UnitDetails(imei="356789012345600")
    )[0]
    sale = sales.create_sale(
        session, customer_id=_customer(session), product="SAM A07 4/64", sale_type="cash",
        wholesale_price=24000, cash_price=30000, imei="356789012345678",
    )
    assert _available(session, product.id) == 0
    assert recorded.sale_id == sale.id and recorded.imei == "356789012345600"
    assert sale.imei == "356789012345678"  # what was typed stays on the sale


def test_typed_imei_takes_a_phone_entered_without_imei_and_records_it(session: Session, owner_id: int) -> None:
    product_id = _stocked_product(session, owner_id, 2)  # stock entered by quantity: no IMEIs
    sale = sales.create_sale(
        session, customer_id=_customer(session), product="SAM A07 4/64", sale_type="credit",
        wholesale_price=24000, cash_price=30000, imei="356789012345678",
    )
    assert _available(session, product_id) == 1
    (unit,) = sale.stock_items
    assert unit.imei == "356789012345678" and sale.imei == "356789012345678"


def test_deleting_a_sale_returns_all_its_phones(session: Session, owner_id: int) -> None:
    product_id = _stocked_product(session, owner_id, 5)
    sale = sales.create_sale(
        session, customer_id=_customer(session), product="SAM A07 4/64", sale_type="cash",
        wholesale_price=72000, cash_price=90000, quantity=3,
    )
    assert _available(session, product_id) == 2
    sales.delete_sales(session, owner_id, [sale.id])
    assert _available(session, product_id) == 5


def test_seller_price_is_the_catalog_price_times_the_quantity(session: Session, owner_id: int) -> None:
    _stocked_product(session, owner_id, 5)
    seller_id = auth.create_seller(
        session, owner_id, username="seller", password=PASSWORD, password_confirmation=PASSWORD
    ).id
    sale = sales.create_sale_for_user(
        session, seller_id, customer_id=_customer(session), product="SAM A07 4/64", sale_type="cash",
        wholesale_price=0, cash_price=0, quantity=3,
    )
    assert (sale.cash_price, sale.wholesale_price, sale.total) == (90000, 72000, 90000)


# ------------------------------------------------------- free-amount payments
def _facility(session: Session, financed: int = 50000, months: int = 5) -> Sale:
    return sales.create_sale(
        session, customer_id=_customer(session), product="Redmi 13", sale_type="installment",
        wholesale_price=30000, cash_price=financed, rate=0, months=months,
        purchase_date=date(2026, 2, 2),
    )


def _paid(session: Session, sale: Sale) -> list[int]:
    rows = session.scalars(
        select(Installment).where(Installment.sale_id == sale.id).order_by(Installment.installment_index)
    )
    return [row.amount_paid for row in rows]


def test_client_pays_any_amount_oldest_installment_first(session: Session) -> None:
    sale = _facility(session)  # 5 × 10,000
    assert payments.installment_sale_remaining(session, sale.id) == 50000

    first = payments.record_sale_payment(session, sale.id, 20000, date(2026, 3, 2), "cash")
    assert [part.amount for part in first] == [10000, 10000]
    assert len({part.batch_id for part in first}) == 1
    assert _paid(session, sale) == [10000, 10000, 0, 0, 0]

    # Nothing the next month, then 10,000, then a smaller amount.
    payments.record_sale_payment(session, sale.id, 10000, date(2026, 5, 2), "CCP")
    payments.record_sale_payment(session, sale.id, 4000, date(2026, 6, 2), "BaridiMob")
    assert _paid(session, sale) == [10000, 10000, 10000, 4000, 0]
    assert payments.installment_sale_remaining(session, sale.id) == 16000

    payments.record_sale_payment(session, sale.id, 7000, date(2026, 7, 2), "cash")
    assert _paid(session, sale) == [10000, 10000, 10000, 10000, 1000]
    paid_rows = session.scalars(select(Installment).where(Installment.sale_id == sale.id)).all()
    assert [row.paid_date for row in sorted(paid_rows, key=lambda row: row.installment_index)][:4] == [
        date(2026, 3, 2), date(2026, 3, 2), date(2026, 5, 2), date(2026, 7, 2),
    ]


def test_payment_above_the_sale_balance_is_refused(session: Session) -> None:
    sale = _facility(session)
    with pytest.raises(ValueError, match="remaining sale balance"):
        payments.record_sale_payment(session, sale.id, 50001, date(2026, 3, 2), "cash")
    payments.record_sale_payment(session, sale.id, 50000, date(2026, 3, 2), "cash")
    with pytest.raises(ValueError, match="already fully paid"):
        payments.record_sale_payment(session, sale.id, 1, date(2026, 3, 3), "cash")


def test_undo_removes_every_part_of_a_spread_payment(session: Session, owner_id: int) -> None:
    sale = _facility(session)
    payments.record_sale_payment(session, sale.id, 5000, date(2026, 3, 2), "cash")
    payments.record_sale_payment(session, sale.id, 25000, date(2026, 4, 2), "cash")
    assert _paid(session, sale) == [10000, 10000, 10000, 0, 0]

    audit = payments.undo_last_payment(session, owner_id, "مبلغ خاطئ")
    assert json.loads(audit.details)["amount"] == 25000
    assert _paid(session, sale) == [5000, 0, 0, 0, 0]
    assert session.scalar(select(AuditLog).where(AuditLog.action == "undo_payment")) is not None
    assert len(session.scalars(select(Payment)).all()) == 1


# ------------------------------------------------------------------ due dates
def test_installments_fall_due_on_the_purchase_day(session: Session) -> None:
    sale = _facility(session, financed=30000, months=1)
    rows = session.scalars(select(Installment).where(Installment.sale_id == sale.id)).all()
    assert [row.due_date for row in rows] == [date(2026, 3, 2)]

    monthly = _facility(session, financed=30000, months=3)
    dues = session.scalars(
        select(Installment.due_date).where(Installment.sale_id == monthly.id).order_by(Installment.installment_index)
    ).all()
    assert dues == [date(2026, 3, 2), date(2026, 4, 2), date(2026, 5, 2)]
    assert schedule.DEFAULT_DUE_MODE == "purchase_day"
    assert settings.get_value(session, "due_mode") == "purchase_day"


def test_first_of_month_is_still_available_as_a_setting(session: Session) -> None:
    settings.set_value(session, "due_mode", "first_of_month")
    sale = _facility(session, financed=30000, months=1)
    rows = session.scalars(select(Installment).where(Installment.sale_id == sale.id)).all()
    assert [row.due_date for row in rows] == [date(2026, 3, 1)]


def test_migration_moves_first_of_month_due_dates_to_the_purchase_day(tmp_path: Path) -> None:
    url = f"sqlite+pysqlite:///{(tmp_path / 'old.sqlite3').as_posix()}"
    config = Config(str(Path(__file__).parents[1] / "app" / "db" / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    command.upgrade(config, "0007_debts")

    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO settings (key, value) VALUES ('due_mode', '\"first_of_month\"')"))
            connection.execute(text("INSERT INTO categories (id, name, is_default) VALUES (1, 'أساتذة', 1)"))
            connection.execute(text("INSERT INTO customers (id, category_id, full_name) VALUES (1, 1, 'زبون')"))
            connection.execute(text(
                "INSERT INTO sales (id, customer_id, product, sale_type, wholesale_price, cash_price, rate, "
                "down_payment, months, payment_interval, total, financed, profit, purchase_date) "
                "VALUES (1, 1, 'Redmi', 'installment', 0, 30000, 0, 0, 3, 1, 30000, 30000, 30000, '2026-02-02')"
            ))
            for index, due in ((1, "2026-03-01"), (2, "2026-04-01"), (3, "2026-05-20")):
                connection.execute(text(
                    "INSERT INTO installments (sale_id, installment_index, due_date, amount_due, amount_paid) "
                    f"VALUES (1, {index}, '{due}', 10000, 0)"
                ))
        command.upgrade(config, "head")
        with engine.connect() as connection:
            dues = connection.scalars(text("SELECT due_date FROM installments ORDER BY installment_index")).all()
            mode = connection.scalar(text("SELECT value FROM settings WHERE key = 'due_mode'"))
            quantity = connection.scalar(text("SELECT quantity FROM sales WHERE id = 1"))
    finally:
        engine.dispose()
    # Dates generated on the 1st move to the purchase day; a hand-set date stays.
    assert dues == ["2026-03-02", "2026-04-02", "2026-05-20"]
    assert mode == '"purchase_day"'
    assert quantity == 1
