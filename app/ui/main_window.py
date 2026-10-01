"""Main right-to-left application window and page navigation."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSettings, QTimer, Qt
from PySide6.QtGui import QCloseEvent, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.config import APP_NAME, RESOURCE_DIR
from app.db.models import User
from app.i18n import ar, get_layout_direction
from app.db.session import session_scope
from app.services.settings import get_value
from app.ui.dialogs.auth_dialogs import ReauthenticationDialog
from app.ui.dialogs.command_palette import CommandPaletteDialog
from app.ui.dialogs.customer_details_dialog import CustomerDetailsDialog
from app.ui.dialogs.shortcuts_dialog import ShortcutsDialog
from app.ui.pages.customers_page import CustomersPage
from app.ui.pages.dashboard_page import DashboardPage
from app.ui.pages.import_page import ImportPage
from app.ui.pages.new_sale_page import NewSalePage
from app.ui.pages.payments_page import PaymentsPage
from app.ui.pages.reports_page import ReportsPage
from app.ui.pages.settings_page import SettingsPage
from app.ui.widgets.toast import ToastManager
from app.ui.widgets.topbar import AppTopBar
from app.ui.events import events


class MainWindow(QMainWindow):
    """Primary RTL window with a right-side navigation rail."""

    def __init__(self, current_user: User | None = None) -> None:
        super().__init__()
        self.current_user = current_user
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.buttons: list[QPushButton] = []
        self.pages = QStackedWidget()
        self.dashboard_page: DashboardPage | None = None
        self.customers_page: CustomersPage | None = None
        self.new_sale_page: NewSalePage | None = None
        self.payments_page: PaymentsPage | None = None
        self.import_page: ImportPage | None = None
        self.reports_page: ReportsPage | None = None
        self.settings_page: SettingsPage | None = None
        self._reauth_dialog: ReauthenticationDialog | None = None
        self.password_button: QPushButton | None = None
        self.role_badge: QLabel | None = None
        self.lock_button: QPushButton | None = None
        self.setWindowTitle(ar.APP_TITLE)
        self.setMinimumSize(1080, 680)
        self.setLayoutDirection(get_layout_direction())
        self._build_ui()
        self.toast_manager = ToastManager(self)
        self._setup_shortcuts()
        self._restore_geometry()
        self._setup_inactivity_lock()
        self._setup_auto_backup_schedule()
        events.language_changed.connect(self._on_language_changed)
        events.settings_changed.connect(self._refresh_inactivity_timeout)
        events.open_customer.connect(self._open_customer_details)
        events.navigate_to.connect(self._select_page)
        events.data_changed.connect(self._on_data_changed)
        events.payment_changed.connect(lambda _: self._on_data_changed())

    def _build_ui(self) -> None:
        direction = get_layout_direction()
        root = QWidget(self)
        root.setLayoutDirection(direction)
        self.root_widget = root
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = self._build_sidebar()
        self.sidebar_widget = sidebar
        layout.addWidget(sidebar)

        content_container = QWidget(root)
        content_container.setLayoutDirection(direction)
        self.content_container = content_container
        content_layout = QVBoxLayout(content_container)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.topbar = AppTopBar(content_container)
        self.topbar.command_palette_requested.connect(self._open_command_palette)
        self.topbar.shortcuts_requested.connect(self._open_shortcuts_dialog)
        self.topbar.new_sale_requested.connect(lambda: self._select_page(2))
        self.topbar.customer_opened.connect(self._open_customer_details)

        content_layout.addWidget(self.topbar)
        content_layout.addWidget(self.pages, 1)

        layout.addWidget(content_container, 1)
        self.setCentralWidget(root)

        for index, title in enumerate(ar.SIDEBAR_ITEMS):
            if index == 0 and self.current_user is not None:
                self.dashboard_page = DashboardPage(self.current_user, self.pages)
                self.pages.addWidget(self.dashboard_page)
            elif index == 1 and self.current_user is not None:
                self.customers_page = CustomersPage(self.current_user, self.pages)
                self.customers_page.customer_selected.connect(self._open_customer_details)
                self.pages.addWidget(self.customers_page)
            elif index == 2 and self.current_user is not None:
                self.new_sale_page = NewSalePage(self.current_user, self.pages)
                self.new_sale_page.customer_details_requested.connect(self._open_customer_details)
                self.pages.addWidget(self.new_sale_page)
            elif index == 3 and self.current_user is not None:
                self.payments_page = PaymentsPage(self.current_user, self.pages)
                self.pages.addWidget(self.payments_page)
            elif index == 4 and self.current_user is not None:
                self.import_page = ImportPage(self.current_user, self.pages)
                self.pages.addWidget(self.import_page)
            elif index == 5 and self.current_user is not None:
                self.reports_page = ReportsPage(self.current_user, self.pages)
                self.pages.addWidget(self.reports_page)
            elif index == 6 and self.current_user is not None:
                self.settings_page = SettingsPage(self.current_user, self.pages)
                self.pages.addWidget(self.settings_page)
            else:
                self.pages.addWidget(self._placeholder_page(title))
        if self.buttons:
            self._select_page(0)
        if self.dashboard_page is not None:
            self.dashboard_page.status_filter_requested.connect(self._show_customer_status)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame(self)
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(260)
        sidebar.setLayoutDirection(get_layout_direction())
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 16, 14, 16)
        layout.setSpacing(6)

        # 1. Modern Compact Branding Header with Circular Logo
        brand_container = QWidget(sidebar)
        brand_layout = QVBoxLayout(brand_container)
        brand_layout.setContentsMargins(6, 4, 6, 12)
        brand_layout.setSpacing(6)
        brand_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Circular Glowing Logo Badge
        logo_badge = QFrame(brand_container)
        logo_badge.setFixedSize(86, 86)
        logo_badge.setStyleSheet(
            "QFrame {"
            "  background-color: #080D19;"
            "  border: 2px solid #06B6D4;"
            "  border-radius: 43px;"
            "}"
        )
        logo_badge_layout = QVBoxLayout(logo_badge)
        logo_badge_layout.setContentsMargins(0, 0, 0, 0)
        logo_badge_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        logo = QLabel(logo_badge)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_path = RESOURCE_DIR / "logo.png"
        if logo_path.exists():
            pixmap = QPixmap(str(logo_path))
            logo.setPixmap(pixmap.scaled(72, 72, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation))
        else:
            logo.setText("AM")
            logo.setStyleSheet("font-size: 20px; font-weight: 900; color: #06B6D4;")
        logo_badge_layout.addWidget(logo)
        brand_layout.addWidget(logo_badge, 0, Qt.AlignmentFlag.AlignCenter)

        brand_title = QLabel("تسيير التقسيط والمبيعات", brand_container)
        brand_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_title.setStyleSheet("font-size: 14px; font-weight: 800; color: #FFFFFF; margin-top: 2px;")
        brand_layout.addWidget(brand_title)

        brand_sub = QLabel("النسخة الاحترافية PRO v4.2", brand_container)
        brand_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_sub.setStyleSheet("font-size: 10px; font-weight: 600; color: #06B6D4;")
        brand_layout.addWidget(brand_sub)

        # Subtle bottom separator
        sep = QFrame(brand_container)
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: rgba(51, 65, 85, 0.45); max-height: 1px; margin-top: 4px;")
        brand_layout.addWidget(sep)

        layout.addWidget(brand_container)

        # 2. Navigation Items (Right-to-Left aligned with clean icon badges)
        nav_icons = ("📊", "👥", "🛒", "💳", "📥", "📈", "⚙️")
        for index, title in enumerate(ar.SIDEBAR_ITEMS):
            icon_prefix = nav_icons[index] if index < len(nav_icons) else "•"
            button = QPushButton(f"  {icon_prefix}   {title}", sidebar)
            button.setObjectName("navButton")
            button.setProperty("active", False)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if self.current_user is not None and self.current_user.role != "owner" and index in {4, 6}:
                button.setVisible(False)
            button.clicked.connect(lambda _checked=False, i=index: self._select_page(i))
            self.buttons.append(button)
            layout.addWidget(button)

        if self.current_user is not None and self.current_user.role != "owner":
            self.password_button = QPushButton(f"  🔒   {ar.SET_CHANGE_PASSWORD}", sidebar)
            self.password_button.setObjectName("navButton")
            self.password_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.password_button.clicked.connect(
                lambda _checked=False: self._select_page(6)
            )
            layout.addWidget(self.password_button)

        layout.addStretch(1)

        # 3. Active User Profile & Quick Screen Lock Card
        if self.current_user is not None:
            user_card = QFrame(sidebar)
            user_card.setObjectName("sidebarUserBadge")
            user_layout = QVBoxLayout(user_card)
            user_layout.setContentsMargins(10, 10, 10, 10)
            user_layout.setSpacing(8)

            top_row = QHBoxLayout()
            top_row.setContentsMargins(0, 0, 0, 0)
            top_row.setSpacing(8)

            # Avatar icon
            avatar = QLabel("AD", user_card)
            avatar.setFixedSize(30, 30)
            avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
            avatar.setStyleSheet(
                "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #06B6D4, stop:1 #3B82F6);"
                "color: #FFFFFF; font-size: 11px; font-weight: 800; border-radius: 8px;"
            )
            top_row.addWidget(avatar)

            info_col = QVBoxLayout()
            info_col.setContentsMargins(0, 0, 0, 0)
            info_col.setSpacing(1)

            username_label = QLabel(self.current_user.username, user_card)
            username_label.setObjectName("sidebarUserName")
            username_label.setStyleSheet("color: #F1F5F9; font-size: 12px; font-weight: 700;")
            info_col.addWidget(username_label)

            role_sub = QLabel("المدير العام", user_card)
            role_sub.setStyleSheet("color: #06B6D4; font-size: 10px; font-weight: 600;")
            info_col.addWidget(role_sub)
            top_row.addLayout(info_col, 1)

            role_text = "المالك" if self.current_user.role == "owner" else "بائع"
            self.role_badge = QLabel(role_text, user_card)
            self.role_badge.setStyleSheet(
                "background-color: rgba(6, 182, 212, 0.15); color: #22D3EE; "
                "border: 1px solid rgba(6, 182, 212, 0.4); border-radius: 5px; "
                "padding: 2px 7px; font-size: 10px; font-weight: 700;"
            )
            top_row.addWidget(self.role_badge)
            user_layout.addLayout(top_row)

            self.lock_button = QPushButton("🔒   قفل الشاشة السريع", user_card)
            self.lock_button.setProperty("variant", "ghost")
            self.lock_button.setProperty("compact", True)
            self.lock_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.lock_button.setStyleSheet(
                "QPushButton {"
                "  font-size: 11px; font-weight: 600; padding: 6px 10px; border-radius: 7px; "
                "  color: #94A3B8; background-color: rgba(30, 41, 59, 0.7); "
                "  border: 1px solid rgba(51, 65, 85, 0.65); text-align: center;"
                "}"
                "QPushButton:hover {"
                "  color: #FFFFFF; background-color: #1E293B; border-color: #06B6D4;"
                "}"
            )
            self.lock_button.clicked.connect(self._lock_after_idle)
            user_layout.addWidget(self.lock_button)

            layout.addWidget(user_card)

        return sidebar

    @staticmethod
    def _placeholder_page(title: str) -> QWidget:
        page = QWidget()
        page.setLayoutDirection(get_layout_direction())
        layout = QVBoxLayout(page)
        layout.setContentsMargins(34, 30, 34, 30)
        heading = QLabel(title, page)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)

        card = QLabel(f"{ar.PLACEHOLDER_TITLE}\n{ar.PLACEHOLDER_BODY}", page)
        card.setObjectName("placeholderCard")
        card.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(card, 1)
        return page

    def _select_page(self, index: int) -> None:
        if self.current_user is not None and self.current_user.role != "owner" and index == 4:
            return
        self.pages.setCurrentIndex(index)
        if hasattr(self, "topbar"):
            self.topbar.set_active_page(index)
        for button_index, button in enumerate(self.buttons):
            button.setProperty("active", button_index == index)
            button.style().unpolish(button)
            button.style().polish(button)

    def _open_command_palette(self) -> None:
        dialog = CommandPaletteDialog(self.current_user, self)
        dialog.exec()

    def _open_shortcuts_dialog(self) -> None:
        dialog = ShortcutsDialog(self)
        dialog.exec()

    def _on_data_changed(self) -> None:
        if hasattr(self, "topbar"):
            self.topbar.refresh_notification_badge()

    def _on_language_changed(self, lang_code: str) -> None:
        """Update window title, sidebar items, layout direction, and child widgets."""
        direction = get_layout_direction(lang_code)

        app = QApplication.instance()
        if app is not None:
            app.setLayoutDirection(direction)
        self.setLayoutDirection(direction)

        if hasattr(self, "root_widget") and self.root_widget is not None:
            self.root_widget.setLayoutDirection(direction)
        if hasattr(self, "sidebar_widget") and self.sidebar_widget is not None:
            self.sidebar_widget.setLayoutDirection(direction)
        if hasattr(self, "content_container") and self.content_container is not None:
            self.content_container.setLayoutDirection(direction)

        self.setWindowTitle(ar.APP_TITLE)

        nav_icons = ("📊  ", "👥  ", "🛒  ", "💳  ", "📥  ", "📈  ", "⚙️  ")
        for index, button in enumerate(self.buttons):
            if index < len(ar.SIDEBAR_ITEMS):
                icon_prefix = nav_icons[index] if index < len(nav_icons) else ""
                button.setText(f"{icon_prefix}{ar.SIDEBAR_ITEMS[index]}")

        if hasattr(self, "password_button") and self.password_button is not None:
            self.password_button.setText(f"🔒  {ar.SET_CHANGE_PASSWORD}")

        if hasattr(self, "role_badge") and self.role_badge is not None and self.current_user is not None:
            self.role_badge.setText(ar.ROLE_OWNER if self.current_user.role == "owner" else ar.ROLE_SELLER)

        if hasattr(self, "lock_button") and self.lock_button is not None:
            self.lock_button.setText(ar.SIDEBAR_LOCK_SCREEN)

        if hasattr(self, "topbar") and self.topbar is not None:
            self.topbar.refresh_translations()

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+K"), self, self._open_command_palette)
        QShortcut(QKeySequence("Ctrl+N"), self, lambda: self._select_page(2))
        QShortcut(QKeySequence("Ctrl+L"), self, self._lock_after_idle)
        QShortcut(QKeySequence("F1"), self, self._open_shortcuts_dialog)
        QShortcut(QKeySequence("Ctrl+1"), self, lambda: self._select_page(0))
        QShortcut(QKeySequence("Ctrl+2"), self, lambda: self._select_page(1))
        QShortcut(QKeySequence("Ctrl+3"), self, lambda: self._select_page(2))
        QShortcut(QKeySequence("Ctrl+4"), self, lambda: self._select_page(3))
        QShortcut(QKeySequence("Ctrl+5"), self, lambda: self._select_page(4))
        QShortcut(QKeySequence("Ctrl+6"), self, lambda: self._select_page(5))
        QShortcut(QKeySequence("Ctrl+7"), self, lambda: self._select_page(6))

    def _show_customer_status(self, status: str) -> None:
        """Navigate from a dashboard status card to matching customers."""
        if self.customers_page is None:
            return
        if self.dashboard_page is not None:
            self.customers_page.month_selector.setDate(
                self.dashboard_page.month_selector.date()
            )
        self.customers_page.set_status_filter(status)
        self._select_page(1)

    def _open_customer_details(self, customer_id: int) -> None:
        """Open the selected customer's sale and payment history."""
        if self.current_user is None:
            return
        dialog = CustomerDetailsDialog(customer_id, self.current_user, self)
        edit_requested: list[int] = []
        dialog.edit_requested.connect(edit_requested.append)
        dialog.exec()
        if edit_requested and self.customers_page is not None:
            self._select_page(1)
            self.customers_page.edit_customer_by_id(edit_requested[-1])

    def _restore_geometry(self) -> None:
        geometry = self.settings.value("window/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(1440, 900)

    def _setup_inactivity_lock(self) -> None:
        """Lock the visible window after the configured idle period."""
        if self.current_user is None:
            return
        with session_scope() as session:
            idle_minutes = get_value(session, "inactivity_minutes", 15)
        if isinstance(idle_minutes, bool) or not isinstance(idle_minutes, int):
            idle_minutes = 15
        idle_minutes = min(max(idle_minutes, 1), 180)
        self._idle_timeout_ms = idle_minutes * 60_000
        self._lock_timer = QTimer(self)
        self._lock_timer.setSingleShot(True)
        self._lock_timer.timeout.connect(self._lock_after_idle)
        application = QApplication.instance()
        if application is not None:
            application.installEventFilter(self)
        self._lock_timer.start(self._idle_timeout_ms)

    def eventFilter(self, watched: object, event: QEvent) -> bool:  # noqa: N802 - Qt callback name
        """Restart the idle timer for keyboard, pointer, and touch activity."""
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
        """Require the current user's password while preserving open form state."""
        if self.current_user is None:
            return
        self._reauth_dialog = ReauthenticationDialog(self.current_user, self)
        try:
            self._reauth_dialog.exec()
        finally:
            self._reauth_dialog = None
        self._lock_timer.start(self._idle_timeout_ms)

    def _refresh_inactivity_timeout(self) -> None:
        """Apply a newly saved idle-lock interval immediately."""
        if self.current_user is None:
            return
        with session_scope() as session:
            idle_minutes = get_value(session, "inactivity_minutes", 15)
        if isinstance(idle_minutes, bool) or not isinstance(idle_minutes, int):
            idle_minutes = 15
        self._idle_timeout_ms = min(max(idle_minutes, 1), 180) * 60_000
        if hasattr(self, "_lock_timer") and self._reauth_dialog is None:
            self._lock_timer.start(self._idle_timeout_ms)

    def _setup_auto_backup_schedule(self) -> None:
        """Schedule periodic background checks to maintain 3 daily automated backups."""
        self._auto_backup_timer = QTimer(self)
        self._auto_backup_timer.setInterval(30 * 60 * 1000)  # check every 30 minutes
        self._auto_backup_timer.timeout.connect(self._run_scheduled_checkpoint_backup)
        self._auto_backup_timer.start()

    def _run_scheduled_checkpoint_backup(self) -> None:
        """Create an automated checkpoint if today has fewer than 3 backups."""
        try:
            from app.services.backup import (
                automated_backups_count,
                create_automated_checkpoint_backup,
            )
            # Minimum 2 hours between periodic runtime backups
            created = create_automated_checkpoint_backup(max_per_day=3, min_interval_seconds=7200)
            if created is not None:
                count = automated_backups_count()
                events.notify.emit("info", ar.SET_BACKUP_AUTO_TOAST.format(count=count))
                events.data_changed.emit()
        except Exception:
            pass

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt callback name
        self.settings.setValue("window/geometry", self.saveGeometry())
        try:
            from app.services.backup import create_automated_checkpoint_backup
            create_automated_checkpoint_backup(max_per_day=3, min_interval_seconds=1800)
        except Exception:
            pass
        super().closeEvent(event)

