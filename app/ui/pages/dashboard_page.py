"""Dashboard page with month selection and summary cards."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QDateEdit,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from app.db.models import User
from app.db.session import session_scope
from app.i18n import ar
from app.services.dashboard import DashboardSummary, get_dashboard_summary
from app.ui.events import events
from app.ui.widgets.collection_gauge import CollectionGaugeWidget
from app.ui.widgets.daily_register_card import DailyRegisterCard
from app.ui.widgets.financial_chart import FinancialTrendChart
from app.ui.widgets.stat_card import StatCard


class DashboardPage(QWidget):
    """Show high-level sales and collection figures for a selected month."""

    status_filter_requested = Signal(str)

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._build_ui()
        events.data_changed.connect(self.refresh)
        self.refresh()

    def _build_ui(self) -> None:
        """Create header toolbar and two structured rows of summary cards."""
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(18)

        # Header toolbar with title and month selector
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        heading = QLabel(ar.SIDEBAR_ITEMS[0], self)
        heading.setObjectName("pageTitle")
        header_row.addWidget(heading)
        header_row.addStretch(1)

        month_container = QWidget(self)
        month_layout = QHBoxLayout(month_container)
        month_layout.setContentsMargins(0, 0, 0, 0)
        month_layout.setSpacing(8)

        month_label = QLabel(f"📅  {ar.DASHBOARD_MONTH}:", month_container)
        month_label.setStyleSheet("color: #9DBEFF; font-weight: 600; font-size: 13px;")
        month_layout.addWidget(month_label)

        self.month_selector = QDateEdit(month_container)
        self.month_selector.setCalendarPopup(True)
        self.month_selector.setDisplayFormat("MM/yyyy")
        self.month_selector.setDate(QDate.currentDate())
        self.month_selector.setMinimumWidth(130)
        self.month_selector.dateChanged.connect(self.refresh)
        month_layout.addWidget(self.month_selector)

        header_row.addWidget(month_container)
        root.addLayout(header_row)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        body = QWidget(scroll)
        body.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(22)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self._role_is_owner = self.current_user.role == "owner"

        # 0. Daily Register Snapshot (Live Cash Box)
        self.daily_register = DailyRegisterCard(is_owner=self._role_is_owner, parent=body)
        body_layout.addWidget(self.daily_register)

        # Section 1: Operations & Collection Status
        sec1_header = QHBoxLayout()
        sec1_header.setContentsMargins(0, 0, 0, 0)
        sec1_title = QLabel("📌  حالة العمليات والاقتطاعات", body)
        sec1_title.setObjectName("sectionTitle")
        sec1_header.addWidget(sec1_title)
        sec1_header.addStretch(1)

        sec1_sub = QLabel("تحديث تلقائي مستمر ومباشر", body)
        sec1_sub.setStyleSheet("color: #64748B; font-size: 11px;")
        sec1_header.addWidget(sec1_sub)
        body_layout.addLayout(sec1_header)

        self.top_cards = QGridLayout()
        self.top_cards.setSpacing(14)
        self.wholesale_card = StatCard(ar.DASHBOARD_WHOLESALE, color="#06B6D4", subtitle="إجمالي السلع بالخارج بسعر الجملة للأعضاء", parent=body)
        self.completed_card = StatCard(ar.DASHBOARD_COMPLETED, color="#10B981", subtitle="تم الدفع بالكامل (نقطة خضراء)", parent=body)
        self.pending_card = StatCard(ar.DASHBOARD_PENDING, color="#F59E0B", subtitle="قيد الانتظار (نقطة برتقالية)", parent=body)
        self.failed_card = StatCard(ar.DASHBOARD_FAILED, color="#EF4444", subtitle="فات تاريخ الدفع ولم يدفع (نقطة حمراء)", parent=body)

        for column, card in enumerate((self.wholesale_card, self.completed_card,
                                       self.pending_card, self.failed_card)):
            self.top_cards.addWidget(card, 0, column)
        self.wholesale_card.clicked.connect(lambda: self.status_filter_requested.emit(None))
        self.failed_card.clicked.connect(lambda: self.status_filter_requested.emit("FAILED"))
        self.completed_card.clicked.connect(lambda: self.status_filter_requested.emit("PAID"))
        self.pending_card.clicked.connect(lambda: self.status_filter_requested.emit("PENDING"))
        body_layout.addLayout(self.top_cards)

        # Section 2: Visual Charts & Intelligence (Trend Chart & Radial Gauge)
        charts_row = QHBoxLayout()
        charts_row.setContentsMargins(0, 0, 0, 0)
        charts_row.setSpacing(16)

        self.trend_chart = FinancialTrendChart(parent=body)
        self.gauge_widget = CollectionGaugeWidget(parent=body)

        charts_row.addWidget(self.trend_chart, 3)
        charts_row.addWidget(self.gauge_widget, 1)
        body_layout.addLayout(charts_row)

        # Section 3: Owner Financial Metrics
        self.owner_metrics = QWidget(body)
        owner_container_layout = QVBoxLayout(self.owner_metrics)
        owner_container_layout.setContentsMargins(0, 0, 0, 0)
        owner_container_layout.setSpacing(14)

        sec2_title = QLabel("💰  المؤشرات المالية والتحصيل", self.owner_metrics)
        sec2_title.setObjectName("sectionTitle")
        owner_container_layout.addWidget(sec2_title)

        self.owner_cards = QGridLayout()
        self.owner_cards.setSpacing(14)
        self.expected_card = StatCard(ar.DASHBOARD_EXPECTED, color="#3B92D9", parent=self.owner_metrics)
        self.collected_card = StatCard(ar.DASHBOARD_COLLECTED, color="#22C55E", parent=self.owner_metrics)
        self.profit_card = StatCard(ar.DASHBOARD_PROFIT, color="#9DBEFF", parent=self.owner_metrics)
        self.remaining_card = StatCard(ar.DASHBOARD_REMAINING, color="#F59E0B", parent=self.owner_metrics)

        for column, card in enumerate((self.expected_card, self.collected_card,
                                       self.profit_card, self.remaining_card)):
            self.owner_cards.addWidget(card, 0, column)
        owner_container_layout.addLayout(self.owner_cards)

        body_layout.addWidget(self.owner_metrics)
        body_layout.addStretch(1)

        self.wholesale_card.setVisible(self._role_is_owner)
        self.owner_metrics.setVisible(self._role_is_owner)

    def refresh(self, _selected_date: QDate | None = None) -> None:
        """Reload summary values after a month or data change."""
        selected = self.month_selector.date()
        with session_scope() as session:
            summary = get_dashboard_summary(
                session,
                year=selected.year(),
                month=selected.month(),
                today=date.today(),
                role=self.current_user.role,
            )
        self._apply_summary(summary)

    def _apply_summary(self, summary: DashboardSummary) -> None:
        """Set card values and visual charts from the service result."""
        self.wholesale_card.set_value(summary.total_wholesale or 0, currency=True)
        self.failed_card.set_value(summary.failed_operations)
        self.completed_card.set_value(summary.completed_operations)
        self.pending_card.set_value(summary.pending_operations)
        self.expected_card.set_value(summary.expected_collections, currency=True)
        self.collected_card.set_value(summary.collected_this_month, currency=True)
        self.profit_card.set_value(summary.total_profit or 0, currency=True)
        self.remaining_card.set_value(summary.remaining_balance, currency=True)

        # Update visual analytics & live register
        self.daily_register.update_metrics(
            collected=summary.today_collected,
            sales_count=summary.today_sales_count,
            profit=summary.today_profit,
        )
        self.trend_chart.set_data(summary.monthly_trends)
        self.gauge_widget.set_values(
            rate=summary.collection_rate,
            collected=summary.collected_this_month,
            expected=summary.expected_collections,
        )
