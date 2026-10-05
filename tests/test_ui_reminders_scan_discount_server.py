"""On screen: weekly WhatsApp reminders, barcode scanning, the seller's discount approval,
and connecting a PC to the shop server."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from sqlalchemy import select

from app.db.models import AuditLog, Sale, User
from app.db.session import dispose_database_engines, session_scope
from app.i18n import ar
from app.services import auth, categories, customers, products, sales, stock


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def shop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp) -> dict:
    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AKREMMOBILE_DATABASE_URL", raising=False)
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.No)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: ("", ""))
    from app.db.migrate import upgrade_database
    from app.services.seed import seed_defaults

    upgrade_database()
    seed_defaults()
    with session_scope() as session:
        owner = auth.create_first_owner(session, username="akrem", password="Secret123", password_confirmation="Secret123")
        seller = auth.create_seller(
            session, owner.id, username="vendeur", password="Secret123", password_confirmation="Secret123"
        )
        redmi = products.create_product(session, owner.id, name="Redmi 13", wholesale_price=25000, cash_price=40000)
        (unit,) = stock.add_units(
            session, owner.id, product_id=redmi.id, details=stock.UnitDetails(imei="356789012345678", color="Blue")
        )
        fallback = categories.get_fallback_type(session).id
        soon = customers.create_customer(session, full_name="قريب", phone="0550000001", category_id=fallback)
        late = customers.create_customer(session, full_name="متأخر", phone="0550000002", category_id=fallback)
        today = date.today()
        sales.create_sale(session, customer_id=soon.id, product="X", sale_type="installment", wholesale_price=0,
                          cash_price=30000, months=3, purchase_date=today - timedelta(days=28))
        sales.create_sale(session, customer_id=late.id, product="Y", sale_type="installment", wholesale_price=0,
                          cash_price=30000, months=3, purchase_date=today - timedelta(days=75))
        ids = {"owner": owner.id, "seller": seller.id, "redmi": redmi.id, "unit": unit.id,
               "soon": soon.id, "late": late.id}
    yield ids
    dispose_database_engines()


def _user(user_id: int) -> User:
    with session_scope() as session:
        return session.get(User, user_id)


# ------------------------------------------------------------------ weekly reminders
def test_weekly_reminders_open_whatsapp_one_after_the_other(shop) -> None:
    from app.ui.dialogs.reminders_dialog import WeeklyRemindersDialog
    from app.ui.pages.payments_page import PaymentsPage

    assert PaymentsPage(_user(shop["owner"])).remind_week_button.text().endswith(ar.REMIND_WEEK_BUTTON)
    dialog = WeeklyRemindersDialog(_user(shop["owner"]))
    opened: list[str] = []
    dialog.opener = opened.append
    names = [dialog.table.item(row, 1).text() for row in range(dialog.table.rowCount())]
    assert names[0] == "متأخر" and "قريب" in names  # late clients first
    assert len(dialog.ticked_rows()) == len(names)
    assert "متأخر" in dialog.preview.toPlainText()

    assert dialog.send_next() == 0
    assert opened[0].startswith("https://wa.me/213550000002?text=")
    assert dialog.table.item(0, 5).text() == ar.REMIND_SENT_NOW
    while dialog.send_next() is not None:
        pass
    assert len(opened) == len(names) and not dialog.next_button.isEnabled()
    with session_scope() as session:
        logged = session.scalars(select(AuditLog.entity_id).where(AuditLog.action == "whatsapp_reminder")).all()
    assert sorted(logged) == sorted({shop["soon"], shop["late"]})

    again = WeeklyRemindersDialog(_user(shop["owner"]))
    assert again.ticked_rows() == []  # everyone was reminded today
    assert again.table.item(0, 5).text() != "—"


# ------------------------------------------------------------------ barcode / IMEI
def test_scanning_a_phone_on_the_sale_form_picks_it(shop) -> None:
    from app.ui.pages.new_sale_page import NewSalePage

    page = NewSalePage(current_user=_user(shop["owner"]))
    details = page.cash_details
    details.scan.setText("356789012345678")
    details.scan.returnPressed.emit()
    assert page.cash_product.currentText() == "Redmi 13"
    assert details.unit.currentData() == shop["unit"]
    assert page.cash_total.value() == 40000 and details.color.currentText() == "Blue"
    assert details.scan_state.text() == ar.SCAN_FOUND.format(product="Redmi 13")
    assert details.scan.text() == ""  # ready for the next scan

    page.sale_details.scan.setText("490154203237518")  # not in stock
    page.sale_details.scan.returnPressed.emit()
    assert page.sale_details.imei.text() == "490154203237518"
    assert page.sale_details.scan_state.text() == ar.SCAN_NOT_IN_STOCK
    page.sale_details.scan.setText("hello")
    page.sale_details.scan.returnPressed.emit()
    assert page.sale_details.scan_state.text() == ar.SCAN_NOT_FOUND.format(code="hello")


def test_receiving_goods_with_a_scanner_adds_one_phone_per_imei(shop, monkeypatch) -> None:
    from app.ui.pages.products_page import ProductsPage

    page = ProductsPage(_user(shop["owner"]))
    dialog = page.stock_panel.new_dialog(title=ar.PROD_RECEIVE)
    dialog.product.setCurrentText("Galaxy A05")
    dialog.cash_price.setValue(21000)
    dialog.scanned.setPlainText("356789012345680\n356789012345681\n356789012345680\n")
    assert dialog.quantity.value() == 2 and not dialog.quantity.isEnabled()
    assert dialog.scanned_state.text() == ar.SCAN_IMEIS_COUNT.format(count=2)
    assert page.stock_panel.save_dialog(dialog)
    with session_scope() as session:
        galaxy = products.find_by_name(session, "galaxy a05")
        assert sorted(unit.imei for unit in stock.available_units(session, galaxy.id)) == [
            "356789012345680", "356789012345681",
        ]

    bad = page.stock_panel.new_dialog()
    bad.product.setCurrentText("Galaxy A05")
    bad.cash_price.setValue(21000)
    bad.scanned.setPlainText("356789012345682\nnot-an-imei")
    assert not page.stock_panel.save_dialog(bad)
    assert "not-an-imei" in bad.error.text.text()


# ------------------------------------------------------------------ seller discount
def test_a_seller_discount_above_the_limit_needs_the_owner(shop, monkeypatch) -> None:
    import app.ui.pages.new_sale_page as sale_module
    from app.ui.pages.new_sale_page import NewSalePage

    page = NewSalePage(current_user=_user(shop["seller"]))
    assert page.cash_price.isEnabled()  # sellers may type a price now
    page.cash_product.setCurrentText("Redmi 13")
    page.cash_total.setValue(36000)  # 10% off, limit 5%

    asked: list[tuple[int, int]] = []
    monkeypatch.setattr(sale_module, "ask_owner_approval", lambda percent, limit, parent: asked.append((percent, limit)))
    page._save_cash_sale()
    assert asked == [(10, 5)]
    with session_scope() as session:
        assert session.scalars(select(Sale)).all()[2:] == []  # only the two fixture sales

    monkeypatch.setattr(sale_module, "ask_owner_approval", lambda percent, limit, parent: shop["owner"])
    page.cash_product.setCurrentText("Redmi 13")
    page.cash_total.setValue(36000)
    page._save_cash_sale()
    with session_scope() as session:
        sale = session.scalar(select(Sale).where(Sale.product == "Redmi 13"))
        assert sale is not None and sale.cash_price == 36000
        approval = session.scalar(select(AuditLog).where(AuditLog.action == "discount_approved"))
        assert approval.user_id == shop["owner"] and approval.entity_id == sale.id


def test_owner_approval_box_checks_the_password(shop) -> None:
    from app.ui.dialogs.owner_approval_dialog import OwnerApprovalDialog

    dialog = OwnerApprovalDialog(12, 5)
    dialog.username.setText("vendeur")
    dialog.password.setText("Secret123")
    assert not dialog.approve() and dialog.owner_id is None  # a seller cannot approve
    assert dialog.error.text.text() == ar.DISCOUNT_APPROVAL_WRONG
    dialog.username.setText("akrem")
    dialog.password.setText("Secret123")
    assert dialog.approve() and dialog.owner_id == shop["owner"]


def test_owner_sets_the_seller_discount_limit_in_settings(shop) -> None:
    from app.services import settings
    from app.ui.pages.settings_page import SettingsPage

    page = SettingsPage(_user(shop["owner"]))
    assert page.seller_discount.value() == 5
    page.seller_discount.setValue(8)
    page._save_owner_settings()
    with session_scope() as session:
        assert settings.get_seller_max_discount(session) == 8
    assert not SettingsPage(_user(shop["seller"])).server_group.isVisibleTo(page)


# ------------------------------------------------------------------ shop server
def test_server_dialog_reports_an_unreachable_server_and_keeps_this_pc_local(shop) -> None:
    from app.services import shared_db
    from app.ui.dialogs.auth_dialogs import SetupOwnerDialog
    from app.ui.dialogs.server_dialog import ServerConnectionDialog
    from app.ui.pages.settings_page import SettingsPage

    settings_page = SettingsPage(_user(shop["owner"]))
    assert settings_page.server_state.text() == ar.SERVER_MODE_LOCAL
    assert SetupOwnerDialog().server_button.text().endswith(ar.SERVER_SETUP_LINK)

    dialog = ServerConnectionDialog()
    assert not dialog.save()  # no address yet
    assert dialog.error.text.text() == ar.SERVER_HOST_REQUIRED
    dialog.host.setText("127.0.0.1")
    dialog.port.setValue(1)
    dialog.password.setText("x")
    assert not dialog.save()
    assert dialog.error.text.text() == ar.SERVER_UNREACHABLE
    assert shared_db.current_server() is None and not dialog.saved


def test_start_up_does_not_wait_for_a_server_when_local(shop) -> None:
    from app.main import wait_for_shop_server

    assert wait_for_shop_server()
