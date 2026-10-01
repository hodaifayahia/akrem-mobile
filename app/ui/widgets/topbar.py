"""Top bar: page title, quick search, new-sale action, notifications, clock, language."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.i18n import SUPPORTED_LANGUAGES, ar, get_language
from app.ui import icons
from app.ui.locale import change_language
from app.ui.notifications import hub
from app.ui.theme import repolish
from app.ui.widgets.notification_center import NotificationCenterPopup


class AppTopBar(QFrame):
    """Header above every page."""

    command_palette_requested = Signal()
    shortcuts_requested = Signal()
    new_sale_requested = Signal()
    customer_opened = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("appTopBar")
        self.setFixedHeight(64)
        self._active_page_idx = 0
        self.notification_popup = NotificationCenterPopup(self)
        self.notification_popup.customer_opened.connect(self.customer_opened)
        self._build_ui()
        self._setup_clock()
        self.refresh_translations()
        self.refresh_notification_badge()

        from app.ui.events import events

        events.language_changed.connect(self._on_global_language_changed)
        hub.changed.connect(self.update_badge)

    # ------------------------------------------------------------------ build
    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 10, 20, 10)
        layout.setSpacing(12)

        titles = QVBoxLayout()
        titles.setContentsMargins(0, 0, 0, 0)
        titles.setSpacing(0)
        self.page_title_label = QLabel(self)
        self.page_title_label.setObjectName("topBarTitle")
        self.page_sub_label = QLabel(self)
        self.page_sub_label.setObjectName("topBarSubtitle")
        titles.addWidget(self.page_title_label)
        titles.addWidget(self.page_sub_label)
        layout.addLayout(titles)
        layout.addSpacing(12)

        self.search_btn = QPushButton(self)
        self.search_btn.setObjectName("spotlightButton")
        self.search_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.search_btn.setMinimumWidth(260)
        self.search_btn.setMaximumWidth(420)
        self.search_btn.setFixedHeight(38)
        search_row = QHBoxLayout(self.search_btn)
        search_row.setContentsMargins(12, 0, 8, 0)
        search_row.setSpacing(8)
        self.search_icon = QLabel(self.search_btn)
        self.search_icon.setPixmap(icons.pixmap("search", "text-muted", 16))
        self.search_text_label = QLabel(self.search_btn)
        self.search_text_label.setObjectName("spotlightText")
        shortcut = QLabel("Ctrl+K", self.search_btn)
        shortcut.setObjectName("kbd")
        for child in (self.search_icon, self.search_text_label, shortcut):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        search_row.addWidget(self.search_icon)
        search_row.addWidget(self.search_text_label, 1)
        search_row.addWidget(shortcut)
        self.search_btn.clicked.connect(self.command_palette_requested.emit)
        layout.addWidget(self.search_btn, 1)
        layout.addStretch(0)

        self.new_sale_btn = QPushButton(self)
        self.new_sale_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_sale_btn.setIconSize(QSize(16, 16))
        self.new_sale_btn.clicked.connect(self.new_sale_requested.emit)
        layout.addWidget(self.new_sale_btn)

        self.kb_btn = self._icon_button("keyboard")
        self.kb_btn.clicked.connect(self.shortcuts_requested.emit)
        layout.addWidget(self.kb_btn)

        self.bell_container = QWidget(self)
        self.bell_container.setFixedSize(40, 40)
        self.bell_btn = self._icon_button("bell", self.bell_container)
        self.bell_btn.move(0, 2)
        self.bell_btn.clicked.connect(self._toggle_notifications)
        self.bell_badge = QLabel(self.bell_container)
        self.bell_badge.setObjectName("bellBadge")
        self.bell_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bell_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.bell_badge.hide()
        layout.addWidget(self.bell_container)

        clock = QFrame(self)
        clock.setObjectName("topBarClock")
        clock_layout = QVBoxLayout(clock)
        clock_layout.setContentsMargins(12, 3, 12, 3)
        clock_layout.setSpacing(0)
        self.time_label = QLabel(clock)
        self.time_label.setObjectName("clockTime")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.date_label = QLabel(clock)
        self.date_label.setObjectName("clockDate")
        self.date_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        clock_layout.addWidget(self.time_label)
        clock_layout.addWidget(self.date_label)
        layout.addWidget(clock)

        self.lang_selector = QComboBox(self)
        self.lang_selector.setObjectName("topBarLangSelector")
        self.lang_selector.setCursor(Qt.CursorShape.PointingHandCursor)
        for code, native_name in SUPPORTED_LANGUAGES.items():
            self.lang_selector.addItem(icons.icon("globe", "text-muted", 14), native_name, code)
        self._select_language_item(get_language())
        self.lang_selector.currentIndexChanged.connect(self._on_lang_changed)
        layout.addWidget(self.lang_selector)

    def _icon_button(self, name: str, parent: QWidget | None = None) -> QToolButton:
        button = QToolButton(parent or self)
        button.setObjectName("iconButton")
        button.setProperty("iconName", name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedSize(38, 38)
        button.setIconSize(QSize(18, 18))
        button.setIcon(icons.icon(name, "text-muted", 18))
        return button

    # ------------------------------------------------------------------ clock
    def _setup_clock(self) -> None:
        self._update_clock()
        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start()

    def _update_clock(self) -> None:
        now = datetime.now()
        self.time_label.setText(now.strftime("%H:%M"))
        self.date_label.setText(format_long_date(now))

    # ------------------------------------------------------------ navigation
    def set_active_page(self, page_index: int) -> None:
        """Show the title and description of the visible page."""
        self._active_page_idx = page_index
        if 0 <= page_index < len(ar.SIDEBAR_ITEMS):
            self.page_title_label.setText(ar.SIDEBAR_ITEMS[page_index])
            self.page_sub_label.setText(ar.PAGE_SUBTITLES[page_index])

    def refresh_translations(self) -> None:
        """Refresh every label and icon after a language change."""
        self.search_text_label.setText(ar.TOPBAR_SEARCH_HINT)
        self.search_btn.setToolTip(ar.TOPBAR_SEARCH_HINT)
        self.new_sale_btn.setText(ar.TOPBAR_NEW_SALE)
        self.new_sale_btn.setToolTip(ar.TOPBAR_NEW_SALE_TIP)
        self.new_sale_btn.setIcon(icons.icon("plus", "#FFFFFF", 16))
        self.kb_btn.setToolTip(ar.TOPBAR_SHORTCUTS_TIP)
        self.bell_btn.setToolTip(ar.TOPBAR_NOTIFICATIONS_TIP)
        self.lang_selector.setToolTip(ar.TOPBAR_LANGUAGE_TIP)
        for button in (self.kb_btn, self.bell_btn):
            button.setIcon(icons.icon(button.property("iconName"), "text-muted", 18))
        self.search_icon.setPixmap(icons.pixmap("search", "text-muted", 16))
        self.set_active_page(self._active_page_idx)
        self._update_clock()
        self.notification_popup.retranslate()
        self._place_badge()

    # -------------------------------------------------------------- language
    def _select_language_item(self, code: str) -> None:
        index = self.lang_selector.findData(code)
        if index >= 0 and self.lang_selector.currentIndex() != index:
            self.lang_selector.blockSignals(True)
            self.lang_selector.setCurrentIndex(index)
            self.lang_selector.blockSignals(False)

    def _on_lang_changed(self, index: int) -> None:
        code = self.lang_selector.itemData(index)
        if code and code != get_language():
            change_language(code)

    def _on_global_language_changed(self, code: str) -> None:
        self._select_language_item(code)
        self.refresh_translations()

    # --------------------------------------------------------- notifications
    def refresh_notification_badge(self) -> None:
        """Reload collection alerts and update the bell badge."""
        self.notification_popup.refresh_alerts()
        self.update_badge()

    def update_badge(self) -> None:
        """Show unread activity plus open collection alerts on the bell."""
        count = self.notification_popup.get_alert_count() + hub.unread_count()
        if count > 0:
            self.bell_badge.setText("99+" if count > 99 else str(count))
            self.bell_badge.show()
        else:
            self.bell_badge.hide()
        self.bell_btn.setProperty("active", count > 0)
        repolish(self.bell_btn)
        self._place_badge()

    def _place_badge(self) -> None:
        self.bell_badge.adjustSize()
        width = max(16, self.bell_badge.sizeHint().width())
        x = 0 if self.isRightToLeft() else self.bell_container.width() - width
        self.bell_badge.setGeometry(x, 0, width, 16)
        self.bell_badge.raise_()

    def _toggle_notifications(self) -> None:
        if self.notification_popup.isVisible():
            self.notification_popup.hide()
            return
        self.open_notifications()

    def open_notifications(self) -> None:
        """Open the notification center under the bell."""
        self.refresh_notification_badge()
        popup = self.notification_popup
        anchor = self.bell_btn
        bottom = anchor.mapToGlobal(QPoint(0, anchor.height() + 8))
        if self.isRightToLeft():
            x = bottom.x()
        else:
            x = anchor.mapToGlobal(QPoint(anchor.width(), 0)).x() - popup.width()
        screen = QGuiApplication.screenAt(bottom) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left() + 8, min(x, area.right() - popup.width() - 8))
        popup.move(x, bottom.y())
        popup.show()
        popup.raise_()


def format_long_date(moment: datetime) -> str:
    """Format a date with the active language's day and month names."""
    day = ar.DAY_NAMES[moment.weekday()]
    month = ar.MONTH_NAMES[moment.month]
    language = get_language()
    if language == "en":
        return f"{day}, {month} {moment.day}, {moment.year}"
    if language == "fr":
        return f"{day} {moment.day} {month} {moment.year}"
    return f"{day}، {moment.day} {month} {moment.year}"
