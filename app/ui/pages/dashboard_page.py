"""Dashboard: greeting, month picker, KPIs, trend, collection rate and follow-ups.

Information is ordered by how often the shop acts on it:

1. operations status for the month (paid / pending / failed) - clickable
   filters into the customer list;
2. the six-month collection trend and this month's collection rate;
3. owner-only financial KPIs;
4. a "needs attention" list of the most overdue customers next to today's
   register.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.db.models import User
from app.db.session import session_scope
from app.i18n import ar
from app.services.alerts import CollectionAlert, collection_alerts
from app.services.dashboard import DashboardSummary, get_dashboard_summary
from app.services.finance import FinancialPosition, financial_position
from app.ui import icons
from app.ui.events import events
from app.ui.theme import qcolor
from app.ui.widgets.collection_gauge import CollectionGaugeWidget
from app.ui.widgets.daily_register_card import DailyRegisterCard
from app.ui.widgets.financial_chart import FinancialTrendChart
from app.ui.widgets.notification_center import alert_line, money
from app.ui.widgets.responsive_grid import ResponsiveGrid
from app.ui.widgets.stat_card import StatCard

_LOG = logging.getLogger(__name__)
ATTENTION_LIMIT = 6


def trend_labels(year: int, month: int, count: int = 6) -> list[str]:
    """Return localized month names for the ``count`` months ending at ``year-month``."""
    labels = []
    for offset in range(count - 1, -1, -1):
        index = year * 12 + (month - 1) - offset
        labels.append(ar.MONTH_NAMES[index % 12 + 1])
    return labels


class _AttentionRow(QFrame):
    """One overdue customer in the "needs attention" card."""

    def __init__(self, alert: CollectionAlert, on_open, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("attentionRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._open = lambda: on_open(alert.customer_id)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 9, 4, 9)
        row.setSpacing(10)
        icon = QLabel(self)
        icon.setPixmap(icons.pixmap("alert", "failed", 16))
        row.addWidget(icon)
        text = QVBoxLayout()
        text.setSpacing(0)
        name = QLabel(alert.customer_name, self)
        name.setObjectName("attentionName")
        meta = QLabel(alert_line(alert), self)
        meta.setObjectName("attentionMeta")
        text.addWidget(name)
        text.addWidget(meta)
        row.addLayout(text, 1)
        amount = QLabel(money(alert.amount), self)
        amount.setProperty("pill", "failed")
        row.addWidget(amount)
        chevron = QLabel(self)
        chevron.setPixmap(icons.pixmap("chevron-forward", "text-muted", 14))
        row.addWidget(chevron)
        for child in (icon, name, meta, amount, chevron):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAccessibleName(f"{alert.customer_name} {money(alert.amount)}")

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt callback name
        if event.button() == Qt.MouseButton.LeftButton:
            self._open()
        super().mousePressEvent(event)


class DashboardPage(QWidget):
    """High-level sales and collection figures for a selected month."""

    status_filter_requested = Signal(str)
    payment_filter_requested = Signal(str)
    open_customer_requested = Signal(int)
    view_overdue_requested = Signal()
    debts_requested = Signal(str)  # "receivable" or "payable"
    stock_requested = Signal()

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self._role_is_owner = current_user.role == "owner"
        self._build_ui()
        events.data_changed.connect(self.refresh)
        self.refresh()

    # ------------------------------------------------------------------ build
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 0)
        root.setSpacing(16)
        root.addLayout(self._build_header())

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget(scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 24)
        layout.setSpacing(20)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self.position_box = self._build_position(body)
        self.position_box.setVisible(self._role_is_owner)
        layout.addWidget(self.position_box)

        layout.addWidget(self._section_title(ar.DASH_SECTION_OPERATIONS))
        self.status_grid = ResponsiveGrid(parent=body)
        self.completed_card = StatCard(ar.DASHBOARD_COMPLETED, color="paid", icon="check-circle",
                                       subtitle=ar.DASH_CAPTION_COMPLETED)
        self.pending_card = StatCard(ar.DASHBOARD_PENDING, color="pending", icon="clock",
                                     subtitle=ar.DASH_CAPTION_PENDING)
        self.failed_card = StatCard(ar.DASHBOARD_FAILED, color="failed", icon="alert",
                                    subtitle=ar.DASH_CAPTION_FAILED)
        self.wholesale_card = StatCard(ar.DASHBOARD_WHOLESALE, color="highlight", icon="tag",
                                       subtitle=ar.DASH_CAPTION_WHOLESALE)
        for card in (self.completed_card, self.pending_card, self.failed_card, self.wholesale_card):
            self.status_grid.add(card)
        self.wholesale_card.setVisible(self._role_is_owner)
        self.status_grid.refresh()
        self.completed_card.clicked.connect(lambda: self.status_filter_requested.emit("PAID"))
        self.pending_card.clicked.connect(lambda: self.status_filter_requested.emit("PENDING"))
        self.failed_card.clicked.connect(lambda: self.status_filter_requested.emit("FAILED"))
        self.wholesale_card.clicked.connect(lambda: self.status_filter_requested.emit("ALL"))
        layout.addWidget(self.status_grid)

        layout.addWidget(self._section_title(ar.DASH_SECTION_MIX))
        self.mix_grid = ResponsiveGrid(max_columns=3, parent=body)
        self.cash_customers_card = StatCard(ar.DASH_MIX_CASH_CUSTOMERS, color="paid", icon="cash",
                                            subtitle=ar.DASH_MIX_CASH_CAPTION)
        self.facility_customers_card = StatCard(ar.DASH_MIX_FACILITY_CUSTOMERS, color="primary-glow",
                                                icon="calendar", subtitle=ar.DASH_MIX_FACILITY_CAPTION)
        self.month_mix_card = StatCard(ar.DASH_MIX_MONTH_SALES, color="highlight", icon="cart",
                                       clickable=False)
        for card in (self.cash_customers_card, self.facility_customers_card, self.month_mix_card):
            self.mix_grid.add(card)
        self.cash_customers_card.clicked.connect(lambda: self.payment_filter_requested.emit("cash"))
        self.facility_customers_card.clicked.connect(
            lambda: self.payment_filter_requested.emit("facilities")
        )
        layout.addWidget(self.mix_grid)

        charts = QHBoxLayout()
        charts.setSpacing(16)
        self.trend_chart = FinancialTrendChart(body)
        self.gauge_widget = CollectionGaugeWidget(body)
        charts.addWidget(self.trend_chart, 3)
        charts.addWidget(self.gauge_widget, 1)
        layout.addLayout(charts)

        self.owner_metrics = QWidget(body)
        owner_layout = QVBoxLayout(self.owner_metrics)
        owner_layout.setContentsMargins(0, 0, 0, 0)
        owner_layout.setSpacing(12)
        owner_layout.addWidget(self._section_title(ar.DASH_SECTION_FINANCE))
        self.owner_grid = ResponsiveGrid(parent=self.owner_metrics)
        self.expected_card = StatCard(ar.DASHBOARD_EXPECTED, color="primary-glow", icon="calendar",
                                      subtitle=ar.DASH_CAPTION_EXPECTED, clickable=False)
        self.collected_card = StatCard(ar.DASHBOARD_COLLECTED, color="paid", icon="wallet",
                                       subtitle=ar.DASH_CAPTION_COLLECTED, clickable=False)
        self.profit_card = StatCard(ar.DASHBOARD_PROFIT, color="highlight", icon="trend-up",
                                    subtitle=ar.DASH_CAPTION_PROFIT, clickable=False)
        self.remaining_card = StatCard(ar.DASHBOARD_REMAINING, color="pending", icon="cash",
                                       subtitle=ar.DASH_CAPTION_REMAINING, clickable=False)
        for card in (self.expected_card, self.collected_card, self.profit_card, self.remaining_card):
            self.owner_grid.add(card)
        owner_layout.addWidget(self.owner_grid)
        self.owner_metrics.setVisible(self._role_is_owner)
        layout.addWidget(self.owner_metrics)

        bottom = QHBoxLayout()
        bottom.setSpacing(16)
        bottom.addWidget(self._build_attention_card(body), 3)
        self.daily_register = DailyRegisterCard(is_owner=self._role_is_owner, parent=body)
        side = QVBoxLayout()
        side.addWidget(self.daily_register)
        side.addStretch(1)
        bottom.addLayout(side, 2)
        layout.addLayout(bottom)
        layout.addStretch(1)

    def _build_position(self, parent: QWidget) -> QWidget:
        """Net financial position: what is inside, what is outside, the net, and this month's cash flow."""
        box = QWidget(parent)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._section_title(ar.DASH_SECTION_POSITION))

        hero = QFrame(box)
        hero.setObjectName("card")
        hero_row = QHBoxLayout(hero)
        hero_row.setContentsMargins(22, 16, 22, 16)
        hero_row.setSpacing(24)
        net_box = QVBoxLayout()
        net_box.setSpacing(2)
        net_caption = QLabel(ar.DASH_NET_TITLE, hero)
        net_caption.setObjectName("kpiTitle")
        self.net_value = QLabel("—", hero)
        self.net_value.setObjectName("netValue")
        net_hint = QLabel(ar.DASH_NET_FORMULA, hero)
        net_hint.setObjectName("kpiCaption")
        net_hint.setWordWrap(True)
        net_box.addWidget(net_caption)
        net_box.addWidget(self.net_value)
        net_box.addWidget(net_hint)
        hero_row.addLayout(net_box, 2)
        self.inside_value = self._side_total(hero, hero_row, ar.DASH_INSIDE, "paid")
        self.outside_value = self._side_total(hero, hero_row, ar.DASH_OUTSIDE, "failed")
        layout.addWidget(hero)

        self.position_grid = ResponsiveGrid(parent=box)
        self.clients_owe_card = StatCard(ar.DASH_CLIENTS_OWE, color="primary-glow", icon="users",
                                         subtitle=ar.DASH_CLIENTS_OWE_CAPTION)
        self.debtors_owe_card = StatCard(ar.DASH_DEBTORS_OWE, color="paid", icon="debt-in")
        self.stock_value_card = StatCard(ar.DASH_STOCK_VALUE, color="highlight", icon="tag")
        self.shop_owes_card = StatCard(ar.DASH_SHOP_OWES, color="failed", icon="debt-out")
        self.clients_owe_card.clicked.connect(lambda: self.payment_filter_requested.emit("facilities"))
        self.debtors_owe_card.clicked.connect(lambda: self.debts_requested.emit("receivable"))
        self.stock_value_card.clicked.connect(self.stock_requested.emit)
        self.shop_owes_card.clicked.connect(lambda: self.debts_requested.emit("payable"))
        for card in (self.clients_owe_card, self.debtors_owe_card, self.stock_value_card, self.shop_owes_card):
            self.position_grid.add(card)
        layout.addWidget(self.position_grid)

        self.flow_grid = ResponsiveGrid(max_columns=3, parent=box)
        self.month_in_card = StatCard(ar.DASH_MONTH_IN, color="paid", icon="trend-up", clickable=False)
        self.month_out_card = StatCard(ar.DASH_MONTH_OUT, color="pending", icon="debt-out",
                                       subtitle=ar.DASH_MONTH_OUT_CAPTION, clickable=False)
        self.month_net_card = StatCard(ar.DASH_MONTH_NET, color="highlight", icon="wallet",
                                       subtitle=ar.DASH_MONTH_NET_CAPTION, clickable=False)
        for card in (self.month_in_card, self.month_out_card, self.month_net_card):
            self.flow_grid.add(card)
        layout.addWidget(self.flow_grid)
        return box

    @staticmethod
    def _side_total(parent: QWidget, row: QHBoxLayout, caption: str, tone: str) -> QLabel:
        column = QVBoxLayout()
        column.setSpacing(2)
        label = QLabel(caption, parent)
        label.setObjectName("kpiCaption")
        value = QLabel("—", parent)
        value.setObjectName("kpiValue")
        value.setStyleSheet(f"color: {qcolor(tone).name()};")
        column.addWidget(label)
        column.addWidget(value)
        column.addStretch(1)
        row.addLayout(column, 1)
        return value

    def _apply_position(self, position: FinancialPosition) -> None:
        """Fill the net position section."""
        net = position.net
        self.net_value.setText(f"{net:,} {ar.CURRENCY_SUFFIX}")
        self.net_value.setStyleSheet(f"color: {qcolor('paid' if net >= 0 else 'failed').name()};")
        self.inside_value.setText(f"{position.money_inside + position.stock_value:,} {ar.CURRENCY_SUFFIX}")
        self.outside_value.setText(f"{position.shop_owes:,} {ar.CURRENCY_SUFFIX}")
        self.clients_owe_card.set_value(position.clients_owe, currency=True)
        self.debtors_owe_card.set_value(position.debtors_owe, currency=True)
        self.debtors_owe_card.set_subtitle(ar.DASH_OVERDUE_CAPTION.format(amount=_money(position.debtors_overdue)))
        self.stock_value_card.set_value(position.stock_value, currency=True)
        self.stock_value_card.set_subtitle(ar.DASH_STOCK_CAPTION.format(
            units=position.stock_units, retail=_money(position.stock_retail_value),
        ))
        self.shop_owes_card.set_value(position.shop_owes, currency=True)
        self.shop_owes_card.set_subtitle(ar.DASH_OVERDUE_CAPTION.format(amount=_money(position.shop_overdue)))
        self.month_in_card.set_value(position.month_received, currency=True)
        self.month_in_card.set_subtitle(ar.DASH_MONTH_IN_CAPTION.format(
            sales=_money(position.month_sales_received), debts=_money(position.month_debts_received),
        ))
        self.month_out_card.set_value(position.month_paid_out, currency=True)
        self.month_net_card.set_value(position.month_net, currency=True)

    def _build_header(self) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.greeting = QLabel(self._greeting_text(), self)
        self.greeting.setObjectName("pageTitle")
        subtitle = QLabel(ar.DASH_SUBTITLE, self)
        subtitle.setObjectName("pageSubtitle")
        titles.addWidget(self.greeting)
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)

        month_icon = QLabel(self)
        month_icon.setPixmap(icons.pixmap("calendar", "text-muted", 16))
        month_label = QLabel(ar.DASHBOARD_MONTH, self)
        month_label.setObjectName("sectionHint")
        self.month_selector = QDateEdit(self)
        self.month_selector.setCalendarPopup(True)
        self.month_selector.setDisplayFormat("MM/yyyy")
        self.month_selector.setDate(QDate.currentDate())
        self.month_selector.setMinimumWidth(130)
        self.month_selector.dateChanged.connect(self.refresh)
        header.addWidget(month_icon)
        header.addWidget(month_label)
        header.addWidget(self.month_selector)
        return header

    def _build_attention_card(self, parent: QWidget) -> QFrame:
        card = QFrame(parent)
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 14)
        layout.setSpacing(8)
        header = QHBoxLayout()
        title = QLabel(ar.DASH_SECTION_ATTENTION, card)
        title.setObjectName("sectionTitle")
        header.addWidget(title, 1)
        self.attention_count = QLabel(card)
        self.attention_count.setProperty("pill", "failed")
        header.addWidget(self.attention_count)
        view_all = QPushButton(ar.DASH_VIEW_ALL, card)
        view_all.setProperty("variant", "ghost")
        view_all.setProperty("compact", True)
        view_all.setCursor(Qt.CursorShape.PointingHandCursor)
        view_all.clicked.connect(self.view_overdue_requested.emit)
        header.addWidget(view_all)
        layout.addLayout(header)
        self.attention_list = QVBoxLayout()
        self.attention_list.setSpacing(0)
        layout.addLayout(self.attention_list)
        layout.addStretch(1)
        return card

    def _section_title(self, text: str) -> QLabel:
        label = QLabel(text, self)
        label.setObjectName("sectionTitle")
        return label

    def _greeting_text(self) -> str:
        key = "DASH_GREETING_MORNING" if datetime.now().hour < 12 else "DASH_GREETING_EVENING"
        return getattr(ar, key).format(name=self.current_user.username)

    # ------------------------------------------------------------------ data
    def refresh(self, _selected_date: QDate | None = None) -> None:
        """Reload figures after a month or data change."""
        selected = self.month_selector.date()
        today = date.today()
        try:
            with session_scope() as session:
                summary = get_dashboard_summary(
                    session,
                    year=selected.year(),
                    month=selected.month(),
                    today=today,
                    role=self.current_user.role,
                )
                alerts = [a for a in collection_alerts(session, today=today) if a.kind != "due_soon"]
                position = (
                    financial_position(
                        session, self.current_user.id, year=selected.year(), month=selected.month(), today=today
                    )
                    if self._role_is_owner else None
                )
        except Exception:  # noqa: BLE001 - show the last figures rather than crash
            _LOG.exception("Dashboard refresh failed")
            return
        self.greeting.setText(self._greeting_text())
        self._apply_summary(summary)
        self._apply_attention(alerts)
        if position is not None:
            self._apply_position(position)

    def _apply_summary(self, summary: DashboardSummary) -> None:
        """Set card values and charts from the service result."""
        self.wholesale_card.set_value(summary.total_wholesale or 0, currency=True)
        self.failed_card.set_value(summary.failed_operations)
        self.completed_card.set_value(summary.completed_operations)
        self.pending_card.set_value(summary.pending_operations)
        self.expected_card.set_value(summary.expected_collections, currency=True)
        self.collected_card.set_value(summary.collected_this_month, currency=True)
        self.profit_card.set_value(summary.total_profit or 0, currency=True)
        self.remaining_card.set_value(summary.remaining_balance, currency=True)
        self.cash_customers_card.set_value(summary.cash_customers)
        self.facility_customers_card.set_value(summary.facility_customers)
        self.month_mix_card.set_value(
            summary.month_cash_sales + summary.month_installment_sales + summary.month_credit_sales
        )
        self.month_mix_card.set_subtitle(ar.DASH_MIX_MONTH_CAPTION.format(
            cash=summary.month_cash_sales,
            installment=summary.month_installment_sales,
            credit=summary.month_credit_sales,
        ))
        self.daily_register.update_metrics(
            collected=summary.today_collected,
            sales_count=summary.today_sales_count,
            profit=summary.today_profit,
        )
        selected = self.month_selector.date()
        labels = trend_labels(selected.year(), selected.month(), len(summary.monthly_trends))
        self.trend_chart.set_data(
            [(label, expected, collected)
             for label, (_name, expected, collected) in zip(labels, summary.monthly_trends)]
        )
        self.gauge_widget.set_values(
            rate=summary.collection_rate,
            collected=summary.collected_this_month,
            expected=summary.expected_collections,
        )

    def _apply_attention(self, alerts: list[CollectionAlert]) -> None:
        while self.attention_list.count():
            item = self.attention_list.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self.attention_count.setText(str(len(alerts)))
        self.attention_count.setVisible(bool(alerts))
        if not alerts:
            empty = QLabel(ar.DASH_ATTENTION_EMPTY, self)
            empty.setObjectName("emptyStateSub")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setMinimumHeight(80)
            self.attention_list.addWidget(empty)
            return
        for alert in alerts[:ATTENTION_LIMIT]:
            self.attention_list.addWidget(_AttentionRow(alert, self.open_customer_requested.emit, self))


def _money(amount: int) -> str:
    return f"{amount:,} {ar.CURRENCY_SUFFIX}"
