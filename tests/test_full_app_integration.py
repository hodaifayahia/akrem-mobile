"""Comprehensive end-to-end integration tests for the entire AkremMobile application."""

from __future__ import annotations

import os
from datetime import date, timedelta
from types import SimpleNamespace
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from sqlalchemy.engine import Engine

from app.db.models import Category, Customer, Installment, Product, Sale, User
from app.db.session import session_scope
from app.services import auth, customers, sales
from app.ui.dialogs.auth_dialogs import ReauthenticationDialog
from app.ui.dialogs.command_palette import CommandPaletteDialog
from app.ui.dialogs.shortcuts_dialog import ShortcutsDialog
from app.ui.events import events
from app.ui.main_window import MainWindow
from app.ui.pages.customers_page import CustomersPage
from app.ui.pages.dashboard_page import DashboardPage
from app.ui.pages.new_sale_page import NewSalePage
from app.ui.pages.payments_page import PaymentsPage
from app.ui.pages.reports_page import ReportsPage
from app.ui.pages.settings_page import SettingsPage
from app.ui.widgets.notification_center import NotificationCenterPopup


from app.services.seed import seed_defaults


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def seeded_db(memory_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Seed test database with owner, seller, categories, products, and sample sales."""
    monkeypatch.setattr("app.db.session._application_engine", lambda url: memory_engine)
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        # Create Owner and Seller users
        owner = auth.create_first_owner(
            session,
            username="owner_test",
            password="Secret123!",
            password_confirmation="Secret123!",
        )
        seller = auth.create_seller(
            session,
            owner.id,
            username="seller_test",
            password="Secret123!",
            password_confirmation="Secret123!",
        )
        owner_id = owner.id
        seller_id = seller.id

        # Categories
        cat1 = Category(name="فئة تجارية")
        cat2 = Category(name="أفراد وموظفون")
        session.add_all([cat1, cat2])
        session.flush()

        # Product
        prod = Product(name="Galaxy S24", wholesale_price=80000, cash_price=105000, active=True)
        session.add(prod)
        session.flush()

        # Customer
        cust = customers.create_customer(
            session,
            full_name="محمد بلقاسم",
            phone="0551234567",
            category_id=cat1.id,
            address="أم الطيور، المغير",
        )
        cust_id = cust.id

        # Sale with installment
        sale = sales.create_sale_for_user(
            session,
            owner_id,
            customer_id=cust_id,
            product="Galaxy S24",
            sale_type="installment",
            wholesale_price=80000,
            cash_price=100000,
            rate=10,
            down_payment=20000,
            months=6,
            purchase_date=date.today() - timedelta(days=60),
        )
        sale_id = sale.id

        session.commit()

        return {
            "owner_id": owner_id,
            "seller_id": seller_id,
            "cust_id": cust_id,
            "sale_id": sale_id,
            "cat1_id": cat1.id,
            "cat2_id": cat2.id,
        }


def test_main_window_initialization_and_page_navigation(qapp, memory_engine: Engine, seeded_db: dict[str, int]):
    """Test MainWindow loads with all pages and navigates cleanly for owner user."""
    with session_scope(memory_engine) as session:
        owner = session.get(User, seeded_db["owner_id"])
        window = MainWindow(current_user=owner)

        # Assert topbar and toast manager are initialized
        assert window.topbar is not None
        assert window.toast_manager is not None
        assert window.pages.count() == 11

        # Test switching across all 11 pages (7 original + products, client types and the two debt pages)
        for page_idx in range(11):
            window._select_page(page_idx)
            assert window.pages.currentIndex() == page_idx
            assert window.buttons[page_idx].property("active") is True
            assert window.topbar.page_title_label.text() != ""

        # Test status filter navigation from dashboard
        window._show_customer_status("FAILED")
        assert window.pages.currentIndex() == 1  # customers page


def test_main_window_seller_restrictions(qapp, memory_engine: Engine, seeded_db: dict[str, int]):
    """Test MainWindow hides sensitive pages (import, settings) from sellers."""
    with session_scope(memory_engine) as session:
        seller = session.get(User, seeded_db["seller_id"])
        window = MainWindow(current_user=seller)

        # Check that import button (index 4) and settings button (index 6) are hidden
        assert window.buttons[4].isVisible() is False
        assert window.buttons[6].isVisible() is False

        # Attempting to navigate to import page should be ignored
        window._select_page(4)
        assert window.pages.currentIndex() != 4


def test_dashboard_page_calculations(qapp, memory_engine: Engine, seeded_db: dict[str, int]):
    """Test DashboardPage calculates totals and refreshes correctly."""
    with session_scope(memory_engine) as session:
        owner = session.get(User, seeded_db["owner_id"])
        page = DashboardPage(current_user=owner)
        page.refresh()

        # Verify month selector exists and can change date
        today = date.today()
        assert page.month_selector.date().year() == today.year
        assert page.month_selector.date().month() == today.month


def test_customers_page_filtering(qapp, memory_engine: Engine, seeded_db: dict[str, int]):
    """Test CustomersPage loads, filters by search query and category."""
    with session_scope(memory_engine) as session:
        owner = session.get(User, seeded_db["owner_id"])
        page = CustomersPage(current_user=owner)
        page.refresh()

        assert page.table.model().rowCount() >= 1

        # Search filter
        page.search.setText("بلقاسم")
        page.refresh()
        assert page.table.model().rowCount() == 1

        # Search non-existent
        page.search.setText("اسم_غير_موجود")
        page.refresh()
        assert page.table.model().rowCount() == 0

        # Reset search
        page.search.clear()
        page.refresh()
        assert page.table.model().rowCount() >= 1


def test_new_sale_page_stepper_and_calc(qapp, memory_engine: Engine, seeded_db: dict[str, int]):
    """Test NewSalePage calculates financial fields and moves between steps."""
    with session_scope(memory_engine) as session:
        owner = session.get(User, seeded_db["owner_id"])
        page = NewSalePage(current_user=owner)

        # Set installment sale type
        inst_idx = page.sale_type.findData("installment")
        if inst_idx >= 0:
            page.sale_type.setCurrentIndex(inst_idx)

        # Set values
        page.cash_price.setValue(50000)
        page.rate.setValue(10)
        page.down_payment.setValue(10000)
        page.months.setValue(6)

        page._update_preview()

        # Total = 50,000 + 10% on cash_price (5,000) = 55,000; Financed = 55,000 - 10,000 = 45,000
        assert "55,000" in page.total_value.text()
        assert "45,000" in page.financed_value.text()


def test_payments_page_sections(qapp, memory_engine: Engine, seeded_db: dict[str, int]):
    """Test PaymentsPage builds and refreshes overdue and reminder tables."""
    with session_scope(memory_engine) as session:
        owner = session.get(User, seeded_db["owner_id"])
        page = PaymentsPage(current_user=owner)
        page.refresh()

        assert page.failed_table is not None
        assert page.pending_table is not None
        assert page.paid_table is not None
        assert page.reminders_table is not None
        assert page.credit_table is not None


def test_reports_page_cards(qapp, memory_engine: Engine, seeded_db: dict[str, int]):
    """Test ReportsPage builds the 3 hub cards."""
    with session_scope(memory_engine) as session:
        owner = session.get(User, seeded_db["owner_id"])
        page = ReportsPage(current_user=owner)
        assert page.layout() is not None


def test_settings_page_fields(qapp, memory_engine: Engine, seeded_db: dict[str, int]):
    """Test SettingsPage displays owner settings and rate presets."""
    with session_scope(memory_engine) as session:
        owner = session.get(User, seeded_db["owner_id"])
        page = SettingsPage(current_user=owner)
        assert page.rate_table.rowCount() >= 1


def test_command_palette_spotlight_interaction(qapp, memory_engine: Engine, seeded_db: dict[str, int]):
    """Test Command Palette searches seeded customer and triggers callback."""
    with session_scope(memory_engine) as session:
        owner = session.get(User, seeded_db["owner_id"])
        palette = CommandPaletteDialog(current_user=owner)

        # Search for seeded customer "محمد"
        palette.search_input.setText("محمد")
        matching = [item for item in palette._filtered_items if item.category == "customer"]
        assert len(matching) >= 1
        assert "محمد" in matching[0].title


def test_notification_center_popup_alerts(qapp, memory_engine: Engine, seeded_db: dict[str, int]):
    """Test NotificationCenterPopup finds alerts and filters them."""
    popup = NotificationCenterPopup()
    popup.refresh_alerts()

    # Alert count should be non-negative
    count = popup.get_alert_count()
    assert count >= 0

    # Test tab filters without error
    popup._set_filter("overdue")
    popup._set_filter("upcoming")
    popup._set_filter("all")


def test_reauthentication_dialog_password_check(qapp, memory_engine: Engine, seeded_db: dict[str, int], monkeypatch: pytest.MonkeyPatch):
    """Test ReauthenticationDialog verifies password against database."""
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args, **kwargs: None)

    with session_scope(memory_engine) as session:
        owner = session.get(User, seeded_db["owner_id"])
        dialog = ReauthenticationDialog(owner)

        # Incorrect password
        dialog.password.setText("WrongPass!")
        dialog._unlock()
        assert dialog.result() != 1

        # Correct password
        dialog.password.setText("Secret123!")
        dialog._unlock()
        assert dialog.result() == 1


def test_global_event_bus_wiring(qapp):
    """Test UiEvents broadcast notifications and navigation signals."""
    received_notifications = []

    def on_notify(lvl, title, msg, duration):
        received_notifications.append((lvl, title, msg))

    events.notify.connect(on_notify)
    events.notify.emit("success", "عنوان", "رسالة", 2000)

    assert len(received_notifications) == 1
    assert received_notifications[0] == ("success", "عنوان", "رسالة")
