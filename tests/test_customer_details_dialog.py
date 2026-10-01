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
    assert owner_dialog.details_table.item(0, 1).text() == "Cash phone"
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
