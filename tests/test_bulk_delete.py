"""Deleting several records at once: customers, sales, payments, debts, products, stock, client types."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Category, Customer, Debt, Installment, Payment, Product, Sale, StockItem
from app.db.session import session_scope
from app.services import auth, backup, categories, customers, debts, importer, payments, products, sales, stock
from app.services.categories import ClientTypeError
from app.services.seed import seed_defaults

PASSWORD = "StrongPass123"
PAID_ON = date(2026, 9, 2)


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


def _customer(session: Session, name: str) -> int:
    category = session.scalar(select(Category).limit(1))
    return customers.create_customer(session, full_name=name, category_id=category.id).id


def _installment_sale(session: Session, customer_id: int, **extra) -> Sale:
    return sales.create_sale(
        session, customer_id=customer_id, product="Redmi 13", sale_type="installment",
        wholesale_price=25000, cash_price=32000, rate=35, months=6,
        purchase_date=date(2026, 8, 10), **extra,
    )


def _credit_sale(session: Session, customer_id: int) -> Sale:
    return sales.create_sale(
        session, customer_id=customer_id, product="Galaxy A15", sale_type="credit",
        wholesale_price=20000, cash_price=26000, down_payment=6000, purchase_date=date(2026, 8, 10),
    )


def _count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


# ------------------------------------------------------------------ sales
def test_delete_sales_removes_schedule_payments_and_returns_the_phone(session: Session, owner_id: int) -> None:
    product = products.create_product(session, owner_id, name="Redmi 13", wholesale_price=25000, cash_price=32000)
    unit = stock.add_units(session, owner_id, product_id=product.id)[0]
    sale = _installment_sale(session, _customer(session, "زبون"), product_id=product.id, stock_item_id=unit.id)
    first = sale.installments[0]
    payments.record_installment_payment(session, first.id, 1000, PAID_ON, "cash")
    assert unit.status == stock.STATUS_SOLD

    assert sales.delete_sales(session, owner_id, [sale.id, sale.id]) == 1

    assert _count(session, Sale) == 0
    assert _count(session, Installment) == 0
    assert _count(session, Payment) == 0
    session.refresh(unit)
    assert unit.status == stock.STATUS_AVAILABLE and unit.sale_id is None and unit.sold_at is None
    audit = session.scalars(select(AuditLog).where(AuditLog.action == "delete_sale")).one()
    assert audit.entity_id == sale.id and '"payments_total": 1000' in audit.details


def test_delete_sales_is_owner_only(session: Session, seller_id: int) -> None:
    sale = _credit_sale(session, _customer(session, "زبون"))
    with pytest.raises(auth.AuthorizationError):
        sales.delete_sales(session, seller_id, [sale.id])
    assert _count(session, Sale) == 1


# -------------------------------------------------------------- customers
def test_delete_customers_keeps_clients_with_sales_unless_asked(session: Session, owner_id: int) -> None:
    without_sales = _customer(session, "بدون مبيعات")
    with_sale = _customer(session, "عنده بيع")
    _credit_sale(session, with_sale)

    result = customers.delete_customers(session, owner_id, [without_sales, with_sale])
    assert (result.deleted, result.sales_deleted, result.kept) == (1, 0, ("عنده بيع",))
    assert session.get(Customer, with_sale) is not None

    result = customers.delete_customers(session, owner_id, [with_sale], with_sales=True)
    assert (result.deleted, result.sales_deleted, result.kept) == (1, 1, ())
    assert _count(session, Customer) == 0 and _count(session, Sale) == 0


def test_delete_customers_is_owner_only(session: Session, seller_id: int) -> None:
    customer_id = _customer(session, "زبون")
    with pytest.raises(auth.AuthorizationError):
        customers.delete_customers(session, seller_id, [customer_id])


# --------------------------------------------------------------- payments
def test_delete_payments_makes_installments_unpaid_and_keeps_an_audit(session: Session, owner_id: int) -> None:
    sale = _installment_sale(session, _customer(session, "زبون"))
    first, second = sale.installments[0], sale.installments[1]
    payments.record_installment_payment(session, first.id, first.amount_due, PAID_ON, "cash")
    payments.record_installment_payment(session, second.id, 500, PAID_ON, "CCP")
    credit = _credit_sale(session, _customer(session, "كريدي"))
    payments.record_credit_payment(session, credit.id, 4000, PAID_ON, "cash")

    removed = payments.delete_payments(
        session, owner_id, installment_ids=[first.id], credit_sale_ids=[credit.id]
    )

    assert removed == 2
    session.refresh(first)
    session.refresh(second)
    assert (first.amount_paid, first.paid_date, first.method) == (0, None, None)
    assert second.amount_paid == 500  # not selected: untouched
    assert session.scalar(select(func.count()).select_from(Payment).where(Payment.sale_id == credit.id)) == 0
    assert _count(session, AuditLog) == 2
    assert payments.delete_payments(session, owner_id) == 0


def test_delete_payments_is_owner_only(session: Session, owner_id: int, seller_id: int) -> None:
    sale = _installment_sale(session, _customer(session, "زبون"))
    with pytest.raises(auth.AuthorizationError):
        payments.delete_payments(session, seller_id, installment_ids=[sale.installments[0].id])


# ------------------------------------------------------------------ debts
def test_delete_debts_removes_several_with_their_payments(session: Session, owner_id: int) -> None:
    ids = []
    for name in ("أحمد", "سمير"):
        debt = debts.create_debt(
            session, owner_id, direction=debts.RECEIVABLE,
            person=debts.PersonDetails(first_name=name, last_name="ب"),
            terms=debts.DebtTerms(amount=10000, debt_date=PAID_ON),
        )
        debts.record_payment(session, owner_id, debt.id, amount=1000, payment_date=PAID_ON)
        ids.append(debt.id)
    assert debts.delete_debts(session, owner_id, ids + [999]) == 2
    assert _count(session, Debt) == 0


# ---------------------------------------------------------- products, stock
def test_delete_units_keeps_sold_phones(session: Session, owner_id: int) -> None:
    product = products.create_product(session, owner_id, name="Phone", cash_price=30000)
    available, sold = stock.add_units(session, owner_id, product_id=product.id, quantity=2)
    sales.create_sale(
        session, customer_id=_customer(session, "زبون"), product="Phone", sale_type="cash",
        wholesale_price=0, cash_price=30000, product_id=product.id, stock_item_id=sold.id,
    )
    assert stock.delete_units(session, owner_id, [available.id, sold.id]) == (1, 1)
    assert [unit.id for unit in session.scalars(select(StockItem))] == [sold.id]


def test_delete_products_keeps_products_used_by_sales(session: Session, owner_id: int) -> None:
    unused = products.create_product(session, owner_id, name="Unused", cash_price=10000)
    stock.add_units(session, owner_id, product_id=unused.id, quantity=2)
    used = products.create_product(session, owner_id, name="Used", cash_price=20000)
    sales.create_sale(
        session, customer_id=_customer(session, "زبون"), product="Used", sale_type="cash",
        wholesale_price=0, cash_price=20000, product_id=used.id,
    )

    result = products.delete_products(session, owner_id, [unused.id, used.id])

    assert (result.deleted, result.units_deleted, result.in_use) == (1, 2, (used.id,))
    assert [product.name for product in session.scalars(select(Product))] == ["Used"]
    assert products.archive_products(session, owner_id, result.in_use) == 1
    assert session.get(Product, used.id).active is False


def test_product_deletion_is_owner_only(session: Session, owner_id: int, seller_id: int) -> None:
    product = products.create_product(session, owner_id, name="Phone", cash_price=30000)
    with pytest.raises(auth.AuthorizationError):
        products.delete_products(session, seller_id, [product.id])
    with pytest.raises(auth.AuthorizationError):
        stock.delete_units(session, seller_id, [])


# ----------------------------------------------------------- client types
def test_delete_client_types_moves_their_customers(session: Session, owner_id: int) -> None:
    first = categories.create_client_type(session, owner_id, name="نوع أ", color="blue")
    second = categories.create_client_type(session, owner_id, name="نوع ب", color="teal")
    customer = customers.create_customer(session, full_name="زبون", category_id=first.id)
    fallback = categories.get_fallback_type(session)

    with pytest.raises(ClientTypeError) as missing_target:
        categories.delete_client_types(session, owner_id, [first.id, second.id])
    assert missing_target.value.code == "in_use"
    with pytest.raises(ClientTypeError) as target_deleted:
        categories.delete_client_types(session, owner_id, [first.id, second.id], reassign_to_id=second.id)
    assert target_deleted.value.code == "invalid_reassign"
    with pytest.raises(ClientTypeError) as system:
        categories.delete_client_types(session, owner_id, [fallback.id])
    assert system.value.code == "system_type"

    moved = categories.delete_client_types(session, owner_id, [first.id, second.id], reassign_to_id=fallback.id)
    assert moved == 1
    assert session.get(Customer, customer.id).category_id == fallback.id
    assert {item.id for item in categories.list_client_types(session)}.isdisjoint({first.id, second.id})


# ----------------------------------------------------------------- import
def test_without_rows_drops_the_chosen_excel_rows(tmp_path: Path) -> None:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = importer.SHEET_NAME
    fields = list(importer.REQUIRED_HEADERS)
    sheet.append([importer.HEADERS[field] for field in fields])
    for name in ("أول", "ثاني", "ثالث"):
        values = {
            "full_name": name, "product": "Redmi 13", "sale_type": "كاش", "wholesale_price": 25000,
            "cash_price": 32000, "rate": 0, "financed": 32000, "down_payment": 0, "months": 1,
            "purchase_date": date(2026, 7, 20),
        }
        sheet.append([values[field] for field in fields])
    path = tmp_path / "sheet.xlsx"
    book.save(path)
    preview = importer.preview_workbook(path)

    kept = importer.without_rows(preview, {3})

    assert [row.full_name for row in kept.rows] == ["أول", "ثالث"]
    assert importer.without_rows(preview, ()) is preview


# ----------------------------------------------------------------- backup
def test_safety_backup_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AKREMMOBILE_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    assert backup.create_safety_backup() is None

    database = tmp_path / "shop.sqlite3"
    import sqlite3

    sqlite3.connect(database).close()
    monkeypatch.setenv("AKREMMOBILE_DATABASE_URL", f"sqlite+pysqlite:///{database}")
    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(tmp_path / "data"))
    from app.db.models import Base
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite+pysqlite:///{database}")
    Base.metadata.create_all(engine)
    engine.dispose()
    created = backup.create_safety_backup()
    assert created is not None and created.is_file() and "before_delete" in created.name
