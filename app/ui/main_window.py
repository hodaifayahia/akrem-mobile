"""Main application window: sidebar, top bar, pages, tray and background work.

Layout direction is never hard-coded here. The sidebar is the first widget
of a horizontal layout, so Qt places it on the right in Arabic and on the
left in English/French. Switching language rebuilds the pages so every
string and alignment is regenerated in the new language and direction.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, QSettings, QTimer
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import APP_NAME
from app.db.models import User
from app.db.session import session_scope
from app.i18n import ar
from app.services.alerts import AlertSummary, CollectionAlert
from app.services.settings import get_value
from app.ui.background import CollectionMonitor
from app.ui.dialogs.auth_dialogs import ReauthenticationDialog
from app.ui.dialogs.command_palette import CommandPaletteDialog
from app.ui.dialogs.customer_details_dialog import CustomerDetailsDialog
from app.ui.dialogs.shortcuts_dialog import ShortcutsDialog
from app.ui.events import events
from app.ui.notifications import hub
from app.ui.pages.client_types_page import ClientTypesPage
from app.ui.pages.customers_page import CustomersPage
from app.ui.pages.dashboard_page import DashboardPage
from app.ui.pages.import_page import ImportPage
from app.ui.pages.new_sale_page import NewSalePage
from app.ui.pages.payments_page import PaymentsPage
from app.ui.pages.products_page import ProductsPage
from app.ui.pages.reports_page import ReportsPage
from app.ui.pages.settings_page import SettingsPage
from app.ui.tray import TrayController, app_icon
from app.ui.widgets.sidebar import Sidebar, page_title
from app.ui.widgets.toast import ToastManager
from app.ui.widgets.topbar import AppTopBar

_LOG = logging.getLogger(__name__)

PAGE_DASHBOARD, PAGE_CUSTOMERS, PAGE_NEW_SALE, PAGE_PAYMENTS = 0, 1, 2, 3
PAGE_IMPORT, PAGE_REPORTS, PAGE_SETTINGS = 4, 5, 6
PAGE_PRODUCTS, PAGE_CLIENT_TYPES = 7, 8
PAGE_COUNT = 9
OWNER_ONLY_PAGES = frozenset({PAGE_IMPORT, PAGE_CLIENT_TYPES})
SIDEBAR_SETTING = "window/sidebar_collapsed"


class MainWindow(QMainWindow):
    """Primary window with a navigation sidebar on the reading-start side."""

    def __init__(self, current_user: User | None = None) -> None:
        super().__init__()
        self.current_user = current_user
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.pages = QStackedWidget()
        self.dashboard_page: DashboardPage | None = None
        self.customers_page: CustomersPage | None = None
        self.new_sale_page: NewSalePage | None = None
        self.payments_page: PaymentsPage | None = None
        self.import_page: ImportPage | None = None
        self.reports_page: ReportsPage | None = None
        self.settings_page: SettingsPage | None = None
        self.products_page: ProductsPage | None = None
        self.client_types_page: ClientTypesPage | None = None
        self._reauth_dialog: ReauthenticationDialog | None = None
        self._quitting = False
        self._lock_pending = False
        self._needs_rebuild = False
        self.setWindowTitle(ar.APP_TITLE)
        self.setWindowIcon(app_icon())
        self.setMinimumSize(1100, 700)

        self._build_ui()
        self.toast_manager = ToastManager(self)
        self.tray = TrayController(hub, self)
        self.monitor = CollectionMonitor(hub, self)
        hub.set_foreground_probe(self._is_foreground)
        self._wire_tray()
        self._setup_shortcuts()
        self._restore_geometry()
        self._setup_inactivity_lock()
        self._setup_auto_backup_schedule()

        events.language_changed.connect(self._on_language_changed)
        events.settings_changed.connect(self._refresh_inactivity_timeout)
        events.open_customer.connect(self._open_customer_details)
        events.navigate_to.connect(self._select_page)
        events.data_changed.connect(self._on_data_changed)
        self.monitor.updated.connect(self._on_alerts_updated)

    # ------------------------------------------------------------------ build
    def _build_ui(self) -> None:
        root = QWidget(self)
        self.root_widget = root
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = Sidebar(self.current_user, root)
        self.sidebar_widget = self.sidebar
        self.buttons = self.sidebar.buttons
        self.password_button = self.sidebar.password_button
        self.lock_button = self.sidebar.lock_button
        self.role_badge = getattr(self.sidebar, "role_badge", None)
        self.sidebar.page_requested.connect(self._select_page)
        self.sidebar.lock_requested.connect(self._lock_after_idle)
        self.sidebar.password_requested.connect(lambda: self._select_page(PAGE_SETTINGS))
        self.sidebar.set_collapsed(self.settings.value(SIDEBAR_SETTING, False) in (True, "true", 1, "1"))
        self.sidebar.collapsed_changed.connect(lambda value: self.settings.setValue(SIDEBAR_SETTING, value))
        layout.addWidget(self.sidebar)

        content = QWidget(root)
        self.content_container = content
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        self.topbar = AppTopBar(content)
        self.topbar.command_palette_requested.connect(self._open_command_palette)
        self.topbar.shortcuts_requested.connect(self._open_shortcuts_dialog)
        self.topbar.new_sale_requested.connect(lambda: self._select_page(PAGE_NEW_SALE))
        self.topbar.customer_opened.connect(self._open_customer_details)
        content_layout.addWidget(self.topbar)
        content_layout.addWidget(self.pages, 1)
        layout.addWidget(content, 1)
        self.setCentralWidget(root)

        self._create_pages()
        self._select_page(PAGE_DASHBOARD)

    _PAGE_CLASSES = (
        DashboardPage, CustomersPage, NewSalePage, PaymentsPage, ImportPage, ReportsPage, SettingsPage,
        ProductsPage, ClientTypesPage,
    )
    _PAGE_ATTRIBUTES = (
        "dashboard_page", "customers_page", "new_sale_page", "payments_page",
        "import_page", "reports_page", "settings_page", "products_page", "client_types_page",
    )

    def _create_pages(self) -> None:
        """Instantiate every page in sidebar order."""
        self._stale_pages: set[int] = set()
        for index in range(PAGE_COUNT):
            self.pages.addWidget(self._build_page(index))

    def _build_page(self, index: int) -> QWidget:
        """Create one page and connect its cross-page signals."""
        if self.current_user is None:
            return self._placeholder_page(page_title(index))
        page = self._PAGE_CLASSES[index](self.current_user, self.pages)
        setattr(self, self._PAGE_ATTRIBUTES[index], page)
        if index == PAGE_DASHBOARD:
            page.status_filter_requested.connect(self._show_customer_status)
            page.payment_filter_requested.connect(self._show_customer_payment_kind)
            page.open_customer_requested.connect(self._open_customer_details)
            page.view_overdue_requested.connect(lambda: self._select_page(PAGE_PAYMENTS))
        elif index == PAGE_CUSTOMERS:
            page.customer_selected.connect(self._open_customer_details)
            page.manage_types_requested.connect(lambda: self._select_page(PAGE_CLIENT_TYPES))
        elif index == PAGE_CLIENT_TYPES:
            page.view_customers_requested.connect(self._show_customers_of_type)
        elif index == PAGE_NEW_SALE:
            page.customer_details_requested.connect(self._open_customer_details)
        return page

    def _replace_page(self, index: int) -> None:
        old = self.pages.widget(index)
        current = self.pages.currentIndex()
        self.pages.insertWidget(index, self._build_page(index))
        self.pages.removeWidget(old)
        old.deleteLater()
        self.pages.setCurrentIndex(current)

    def _rebuild_pages(self) -> None:
        """Re-create pages in the new language: the visible one now, others on first visit."""
        current = max(self.pages.currentIndex(), 0)
        self._stale_pages = set(range(self.pages.count())) - {current}
        self._replace_page(current)
        self._select_page(current)
        self._needs_rebuild = False

    @staticmethod
    def _placeholder_page(title: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 28, 32, 28)
        heading = QLabel(title, page)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        card = QLabel(f"{ar.PLACEHOLDER_TITLE}\n{ar.PLACEHOLDER_BODY}", page)
        card.setObjectName("placeholderCard")
        layout.addWidget(card, 1)
        return page

    # ------------------------------------------------------------- navigation
    def _select_page(self, index: int) -> None:
        if not 0 <= index < self.pages.count():
            return
        if (
            self.current_user is not None
            and self.current_user.role != "owner"
            and index in OWNER_ONLY_PAGES
        ):
            return
        if index in getattr(self, "_stale_pages", set()):
            self._stale_pages.discard(index)
            self._replace_page(index)
        self.pages.setCurrentIndex(index)
        self.topbar.set_active_page(index)
        self.sidebar.set_active(index)

    def _open_command_palette(self) -> None:
        CommandPaletteDialog(self.current_user, self).exec()

    def _open_shortcuts_dialog(self) -> None:
        ShortcutsDialog(self).exec()

    def _show_customer_status(self, status: str) -> None:
        """Navigate from a dashboard status card to matching customers."""
        if self.customers_page is None:
            return
        month = self.dashboard_page.month_selector.date() if self.dashboard_page is not None else None
        self._select_page(PAGE_CUSTOMERS)  # may rebuild the page after a language change
        if month is not None:
            self.customers_page.month_selector.setDate(month)
        self.customers_page.set_status_filter(status)

    def _show_customers_of_type(self, type_id: int) -> None:
        """Open the customer list filtered to one client type."""
        self._select_page(PAGE_CUSTOMERS)
        if self.customers_page is not None:
            self.customers_page.set_type_filter(type_id)

    def _show_customer_payment_kind(self, kind: str) -> None:
        """Open the customer list filtered to cash or installment/credit customers."""
        if self.customers_page is None:
            return
        self._select_page(PAGE_CUSTOMERS)
        self.customers_page.set_status_filter("ALL")
        self.customers_page.set_payment_filter(kind)

    def _open_customer_details(self, customer_id: int) -> None:
        """Open the selected customer's sale and payment history."""
        if self.current_user is None:
            return
        self.restore_from_tray()
        dialog = CustomerDetailsDialog(customer_id, self.current_user, self)
        edit_requested: list[int] = []
        dialog.edit_requested.connect(edit_requested.append)
        dialog.exec()
        if edit_requested and self.customers_page is not None:
            self._select_page(PAGE_CUSTOMERS)
            self.customers_page.edit_customer_by_id(edit_requested[-1])

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+K"), self, self._open_command_palette)
        QShortcut(QKeySequence("Ctrl+N"), self, lambda: self._select_page(PAGE_NEW_SALE))
        QShortcut(QKeySequence("Ctrl+L"), self, self._lock_after_idle)
        QShortcut(QKeySequence("F1"), self, self._open_shortcuts_dialog)
        for index in range(PAGE_COUNT):
            QShortcut(QKeySequence(f"Ctrl+{index + 1}"), self, lambda i=index: self._select_page(i))

    # ------------------------------------------------------- language change
    def _on_language_changed(self, _code: str) -> None:
        """Re-translate the shell now; rebuild pages now or when next shown."""
        self.setWindowTitle(ar.APP_TITLE)
        self.sidebar.retranslate()
        self.tray.retranslate()
        if self.isVisible():
            self._rebuild_pages()
        else:
            self._needs_rebuild = True

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt callback name
        """Apply a language change that happened while the window was hidden."""
        if self._needs_rebuild:
            self._rebuild_pages()
        super().showEvent(event)

    # ------------------------------------------------- alerts and background
    def start_background_services(self) -> None:
        """Start the collection monitor (called once the window is shown)."""
        self.monitor.start()

    def _on_data_changed(self) -> None:
        self.topbar.refresh_notification_badge()
        self.sidebar.set_badge(PAGE_PAYMENTS, self.topbar.notification_popup.attention_count())

    def _on_alerts_updated(self, alerts: list[CollectionAlert], summary: AlertSummary) -> None:
        self.topbar.notification_popup.set_alerts(alerts)
        self.topbar.update_badge()
        self.sidebar.set_badge(PAGE_PAYMENTS, summary.attention_count)

    def _is_foreground(self) -> bool:
        """Whether the user is looking at the app (any of its windows active)."""
        return (
            self.isVisible()
            and not self.isMinimized()
            and QApplication.activeWindow() is not None
        )

    # -------------------------------------------------------------- the tray
    def _wire_tray(self) -> None:
        self.tray.open_requested.connect(self.restore_from_tray)
        self.tray.new_sale_requested.connect(self._tray_new_sale)
        self.tray.notifications_requested.connect(self._tray_notifications)
        self.tray.lock_requested.connect(self._tray_lock)
        self.tray.quit_requested.connect(self.quit_application)

    def restore_from_tray(self) -> None:
        """Bring the window back from the tray, minimized state, or background."""
        was_hidden = not self.isVisible()
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        if was_hidden and self._lock_pending:
            QTimer.singleShot(0, self._lock_after_idle)

    def _tray_new_sale(self) -> None:
        self.restore_from_tray()
        self._select_page(PAGE_NEW_SALE)

    def _tray_notifications(self) -> None:
        self.restore_from_tray()
        QTimer.singleShot(150, self.topbar.open_notifications)

    def _tray_lock(self) -> None:
        if self.isVisible():
            self._lock_after_idle()
        else:
            self._lock_pending = True

    def quit_application(self) -> None:
        """Really exit (from the tray menu), bypassing minimize-to-tray."""
        self._quitting = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt callback name
        """Hide to the tray, or save state and exit."""
        if not self._quitting and self.tray.should_hide_on_close():
            event.ignore()
            self.settings.setValue("window/geometry", self.saveGeometry())
            self.hide()
            self.tray.show_background_hint_once()
            return
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.monitor.stop()
        try:
            from app.services.backup import create_automated_checkpoint_backup

            create_automated_checkpoint_backup(max_per_day=3, min_interval_seconds=1800)
        except Exception:  # noqa: BLE001 - never block exit on a backup failure
            _LOG.warning("Exit checkpoint backup failed", exc_info=True)
        self.tray.hide()
        super().closeEvent(event)
        if not QApplication.quitOnLastWindowClosed():
            QApplication.quit()

    def _restore_geometry(self) -> None:
        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(1440, 900)

    # ---------------------------------------------------------- idle locking
    def _setup_inactivity_lock(self) -> None:
        """Lock the window after the configured idle period."""
        if self.current_user is None:
            return
        self._idle_timeout_ms = self._read_idle_timeout()
        self._lock_timer = QTimer(self)
        self._lock_timer.setSingleShot(True)
        self._lock_timer.timeout.connect(self._lock_after_idle)
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)
        self._lock_timer.start(self._idle_timeout_ms)

    @staticmethod
    def _read_idle_timeout() -> int:
        with session_scope() as session:
            idle_minutes = get_value(session, "inactivity_minutes", 15)
        if isinstance(idle_minutes, bool) or not isinstance(idle_minutes, int):
            idle_minutes = 15
        return min(max(idle_minutes, 1), 180) * 60_000

    def eventFilter(self, watched: object, event: QEvent) -> bool:  # noqa: N802 - Qt callback name
        """Restart the idle timer on keyboard, pointer and touch activity."""
        activity_types = {
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseMove,
            QEvent.Type.KeyPress,
            QEvent.Type.Wheel,
            QEvent.Type.TouchBegin,
            QEvent.Type.TouchUpdate,
            QEvent.Type.TabletPress,
        }
        if (
            hasattr(self, "_lock_timer")
            and self._reauth_dialog is None
            and event.type() in activity_types
        ):
            self._lock_timer.start(self._idle_timeout_ms)
        return super().eventFilter(watched, event)

    def _lock_after_idle(self) -> None:
        """Require the user's password; page content stays hidden meanwhile."""
        if self.current_user is None or self._reauth_dialog is not None:
            return
        if not self.isVisible():
            # Hidden in the tray: lock as soon as the window is restored.
            self._lock_pending = True
            return
        self._lock_pending = False
        self.root_widget.setVisible(False)
        self._reauth_dialog = ReauthenticationDialog(self.current_user, self)
        try:
            self._reauth_dialog.exec()
        finally:
            self._reauth_dialog = None
            self.root_widget.setVisible(True)
        if hasattr(self, "_lock_timer"):
            self._lock_timer.start(self._idle_timeout_ms)

    def _refresh_inactivity_timeout(self) -> None:
        """Apply a newly saved idle-lock interval immediately."""
        if self.current_user is None:
            return
        self._idle_timeout_ms = self._read_idle_timeout()
        if hasattr(self, "_lock_timer") and self._reauth_dialog is None:
            self._lock_timer.start(self._idle_timeout_ms)

    # --------------------------------------------------------------- backups
    def _setup_auto_backup_schedule(self) -> None:
        """Check every 30 minutes whether today's automatic checkpoint is due."""
        self._auto_backup_timer = QTimer(self)
        self._auto_backup_timer.setInterval(30 * 60 * 1000)
        self._auto_backup_timer.timeout.connect(self._run_scheduled_checkpoint_backup)
        self._auto_backup_timer.start()

    def _run_scheduled_checkpoint_backup(self) -> None:
        """Create an automatic checkpoint (max 3 a day, 2 hours apart)."""
        try:
            from app.services.backup import automated_backups_count, create_automated_checkpoint_backup

            created = create_automated_checkpoint_backup(max_per_day=3, min_interval_seconds=7200)
        except Exception:  # noqa: BLE001 - a failed backup must not interrupt work
            _LOG.warning("Scheduled checkpoint backup failed", exc_info=True)
            return
        if created is not None:
            hub.post(
                "info",
                ar.NOTIF_BACKUP_TITLE,
                ar.SET_BACKUP_AUTO_TOAST.format(count=automated_backups_count()),
                native=False,
            )
            events.data_changed.emit()
