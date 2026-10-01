"""World-class Notification Center and drawer popup for AkremMobile."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import urllib.parse
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models import Customer, Installment, Sale
from app.db.session import session_scope
from app.services.customers import _grace_days
from app.ui.events import events


@dataclass
class AlertItem:
    alert_type: str  # 'overdue', 'upcoming', 'system'
    customer_id: int | None
    customer_name: str
    customer_phone: str | None
    amount: int
    due_date: date
    days_delta: int  # > 0 means overdue by N days, < 0 means due in N days


class NotificationCard(QFrame):
    """Interactive notification card with quick action buttons."""

    def __init__(self, alert: AlertItem, on_open_customer: callable, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.alert = alert
        self.on_open_customer = on_open_customer
        self.setObjectName("notificationCard")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        # Header row: Status pill + Customer Name + Amount
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        if self.alert.alert_type == "overdue":
            pill = QLabel(f"متأخر {self.alert.days_delta} يوم", self)
            pill.setStyleSheet(
                "background-color: #361014; color: #F87171; border: 1px solid #7F1D1D; "
                "border-radius: 4px; padding: 2px 6px; font-size: 11px; font-weight: 700;"
            )
            border_color = "#7F1D1D"
        else:
            pill = QLabel(f"مستحق خلال {abs(self.alert.days_delta)} يوم", self)
            pill.setStyleSheet(
                "background-color: #12284C; color: #9DBEFF; border: 1px solid #234275; "
                "border-radius: 4px; padding: 2px 6px; font-size: 11px; font-weight: 700;"
            )
            border_color = "#234275"

        name_label = QLabel(self.alert.customer_name, self)
        name_label.setStyleSheet("color: #FFFFFF; font-weight: 700; font-size: 13px;")

        amount_label = QLabel(f"{self.alert.amount:,} دج", self)
        amount_label.setStyleSheet(
            "color: #F87171; font-weight: 700; font-size: 13px; font-family: 'Rajdhani', sans-serif;"
            if self.alert.alert_type == "overdue"
            else "color: #9DBEFF; font-weight: 700; font-size: 13px; font-family: 'Rajdhani', sans-serif;"
        )

        header.addWidget(pill)
        header.addWidget(name_label, 1)
        header.addWidget(amount_label)
        layout.addLayout(header)

        # Subtext row: Due date & info
        sub_layout = QHBoxLayout()
        sub_layout.setContentsMargins(0, 0, 0, 0)

        date_str = self.alert.due_date.strftime("%Y-%m-%d")
        info_label = QLabel(f"تاريخ الاستحقاق: {date_str}", self)
        info_label.setStyleSheet("color: #8A94A6; font-size: 11px;")
        sub_layout.addWidget(info_label, 1)

        # Action buttons
        if self.alert.customer_id is not None:
            view_btn = QPushButton("عرض الزبون 👤", self)
            view_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            view_btn.setStyleSheet(
                "QPushButton { background-color: #121A2C; color: #9DBEFF; border: 1px solid #24334C; "
                "border-radius: 5px; padding: 3px 8px; font-size: 11px; } "
                "QPushButton:hover { background-color: #0758CD; color: #FFFFFF; border-color: #3B92D9; }"
            )
            view_btn.clicked.connect(self._handle_view)
            sub_layout.addWidget(view_btn)

        if self.alert.customer_phone:
            wa_btn = QPushButton("واتساب 💬", self)
            wa_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            wa_btn.setStyleSheet(
                "QPushButton { background-color: #0D2E1E; color: #4ADE80; border: 1px solid #14532D; "
                "border-radius: 5px; padding: 3px 8px; font-size: 11px; font-weight: 600; } "
                "QPushButton:hover { background-color: #16A34A; color: #FFFFFF; border-color: #22C55E; }"
            )
            wa_btn.clicked.connect(self._open_whatsapp)
            sub_layout.addWidget(wa_btn)

        layout.addLayout(sub_layout)

        self.setStyleSheet(
            f"QFrame#notificationCard {{ "
            f"  background-color: #0E1522; "
            f"  border: 1px solid {border_color}; "
            f"  border-radius: 8px; "
            f"}} "
            f"QFrame#notificationCard:hover {{ "
            f"  background-color: #121B2C; "
            f"}}"
        )

    def _handle_view(self) -> None:
        if self.alert.customer_id is not None:
            self.on_open_customer(self.alert.customer_id)

    def _open_whatsapp(self) -> None:
        if not self.alert.customer_phone:
            return
        phone = self.alert.customer_phone.strip()
        if phone.startswith("0"):
            phone = "213" + phone[1:]
        msg = (
            f"السلام عليكم أخي {self.alert.customer_name}، نود تذكيركم بمستحقات القسط "
            f"بقيمة {self.alert.amount:,} دج لدى أكرم موبايل. شكراً لوفائكم."
        )
        url = f"https://wa.me/{phone}?text={urllib.parse.quote(msg)}"
        QDesktopServices.openUrl(QUrl(url))


class NotificationCenterPopup(QFrame):
    """Drop-down drawer attached to the topbar notification bell."""

    customer_opened = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("notificationCenterPopup")
        self.setFixedSize(420, 520)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._alerts: list[AlertItem] = []
        self._current_filter = "all"
        self._build_ui()
        self.refresh_alerts()

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(10)

        # Header
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        bell_icon = QLabel("🔔", self)
        bell_icon.setStyleSheet("font-size: 16px;")
        header.addWidget(bell_icon)

        title = QLabel("مركز التنبيهات والإشعارات", self)
        title.setStyleSheet("color: #FFFFFF; font-size: 15px; font-weight: 700;")
        header.addWidget(title, 1)

        self.badge_count = QLabel("0", self)
        self.badge_count.setStyleSheet(
            "background-color: #EF4444; color: #FFFFFF; border-radius: 9px; "
            "padding: 2px 7px; font-size: 11px; font-weight: 700;"
        )
        header.addWidget(self.badge_count)

        refresh_btn = QPushButton("🔄", self)
        refresh_btn.setFixedSize(28, 28)
        refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_btn.setToolTip("تحديث البيانات")
        refresh_btn.setStyleSheet(
            "QPushButton { background-color: #121A2C; border: 1px solid #1F2A3D; border-radius: 6px; } "
            "QPushButton:hover { background-color: #1A2740; }"
        )
        refresh_btn.clicked.connect(self.refresh_alerts)
        header.addWidget(refresh_btn)

        main_layout.addLayout(header)

        # Filter Tabs
        tab_row = QHBoxLayout()
        tab_row.setContentsMargins(0, 0, 0, 0)
        tab_row.setSpacing(6)

        self.btn_all = QPushButton("الكل", self)
        self.btn_overdue = QPushButton("متأخرات ⚠️", self)
        self.btn_upcoming = QPushButton("قريباً ⏳", self)

        for btn in (self.btn_all, self.btn_overdue, self.btn_upcoming):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton { background-color: #0E1522; color: #8A94A6; border: 1px solid #1F2E47; "
                "border-radius: 6px; padding: 4px 10px; font-size: 11px; font-weight: 600; } "
                "QPushButton:hover { color: #FFFFFF; background-color: #162032; } "
                "QPushButton[active='true'] { background-color: #0758CD; color: #FFFFFF; border-color: #3B92D9; }"
            )
            tab_row.addWidget(btn)

        self.btn_all.setProperty("active", True)
        self.btn_all.clicked.connect(lambda: self._set_filter("all"))
        self.btn_overdue.clicked.connect(lambda: self._set_filter("overdue"))
        self.btn_upcoming.clicked.connect(lambda: self._set_filter("upcoming"))

        main_layout.addLayout(tab_row)

        # Scroll Area for notifications
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self.cards_container = QWidget()
        self.cards_container.setObjectName("cardsContainer")
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 4, 0, 4)
        self.cards_layout.setSpacing(8)
        self.scroll.setWidget(self.cards_container)

        main_layout.addWidget(self.scroll, 1)

        # Container styling
        self.setStyleSheet(
            "QFrame#notificationCenterPopup { "
            "  background-color: #0A0E17; "
            "  border: 1px solid #1F2E47; "
            "  border-radius: 12px; "
            "}"
        )

    def _set_filter(self, filter_name: str) -> None:
        self._current_filter = filter_name
        self.btn_all.setProperty("active", filter_name == "all")
        self.btn_overdue.setProperty("active", filter_name == "overdue")
        self.btn_upcoming.setProperty("active", filter_name == "upcoming")
        for btn in (self.btn_all, self.btn_overdue, self.btn_upcoming):
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._render_cards()

    def refresh_alerts(self) -> None:
        """Fetch overdue installments and upcoming obligations."""
        today = date.today()
        self._alerts.clear()

        try:
            with session_scope() as session:
                grace_days = _grace_days(session)
                # Fetch pending or failed installments
                sales = session.scalars(
                    select(Sale).options(
                        selectinload(Sale.customer),
                        selectinload(Sale.installments).selectinload(Installment.payments),
                    )
                ).all()

                for sale in sales:
                    if not sale.customer:
                        continue
                    for inst in sale.installments:
                        paid = max(
                            inst.amount_paid,
                            sum(p.amount for p in inst.payments),
                        )
                        remaining = inst.amount_due - paid
                        if remaining <= 0:
                            continue

                        due_date = inst.due_date
                        delta_days = (today - due_date).days

                        # If past due + grace period -> Overdue alert
                        if delta_days > grace_days:
                            self._alerts.append(
                                AlertItem(
                                    alert_type="overdue",
                                    customer_id=sale.customer.id,
                                    customer_name=sale.customer.full_name,
                                    customer_phone=sale.customer.phone,
                                    amount=remaining,
                                    due_date=due_date,
                                    days_delta=delta_days,
                                )
                            )
                        # If due within next 7 days -> Upcoming alert
                        elif 0 <= -delta_days <= 7:
                            self._alerts.append(
                                AlertItem(
                                    alert_type="upcoming",
                                    customer_id=sale.customer.id,
                                    customer_name=sale.customer.full_name,
                                    customer_phone=sale.customer.phone,
                                    amount=remaining,
                                    due_date=due_date,
                                    days_delta=delta_days,
                                )
                            )

            # Sort: overdues by highest days overdue first, upcoming by closest date
            self._alerts.sort(
                key=lambda a: (0 if a.alert_type == "overdue" else 1, -a.days_delta if a.alert_type == "overdue" else a.days_delta)
            )
        except Exception:
            pass

        self.badge_count.setText(str(len(self._alerts)))
        self._render_cards()

    def _render_cards(self) -> None:
        # Clear existing cards
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        filtered = [
            a for a in self._alerts
            if self._current_filter == "all"
            or (self._current_filter == "overdue" and a.alert_type == "overdue")
            or (self._current_filter == "upcoming" and a.alert_type == "upcoming")
        ]

        if not filtered:
            empty_widget = QWidget()
            empty_layout = QVBoxLayout(empty_widget)
            empty_layout.setContentsMargins(16, 40, 16, 40)
            empty_layout.setSpacing(10)
            empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

            icon = QLabel("✨", empty_widget)
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon.setStyleSheet("font-size: 38px;")
            empty_layout.addWidget(icon)

            msg = QLabel("لا توجد إشعارات جديدة\nجميع الأقساط منتظمة وحسابات الزبائن محدثة.", empty_widget)
            msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
            msg.setStyleSheet("color: #8A94A6; font-size: 13px; line-height: 1.4;")
            empty_layout.addWidget(msg)

            self.cards_layout.addWidget(empty_widget)
            return

        for alert in filtered:
            card = NotificationCard(alert, on_open_customer=self._handle_open_customer, parent=self.cards_container)
            self.cards_layout.addWidget(card)

        self.cards_layout.addStretch(1)

    def _handle_open_customer(self, customer_id: int) -> None:
        self.hide()
        self.customer_opened.emit(customer_id)
        events.open_customer.emit(customer_id)

    def get_alert_count(self) -> int:
        return len(self._alerts)
