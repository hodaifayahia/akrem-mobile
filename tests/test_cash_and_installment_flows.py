"""Tests for the payment management overhaul: Cash vs Installments and partial/credit flows."""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox
from sqlalchemy.engine import Engine

from app.db.models import Category, Customer, Product, Sale, User
from app.db.session import session_scope
from app.services import auth, customers, sales
from app.services.pdf_forms import generate_cash_sale_receipt_pdf
from app.services.seed import seed_defaults
from app.ui.pages.new_sale_page import NewSalePage


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def sales_test_db(memory_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Seed test database with owner, seller, categories, products, and customer."""
    monkeypatch.setattr("app.db.session._application_engine", lambda url: memory_engine)
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        owner = auth.create_first_owner(
            session,
            username="owner_user",
            password="Password123!",
            password_confirmation="Password123!",
        )
        seller = auth.create_seller(
            session,
            owner.id,
            username="seller_user",
            password="Password123!",
            password_confirmation="Password123!",
        )
        cat = Category(name="إلكترونيات")
        session.add(cat)
        session.flush()

        prod = Product(name="Redmi Note 13", wholesale_price=25000, cash_price=32000, active=True)
        session.add(prod)
        session.flush()

        cust = customers.create_customer(
            session,
            full_name="كريم بن زيان",
            phone="0661234567",
            category_id=cat.id,
            address="وسط المدينة",
        )
        session.commit()

        return {
            "owner_id": owner.id,
            "seller_id": seller.id,
            "cat_id": cat.id,
            "prod_id": prod.id,
            "cust_id": cust.id,
        }


def test_module_switching(qapp, memory_engine: Engine, sales_test_db: dict[str, int]):
    """Test switching between Cash module and Installment module."""
    with session_scope(memory_engine) as session:
        owner = session.get(User, sales_test_db["owner_id"])
        page = NewSalePage(current_user=owner)

        # Default is cash module (index 0)
        assert page.module_stack.currentIndex() == 0
        assert page.tab_cash_btn.property("active") is True
        assert page.tab_installment_btn.property("active") is False

        # Switch to installment module (index 1)
        page.tab_installment_btn.click()
        assert page.module_stack.currentIndex() == 1
        assert page.tab_installment_btn.property("active") is True
        assert page.tab_cash_btn.property("active") is False
        assert page.sale_type.currentData() == "installment"

        # Switch back to cash module
        page.tab_cash_btn.click()
        assert page.module_stack.currentIndex() == 0
        assert page.tab_cash_btn.property("active") is True
        assert page.tab_installment_btn.property("active") is False


def test_cash_flow_fully_paid_spot_sale(qapp, memory_engine: Engine, sales_test_db: dict[str, int], monkeypatch: pytest.MonkeyPatch):
    """Test fully paid spot cash sale creates completed cash sale with 0 remaining balance."""
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No)

    with session_scope(memory_engine) as session:
        owner = session.get(User, sales_test_db["owner_id"])
        page = NewSalePage(current_user=owner)

        # Verify default status is 'paid'
        assert page._cash_status == "paid"
        assert page.cash_amount_paid.isEnabled() is False
        assert page.cash_walkin_check.isEnabled() is True
        assert page.cash_walkin_check.isChecked() is True
        assert page.cash_debt_warning.isVisible() is False

        # Fill product and total
        page.cash_product.setCurrentText("Redmi Note 13")
        page.cash_wholesale.setValue(25000)
        page.cash_total.setValue(32000)
        page._update_cash_calculations()

        # Check calculated fields
        assert "32,000" in page.c_tile_total[1].text()
        assert "32,000" in page.c_tile_collected[1].text()
        assert "0" in page.c_tile_debt[1].text()
        assert "7,000" in page.c_tile_profit[1].text()

        # Save spot cash sale
        page._save_cash_sale()

        # Verify sale in database
        saved = session.query(Sale).filter_by(sale_type="cash").first()
        assert saved is not None
        assert saved.product == "Redmi Note 13"
        assert saved.cash_price == 32000
        assert saved.down_payment == 0
        assert saved.total == 32000

        # Walk-in customer was created or associated
        assert saved.customer is not None
        assert saved.customer.full_name == "زبون نقدي مباشر"

        # Walk-in customer has 0 outstanding debt
        cust_summaries = customers.list_customers(
            session,
            year=date.today().year,
            month=date.today().month,
            today=date.today(),
            search="زبون نقدي مباشر",
        )
        assert len(cust_summaries) == 1
        assert cust_summaries[0].remaining_balance == 0


def test_cash_flow_partial_credit_requires_registered_customer(
    qapp, memory_engine: Engine, sales_test_db: dict[str, int], monkeypatch: pytest.MonkeyPatch
):
    """Test that partial payment / credit strictly requires an identified customer."""
    errors_shown = []
    monkeypatch.setattr(QMessageBox, "critical", lambda parent, title, msg: errors_shown.append(msg))
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, msg: errors_shown.append(msg))
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No)

    with session_scope(memory_engine) as session:
        owner = session.get(User, sales_test_db["owner_id"])
        page = NewSalePage(current_user=owner)
        page.show()

        # Switch status to credit
        page.cash_btn_partial_credit.click()
        assert page._cash_status == "credit"
        assert page.cash_amount_paid.isEnabled() is True
        assert page.cash_walkin_check.isEnabled() is False
        assert page.cash_walkin_check.isChecked() is False
        assert not page.cash_cust_select_container.isHidden()
        assert not page.cash_debt_warning.isHidden()

        page.cash_product.setCurrentText("Redmi Note 13")
        page.cash_total.setValue(32000)
        page.cash_amount_paid.setValue(12000)

        # Clear customer combo data to simulate missing customer selection
        page.cash_customer_combo.clear()

        page._save_cash_sale()
        assert len(errors_shown) > 0
        assert "يجب ربط المعاملة بحساب زبون مسجل" in errors_shown[0]


def test_cash_flow_partial_credit_save_and_ledger_update(
    qapp, memory_engine: Engine, sales_test_db: dict[str, int], monkeypatch: pytest.MonkeyPatch
):
    """Test saving partial credit sale accurately records debt and updates customer balance."""
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No)

    with session_scope(memory_engine) as session:
        owner = session.get(User, sales_test_db["owner_id"])
        page = NewSalePage(current_user=owner)

        # Set partial credit mode
        page._set_cash_status("credit")
        page.cash_product.setCurrentText("Redmi Note 13")
        page.cash_wholesale.setValue(25000)
        page.cash_total.setValue(32000)
        page.cash_amount_paid.setValue(12000)  # Down-payment / Montant versé
        page._update_cash_calculations()

        # Check remaining balance calculation: 32,000 - 12,000 = 20,000
        assert "32,000" in page.c_tile_total[1].text()
        assert "12,000" in page.c_tile_collected[1].text()
        assert "20,000" in page.c_tile_debt[1].text()

        # Select registered customer "كريم بن زيان"
        page.cash_customer_combo.addItem("كريم بن زيان", sales_test_db["cust_id"])
        page.cash_customer_combo.setCurrentIndex(0)

        # Save partial credit sale
        page._save_cash_sale()

        # Verify sale in database
        saved_credit = session.query(Sale).filter_by(sale_type="credit").first()
        assert saved_credit is not None
        assert saved_credit.customer_id == sales_test_db["cust_id"]
        assert saved_credit.cash_price == 32000
        assert saved_credit.down_payment == 12000
        assert saved_credit.financed == 20000
        assert saved_credit.expected_pay_date is not None

        # Verify customer outstanding balance / ledger
        cust_summaries = customers.list_customers(
            session,
            year=date.today().year,
            month=date.today().month,
            today=date.today(),
            search="كريم",
        )
        assert len(cust_summaries) == 1
        summary = cust_summaries[0]
        assert summary.remaining_balance == 20000  # Debt recorded in customer ledger


def test_generate_cash_sale_receipt_pdf(tmp_path: Path):
    """Test PDF receipt generator produces valid A5 receipt file for cash and credit."""
    cash_pdf = tmp_path / "recu_cash.pdf"
    out_cash = generate_cash_sale_receipt_pdf(
        path=cash_pdf,
        customer_name="زبون نقدي مباشر",
        customer_phone=None,
        product="Samsung Galaxy A55",
        sale_type="cash",
        total=54000,
        amount_paid=54000,
        remaining_balance=0,
        purchase_date=date.today(),
        issued_by="admin",
    )
    assert out_cash.exists()
    assert out_cash.stat().st_size > 1000

    credit_pdf = tmp_path / "recu_credit.pdf"
    out_credit = generate_cash_sale_receipt_pdf(
        path=credit_pdf,
        customer_name="عمر بوعزة",
        customer_phone="0555123456",
        product="iPhone 15 Pro",
        sale_type="credit",
        total=180000,
        amount_paid=50000,
        remaining_balance=130000,
        purchase_date=date.today(),
        expected_pay_date=date.today() + timedelta(days=30),
        issued_by="admin",
    )
    assert out_credit.exists()
    assert out_credit.stat().st_size > 1000
