"""Row checkboxes, "select all" and "delete selected" on the list pages."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from sqlalchemy import func, select

from app.db.models import Customer, Debt, Installment, Product, Sale, StockItem, User
from app.db.session import dispose_database_engines, session_scope
from app.i18n import ar
from app.services import auth, backup, categories, customers, debts, importer, payments, products, sales, stock
from app.ui.widgets import row_selection


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def shop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp) -> dict:
    """Migrated file database with an owner and a seller."""
    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AKREMMOBILE_DATABASE_URL", raising=False)
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
        ids = {"owner": owner.id, "seller": seller.id, "tmp": tmp_path}
    yield ids
    dispose_database_engines()


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> tuple[list, list]:
    """Script the confirmation dialogs.

    Returns ``(script, asked)``: each dialog pops its answer from ``script``
    and records the choice keys it offered in ``asked``.
    """
    script: list = []
    asked: list = []

    def fake(parent, title, message, choices=None, *, details=None):
        asked.append([key for key, _text in choices] if choices else ["delete"])
        return script.pop(0)

    monkeypatch.setattr(row_selection, "ask_delete", fake)
    monkeypatch.setattr(row_selection, "ask_choice", fake)
    return script, asked


def _user(user_id: int) -> User:
    with session_scope() as session:
        return session.get(User, user_id)


def _customer(name: str, *, with_sale: bool = False, paid: int = 0) -> int:
    with session_scope() as session:
        fallback = categories.get_fallback_type(session)
        customer = customers.create_customer(session, full_name=name, category_id=fallback.id)
        if with_sale:
            sale = sales.create_sale(
                session, customer_id=customer.id, product="Redmi 13", sale_type="installment",
                wholesale_price=25000, cash_price=32000, rate=0, months=3,
                purchase_date=date.today() - timedelta(days=100),
            )
            if paid:
                payments.record_installment_payment(session, sale.installments[0].id, paid, date.today(), "cash")
        return customer.id


def _count(model) -> int:
    with session_scope() as session:
        return session.scalar(select(func.count()).select_from(model))


# -------------------------------------------------------------- customers
def test_customers_tick_select_all_and_delete(shop, answers) -> None:
    from app.ui.pages.customers_page import CustomersPage

    script, asked = answers
    plain = _customer("أمين بدون مبيعات")
    buyer = _customer("بلال عنده بيع", with_sale=True, paid=1000)
    page = CustomersPage(_user(shop["owner"]))
    bar = page.selection
    assert bar is not None and page.table.verticalHeader() is bar.header
    assert bar.header.isVisibleTo(page) and bar.header.sizeHint().width() == row_selection.HEADER_WIDTH

    with session_scope() as session:
        buyer_sale = session.scalar(select(Sale.id).where(Sale.customer_id == buyer))
    plain_key, buyer_key = ("customer", plain), ("sale", buyer_sale)

    bar.select_all_box.click()
    assert set(bar.checked_keys()) == {plain_key, buyer_key}
    assert bar.select_all_box.checkState() == Qt.CheckState.Checked
    assert bar.count_label.text() == ar.SEL_COUNT.format(count=2)
    assert bar.delete_button.text() == f"{ar.SEL_DELETE} (2)"

    # A click on a row's checkbox unticks it.
    page.resize(1200, 700)
    page.show()
    QApplication.processEvents()
    plain_row = next(row for row in range(page.table.model().rowCount()) if page._key_at(row) == plain_key)
    QTest.mouseClick(bar.header.viewport(), Qt.MouseButton.LeftButton, pos=bar.header.row_rect(plain_row).center())
    assert bar.checked_keys() == [buyer_key]
    assert bar.select_all_box.checkState() == Qt.CheckState.PartiallyChecked

    # Deleting both: the client with sales needs the "with sales" choice.
    bar.select_all(True)
    script.append("without_sales")
    page._delete_selected()
    assert asked[-1] == ["purchases", "with_sales", "without_sales"]
    with session_scope() as session:
        assert session.get(Customer, plain) is None and session.get(Customer, buyer) is not None
    assert bar.checked_keys() == []

    bar.select_all(True)
    script.append("with_sales")
    page._delete_selected()
    assert _count(Customer) == 0 and _count(Sale) == 0 and _count(Installment) == 0
    backups = backup.list_backups()
    assert any(info.backup_type == backup.SAFETY_LABEL for info in backups)
    page.close()


def test_ticking_one_purchase_of_a_client_ticks_only_that_row(shop, answers) -> None:
    """A client with two products has two rows; ticking one must not tick the other,
    and deleting it removes only that purchase (the client and the other product stay)."""
    from app.ui.pages.customers_page import CustomersPage

    script, asked = answers
    client = _customer("حمادة عبديه", with_sale=True)
    with session_scope() as session:
        second = sales.create_sale(
            session, customer_id=client, product="Spark 2", sale_type="cash",
            wholesale_price=20000, cash_price=26000, purchase_date=date.today(),
        ).id
        first = session.scalar(select(Sale.id).where(Sale.customer_id == client, Sale.id != second))
    page = CustomersPage(_user(shop["owner"]))
    bar = page.selection
    rows = {page._key_at(row): row for row in range(page.table.model().rowCount())}
    assert set(rows) == {("sale", first), ("sale", second)}

    bar.toggle_row(rows[("sale", second)])
    assert bar.checked_keys() == [("sale", second)]
    assert not bar.header._is_checked(rows[("sale", first)])
    assert bar.delete_button.text() == ar.SEL_DELETE
    assert [summary.sale_id for summary in page.checked_summaries()] == [second]

    script.append("purchases")
    page._delete_selected()
    assert asked[-1] == ["purchases", "with_sales"]
    with session_scope() as session:
        assert session.get(Sale, second) is None
        assert session.get(Sale, first) is not None
        assert session.get(Customer, client) is not None
    assert bar.checked_keys() == []
    page.close()


def test_cancelling_the_confirmation_deletes_nothing(shop, answers) -> None:
    from app.ui.pages.customers_page import CustomersPage

    script, _asked = answers
    _customer("زبون")
    page = CustomersPage(_user(shop["owner"]))
    page.selection.select_all(True)
    script.append(None)
    page._delete_selected()
    assert _count(Customer) == 1


def test_ticks_on_rows_that_leave_the_list_are_dropped(shop) -> None:
    from app.ui.pages.customers_page import CustomersPage

    first = _customer("أحمد")
    _customer("سمير")
    page = CustomersPage(_user(shop["owner"]))
    page.selection.select_all(True)
    page.search.setText("أحمد")
    page.refresh()
    assert page.selection.checked_keys() == [("customer", first)]
    page.search.clear()
    page.refresh()
    assert page.selection.checked_keys() == [("customer", first)]  # the other row was unticked when it left


def test_without_ticks_the_highlighted_row_is_the_target(shop) -> None:
    from app.ui.pages.customers_page import CustomersPage

    _customer("أحمد")
    second = _customer("سمير")
    page = CustomersPage(_user(shop["owner"]))
    row = next(row for row in range(page.table.model().rowCount()) if page._key_at(row) == ("customer", second))
    page.table.selectRow(row)
    assert page.selection.target_keys() == [("customer", second)]
    assert page.selection.delete_button.isEnabled()
    assert page.selection.delete_button.text() == ar.SEL_DELETE


def test_sellers_get_no_checkboxes(shop) -> None:
    from app.ui.pages.customers_page import CustomersPage
    from app.ui.pages.payments_page import PaymentsPage

    _customer("زبون", with_sale=True)
    for page_class in (CustomersPage, PaymentsPage):
        page = page_class(_user(shop["seller"]))
        assert page.selection is None
        assert not page.table.verticalHeader().isVisibleTo(page)


# --------------------------------------------------------------- payments
def test_payments_delete_paid_amounts_or_whole_sales(shop, answers) -> None:
    from app.ui.pages.payments_page import PaymentsPage

    script, asked = answers
    _customer("زبون الأقساط", with_sale=True, paid=1000)
    page = PaymentsPage(_user(shop["owner"]))
    page.show_status(None)
    assert len(page.visible_rows()) == 3
    page.selection.check_rows([0])
    assert [row.paid for row in page.checked_rows()] == [1000]

    script.append("payments")
    page._delete_selected()
    assert asked[-1] == ["payments", "sales"]
    with session_scope() as session:
        assert session.scalar(select(func.sum(Installment.amount_paid))) == 0
    assert _count(Sale) == 1

    page.selection.select_all(True)
    script.append("sales")
    page._delete_selected()
    assert asked[-1] == ["sales"]  # nothing paid any more: only the whole-sale choice
    assert _count(Sale) == 0 and page.visible_rows() == []


# ------------------------------------------------------------------ debts
def test_debts_select_all_and_delete(shop, answers) -> None:
    from app.ui.pages.debts_page import ReceivablesPage

    script, _asked = answers
    with session_scope() as session:
        for name in ("أحمد", "سمير"):
            debts.create_debt(
                session, shop["owner"], direction=debts.RECEIVABLE,
                person=debts.PersonDetails(first_name=name, last_name="ب"),
                terms=debts.DebtTerms(amount=10000, debt_date=date.today()),
            )
    page = ReceivablesPage(_user(shop["owner"]))
    assert page.delete_button is page.selection.delete_button
    page.selection.select_all_box.click()
    script.append("delete")
    page.delete_button.click()
    assert _count(Debt) == 0
    assert page.table.rowCount() == 0


# --------------------------------------------------------- products, stock
def test_products_delete_unused_and_archive_the_ones_with_sales(shop, answers) -> None:
    from app.ui.pages.products_page import ProductsPage

    script, asked = answers
    with session_scope() as session:
        used = products.create_product(session, shop["owner"], name="iPhone 13", cash_price=110000)
        unused = products.create_product(session, shop["owner"], name="Redmi 13", cash_price=32000)
        stock.add_units(session, shop["owner"], product_id=unused.id, quantity=2)
        fallback = categories.get_fallback_type(session)
        customer = customers.create_customer(session, full_name="زبون", category_id=fallback.id)
        sales.create_sale(
            session, customer_id=customer.id, product="iPhone 13", sale_type="cash",
            wholesale_price=0, cash_price=110000, product_id=used.id,
        )
        used_id, unused_id = used.id, unused.id
    page = ProductsPage(_user(shop["owner"]))
    page.selection.select_all(True)
    script.extend(["delete", "archive"])
    page._delete_selected()
    assert asked == [["delete"], ["archive"]]
    with session_scope() as session:
        assert session.get(Product, unused_id) is None
        assert session.get(Product, used_id).active is False
    assert _count(StockItem) == 0


def test_stock_select_all_deletes_unsold_phones_only(shop, answers) -> None:
    from app.ui.pages.products_page import ProductsPage

    script, _asked = answers
    with session_scope() as session:
        product = products.create_product(session, shop["owner"], name="Phone", cash_price=30000)
        units = stock.add_units(session, shop["owner"], product_id=product.id, quantity=3)
        fallback = categories.get_fallback_type(session)
        customer = customers.create_customer(session, full_name="زبون", category_id=fallback.id)
        sales.create_sale(
            session, customer_id=customer.id, product="Phone", sale_type="cash",
            wholesale_price=0, cash_price=30000, product_id=product.id, stock_item_id=units[0].id,
        )
    panel = ProductsPage(_user(shop["owner"])).stock_panel
    panel.status_filter.setCurrentIndex(panel.status_filter.findData(None))
    assert panel.table.rowCount() == 3
    panel.selection.select_all(True)
    script.append("delete")
    panel._delete_selected()
    with session_scope() as session:
        assert [unit.status for unit in session.scalars(select(StockItem))] == [stock.STATUS_SOLD]


# ----------------------------------------------------------- client types
def test_client_types_tick_and_delete_with_reassignment(shop, answers, monkeypatch) -> None:
    from app.ui.dialogs import client_types_dialog
    from app.ui.dialogs.client_types_dialog import ClientTypesPanel

    script, _asked = answers
    with session_scope() as session:
        first = categories.create_client_type(session, shop["owner"], name="نوع أ", color="blue")
        second = categories.create_client_type(session, shop["owner"], name="نوع ب", color="teal")
        customer = customers.create_customer(session, full_name="زبون", category_id=first.id)
        fallback_id = categories.get_fallback_type(session).id
        first_id, second_id, customer_id = first.id, second.id, customer.id
    panel = ClientTypesPanel(shop["owner"])
    assert fallback_id not in panel.row_boxes  # the protected type has no active checkbox
    panel.select_all_box.click()
    assert {item.id for item in panel.checked_types()} == set(panel.row_boxes)
    panel.select_all_box.click()
    assert panel.checked_types() == []

    panel.set_checked(first_id, True)
    panel.set_checked(second_id, True)
    assert panel.delete_button.text() == f"{ar.SEL_DELETE} (2)"
    monkeypatch.setattr(client_types_dialog._ReassignDialog, "exec", lambda dialog: QDialog.DialogCode.Accepted)
    panel._delete_checked()
    with session_scope() as session:
        assert session.get(Customer, customer_id).category_id == fallback_id
        remaining = {item.id for item in categories.list_client_types(session)}
    assert first_id not in remaining and second_id not in remaining
    assert panel.checked_types() == []


# ----------------------------------------------------------------- import
def _workbook(path: Path, names: list[str]) -> Path:
    book = Workbook()
    sheet = book.active
    sheet.title = importer.SHEET_NAME
    fields = list(importer.REQUIRED_HEADERS)
    sheet.append([importer.HEADERS[field] for field in fields])
    for name in names:
        values = {
            "full_name": name, "product": "Redmi 13", "sale_type": "كاش", "wholesale_price": 25000,
            "cash_price": 32000, "rate": 0, "financed": 32000, "down_payment": 0, "months": 1,
            "purchase_date": date(2026, 7, 20),
        }
        sheet.append([values[field] for field in fields])
    book.save(path)
    return path


def test_import_rows_can_be_removed_restored_and_left_out(shop, monkeypatch) -> None:
    from app.ui.pages.import_page import ImportPage

    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    page = ImportPage(_user(shop["owner"]))
    page.load_file(_workbook(shop["tmp"] / "sheet.xlsx", ["أول", "ثاني", "ثالث"]))
    assert page.preview_table.rowCount() == 3

    page.selection.check_rows([1])
    assert page.selection.delete_button.text() == ar.IMP_REMOVE_ROWS
    page.selection.delete_button.click()
    assert [row.full_name for row in page._preview.rows] == ["أول", "ثالث"]
    assert page.removed_label.text() == ar.IMP_REMOVED_ROWS.format(count=1)
    assert not page.restore_button.isHidden()

    page.restore_button.click()
    assert len(page._preview.rows) == 3 and page.restore_button.isHidden()

    page.remove_rows([3])
    page._run_import()
    with session_scope() as session:
        names = set(session.scalars(select(Customer.full_name)))
    assert names == {"أول", "ثالث"}
