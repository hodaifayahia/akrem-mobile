"""Notification center popup opened from the top-bar bell.

Two kinds of content share one panel:

* collection alerts (overdue / upcoming installments, late credit), computed
  by :mod:`app.services.alerts`; and
* the activity history kept by the notification hub (background digests,
  backups, confirmations), with unread markers.
"""

from __future__ import annotations

import logging
from datetime import date

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.db.session import session_scope
from app.i18n import ar
from app.services.alerts import CollectionAlert, collection_alerts
from app.services.reminders import whatsapp_link
from app.ui import icons
from app.ui.notifications import AppNotification, hub, relative_time

_LOG = logging.getLogger(__name__)

FILTERS = ("all", "overdue", "upcoming", "activity")
_FILTER_KEYS = {
    "all": "NOTIF_TAB_ALL",
    "overdue": "NOTIF_TAB_OVERDUE",
    "upcoming": "NOTIF_TAB_UPCOMING",
    "activity": "NOTIF_TAB_ACTIVITY",
}
_LEVEL_ICONS = {
    "success": ("check-circle", "paid"),
    "warning": ("alert", "pending"),
    "error": ("alert", "failed"),
    "info": ("info", "primary-glow"),
}


def alert_line(alert: CollectionAlert, grace_days: int | None = None) -> str:
    """Describe an alert in one short line ("3 days overdue — A15")."""
    if alert.kind == "credit_overdue":
        return ar.NOTIF_CREDIT_OVERDUE_ITEM.format(days=alert.days_late, product=alert.product)
    if alert.kind == "overdue":
        return ar.NOTIF_OVERDUE_ITEM.format(days=alert.days_late, product=alert.product)
    if alert.days_late > 0:
        return ar.NOTIF_GRACE_ITEM.format(days=alert.days_late, product=alert.product)
    if alert.days_late == 0:
        return ar.NOTIF_DUE_TODAY_ITEM.format(product=alert.product)
    return ar.NOTIF_DUE_IN_ITEM.format(days=-alert.days_late, product=alert.product)


def money(amount: int) -> str:
    """Format whole dinars with Western digits and the currency suffix."""
    return f"{amount:,} {ar.CURRENCY_SUFFIX}"


class NotificationCard(QFrame):
    """One collection alert with quick actions."""

    def __init__(self, alert: CollectionAlert, on_open_customer, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.alert = alert
        self.on_open_customer = on_open_customer
        self.setObjectName("notificationCard")
        late = alert.kind != "due_soon"

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        badge = QLabel(self)
        badge.setFixedSize(30, 30)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setProperty("pill", "failed" if late else "info")
        badge.setPixmap(icons.pixmap("alert" if late else "clock", "failed" if late else "highlight", 16))
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(3)
        header = QHBoxLayout()
        header.setSpacing(8)
        name = QLabel(alert.customer_name, self)
        name.setObjectName("notificationTitle")
        amount = QLabel(money(alert.amount), self)
        amount.setObjectName("moneyValue")
        amount.setProperty("pill", "failed" if late else "info")
        header.addWidget(name, 1)
        header.addWidget(amount)
        body.addLayout(header)

        detail = QLabel(alert_line(alert), self)
        detail.setObjectName("notificationBody")
        detail.setWordWrap(True)
        body.addWidget(detail)

        footer = QHBoxLayout()
        footer.setSpacing(6)
        when = QLabel(ar.NOTIF_DUE_DATE.format(date=alert.due_date.strftime("%d/%m/%Y")), self)
        when.setObjectName("notificationTime")
        footer.addWidget(when, 1)
        view = QPushButton(ar.NOTIF_OPEN_CUSTOMER, self)
        view.setProperty("variant", "secondary")
        view.setProperty("compact", True)
        view.setCursor(Qt.CursorShape.PointingHandCursor)
        view.clicked.connect(lambda: self.on_open_customer(alert.customer_id))
        footer.addWidget(view)
        if alert.customer_phone:
            whatsapp = QPushButton(ar.NOTIF_WHATSAPP, self)
            whatsapp.setProperty("variant", "whatsapp")
            whatsapp.setProperty("compact", True)
            whatsapp.setIcon(icons.icon("message", "#FFFFFF", 14))
            whatsapp.setCursor(Qt.CursorShape.PointingHandCursor)
            whatsapp.clicked.connect(self._open_whatsapp)
            footer.addWidget(whatsapp)
        body.addLayout(footer)
        layout.addLayout(body, 1)

    def _open_whatsapp(self) -> None:
        message = ar.PAY_REMINDERS_MESSAGE.format(
            customer=self.alert.customer_name,
            amount=f"{self.alert.amount:,}",
            due=self.alert.due_date.strftime("%d/%m/%Y"),
        )
        link = whatsapp_link(self.alert.customer_phone, message)
        if link is not None:
            QDesktopServices.openUrl(QUrl(link))


class ActivityCard(QFrame):
    """One entry of the notification history."""

    def __init__(self, entry: AppNotification, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("notificationCard")
        self.setProperty("unread", not entry.read)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        icon_name, color = _LEVEL_ICONS.get(entry.level, _LEVEL_ICONS["info"])
        badge = QLabel(self)
        badge.setFixedSize(30, 30)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setPixmap(icons.pixmap(icon_name, color, 16))
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        body = QVBoxLayout()
        body.setSpacing(2)
        top = QHBoxLayout()
        title = QLabel(entry.title, self)
        title.setObjectName("notificationTitle")
        when = QLabel(relative_time(entry.created_at), self)
        when.setObjectName("notificationTime")
        top.addWidget(title, 1)
        top.addWidget(when)
        body.addLayout(top)
        message = QLabel(entry.message, self)
        message.setObjectName("notificationBody")
        message.setWordWrap(True)
        body.addWidget(message)
        layout.addLayout(body, 1)


class NotificationCenterPopup(QFrame):
    """Drop-down panel attached to the notification bell."""

    customer_opened = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("notificationCenterPopup")
        self.setFixedSize(430, 540)
        self._alerts: list[CollectionAlert] = []
        self._current_filter = "all"
        self._build_ui()
        self.retranslate()
        hub.changed.connect(self._on_history_changed)
        self.refresh_alerts()

    # ------------------------------------------------------------------ build
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.title_label = QLabel(self)
        self.title_label.setObjectName("popupTitle")
        header.addWidget(self.title_label, 1)
        self.badge_count = QLabel("0", self)
        self.badge_count.setProperty("pill", "failed")
        header.addWidget(self.badge_count)
        self.mark_read_btn = QPushButton(self)
        self.mark_read_btn.setProperty("variant", "ghost")
        self.mark_read_btn.setProperty("compact", True)
        self.mark_read_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mark_read_btn.clicked.connect(hub.mark_all_read)
        header.addWidget(self.mark_read_btn)
        self.refresh_btn = QToolButton(self)
        self.refresh_btn.setObjectName("iconButton")
        self.refresh_btn.setFixedSize(30, 30)
        self.refresh_btn.setIconSize(QSize(15, 15))
        self.refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_btn.clicked.connect(self.refresh_alerts)
        header.addWidget(self.refresh_btn)
        layout.addLayout(header)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)
        self.filter_buttons: dict[str, QPushButton] = {}
        for name in FILTERS:
            chip = QPushButton(self)
            chip.setProperty("chip", True)
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _checked=False, n=name: self._set_filter(n))
            self.filter_group.addButton(chip)
            self.filter_buttons[name] = chip
            chips.addWidget(chip)
        chips.addStretch(1)
        self.filter_buttons["all"].setChecked(True)
        layout.addLayout(chips)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 2, 0, 2)
        self.cards_layout.setSpacing(8)
        self.scroll.setWidget(self.cards_container)
        layout.addWidget(self.scroll, 1)

    def retranslate(self) -> None:
        """Refresh labels after a language change."""
        self.title_label.setText(ar.NOTIF_CENTER_TITLE)
        self.mark_read_btn.setText(ar.NOTIF_MARK_ALL_READ)
        self.refresh_btn.setToolTip(ar.NOTIF_REFRESH)
        self.refresh_btn.setIcon(icons.icon("refresh", "text-muted", 15))
        self._update_chip_labels()
        self._render_cards()

    # ------------------------------------------------------------------- data
    def refresh_alerts(self) -> None:
        """Reload collection alerts from the database."""
        try:
            with session_scope() as session:
                self.set_alerts(collection_alerts(session, today=date.today()))
        except Exception:  # noqa: BLE001 - keep the shell usable if the DB is unavailable
            _LOG.warning("Could not load collection alerts", exc_info=True)

    def set_alerts(self, alerts: list[CollectionAlert]) -> None:
        """Show alerts computed elsewhere (e.g. by the background monitor)."""
        self._alerts = list(alerts)
        self.badge_count.setText(str(len(self._alerts)))
        self.badge_count.setVisible(bool(self._alerts))
        self._update_chip_labels()
        self._render_cards()

    def get_alert_count(self) -> int:
        """Return how many collection alerts are listed."""
        return len(self._alerts)

    def attention_count(self) -> int:
        """Return how many listed alerts are already late (installment or credit)."""
        return sum(1 for alert in self._alerts if alert.kind != "due_soon")

    def _set_filter(self, filter_name: str) -> None:
        self._current_filter = filter_name if filter_name in FILTERS else "all"
        self.filter_buttons[self._current_filter].setChecked(True)
        self._render_cards()

    def _on_history_changed(self) -> None:
        self._update_chip_labels()
        if self._current_filter in ("all", "activity"):
            self._render_cards()

    # ------------------------------------------------------------- rendering
    def _update_chip_labels(self) -> None:
        overdue = sum(1 for alert in self._alerts if alert.kind != "due_soon")
        counts = {
            "all": len(self._alerts) + hub.unread_count(),
            "overdue": overdue,
            "upcoming": len(self._alerts) - overdue,
            "activity": hub.unread_count(),
        }
        for name, button in self.filter_buttons.items():
            label = getattr(ar, _FILTER_KEYS[name])
            button.setText(f"{label}  {counts[name]}" if counts[name] else label)
        self.mark_read_btn.setVisible(hub.unread_count() > 0)

    def _render_cards(self) -> None:
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        widgets: list[QWidget] = []
        if self._current_filter in ("all", "overdue", "upcoming"):
            for alert in self._alerts:
                late = alert.kind != "due_soon"
                if self._current_filter == "overdue" and not late:
                    continue
                if self._current_filter == "upcoming" and late:
                    continue
                widgets.append(NotificationCard(alert, self._handle_open_customer, self.cards_container))
        if self._current_filter in ("all", "activity"):
            widgets.extend(ActivityCard(entry, self.cards_container) for entry in hub.history())
        if not widgets:
            self.cards_layout.addWidget(self._empty_state())
        for widget in widgets:
            self.cards_layout.addWidget(widget)
        self.cards_layout.addStretch(1)

    def _empty_state(self) -> QWidget:
        box = QWidget(self.cards_container)
        column = QVBoxLayout(box)
        column.setContentsMargins(16, 48, 16, 48)
        column.setSpacing(8)
        icon = QLabel(box)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setPixmap(icons.pixmap("check-circle", "paid", 40, stroke=1.5))
        column.addWidget(icon)
        title = QLabel(ar.NOTIF_EMPTY_TITLE, box)
        title.setObjectName("emptyStateTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(title)
        body_text = ar.NOTIF_EMPTY_ACTIVITY if self._current_filter == "activity" else ar.NOTIF_EMPTY_BODY
        body = QLabel(body_text, box)
        body.setObjectName("emptyStateSub")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setWordWrap(True)
        column.addWidget(body)
        return box

    def _handle_open_customer(self, customer_id: int) -> None:
        self.hide()
        self.customer_opened.emit(customer_id)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt callback name
        """Opening the panel counts as reading the activity history."""
        super().hideEvent(event)
        hub.mark_all_read()
