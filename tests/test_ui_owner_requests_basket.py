"""Owner requests of 2026-10-03 on screen: basket, one row per sale, fresh prices,
stock quantity on the product, receiving goods, a new client from the cash sale."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox
from sqlalchemy import select

from app.db.models import Sale, User
from app.db.session import dispose_database_engines, session_scope
from app.i18n import ar
from app.services import auth, categories, customers, products, stock
from app.ui.events import events


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def shop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp) -> dict:
    """Migrated file database with an owner, a seller, two products and a client."""
    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AKREMMOBILE_DATABASE_URL", raising=False)
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    # "Print the receipt?" is answered yes: cancel the file chooser it opens.
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: ("", ""))
    from app.db.migrate import upgrade_database
    from app.services.seed import seed_defaults

    upgrade_database()
    seed_defaults()
    with session_scope() as session:
        owner = auth.create_first_owner(
            session, username="akrem", password="Secret123", password_confirmation="Secret123"
        )
        seller = auth.create_seller(
            session, owner.id, username="vendeur", password="Secret123", password_confirmation="Secret123"
        )
        iphone = products.create_product(session, owner.id, name="13 PRO 2SIM 128GB", wholesale_price=80000,
                                         cash_price=90000)
        redmi = products.create_product(session, owner.id, name="Redmi 13", wholesale_price=25000, cash_price=32000)
        stock.add_units(session, owner.id, product_id=redmi.id, quantity=3)
        client = customers.create_customer(
            session, full_name="أكرم", phone="0550001122", category_id=categories.get_fallback_type(session).id
        )
        ids = {"owner": owner.id, "seller": seller.id, "iphone": iphone.id, "redmi": redmi.id, "client": client.id}
    yield ids
    dispose_database_engines()


def _user(user_id: int) -> User:
    with session_scope() as session:
        return session.get(User, user_id)


# ------------------------------------------------------------------ basket
def test_installment_basket_saves_each_product_on_one_client_row(shop) -> None:
    from app.ui.pages.customers_page import CustomersPage
    from app.ui.pages.new_sale_page import NewSalePage

    page = NewSalePage(current_user=_user(shop["owner"]))
    page._customer_id = shop["client"]
    page.sale_type.setCurrentIndex(page.sale_type.findData("installment"))
    page.product.setCurrentText("13 PRO 2SIM 128GB")
    assert page.cash_price.value() == 90000
    page._add_sale_to_basket()
    assert page.sale_basket.count() == 1 and page.product.currentText() == ""
    assert page.save_button.text() == ar.BASKET_SAVE_MANY.format(count=1)

    page.product.setCurrentText("Redmi 13")
    page.sale_details.quantity.setValue(2)
    assert page.cash_price.value() == 64000
    page._save_sale()

    with session_scope() as session:
        saved = session.scalars(select(Sale).where(Sale.customer_id == shop["client"]).order_by(Sale.id)).all()
        assert [(sale.product, sale.quantity, sale.cash_price) for sale in saved] == [
            ("13 PRO 2SIM 128GB", 1, 90000), ("Redmi 13", 2, 64000),
        ]
        assert len(stock.available_units(session, shop["redmi"])) == 1
    assert page.sale_basket.count() == 0

    clients = CustomersPage(_user(shop["owner"]))
    model = clients.table.model()
    rows = [row for row in range(model.rowCount()) if model.index(row, 1).data() == "أكرم"]
    assert len(rows) == 1  # products bought together share one row (owner's choice 2026-10-04)
    assert model.index(rows[0], 3).data() == "\u206613 PRO 2SIM 128GB\u2069 + \u2066Redmi 13\u2069"


def test_a_phone_in_the_basket_is_not_offered_again(shop) -> None:
    from app.ui.pages.new_sale_page import NewSalePage

    page = NewSalePage(current_user=_user(shop["owner"]))
    page.cash_product.setCurrentText("Redmi 13")
    details = page.cash_details
    assert details.unit.count() == 1 + 3
    picked = details.unit.itemData(1)
    details.unit.setCurrentIndex(1)
    page._add_cash_to_basket()
    page.cash_product.setCurrentText("Redmi 13")
    assert details.unit.findData(picked) < 0
    assert details.quantity.maximum() == 2


def test_cash_basket_goes_to_the_new_client_added_from_the_search(shop, monkeypatch) -> None:
    from app.ui.dialogs.customer_form_dialog import CustomerFormDialog
    from app.ui.pages.new_sale_page import NewSalePage

    page = NewSalePage(current_user=_user(shop["owner"]))
    assert page.cash_add_customer_button.text().endswith(ar.ADD_CUSTOMER)

    def fill_and_save(dialog: CustomerFormDialog) -> QDialog.DialogCode:
        dialog.full_name.setText("زبون جديد من الكاش")
        dialog.phone.setText("0661234567")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(CustomerFormDialog, "exec", fill_and_save)
    new_id = page.add_new_customer()
    assert new_id is not None
    # Selected in both pickers, and the cash sale is no longer a walk-in.
    assert page.cash_customer_combo.currentData() == new_id
    assert page.customer_combo.currentData() == new_id
    assert not page.cash_walkin_check.isChecked()

    page.cash_product.setCurrentText("Redmi 13")
    page._add_cash_to_basket()
    page.cash_product.setCurrentText("13 PRO 2SIM 128GB")
    page._save_cash_sale()
    with session_scope() as session:
        saved = session.scalars(select(Sale).where(Sale.customer_id == new_id)).all()
        assert sorted(sale.product for sale in saved) == ["13 PRO 2SIM 128GB", "Redmi 13"]
        assert {sale.sale_type for sale in saved} == {"cash"}


def test_picking_a_client_in_one_module_picks_it_in_the_other(shop) -> None:
    from app.ui.pages.new_sale_page import NewSalePage

    page = NewSalePage(current_user=_user(shop["owner"]))
    index = page.customer_combo.findData(shop["client"])
    page.customer_combo.setCurrentIndex(index)
    assert page.cash_customer_combo.currentData() == shop["client"]


# ------------------------------------------------------------------ fresh prices
def test_product_edits_reach_the_open_sale_forms(shop) -> None:
    from app.ui.pages.new_sale_page import NewSalePage

    page = NewSalePage(current_user=_user(shop["owner"]))
    page.product.setCurrentText("Redmi 13")
    page.cash_product.setCurrentText("Redmi 13")
    assert (page.cash_price.value(), page.cash_total.value()) == (32000, 32000)

    with session_scope() as session:
        products.update_product(session, shop["owner"], shop["redmi"], name="Redmi 13", cash_price=35000)
    events.data_changed.emit()
    assert (page.cash_price.value(), page.cash_total.value()) == (35000, 35000)

    # A price typed by hand (a discount) is kept when data changes elsewhere.
    page.cash_price.setValue(30000)
    with session_scope() as session:
        products.update_product(session, shop["owner"], shop["redmi"], name="Redmi 13", cash_price=36000)
    events.data_changed.emit()
    assert (page.cash_price.value(), page.cash_total.value()) == (30000, 36000)

    # A phone picked from stock follows the product's new price too.
    page.cash_details.unit.setCurrentIndex(1)
    assert page.cash_total.value() == 36000


# ------------------------------------------------------------------ products: quantity and receiving goods
def test_products_table_shows_and_edits_the_available_quantity(shop) -> None:
    from app.ui.pages.products_page import ProductDialog, ProductsPage

    page = ProductsPage(_user(shop["owner"]))
    page.tabs.setCurrentIndex(1)
    assert page._columns[1] == "stock"
    assert page.table.horizontalHeaderItem(1).text() == ar.PROD_COL_AVAILABLE
    quantities = {page._rows[row].name: page.table.item(row, 1).text() for row in range(page.table.rowCount())}
    assert quantities == {"13 PRO 2SIM 128GB": "0", "Redmi 13": "3"}

    iphone_row = next(row for row, product in enumerate(page._rows) if product.id == shop["iphone"])
    dialog = ProductDialog(title="x", product=page._rows[iphone_row], available=0, parent=page)
    dialog.quantity.setValue(4)
    assert page.save_dialog(dialog, shop["iphone"])
    with session_scope() as session:
        assert len(stock.available_units(session, shop["iphone"])) == 4

    redmi = next(product for product in page._rows if product.id == shop["redmi"])
    dialog = ProductDialog(title="x", product=redmi, available=3, parent=page)
    dialog.quantity.setValue(1)
    assert page.save_dialog(dialog, shop["redmi"])
    quantities = {page._rows[row].name: page.table.item(row, 1).text() for row in range(page.table.rowCount())}
    assert quantities == {"13 PRO 2SIM 128GB": "4", "Redmi 13": "1"}

    seller_page = ProductsPage(_user(shop["seller"]))
    assert "stock" in seller_page._columns and seller_page.receive_button.isHidden()
    assert not hasattr(page, "export_button")


def test_receive_goods_records_new_phones(shop, monkeypatch) -> None:
    from app.ui.pages.products_page import ProductsPage
    from app.ui.pages.stock_panel import StockUnitDialog

    page = ProductsPage(_user(shop["owner"]))
    seen: list[str] = []

    def fill(dialog: StockUnitDialog) -> int:
        seen.append(dialog.windowTitle())
        dialog.product.setCurrentText("Galaxy A05")
        dialog.cash_price.setValue(21000)
        dialog.wholesale_price.setValue(17000)
        dialog.quantity.setValue(6)
        dialog.save_button.click()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(StockUnitDialog, "exec", fill)
    page.receive_button.click()
    assert seen == [ar.PROD_RECEIVE]
    assert page.tabs.currentWidget() is page.stock_panel
    with session_scope() as session:
        galaxy = products.find_by_name(session, "galaxy a05")
        assert galaxy is not None and len(stock.available_units(session, galaxy.id)) == 6


# ------------------------------------------------------------------ dashboard
def test_dashboard_headline_is_my_net_money(shop) -> None:
    from app.ui.pages.dashboard_page import DashboardPage

    page = DashboardPage(_user(shop["owner"]))
    # No client debt and no debt book yet; only the stock (3 Redmi at 25,000) exists.
    assert page.net_value.text() == "0 دج"
    assert "75,000 دج" in page.with_stock_label.text()
