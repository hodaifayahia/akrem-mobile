"""Design system, direction switching, tray, notifications and background work."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QLabel
from sqlalchemy.engine import Engine

from app.db.models import User
from app.db.session import session_scope
from app.i18n import ar, en, get_language
from app.services import auth, categories, customers, sales
from app.services.alerts import CollectionAlert, summarize
from app.services.seed import seed_defaults


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, qapp):
    """Keep QSettings writes out of the real profile; close windows afterwards."""
    QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, str(tmp_path))
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    yield
    for widget in QApplication.topLevelWidgets():
        widget.hide()
    from app.ui.locale import apply_language

    if get_language() != "ar":
        apply_language("ar")


@pytest.fixture
def shop(memory_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """An owner, a seller and one overdue installment customer."""
    monkeypatch.setattr("app.db.session._application_engine", lambda url: memory_engine)
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        owner = auth.create_first_owner(
            session, username="owner_ui", password="Secret123", password_confirmation="Secret123"
        )
        seller = auth.create_seller(
            session, owner.id, username="seller_ui", password="Secret123", password_confirmation="Secret123"
        )
        fallback = categories.get_fallback_type(session)
        customer = customers.create_customer(
            session, full_name="زبون متأخر", category_id=fallback.id, phone="0661234567"
        )
        sales.create_sale(
            session,
            customer_id=customer.id,
            product="A15",
            sale_type="installment",
            wholesale_price=30000,
            cash_price=40000,
            rate=35,
            down_payment=4000,
            months=6,
            purchase_date=date.today() - timedelta(days=120),
        )
        return {"owner_id": owner.id, "seller_id": seller.id, "customer_id": customer.id}


def _user(engine: Engine, user_id: int) -> User:
    with session_scope(engine) as session:
        return session.get(User, user_id)


# ---------------------------------------------------------------- theme
def test_stylesheet_maps_logical_sides_and_tokens() -> None:
    from app.ui.theme import TOKENS, render_stylesheet

    template = "/* @ignored */ QFrame { border-@end: 1px solid @border; padding-@start: 4px; }"
    rtl = render_stylesheet(Qt.LayoutDirection.RightToLeft, template)
    ltr = render_stylesheet(Qt.LayoutDirection.LeftToRight, template)
    assert "border-left: 1px solid " + TOKENS["border"] in rtl
    assert "padding-right" in rtl
    assert "border-right: 1px solid" in ltr and "padding-left" in ltr
    assert "@" not in rtl and "ignored" not in rtl


def test_full_theme_renders_without_unknown_tokens() -> None:
    from app.ui.theme import render_stylesheet

    for direction in (Qt.LayoutDirection.RightToLeft, Qt.LayoutDirection.LeftToRight):
        sheet = render_stylesheet(direction)
        assert "@" not in sheet
        assert "#0758CD" in sheet  # primary brand token


def test_unknown_token_is_an_error() -> None:
    from app.ui.theme import render_stylesheet

    with pytest.raises(KeyError):
        render_stylesheet(Qt.LayoutDirection.LeftToRight, "QWidget { color: @no-such-token; }")


# ---------------------------------------------------------------- icons
def test_every_icon_renders(qapp) -> None:
    from app.ui import icons

    for name in icons.names():
        assert not icons.pixmap(name, "text", 18).isNull(), name


def test_directional_icons_mirror_in_rtl(qapp) -> None:
    from app.ui import icons

    qapp.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    icons.clear_cache()
    ltr = icons.pixmap("chevron-forward", "#FFFFFF", 24).toImage()
    qapp.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    icons.clear_cache()
    rtl = icons.pixmap("chevron-forward", "#FFFFFF", 24).toImage()
    assert rtl == ltr.flipped(Qt.Orientation.Horizontal)
    assert rtl != ltr


# --------------------------------------------------------- label direction
def test_labels_follow_interface_direction_not_content(qapp) -> None:
    from app.ui.locale import align_to_direction

    qapp.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
    number = QLabel("41,850")
    align_to_direction(number)
    assert number.alignment() & Qt.AlignmentFlag.AlignRight
    assert number.alignment() & Qt.AlignmentFlag.AlignAbsolute

    qapp.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    number.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    align_to_direction(number)
    assert number.alignment() & Qt.AlignmentFlag.AlignLeft

    centred = QLabel("x")
    centred.setAlignment(Qt.AlignmentFlag.AlignCenter)
    align_to_direction(centred)
    assert centred.alignment() == Qt.AlignmentFlag.AlignCenter


# ---------------------------------------------------------------- sidebar
def test_sidebar_navigation_badges_and_collapse(qapp, memory_engine, shop) -> None:
    from app.ui.widgets.sidebar import COLLAPSED_WIDTH, EXPANDED_WIDTH, Sidebar

    sidebar = Sidebar(_user(memory_engine, shop["owner_id"]))
    assert len(sidebar.buttons) == 11
    sidebar.set_active(3)
    assert sidebar.buttons[3].property("active") is True
    assert sidebar.buttons[0].property("active") is False
    sidebar.set_badge(3, 4)
    assert sidebar.buttons[3].badge.text() == "4"
    sidebar.set_badge(3, 0)
    assert sidebar.buttons[3].badge.isHidden()

    assert sidebar.width() == EXPANDED_WIDTH
    sidebar.set_collapsed(True)
    assert sidebar.width() == COLLAPSED_WIDTH
    assert sidebar.buttons[1].text() == ""
    assert sidebar.buttons[1].toolTip() == ar.SIDEBAR_ITEMS[1]
    sidebar.set_collapsed(False)
    assert sidebar.buttons[1].text() == ar.SIDEBAR_ITEMS[1]


def test_sidebar_hides_owner_pages_from_sellers_and_shows_initials(qapp, memory_engine, shop) -> None:
    from app.ui.widgets.sidebar import Sidebar, _initials

    sidebar = Sidebar(_user(memory_engine, shop["seller_id"]))
    assert sidebar.buttons[4].isHidden() and sidebar.buttons[6].isHidden()
    assert sidebar.buttons[8].isHidden()  # client types: owner only
    assert not sidebar.buttons[7].isHidden()  # products: sellers may browse
    assert sidebar.password_button is not None
    assert sidebar.role_badge.text() == ar.ROLE_SELLER
    assert _initials("seller_ui") == "SU"
    assert _initials("akrem") == "AK"


# ---------------------------------------------------------- notifications
def test_hub_routes_toasts_native_and_history(qapp) -> None:
    from app.ui.notifications import NotificationHub

    hub = NotificationHub()
    toasts: list[tuple] = []
    natives: list[tuple] = []
    hub.toast_requested.connect(lambda *args: toasts.append(args))
    hub.native_requested.connect(lambda *args: natives.append(args))
    hub.set_native_probe(lambda: True)

    hub.set_foreground_probe(lambda: True)
    hub.post("info", "T", "in front")
    assert len(toasts) == 1 and natives == []

    hub.set_foreground_probe(lambda: False)
    hub.post("warning", "T", "in tray")
    assert len(natives) == 1 and len(toasts) == 2
    hub.post("success", "T", "never native", native=False)
    assert len(natives) == 1

    assert hub.unread_count() == 3
    hub.mark_all_read()
    assert hub.unread_count() == 0
    assert [entry.message for entry in hub.history()][0] == "never native"


def test_hub_deduplicates_keys_and_respects_native_preference(qapp) -> None:
    from app.ui.notifications import NotificationHub, set_native_enabled

    hub = NotificationHub()
    natives: list[tuple] = []
    hub.native_requested.connect(lambda *args: natives.append(args))
    hub.set_native_probe(lambda: True)
    hub.set_foreground_probe(lambda: False)
    assert hub.post("info", "T", "once", key="k") is not None
    assert hub.post("info", "T", "again", key="k") is None
    set_native_enabled(False)
    hub.post("info", "T", "muted")
    set_native_enabled(True)
    assert len(natives) == 1


def test_ui_events_reach_the_hub_without_native_toasts(qapp) -> None:
    from app.ui.events import events
    from app.ui.notifications import hub

    toasts: list[tuple] = []
    natives: list[tuple] = []
    hub.toast_requested.connect(lambda *args: toasts.append(args))
    hub.native_requested.connect(lambda *args: natives.append(args))
    before = len(hub.history())
    events.notify.emit("success", "Saved", "ok", 1000)
    events.notify.emit("error", "Failed", "no", 1000)
    assert len(toasts) == 2 and natives == []
    assert len(hub.history()) == before + 1  # only the error is kept in history


# ------------------------------------------------------------------- tray
def test_tray_menu_and_preferences(qapp) -> None:
    from app.ui.notifications import NotificationHub
    from app.ui.tray import TrayController, minimize_to_tray_enabled

    tray = TrayController(NotificationHub())
    texts = [action.text() for action in tray.menu.actions() if not action.isSeparator()]
    assert ar.TRAY_OPEN in texts and ar.TRAY_QUIT in texts
    assert minimize_to_tray_enabled() is True
    tray.minimize_action.setChecked(False)
    assert minimize_to_tray_enabled() is False
    if not tray.available:
        # Headless test runs have no notification area: never hide on close.
        assert tray.should_hide_on_close() is False
        tray.show_native("info", "T", "M")  # must be a harmless no-op


def test_windows_app_id_is_a_noop_elsewhere() -> None:
    import sys

    from app.ui.tray import set_windows_app_id

    if sys.platform != "win32":
        assert set_windows_app_id() is False


# ------------------------------------------------------------- background
def _alert(key_id: int, kind: str = "overdue") -> CollectionAlert:
    return CollectionAlert(
        kind=kind, customer_id=1, customer_name="زبون", customer_phone=None, sale_id=1,
        installment_id=key_id, product="A15", amount=1000, due_date=date(2026, 1, 1),
        days_late=10, key=f"installment:{key_id}:{kind}",
    )


def test_monitor_posts_daily_digest_once_then_only_new_overdue(qapp) -> None:
    from app.ui.background import CollectionMonitor
    from app.ui.notifications import NotificationHub

    hub = NotificationHub()
    monitor = CollectionMonitor(hub, today=lambda: date(2026, 10, 1))
    first = [_alert(1), _alert(2, "due_soon")]
    monitor.apply(first, summarize(first))
    assert [entry.title for entry in hub.history()] == [ar.NOTIF_DIGEST_TITLE]

    monitor.apply(first, summarize(first))
    assert len(hub.history()) == 1  # nothing new

    second = [*first, _alert(3)]
    monitor.apply(second, summarize(second))
    assert hub.history()[0].title == ar.NOTIF_NEW_OVERDUE_TITLE

    restarted = CollectionMonitor(NotificationHub(), today=lambda: date(2026, 10, 1))
    restarted.apply(first, summarize(first))
    assert restarted.hub.history() == []  # digest already shown today


def test_monitor_scans_the_database(qapp, memory_engine, shop) -> None:
    from app.ui.background import CollectionMonitor
    from app.ui.notifications import NotificationHub

    monitor = CollectionMonitor(NotificationHub())
    received: list = []
    monitor.updated.connect(lambda alerts, summary: received.append(summary))
    monitor.refresh()  # in-memory test DB: runs inline
    assert received and received[0].overdue_count >= 1


# -------------------------------------------------------- single instance
def test_second_launch_activates_the_first(qapp) -> None:
    from app.ui.single_instance import SingleInstance

    name = f"akrem-test-{uuid.uuid4().hex[:8]}"
    first = SingleInstance(name)
    assert first.notify_existing() is False
    assert first.listen() is True
    activated: list[bool] = []
    first.activation_requested.connect(lambda: activated.append(True))
    assert SingleInstance(name).notify_existing() is True
    for _ in range(50):
        qapp.processEvents()
        if activated:
            break
    first.close()
    assert activated


# ----------------------------------------------------- main window shell
def test_language_switch_rebuilds_pages_and_mirrors_layout(qapp, memory_engine, shop) -> None:
    from app.ui.locale import change_language
    from app.ui.main_window import MainWindow

    window = MainWindow(current_user=_user(memory_engine, shop["owner_id"]))
    window.show()
    old_dashboard = window.dashboard_page
    window._select_page(1)
    old_customers = window.customers_page
    change_language("en")
    assert get_language() == "en"
    assert window.layoutDirection() == Qt.LayoutDirection.LeftToRight
    # The visible page is rebuilt at once; the others on first visit.
    assert window.customers_page is not old_customers
    assert window.dashboard_page is old_dashboard
    window._select_page(0)
    assert window.dashboard_page is not old_dashboard
    window._select_page(1)
    assert window.pages.currentIndex() == 1
    assert window.buttons[1].text() == en.SIDEBAR_ITEMS[1]
    assert window.topbar.page_sub_label.text() == en.PAGE_SUBTITLES[1]
    assert QSettings("AkremMobile", "AkremMobile").value("language") == "en"
    change_language("ar")
    assert window.layoutDirection() == Qt.LayoutDirection.RightToLeft
    window._quitting = True
    window.close()


def test_close_hides_to_tray_and_lock_waits_until_restore(qapp, memory_engine, shop, monkeypatch) -> None:
    from app.ui.main_window import MainWindow

    window = MainWindow(current_user=_user(memory_engine, shop["owner_id"]))
    window.show()
    monkeypatch.setattr(window.tray, "should_hide_on_close", lambda: True)
    monkeypatch.setattr(window.tray, "show_background_hint_once", lambda: None)
    window.close()
    assert not window.isVisible()
    assert window._quitting is False

    window._lock_after_idle()  # hidden: no dialog, lock deferred
    assert window._lock_pending is True
    assert window._reauth_dialog is None

    monkeypatch.setattr(window, "_lock_after_idle", lambda: setattr(window, "_locked", True))
    window.restore_from_tray()
    qapp.processEvents()
    assert window.isVisible()
    assert getattr(window, "_locked", False) is True

    window.quit_application()
    assert not window.isVisible()


def test_alert_updates_reach_badges(qapp, memory_engine, shop) -> None:
    from app.ui.main_window import PAGE_PAYMENTS, MainWindow

    window = MainWindow(current_user=_user(memory_engine, shop["owner_id"]))
    alerts = [_alert(1), _alert(2)]
    window._on_alerts_updated(alerts, summarize(alerts))
    assert window.sidebar.buttons[PAGE_PAYMENTS].badge.text() == "2"
    assert window.topbar.notification_popup.get_alert_count() == 2
    window._quitting = True
    window.close()


# ------------------------------------------------------------ dashboard
def test_trend_labels_are_localized(qapp) -> None:
    from app.i18n import set_language
    from app.ui.pages.dashboard_page import trend_labels

    set_language("en")
    assert trend_labels(2026, 2, 3) == ["December", "January", "February"]
    set_language("ar")
    assert trend_labels(2026, 10, 1) == ["أكتوبر"]


# --------------------------------------------------------- client types UI
def test_client_type_editor_saves_and_reports_duplicates(qapp, memory_engine, shop) -> None:
    from app.ui.dialogs.client_types_dialog import ClientTypeEditor, ClientTypesPanel

    dialog = ClientTypesPanel(shop["owner_id"])
    editor = ClientTypeEditor(dialog)
    editor.name.setText("طلبة")
    editor.color_group.buttons()[2].setChecked(True)
    dialog._save_editor(editor)
    assert editor.result() == 1
    names = {item.name: item.color for item in dialog._types}
    assert names["طلبة"] == "green"

    duplicate = ClientTypeEditor(dialog)
    duplicate.name.setText("طلبه")  # same name after normalization
    dialog._save_editor(duplicate)
    assert duplicate.result() != 1
    assert duplicate.error.text.text() == ar.CT_ERR_DUPLICATE


def test_customers_page_filters_by_client_type(qapp, memory_engine, shop) -> None:
    from app.ui.pages.customers_page import CustomersPage

    page = CustomersPage(_user(memory_engine, shop["owner_id"]))
    assert page.table.model().rowCount() == 1
    with session_scope(memory_engine) as session:
        teachers = next(t for t in categories.list_client_types(session) if t.name == "أساتذة")
    page.type_filter._choose(teachers.id)
    assert page.table.model().rowCount() == 0
    page.type_filter._choose(None)
    assert page.table.model().rowCount() == 1


def test_seller_cannot_manage_client_types(qapp, memory_engine, shop) -> None:
    from app.ui.pages.customers_page import CustomersPage

    page = CustomersPage(_user(memory_engine, shop["seller_id"]))
    assert page.assign_type_button.isHidden()


_LAUNCH = """
import sys
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from app.ui.single_instance import SingleInstance
second = SingleInstance(sys.argv[1], build=sys.argv[2])
second._wait_until_free = lambda: print("waited")
print(second.request_quit() if sys.argv[3] == "quit" else second.notify_existing())
"""


def _launch_second_copy(name: str, build: str, qapp, *, quit_only: bool = False) -> list:
    """Run a second launch in a separate process while the first one serves events."""
    import subprocess
    import sys
    import time

    process = subprocess.Popen(
        [sys.executable, "-c", _LAUNCH, name, build, "quit" if quit_only else "launch"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    deadline = time.monotonic() + 30
    while process.poll() is None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    for _ in range(10):
        qapp.processEvents()
    lines = process.stdout.read().split()
    return [{"True": True, "False": False}.get(line, line) for line in lines]


def test_a_newer_build_replaces_a_stale_copy_running_in_the_tray(qapp) -> None:
    from app.ui.single_instance import SingleInstance

    name = f"akrem-test-{uuid.uuid4().hex[:8]}"
    old = SingleInstance(name, build="0.1.0-1000")
    assert old.listen()
    shown: list[bool] = []
    quit_asked: list[bool] = []
    old.activation_requested.connect(lambda: shown.append(True))
    old.quit_requested.connect(lambda: quit_asked.append(True))
    try:
        # Same build: the running copy shows itself and the new launch exits.
        assert _launch_second_copy(name, "0.1.0-1000", qapp) == [True]
        assert shown and not quit_asked
        # Reinstalled build: the old copy is told to quit and the new launch continues.
        assert _launch_second_copy(name, "0.1.0-2000", qapp) == ["waited", False]
        assert quit_asked
        # The installer's "--quit".
        quit_asked.clear()
        assert _launch_second_copy(name, "x", qapp, quit_only=True) == [True]
        assert quit_asked
    finally:
        old.close()


def test_every_sidebar_and_topbar_button_receives_real_mouse_clicks(qapp, memory_engine, shop) -> None:
    """Regression for "after installing, nothing can be clicked"."""
    from app.ui.locale import apply_language
    from app.ui.main_window import MainWindow
    from app.ui.self_test import SelfTestReport, check_window

    for language in ("ar", "en"):
        apply_language(language)
        window = MainWindow(current_user=_user(memory_engine, shop["owner_id"]))
        window.resize(1366, 768)
        window.show()
        report = SelfTestReport()
        check_window(qapp, window, report)
        window.monitor.stop()
        window.hide()
        assert report.ok, report.failures
        assert sum("opens its page" in check for check in report.checks) == 11
    apply_language("ar")


def test_uncaught_errors_never_block_the_window(qapp, monkeypatch) -> None:
    import app.main as entry

    monkeypatch.setattr(entry, "_error_notice", None)
    entry.show_uncaught_exception(RuntimeError, RuntimeError("boom"), None)
    notice = entry._error_notice
    assert notice is not None and notice.isVisible()
    assert notice.windowModality() == Qt.WindowModality.NonModal
    assert QApplication.activeModalWidget() is None
    entry.show_uncaught_exception(RuntimeError, RuntimeError("again"), None)
    assert entry._error_notice is notice  # one notice at a time
    notice.close()
