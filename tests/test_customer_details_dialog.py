"""Offscreen smoke tests for the customer details dialog."""

from __future__ import annotations

import os
from datetime import date

import pytest
from PySide6.QtWidgets import QApplication, QDialog
from sqlalchemy.engine import Engine

from app.db.models import Category, User
from app.db.session import session_scope
from app.services import customers, sales
from app.ui.dialogs.customer_details_dialog import CustomerDetailsDialog


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    """Create a headless Qt application for dialog construction tests."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_customer_details_dialog_switches_sales_and_hides_owner_fields(
    memory_engine: Engine,
    qt_app: QApplication,
) -> None:
    """Show multiple sales while keeping wholesale and profit owner-only."""
    with session_scope(memory_engine) as session:
        category = Category(name="Test")
        session.add(category)
        session.flush()
        customer = customers.create_customer(
            session,
            full_name="Test Customer",
            category_id=category.id,
            phone="0550000001",
        )
        customer_id = customer.id
        sales.create_sale(
            session,
            customer_id=customer.id,
            product="Installment phone",
            sale_type="installment",
            wholesale_price=30_000,
            cash_price=40_000,
            rate=0,
            months=2,
            purchase_date=date(2026, 9, 1),
        )
        sales.create_sale(
            session,
            customer_id=customer.id,
            product="Cash phone",
            sale_type="cash",
            wholesale_price=20_000,
            cash_price=25_000,
            purchase_date=date(2026, 10, 1),
        )

    owner_dialog = CustomerDetailsDialog(
        customer_id,
        User(username="owner", password_hash="unused", role="owner"),
        engine=memory_engine,
    )
    assert owner_dialog.sale_selector.count() == 2
    owner_fields = {
        owner_dialog.details_table.item(row, column).text()
        for row in range(owner_dialog.details_table.rowCount())
        for column in (0, 2)
        if owner_dialog.details_table.item(row, column) is not None
    }
    assert "سعر الجملة" in owner_fields
    assert "الربح" in owner_fields
    assert owner_dialog.financial_table.columnCount() == 7
    financial_headers = [
        owner_dialog.financial_table.horizontalHeaderItem(col).text()
        for col in range(7)
    ]
    assert financial_headers == [
        "سعر الجملة", "سعر ديطاي", "نسبة التقسيط %",
        "إجمالي المبلغ بعد التقسيط", "الاقتطاع الشهري", "كم شهر", "الربح الإجمالي"
    ]
    assert owner_dialog.financial_table.item(0, 0).text() == "30,000 دج"
    assert owner_dialog.financial_table.item(0, 1).text() == "40,000 دج"
    assert owner_dialog.financial_table.item(0, 2).text() == "0%"
    assert owner_dialog.financial_table.item(0, 3).text() == "40,000 دج"
    assert owner_dialog.financial_table.item(0, 4).text() == "20,000 دج"
    assert owner_dialog.financial_table.item(0, 5).text() == "2 أشهر"
    assert owner_dialog.financial_table.item(0, 6).text() == "10,000 دج"

    owner_dialog.sale_selector.setCurrentIndex(1)
    # Latin product names stay in reading order inside the right-to-left table.
    assert owner_dialog.details_table.item(0, 1).text() == "\u2066Cash phone\u2069"
    owner_dialog.close()

    seller_dialog = CustomerDetailsDialog(
        customer_id,
        User(username="seller", password_hash="unused", role="seller"),
        engine=memory_engine,
    )
    seller_fields = {
        seller_dialog.details_table.item(row, column).text()
        for row in range(seller_dialog.details_table.rowCount())
        for column in (0, 2)
        if seller_dialog.details_table.item(row, column) is not None
    }
    assert "سعر الجملة" not in seller_fields
    assert "الربح" not in seller_fields
    assert seller_dialog.financial_table.item(0, 0).text() == "—"
    assert seller_dialog.financial_table.item(0, 6).text() == "—"

    requested: list[int] = []
    seller_dialog.edit_requested.connect(requested.append)
    seller_dialog._request_edit()
    assert requested == [customer_id]
    assert seller_dialog.result() == QDialog.DialogCode.Accepted


def test_customer_details_dialog_scrolls_and_fits_the_screen(
    memory_engine: Engine,
    qt_app: QApplication,
) -> None:
    """A long schedule scrolls inside the sale tab; the dialog stays on screen."""
    with session_scope(memory_engine) as session:
        category = Category(name="Long plan")
        session.add(category)
        session.flush()
        customer = customers.create_customer(session, full_name="Long Plan", category_id=category.id)
        customer_id = customer.id
        sales.create_sale(
            session, customer_id=customer.id, product="Phone", sale_type="installment",
            wholesale_price=30_000, cash_price=40_000, rate=0, months=24, purchase_date=date(2026, 9, 1),
        )
    dialog = CustomerDetailsDialog(
        customer_id, User(username="owner", password_hash="unused", role="owner"), engine=memory_engine,
    )
    dialog.show()
    qt_app.processEvents()
    area = dialog.screen().availableGeometry()
    assert area.contains(dialog.frameGeometry())
    table = dialog.schedule_table
    assert table.rowCount() == 24
    # Every schedule row is laid out; the sale tab scrolls instead of the table.
    assert table.height() >= sum(table.rowHeight(row) for row in range(24))
    assert dialog.sale_scroll.verticalScrollBar().maximum() > 0
    dialog.close()


def test_the_schedule_offers_postponing_an_unpaid_installment(
    memory_engine: Engine,
    qt_app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The schedule's "postpone" button opens the three choices and applies the chosen one."""
    from PySide6.QtWidgets import QMessageBox, QPushButton

    from app.db.models import Installment
    from app.i18n import ar
    from app.services import auth, postponement
    from app.ui.dialogs import postpone_dialog

    with session_scope(memory_engine) as session:
        owner = auth.create_first_owner(
            session, username="owner", password="Secret123", password_confirmation="Secret123"
        )
        category = Category(name="Test")
        session.add(category)
        session.flush()
        customer_id = customers.create_customer(session, full_name="Postpone", category_id=category.id).id
        sale_id = sales.create_sale(
            session, customer_id=customer_id, product="A16", sale_type="installment",
            wholesale_price=20_000, cash_price=30_000, rate=0, months=3, purchase_date=date(2026, 10, 3),
        ).id
        session.expunge(owner)

    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(postpone_dialog.PostponeDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(
        postpone_dialog.PostponeDialog, "values", lambda self: (postponement.KIND_DAYS, 3, None)
    )
    dialog = CustomerDetailsDialog(customer_id, owner, engine=memory_engine)
    buttons = dialog.schedule_table.cellWidget(0, 5).findChildren(QPushButton)
    postpone = next(button for button in buttons if button.text() == ar.POSTPONE_ACTION)
    postpone.click()

    with session_scope(memory_engine) as session:
        first = session.query(Installment).filter_by(sale_id=sale_id, installment_index=1).one()
        assert first.due_date == date(2026, 11, 6)
    assert dialog.schedule_table.item(0, 1).text() == "06/11/2026"
    dialog.close()
