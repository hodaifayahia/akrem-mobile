"""Products and client-type pages, new client + purchase, history, settings, 2FA sign-in."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox
from sqlalchemy import select

from app.db.models import Customer, Sale, User
from app.db.session import dispose_database_engines, session_scope
from app.i18n import ar
from app.services import auth, backup, categories, customers, payments, products, sales, two_factor


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def shop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp) -> dict:
    """Migrated file database with an owner, a seller and a small catalog."""
    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AKREMMOBILE_DATABASE_URL", raising=False)
    QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
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
        products.create_product(session, owner.id, name="iPhone 13", wholesale_price=90000, cash_price=110000)
        products.create_product(session, owner.id, name="Redmi 13", wholesale_price=25000, cash_price=32000)
        ids = {"owner": owner.id, "seller": seller.id, "tmp": str(tmp_path)}
    yield ids
    dispose_database_engines()


def _user(user_id: int) -> User:
    with session_scope() as session:
        return session.get(User, user_id)


# ------------------------------------------------------------- sidebar pages
def test_products_and_client_types_are_sidebar_pages(shop) -> None:
    from app.ui.main_window import PAGE_CLIENT_TYPES, PAGE_PRODUCTS, MainWindow

    window = MainWindow(current_user=_user(shop["owner"]))
    window._select_page(PAGE_PRODUCTS)
    assert window.pages.currentWidget() is window.products_page
    assert window.topbar.page_title_label.text() == ar.NAV_PRODUCTS
    window._select_page(PAGE_CLIENT_TYPES)
    assert window.pages.currentWidget() is window.client_types_page

    teachers = next(t for t in window.client_types_page.panel._types if t.name == "أساتذة")
    window.client_types_page.view_customers_requested.emit(teachers.id)
    assert window.pages.currentWidget() is window.customers_page
    assert window.customers_page.type_filter.selected_type_id() == teachers.id
    window._quitting = True
    window.close()

    seller_window = MainWindow(current_user=_user(shop["seller"]))
    seller_window._select_page(PAGE_CLIENT_TYPES)
    assert seller_window.pages.currentIndex() != PAGE_CLIENT_TYPES
    seller_window._select_page(PAGE_PRODUCTS)
    assert seller_window.pages.currentIndex() == PAGE_PRODUCTS
    seller_window._quitting = True
    seller_window.close()


def test_products_page_lists_prices_quote_and_hides_wholesale_from_sellers(shop) -> None:
    from app.ui.pages.products_page import ProductDialog, ProductsPage

    page = ProductsPage(_user(shop["owner"]))
    headers = [page.table.horizontalHeaderItem(c).text() for c in range(page.table.columnCount())]
    assert ar.SET_PRODUCT_WHOLESALE in headers
    names = [page.table.item(r, 0).text() for r in range(page.table.rowCount())]
    assert names == ["iPhone 13", "Redmi 13"]
    quote_column = page._columns.index("quote")
    # 110,000 + 35% = 148,500 over 6 months
    assert page.table.item(0, quote_column).text().startswith("24,750")

    dialog = ProductDialog(title="x", parent=page)
    dialog.name.setText("Galaxy A15")
    dialog.wholesale_price.setValue(20000)
    dialog.cash_price.setValue(26000)
    assert page.save_dialog(dialog)
    assert page.table.rowCount() == 3

    duplicate = ProductDialog(title="x", parent=page)
    duplicate.name.setText("Galaxy A15")
    duplicate.cash_price.setValue(27000)
    assert not page.save_dialog(duplicate)
    assert duplicate.error.text.text() == ar.PROD_DUPLICATE_NAME

    seller_page = ProductsPage(_user(shop["seller"]))
    seller_headers = [seller_page.table.horizontalHeaderItem(c).text()
                      for c in range(seller_page.table.columnCount())]
    assert ar.SET_PRODUCT_WHOLESALE not in seller_headers
    for button in (seller_page.add_button, seller_page.template_button, seller_page.upload_button):
        assert button.isHidden()


def test_product_dialog_requires_only_name_and_price(shop) -> None:
    from app.ui.pages.products_page import ProductDialog, ProductsPage

    page = ProductsPage(_user(shop["owner"]))
    dialog = ProductDialog(title="x", parent=page)
    assert dialog.wholesale_price.specialValueText() == ar.PROD_OPTIONAL

    assert not page.save_dialog(dialog)
    assert dialog.error.text.text() == ar.PROD_NAME_REQUIRED
    dialog.name.setText("Nokia 105")
    assert not page.save_dialog(dialog)
    assert dialog.error.text.text() == ar.PROD_PRICE_REQUIRED

    dialog.cash_price.setValue(4500)  # wholesale left empty
    assert page.save_dialog(dialog)
    row = [page.table.item(r, 0).text() for r in range(page.table.rowCount())].index("Nokia 105")
    assert page.table.item(row, page._columns.index("wholesale")).text() == "—"
    assert page.table.item(row, page._columns.index("margin")).text() == "—"
    with session_scope() as session:
        saved = {p.name: p for p in products.list_products(session)}["Nokia 105"]
        assert (saved.cash_price, saved.wholesale_price) == (4500, 0)


def test_products_page_downloads_template_and_uploads_products(shop, monkeypatch) -> None:
    from openpyxl import load_workbook
    from PySide6.QtWidgets import QFileDialog

    from app.ui.pages import products_page
    from app.ui.pages.products_page import ProductImportDialog, ProductsPage

    page = ProductsPage(_user(shop["owner"]))
    assert page.template_button.isVisibleTo(page) and page.upload_button.isVisibleTo(page)

    target = Path(shop["tmp"]) / "out" / "products"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    page.template_button.click()
    template = target.with_suffix(".xlsx")
    assert template.exists()

    workbook = load_workbook(template)
    sheet = workbook.worksheets[0]
    sheet.append(["Redmi 13", 33000, None])        # price update, wholesale kept
    sheet.append(["Galaxy A05", "21,000 دج", None])  # new product without wholesale
    sheet.append(["Bad row", None, 1000])            # rejected: no price
    workbook.save(template)

    shown: list[ProductImportDialog] = []

    def fake_exec(dialog):
        shown.append(dialog)
        assert dialog.import_button.isEnabled()
        dialog.import_button.click()
        return 1

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(template), ""))
    monkeypatch.setattr(ProductImportDialog, "exec", fake_exec)
    page.upload_button.click()

    dialog = shown[0]
    results = [dialog.table.item(r, 4).text() for r in range(dialog.table.rowCount())]
    assert results == [ar.PROD_ACTION_UPDATE, ar.PROD_ACTION_CREATE, ar.PROD_ACTION_INVALID]
    assert ar.PROD_ERR_PRICE_MISSING in dialog.table.item(2, 5).text()
    assert dialog.result() == 1
    with session_scope() as session:
        catalog = {p.name: p for p in products.list_products(session)}
        assert (catalog["Redmi 13"].cash_price, catalog["Redmi 13"].wholesale_price) == (33000, 25000)
        assert (catalog["Galaxy A05"].cash_price, catalog["Galaxy A05"].wholesale_price) == (21000, 0)
        assert "Bad row" not in catalog
    names = [page.table.item(r, 0).text() for r in range(page.table.rowCount())]
    assert "Galaxy A05" in names

    # A sheet without product columns is refused with a clear message.
    other = Path(shop["tmp"]) / "customers.xlsx"
    from openpyxl import Workbook
    book = Workbook()
    book.active.append(["الاسم واللقب", "الهاتف"])
    book.active.append(["علي", "0550"])
    book.save(other)
    warnings: list[str] = []
    monkeypatch.setattr(products_page.QMessageBox, "warning", lambda _p, _t, text: warnings.append(text))
    assert page.preview_upload(other) is None
    assert warnings == [ar.PROD_UPLOAD_NO_HEADER]


# ----------------------------------------------------- new client + purchase
def test_new_client_form_fills_the_catalog_price_and_saves_the_purchase(shop) -> None:
    from app.ui.pages.customers_page import CustomersPage

    page = CustomersPage(_user(shop["owner"]))
    section = page._purchase_section()
    assert section.isChecked()
    section.product.setCurrentIndex(section.product.findData("iPhone 13"))
    assert section.cash_price.value() == 110000
    section.sale_type.setCurrentIndex(section.sale_type.findData("installment"))
    section.months.setValue(6)
    assert section.rate.value() == 35  # preset for 6 months
    section.down_payment.setValue(28500)
    result = section.calculation()
    assert result is not None and result.total == 148500 and result.monthly_list[0] == 20000
    assert "20,000" in section.summary.text()

    fallback_id = page._types[-1].id
    values = {"full_name": "زبون جديد", "phone": "0550000001", "category_id": fallback_id}
    customer_id = page.save_new_customer(values, section.sale_values())
    assert customer_id is not None
    with session_scope() as session:
        sale = session.scalar(select(Sale).where(Sale.customer_id == customer_id))
        assert (sale.product, sale.sale_type, sale.total, sale.months) == ("iPhone 13", "installment", 148500, 6)

    cash = page._purchase_section()
    cash.product.setCurrentIndex(cash.product.findData("Redmi 13"))
    cash.sale_type.setCurrentIndex(cash.sale_type.findData("cash"))
    cash_id = page.save_new_customer(
        {"full_name": "زبون كاش", "phone": "", "category_id": fallback_id}, cash.sale_values()
    )
    with session_scope() as session:
        assert session.scalar(select(Sale.sale_type).where(Sale.customer_id == cash_id)) == "cash"

    unchecked = page._purchase_section()
    unchecked.setChecked(False)
    assert unchecked.sale_values() is None


# ------------------------------------------------------------------- history
def test_clicking_a_client_shows_their_full_history(shop) -> None:
    from app.ui.dialogs.customer_details_dialog import CustomerDetailsDialog

    with session_scope() as session:
        fallback = categories.get_fallback_type(session)
        customer, sale = sales.create_customer_with_sale(
            session, shop["owner"],
            customer={"full_name": "زبون قديم", "category_id": fallback.id},
            sale={"product": "iPhone 13", "sale_type": "installment", "wholesale_price": 90000,
                  "cash_price": 110000, "rate": 35, "down_payment": 28500, "months": 6,
                  "purchase_date": date(2026, 1, 10)},
        )
        sales.create_sale(session, customer_id=customer.id, product="Redmi 13", sale_type="cash",
                          wholesale_price=25000, cash_price=32000, purchase_date=date(2026, 3, 2))
        first = min(sale.installments, key=lambda row: row.installment_index)
        payments.record_installment_payment(session, first.id, 20000, date(2026, 2, 3), "CCP")
        customer_id = customer.id

    dialog = CustomerDetailsDialog(customer_id, _user(shop["owner"]))
    assert dialog.tabs.count() == 2
    summary = dialog.history_summary
    assert summary.values["count"].text() == "2"
    assert summary.values["bought"].text() == f"{148500 + 32000:,} دج"
    assert summary.values["paid"].text() == f"{28500 + 20000 + 32000:,} دج"
    assert summary.profile.text() == ar.HIST_PROFILE_FACILITIES

    panel = dialog.history_panel
    assert panel.purchases_table.rowCount() == 2
    assert panel.purchases_table.item(0, 1).text() == "Redmi 13"  # newest first
    assert panel.purchases_table.item(0, 2).text() == ar.SALE_CASH
    assert panel.payments_table.rowCount() == 1
    assert panel.payments_table.item(0, 2).text() == "CCP"

    panel.purchase_activated.emit(dialog.sale_selector.itemData(1))
    assert dialog.tabs.currentIndex() == 0


# ------------------------------------------------------------------ settings
def test_settings_can_move_the_backup_folder(shop) -> None:
    from app.ui.pages.settings_page import SettingsPage

    page = SettingsPage(_user(shop["owner"]))
    target = Path(shop["tmp"]) / "usb" / "akrem-backups"
    assert page.set_backup_folder(str(target))
    assert page.backup_folder_label.text() == str(target.resolve())
    created = backup.create_backup()
    assert created.parent == target.resolve()
    assert page.set_backup_folder(None)
    assert backup.backup_directory() == backup.default_backup_directory()

    a_file = Path(shop["tmp"]) / "not-a-folder"
    a_file.write_text("x")
    assert not page.set_backup_folder(str(a_file))


def test_owner_can_add_owner_and_seller_accounts(shop) -> None:
    from app.ui.pages.settings_page import SettingsPage, _UserCredentialsDialog

    page = SettingsPage(_user(shop["owner"]))
    roles = [page.seller_table.item(r, 1).text() for r in range(page.seller_table.rowCount())]
    assert ar.ROLE_OWNER in roles and ar.ROLE_SELLER in roles

    original = _UserCredentialsDialog.exec

    def fake_exec(dialog):
        dialog.username.setText("partner")
        dialog.password.setText("Partner123")
        dialog.confirmation.setText("Partner123")
        dialog.role.setCurrentIndex(dialog.role.findData("owner"))
        return 1

    _UserCredentialsDialog.exec = fake_exec
    try:
        page._create_seller()
    finally:
        _UserCredentialsDialog.exec = original
    with session_scope() as session:
        partner = session.scalar(select(User).where(User.username == "partner"))
        assert partner.role == "owner"
    assert auth.authenticate(
        session_scope().__enter__(), username="partner", password="Partner123"
    ).status == "success"


def test_password_change_from_settings(shop) -> None:
    from app.ui.pages.settings_page import SettingsPage

    page = SettingsPage(_user(shop["seller"]))
    page.current_password.setText("Secret123")
    page.new_password.setText("Changed456")
    page.new_password_confirmation.setText("Changed456")
    page._change_own_password()
    with session_scope() as session:
        assert auth.authenticate(session, username="vendeur", password="Changed456").status == "success"


# ------------------------------------------------- Google Authenticator 2FA
def test_two_factor_setup_shows_a_qr_code_and_protects_sign_in(shop, monkeypatch) -> None:
    from app.ui.dialogs.auth_dialogs import LoginDialog
    from app.ui.dialogs.two_factor_dialog import TwoFactorSetupDialog
    from app.ui.pages.settings_page import SettingsPage

    owner = _user(shop["owner"])
    page = SettingsPage(owner)
    assert page.tfa_status.text() == ar.TFA_STATUS_OFF

    setup = TwoFactorSetupDialog(owner, page)
    assert setup.qr.pixmap() is not None and not setup.qr.pixmap().isNull()
    assert setup.key_label.text().replace(" ", "") == setup.secret
    setup.code.setText("000000")
    if two_factor.totp_code(setup.secret, datetime.now(timezone.utc)) != "000000":
        assert not setup.confirm()
        assert setup.error.text.text() == ar.TFA_INVALID_CODE
    setup.code.setText(two_factor.totp_code(setup.secret, datetime.now(timezone.utc)))
    assert setup.confirm()
    page._refresh_two_factor()
    assert page.tfa_status.text() == ar.TFA_STATUS_ON

    login = LoginDialog()
    login.username.setText("akrem")
    login.password.setText("Secret123")
    login._login()
    assert login.user is None and login.code_page.isVisibleTo(login)
    login.code.setText("999999" if setup.secret else "")
    login._verify_code()
    assert login.user is None
    # A fresh code for the next 30-second window (the current one was used to enable).
    future = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + 30, timezone.utc)
    login.code.setText(two_factor.totp_code(setup.secret, future))
    login._verify_code()
    assert login.user is not None and login.user.username == "akrem"

    page._reset_user_two_factor  # owner reset is available
    with session_scope() as session:
        auth.reset_two_factor(session, shop["owner"], shop["owner"])
    page._refresh_two_factor()
    assert page.tfa_status.text() == ar.TFA_STATUS_OFF
