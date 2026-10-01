"""End-to-end checks for the shop owner's requirements, one test per requirement.

Everything runs against a real, migrated SQLite file (as on a shop PC) and
drives the actual screens where the requirement is about the interface.
Dates are relative to today, and the due mode is "purchase_day", so the
pending / failed / paid examples hold on any day of the month.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from PySide6.QtWidgets import QApplication, QMessageBox
from sqlalchemy import select

from app.db.models import Customer, Sale, User
from app.db.session import dispose_database_engines, session_scope
from app.i18n import ar
from app.services import auth, backup, categories, customers, importer, payments, reports, sales, settings
from app.services.calc import add_months
from app.services.status import Status


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def shop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp) -> dict:
    """A migrated file database with an owner, a seller and four kinds of customer.

    * Pending - installment due today, not yet paid (inside the grace period).
    * Failed  - installment due last month + 10 days, never paid.
    * Paid    - installment due today, paid in full.
    * Cash    - one cash purchase, nothing to collect.
    * Credit  - one credit purchase with a remaining balance.
    """
    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AKREMMOBILE_DATABASE_URL", raising=False)
    from PySide6.QtCore import QSettings

    QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)

    from app.db.migrate import upgrade_database
    from app.services.seed import seed_defaults

    upgrade_database()
    seed_defaults()
    today = date.today()
    ids: dict[str, int] = {"today": today.toordinal()}
    with session_scope() as session:
        settings.set_value(session, "due_mode", "purchase_day")
        owner = auth.create_first_owner(
            session, username="akrem", password="Secret123", password_confirmation="Secret123"
        )
        seller = auth.create_seller(
            session, owner.id, username="vendeur", password="Secret123", password_confirmation="Secret123"
        )
        types = {t.name: t.id for t in categories.list_client_types(session)}

        def customer(name: str, type_name: str, phone: str) -> int:
            return customers.create_customer(
                session, full_name=name, category_id=types[type_name], phone=phone
            ).id

        def installment_sale(customer_id: int, purchase: date, product: str) -> Sale:
            return sales.create_sale(
                session, customer_id=customer_id, product=product, sale_type="installment",
                wholesale_price=41500, cash_price=47000, rate=35, down_payment=12300, months=6,
                purchase_date=purchase,
            )

        ids["pending"] = customer("ليلى خليفي", "أساتذة", "0661000001")
        installment_sale(ids["pending"], add_months(today, -1), "A17 6/128")
        ids["failed"] = customer("محمد بن علي", "عسكري", "0661000002")
        installment_sale(ids["failed"], add_months(today, -2) - timedelta(days=10), "iPhone 13")
        ids["paid"] = customer("سارة بوزيد", "منحة البطالة", "0661000003")
        paid_sale = installment_sale(ids["paid"], add_months(today, -1), "Redmi Note 13")
        first = min(paid_sale.installments, key=lambda row: row.installment_index)
        payments.record_installment_payment(session, first.id, first.amount_due, today, "cash")
        ids["cash"] = customer("يوسف قاسمي", "أعمال حرة", "0661000004")
        sales.create_sale(
            session, customer_id=ids["cash"], product="Galaxy A55", sale_type="cash",
            wholesale_price=50000, cash_price=58000, purchase_date=today,
        )
        ids["credit"] = customer("كريم عمراني", "أعمال حرة", "0661000005")
        sales.create_sale(
            session, customer_id=ids["credit"], product="Airpods", sale_type="credit",
            wholesale_price=9000, cash_price=12000, down_payment=2000, purchase_date=today,
        )
        ids["owner"] = owner.id
        ids["seller"] = seller.id
    yield ids
    dispose_database_engines()


def _user(user_id: int) -> User:
    with session_scope() as session:
        return session.get(User, user_id)


def _month_args() -> dict:
    today = date.today()
    return {"year": today.year, "month": today.month, "today": today}


# 1. Customer categories --------------------------------------------------------
def test_default_categories_exist_and_owner_can_add_many_more(shop) -> None:
    with session_scope() as session:
        names = [t.name for t in categories.list_client_types(session)]
        assert names[:4] == ["أساتذة", "منحة البطالة", "عسكري", "أعمال حرة"]
        for index in range(12):
            categories.create_client_type(session, shop["owner"], name=f"نوع إضافي {index + 1}", color="teal")
        types = categories.list_client_types(session)
    assert len(types) == 5 + 12
    assert types[-1].is_system  # "غير مصنف" stays last as the catch-all

    from app.ui.pages.customers_page import CustomersPage

    page = CustomersPage(_user(shop["owner"]))
    assert len(page.type_filter._chips) == 1 + 17  # "All" + every type


# 2. Customer list columns ----------------------------------------------------------
def test_customer_list_shows_name_phone_purchase_and_end_dates(shop) -> None:
    from app.ui.pages.customers_page import CustomersPage

    page = CustomersPage(_user(shop["owner"]))
    model = page.table.model()
    headers = [model.headerData(c, __import__("PySide6.QtCore").QtCore.Qt.Orientation.Horizontal)
               for c in range(model.columnCount())]
    for wanted in ("الاسم واللقب", "رقم الهاتف", "تاريخ الشراء", "تاريخ نهاية الاقتطاع", "الحالة"):
        assert wanted in headers
    row = next(r for r in range(model.rowCount()) if model.index(r, 1).data() == "ليلى خليفي")
    purchase = add_months(date.today(), -1)
    assert model.index(row, 2).data() == "0661000001"
    assert model.index(row, 4).data() == purchase.strftime("%d/%m/%Y")
    assert model.index(row, 5).data() == add_months(purchase, 6).strftime("%d/%m/%Y")


# 3. Monthly status dots ----------------------------------------------------------------
def test_status_this_month_is_pending_orange_failed_red_paid_green(shop) -> None:
    from app.ui.pages.customers_page import _STATUS_COLORS

    with session_scope() as session:
        rows = {row.id: row.status for row in customers.list_customers(session, **_month_args())}
    assert rows[shop["pending"]] == Status.PENDING
    assert rows[shop["failed"]] == Status.FAILED
    assert rows[shop["paid"]] == Status.PAID
    assert _STATUS_COLORS[Status.PENDING] == "#F59E0B"  # orange
    assert _STATUS_COLORS[Status.FAILED] == "#EF4444"   # red
    assert _STATUS_COLORS[Status.PAID] == "#22C55E"     # green

    from app.ui.pages.customers_page import CustomersPage

    page = CustomersPage(_user(shop["owner"]))
    page.set_status_filter("FAILED")
    names = [page.table.model().index(r, 1).data() for r in range(page.table.model().rowCount())]
    assert names == ["محمد بن علي"]


# 4. Clicking a name opens the price/installment table ----------------------------------
def test_clicking_a_name_opens_the_installment_and_price_table(shop) -> None:
    from PySide6.QtCore import Qt

    from app.ui.dialogs.customer_details_dialog import CustomerDetailsDialog
    from app.ui.pages.customers_page import CustomersPage

    page = CustomersPage(_user(shop["owner"]))
    opened: list[int] = []
    page.customer_selected.connect(opened.append)
    model = page.table.model()
    row = next(r for r in range(model.rowCount()) if model.index(r, 1).data() == "ليلى خليفي")
    page._cell_clicked(model.index(row, 1))
    assert opened == [shop["pending"]]

    dialog = CustomerDetailsDialog(shop["pending"], _user(shop["owner"]))
    table = dialog.financial_table
    headers = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
    assert headers == ["سعر الجملة", "سعر ديطاي", "نسبة التقسيط %", "إجمالي المبلغ بعد التقسيط",
                       "الاقتطاع الشهري", "كم شهر", "الربح الإجمالي"]
    values = [table.item(0, c).text() for c in range(table.columnCount())]
    # 47,000 + 35% = 63,450; financed 51,150 over 6 months = 8,525; profit 63,450 - 41,500
    assert values == ["41,500 دج", "47,000 دج", "35%", "63,450 دج", "8,525 دج", "6 أشهر", "21,950 دج"]
    assert table.item(0, 0).textAlignment() & int(Qt.AlignmentFlag.AlignHCenter)

    seller_view = CustomerDetailsDialog(shop["pending"], _user(shop["seller"]))
    hidden = [seller_view.financial_table.item(0, c).text() for c in (0, 6)]
    assert hidden == ["—", "—"]  # sellers never see wholesale price or profit


# 5. Dashboard headline cards --------------------------------------------------------------
def test_dashboard_wholesale_total_and_operation_counts(shop) -> None:
    from app.ui.pages.dashboard_page import DashboardPage

    page = DashboardPage(_user(shop["owner"]))
    wholesale_total = 41500 * 3 + 50000 + 9000
    assert page.wholesale_card.value_label.text() == f"{wholesale_total:,} دج"
    assert page.failed_card.value_label.text() == "1"
    assert page.completed_card.value_label.text() == "1"
    assert page.pending_card.value_label.text() == "1"

    seller_page = DashboardPage(_user(shop["seller"]))
    assert seller_page.wholesale_card.isHidden()


# 6. Cash customers vs. installment/credit customers -----------------------------------------
def test_cash_customers_are_distinguished_from_facility_customers(shop) -> None:
    with session_scope() as session:
        def names(**kwargs) -> set[str]:
            return {row.full_name for row in customers.list_customers(session, **_month_args(), **kwargs)}

        assert names(payment_kind="cash") == {"يوسف قاسمي"}
        assert names(payment_kind="facilities") == {
            "ليلى خليفي", "محمد بن علي", "سارة بوزيد", "كريم عمراني"
        }
        assert names(payment_kind="credit") == {"كريم عمراني"}
        assert names(balance="settled") == {"يوسف قاسمي"}
        assert "يوسف قاسمي" not in names(balance="open")
        assert names(search="iphone") == {"محمد بن علي"}  # product search

    from app.ui.pages.customers_page import PAYMENT_COLUMN, CustomersPage
    from app.ui.pages.dashboard_page import DashboardPage

    page = CustomersPage(_user(shop["owner"]))
    page.set_payment_filter("cash")
    model = page.table.model()
    assert model.rowCount() == 1
    assert model.index(0, PAYMENT_COLUMN).data() == "كاش"

    dashboard = DashboardPage(_user(shop["owner"]))
    assert dashboard.cash_customers_card.value_label.text() == "1"
    assert dashboard.facility_customers_card.value_label.text() == "4"
    received: list[str] = []
    dashboard.payment_filter_requested.connect(received.append)
    dashboard.cash_customers_card.clicked.emit()
    assert received == ["cash"]


# 7. Excel import (upload) ------------------------------------------------------------
def _legacy_workbook(path: Path) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = importer.SHEET_NAME
    sheet.append([importer.HEADERS[key] for key in importer.HEADERS])
    purchase = add_months(date.today(), -1)
    sheet.append(["زبون مستورد", "Honor X8", "بالتقسيط", 30000, 36000, 35, 38600, 10000, 6,
                  6433, 18600, purchase, add_months(purchase, 6)])
    sheet.append(["زبون نقدي مستورد", "Nokia 105", "كاش", 2500, 3500, 0, 3500, 0, None,
                  None, 1000, date.today(), None])
    workbook.save(path)
    return path


def test_excel_upload_imports_installment_and_cash_rows(shop, tmp_path) -> None:
    from app.ui.pages.import_page import ImportPage

    page = ImportPage(_user(shop["owner"]))
    page._path = _legacy_workbook(tmp_path / "legacy.xlsx")
    page._load_preview()
    assert page._preview is not None and not page._preview.missing_columns
    assert all(row.ready for row in page._preview.rows)
    page._run_import()
    with session_scope() as session:
        imported = {
            s.customer.full_name: s.sale_type
            for s in session.scalars(select(Sale).join(Customer).where(Customer.full_name.like("%مستورد%")))
        }
        fallback = categories.get_fallback_type(session)
        new_customer = session.scalar(select(Customer).where(Customer.full_name == "زبون مستورد"))
        assert new_customer.category_id == fallback.id
    assert imported == {"زبون مستورد": "installment", "زبون نقدي مستورد": "cash"}
    reports_folder = Path(__import__("app.config", fromlist=["data_dir"]).data_dir()) / "exports"
    assert any(reports_folder.glob("*.xlsx"))  # the import report was saved


def test_excel_template_round_trips_through_the_importer(shop, tmp_path) -> None:
    from app.ui.pages.import_page import _write_template

    template = tmp_path / "template.xlsx"
    _write_template(template)
    preview = importer.preview_workbook(template)
    assert preview.missing_columns == ()


# 8. Excel export ---------------------------------------------------------------------
def test_every_excel_export_opens_with_the_expected_data(shop, tmp_path, monkeypatch) -> None:
    from app.ui.pages.customers_page import CustomersPage
    from app.ui.pages.reports_page import ReportsPage

    today = date.today()
    with session_scope() as session:
        collection = reports.monthly_collection_rows(session, year=today.year, month=today.month, today=today)
        overdue = reports.overdue_rows(session, today=today)
        profit = reports.profit_by_month(
            session, start_year=today.year - 1, end_year=today.year, owner_user_id=shop["owner"]
        )
    reports.export_collection_xlsx(collection, tmp_path / "collection.xlsx")
    reports.export_overdue_xlsx(overdue, tmp_path / "overdue.xlsx")
    reports.export_profit_xlsx(profit, tmp_path / "profit.xlsx")
    assert load_workbook(tmp_path / "overdue.xlsx").active.max_row >= 2

    page = CustomersPage(_user(shop["owner"]))
    page.set_payment_filter("facilities")
    assert page.export_to(str(tmp_path / "customers"))
    sheet = load_workbook(tmp_path / "customers.xlsx").active
    assert sheet.max_row == 1 + 4 and sheet.sheet_view.rightToLeft

    reports_page = ReportsPage(_user(shop["owner"]))
    assert reports_page.export_everything_to(str(tmp_path / "all.xlsx"))
    workbook = load_workbook(tmp_path / "all.xlsx")
    assert workbook.sheetnames == ["الزبائن", "المبيعات", "الأقساط", "الدفعات", "أنواع الزبائن", "المخزون"]
    assert workbook["الزبائن"].max_row == 1 + 5
    assert workbook["المبيعات"].max_row == 1 + 5
    assert workbook["الأقساط"].max_row == 1 + 3 * 6
    assert workbook["المبيعات"]["O2"].number_format == "dd/mm/yyyy"  # purchase date
    assert workbook["المبيعات"]["J1"].value == ar.PROD_TPL_INTERVAL

    with session_scope() as session, pytest.raises(auth.AuthorizationError):
        reports.export_full_workbook(session, tmp_path / "nope.xlsx", owner_user_id=shop["seller"])
    assert ReportsPage(_user(shop["seller"])).full_export_card.isHidden()


# 9. Backups -----------------------------------------------------------------------------------
def test_backup_and_restore_round_trip(shop) -> None:
    created = backup.create_backup()
    backup.validate_backup(created)
    assert created in [info.path for info in backup.list_backups()]

    with session_scope() as session:
        fallback = categories.get_fallback_type(session)
        customers.create_customer(session, full_name="سيُحذف بالاستعادة", category_id=fallback.id)
    dispose_database_engines()  # the app closes before restoring
    backup.restore_backup(created)
    with session_scope() as session:
        names = set(session.scalars(select(Customer.full_name)))
    assert "سيُحذف بالاستعادة" not in names
    assert "ليلى خليفي" in names


def test_automatic_backup_runs_from_the_main_window(shop) -> None:
    from app.ui.main_window import MainWindow

    window = MainWindow(current_user=_user(shop["owner"]))
    window._run_scheduled_checkpoint_backup()
    assert backup.automated_backups_count() == 1
    window._quitting = True
    window.close()


# 10. Notifications ----------------------------------------------------------------------------
def test_overdue_customer_reaches_the_notification_center_and_background_monitor(shop, qapp) -> None:
    from app.ui.background import CollectionMonitor
    from app.ui.notifications import NotificationHub
    from app.ui.widgets.notification_center import NotificationCenterPopup

    popup = NotificationCenterPopup()
    late = [alert for alert in popup._alerts if alert.kind == "overdue"]
    # Bought 2 months + 10 days ago: installments 1 and 2 are both past grace.
    assert [alert.customer_name for alert in late] == ["محمد بن علي", "محمد بن علي"]
    assert any(alert.customer_name == "ليلى خليفي" for alert in popup._alerts)  # due soon

    hub = NotificationHub()
    monitor = CollectionMonitor(hub)
    updates: list = []
    monitor.updated.connect(lambda alerts, summary: updates.append(summary))
    monitor.refresh()  # file database: runs on a worker thread
    deadline = time.monotonic() + 10
    while not updates and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    assert updates and updates[0].overdue_count == 2
    assert hub.history()[0].title  # the daily digest was posted
