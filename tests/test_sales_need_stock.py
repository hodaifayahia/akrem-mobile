"""Owner's request 2026-10-20: every sale (one phone or several, cash, credit or installment)
leaves the stock, and the sale forms only offer products that are in stock."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Category, Product, Sale, User
from app.db.session import dispose_database_engines, session_scope
from app.i18n import ar
from app.services import auth, categories, customers, products, sales, stock
from app.services.seed import seed_defaults

PASSWORD = "StrongPass123"
DAY = date(2026, 10, 1)


# ================================================================== services
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


def _customer(session: Session) -> int:
    category = session.scalar(select(Category).limit(1))
    return customers.create_customer(session, full_name="زبون", category_id=category.id).id


def _product(session: Session, owner_id: int, name: str, units: int, cash: int = 30000) -> int:
    product = products.create_product(session, owner_id, name=name, cash_price=cash, wholesale_price=cash * 4 // 5)
    if units:
        stock.add_units(session, owner_id, product_id=product.id, quantity=units)
    return product.id


def _available(session: Session, product_id: int) -> int:
    return len(stock.available_units(session, product_id))


def _line(name: str, sale_type: str, cash: int = 30000, **extra: object) -> dict:
    values = {
        "product": name, "sale_type": sale_type, "wholesale_price": cash * 4 // 5, "cash_price": cash,
        "rate": 35 if sale_type == "installment" else 0, "down_payment": 0,
        "months": 6 if sale_type == "installment" else None, "purchase_date": DAY,
    }
    values.update(extra)
    return values


@pytest.mark.parametrize("sale_type", ["cash", "credit", "installment"])
def test_every_sale_type_takes_one_phone_and_two_phones_take_two(
    session: Session, owner_id: int, sale_type: str
) -> None:
    product_id = _product(session, owner_id, "Redmi 15C", 4)
    client = _customer(session)
    sales.create_sale_for_user(session, owner_id, customer_id=client, **_line("Redmi 15C", sale_type))
    assert _available(session, product_id) == 3
    sales.create_sale_for_user(
        session, owner_id, customer_id=client, **_line("Redmi 15C", sale_type, cash=60000, quantity=2)
    )
    assert _available(session, product_id) == 1


@pytest.mark.parametrize("role", ["owner", "seller"])
def test_a_product_with_no_phone_in_stock_is_not_sold(
    session: Session, owner_id: int, seller_id: int, role: str
) -> None:
    _product(session, owner_id, "SAM A07", 0)
    user = owner_id if role == "owner" else seller_id
    with pytest.raises(stock.StockError) as refused:
        sales.create_sale_for_user(session, user, customer_id=_customer(session), **_line("SAM A07", "cash"))
    assert refused.value.code == "out_of_stock"
    assert session.scalar(select(Sale)) is None


def test_a_name_that_is_not_in_the_catalog_is_not_sold_nor_added(session: Session, owner_id: int) -> None:
    with pytest.raises(stock.StockError) as refused:
        sales.create_sale_for_user(session, owner_id, customer_id=_customer(session), **_line("Galaxy S99", "cash"))
    assert refused.value.code == "out_of_stock"
    assert session.scalar(select(Product).where(Product.name == "Galaxy S99")) is None


def test_a_quantity_above_the_stock_is_refused(session: Session, owner_id: int) -> None:
    product_id = _product(session, owner_id, "Honor X9D", 1)
    with pytest.raises(stock.StockError) as refused:
        sales.create_sale_for_user(
            session, owner_id, customer_id=_customer(session), **_line("Honor X9D", "installment", quantity=2)
        )
    assert refused.value.code == "not_enough"
    assert _available(session, product_id) == 1


def test_a_basket_takes_a_phone_per_line_or_saves_nothing(session: Session, owner_id: int) -> None:
    redmi = _product(session, owner_id, "Redmi 15C", 1)
    honor = _product(session, owner_id, "Honor X9D", 2)
    client = _customer(session)
    sales.create_sales_batch(session, owner_id, customer_id=client, lines=[
        _line("Redmi 15C", "cash"), _line("Honor X9D", "installment"),
    ])
    assert (_available(session, redmi), _available(session, honor)) == (0, 1)

    with pytest.raises(stock.StockError):  # the Redmi is sold out: nothing of the basket is saved
        sales.create_sales_batch(session, owner_id, customer_id=client, lines=[
            _line("Honor X9D", "credit"), _line("Redmi 15C", "credit"),
        ])
    assert _available(session, honor) == 1
    assert len(session.scalars(select(Sale)).all()) == 2


def test_a_typed_imei_of_no_phone_still_takes_a_phone_of_the_product(session: Session, owner_id: int) -> None:
    product = products.create_product(session, owner_id, name="IP 13PM", cash_price=90000)
    stock.add_scanned_units(session, owner_id, product_id=product.id, imeis=["356789012345600"])
    sale = sales.create_sale_for_user(
        session, owner_id, customer_id=_customer(session),
        **_line("IP 13PM", "credit", cash=90000, imei="356789012345678"),
    )
    assert _available(session, product.id) == 0
    assert len(sale.stock_items) == 1


# ================================================================== screens
@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def shop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp) -> Iterator[dict]:
    """Migrated file database: an owner, a seller, one product with 2 phones, one sold out, a client."""
    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AKREMMOBILE_DATABASE_URL", raising=False)
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, _title, text, *a, **k: warnings.append(text))
    for name in ("information", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: ("", ""))
    from app.db.migrate import upgrade_database

    upgrade_database()
    seed_defaults()
    with session_scope() as session:
        owner = auth.create_first_owner(
            session, username="akrem", password="Secret123", password_confirmation="Secret123"
        )
        seller = auth.create_seller(
            session, owner.id, username="vendeur", password="Secret123", password_confirmation="Secret123"
        )
        stocked = products.create_product(session, owner.id, name="Redmi 13", wholesale_price=25000, cash_price=32000)
        products.create_product(session, owner.id, name="S23 ULTRA", wholesale_price=90000, cash_price=110000)
        stock.add_units(session, owner.id, product_id=stocked.id, quantity=2)
        client = customers.create_customer(
            session, full_name="أكرم", phone="0550001122", category_id=categories.get_fallback_type(session).id
        )
        ids = {"owner": owner.id, "seller": seller.id, "redmi": stocked.id, "client": client.id, "warnings": warnings}
    yield ids
    dispose_database_engines()


def _user(user_id: int) -> User:
    with session_scope() as session:
        return session.get(User, user_id)


def _redmi_left(shop: dict) -> int:
    with session_scope() as session:
        return len(stock.available_units(session, shop["redmi"]))


def _listed(combo) -> list[str]:
    return [combo.itemText(index) for index in range(combo.count())]


@pytest.mark.parametrize("role", ["owner", "seller"])
def test_the_sale_forms_list_only_products_in_stock(shop, role: str) -> None:
    from app.ui.pages.new_sale_page import NewSalePage

    page = NewSalePage(current_user=_user(shop[role]))
    assert _listed(page.cash_product) == ["Redmi 13"]
    assert _listed(page.product) == ["Redmi 13"]


def test_selling_the_last_phones_takes_the_product_off_the_lists(shop) -> None:
    from app.ui.pages.new_sale_page import NewSalePage

    page = NewSalePage(current_user=_user(shop["owner"]))
    page.cash_walkin_check.setChecked(True)
    page.cash_product.setCurrentText("Redmi 13")
    page._save_cash_sale()  # cash, no phone picked: the oldest one leaves the stock
    assert _redmi_left(shop) == 1

    page._customer_id = shop["client"]
    page._switch_module(1)
    page.product.setCurrentIndex(page.product.findText("Redmi 13"))
    page._save_sale()  # installment
    assert _redmi_left(shop) == 0
    assert _listed(page.cash_product) == [] and _listed(page.product) == []
    assert shop["warnings"] == []


def test_typing_a_product_that_is_not_in_stock_is_refused(shop) -> None:
    from app.ui.pages.new_sale_page import NewSalePage

    page = NewSalePage(current_user=_user(shop["owner"]))
    page.cash_walkin_check.setChecked(True)
    page.cash_product.setCurrentText("S23 ULTRA")  # in the catalog, no phone left
    assert page.cash_details.product_state.text() == ar.SALE_PRODUCT_NOT_IN_STOCK
    page.cash_total.setValue(110000)
    page._save_cash_sale()
    assert shop["warnings"] == [ar.SALE_OUT_OF_STOCK]
    with session_scope() as session:
        assert session.scalar(select(Sale)) is None


def test_a_basket_cannot_take_more_phones_than_the_stock(shop) -> None:
    from app.ui.pages.new_sale_page import NewSalePage

    page = NewSalePage(current_user=_user(shop["owner"]))
    page._set_cash_status("credit")
    page.cash_customer_combo.setCurrentIndex(page.cash_customer_combo.findData(shop["client"]))
    for _ in range(2):
        page.cash_product.setCurrentText("Redmi 13")
        page._add_cash_to_basket()
    assert page.cash_basket.count() == 2
    page.cash_product.setCurrentText("Redmi 13")  # both phones are already in the basket
    assert not page.cash_details.in_stock()
    page._add_cash_to_basket()
    assert page.cash_basket.count() == 2 and shop["warnings"] == [ar.SALE_OUT_OF_STOCK]
    page.cash_product.setCurrentText("")
    page._save_cash_sale()
    assert _redmi_left(shop) == 0
