"""Reports shown in the app (no Excel files): my net money, profits, debts and collections.

The owner sees, for the selected month:

1. "my net money": what clients owe on installments and credit, plus what
   people owe in the debt book, minus what the shop owes;
2. profits: the month, the year, and a month-by-month table of the year;
3. the debt book: owed to me / I owe, with the overdue part.

Everyone sees this month's collection and the overdue installments.
"""

from __future__ import annotations

import logging
from datetime import date

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import User
from app.db.session import session_scope
from app.i18n import ar
from app.i18n.stock_text import ltr_text
from app.services import auth, debts, reports
from app.services.finance import FinancialPosition, financial_position
from app.ui import icons
from app.ui.events import events
from app.ui.screen_fit import fit_table_to_rows
from app.ui.theme import qcolor
from app.ui.widgets.responsive_grid import ResponsiveGrid
from app.ui.widgets.stat_card import StatCard

_LOG = logging.getLogger(__name__)
OVERDUE_LIMIT = 50


class ReportsPage(QWidget):
    """Owner figures (net money, profits, debts) and the month's collection, on screen."""

    debts_requested = Signal(str)  # "receivable" or "payable"

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self._is_owner = current_user.role == "owner"
        self._build_ui()
        events.data_changed.connect(self.refresh)
        self.refresh()

    # ------------------------------------------------------------------ build
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 0)
        root.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        heading = QLabel(ar.REPORT_PAGE_TITLE, self)
        heading.setObjectName("pageTitle")
        subtitle = QLabel(ar.REPORT_PAGE_SUBTITLE, self)
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        titles.addWidget(heading)
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        month_icon = QLabel(self)
        month_icon.setPixmap(icons.pixmap("calendar", "text-muted", 16))
        month_label = QLabel(ar.REPORT_MONTH, self)
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
        root.addLayout(header)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget(scroll)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 24)
        layout.setSpacing(18)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self.owner_box = QWidget(body)
        owner_layout = QVBoxLayout(self.owner_box)
        owner_layout.setContentsMargins(0, 0, 0, 0)
        owner_layout.setSpacing(16)
        owner_layout.addWidget(self._build_net_money(self.owner_box))
        owner_layout.addWidget(self._title(ar.REPORT_PROFIT_SECTION))
        self.profit_grid = ResponsiveGrid(max_columns=3, parent=self.owner_box)
        self.month_profit_card = StatCard(ar.REPORT_MONTH_PROFIT, color="paid", icon="trend-up", clickable=False)
        self.year_profit_card = StatCard(ar.REPORT_YEAR_PROFIT, color="highlight", icon="wallet", clickable=False)
        self.month_sales_card = StatCard(ar.REPORT_MONTH_SALES, color="primary-glow", icon="cart", clickable=False)
        for card in (self.month_profit_card, self.year_profit_card, self.month_sales_card):
            self.profit_grid.add(card)
        owner_layout.addWidget(self.profit_grid)
        self.profit_table = self._table(self.owner_box, (
            ar.REPORT_MONTH, ar.REPORT_SALE_COUNT, ar.REPORT_TOTAL, ar.REPORT_WHOLESALE, ar.REPORT_PROFIT,
        ))
        owner_layout.addWidget(self.profit_table)

        owner_layout.addWidget(self._title(ar.REPORT_DEBTS_SECTION))
        self.debt_grid = ResponsiveGrid(max_columns=2, parent=self.owner_box)
        self.receivable_card = StatCard(ar.DASH_DEBTORS_OWE, color="paid", icon="debt-in")
        self.payable_card = StatCard(ar.DASH_SHOP_OWES, color="failed", icon="debt-out")
        self.receivable_card.clicked.connect(lambda: self.debts_requested.emit(debts.RECEIVABLE))
        self.payable_card.clicked.connect(lambda: self.debts_requested.emit(debts.PAYABLE))
        for card in (self.receivable_card, self.payable_card):
            self.debt_grid.add(card)
        owner_layout.addWidget(self.debt_grid)
        self.owner_box.setVisible(self._is_owner)
        layout.addWidget(self.owner_box)

        layout.addWidget(self._title(ar.REPORT_COLLECTION_TITLE))
        self.collection_grid = ResponsiveGrid(max_columns=3, parent=body)
        self.due_card = StatCard(ar.DASHBOARD_EXPECTED, color="primary-glow", icon="calendar", clickable=False)
        self.paid_card = StatCard(ar.DASHBOARD_COLLECTED, color="paid", icon="check-circle", clickable=False)
        self.open_card = StatCard(ar.REPORT_REMAINING, color="pending", icon="clock", clickable=False)
        for card in (self.due_card, self.paid_card, self.open_card):
            self.collection_grid.add(card)
        layout.addWidget(self.collection_grid)

        overdue_header = QHBoxLayout()
        overdue_header.addWidget(self._title(ar.REPORT_OVERDUE_TITLE), 1)
        self.overdue_total = QLabel(body)
        self.overdue_total.setProperty("pill", "failed")
        overdue_header.addWidget(self.overdue_total)
        layout.addLayout(overdue_header)
        self.overdue_table = self._table(body, (
            ar.REPORT_CUSTOMER, ar.REPORT_PHONE, ar.REPORT_PRODUCT, ar.REPORT_DUE_DATE,
            ar.REPORT_REMAINING, ar.REPORT_DAYS_LATE,
        ))
        layout.addWidget(self.overdue_table)
        self.overdue_empty = QLabel(ar.DASH_ATTENTION_EMPTY, body)
        self.overdue_empty.setObjectName("emptyStateSub")
        self.overdue_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overdue_empty.setMinimumHeight(60)
        layout.addWidget(self.overdue_empty)
        layout.addStretch(1)

    def _build_net_money(self, parent: QWidget) -> QFrame:
        """The headline: how much money is mine outside the drawer, and what it is made of."""
        card = QFrame(parent)
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(6)
        caption = QLabel(ar.NET_MONEY_TITLE, card)
        caption.setObjectName("sectionTitle")
        self.net_money_value = QLabel("—", card)
        self.net_money_value.setObjectName("netValue")
        formula = QLabel(ar.NET_MONEY_FORMULA, card)
        formula.setObjectName("kpiCaption")
        formula.setWordWrap(True)
        layout.addWidget(caption)
        layout.addWidget(self.net_money_value)
        layout.addWidget(formula)
        parts = QHBoxLayout()
        parts.setSpacing(18)
        self.installments_value = _part(card, parts, ar.NET_MONEY_INSTALLMENTS, "primary-glow")
        self.credit_value = _part(card, parts, ar.NET_MONEY_CREDIT, "pending")
        self.receivable_value = _part(card, parts, ar.NET_MONEY_RECEIVABLE, "paid")
        self.payable_value = _part(card, parts, ar.NET_MONEY_PAYABLE, "failed")
        layout.addLayout(parts)
        self.with_stock_label = QLabel(card)
        self.with_stock_label.setObjectName("kpiCaption")
        self.with_stock_label.setWordWrap(True)
        layout.addWidget(self.with_stock_label)
        return card

    def _title(self, text: str) -> QLabel:
        label = QLabel(text, self)
        label.setObjectName("sectionTitle")
        return label

    @staticmethod
    def _table(parent: QWidget, headers: tuple[str, ...]) -> QTableWidget:
        table = QTableWidget(0, len(headers), parent)
        table.setHorizontalHeaderLabels(list(headers))
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(36)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        return table

    # ------------------------------------------------------------------ data
    def refresh(self, *_args: object) -> None:
        """Reload every figure for the selected month."""
        selected = self.month_selector.date()
        year, month, today = selected.year(), selected.month(), date.today()
        try:
            with session_scope() as session:
                collection = reports.collection_totals(
                    reports.monthly_collection_rows(session, year=year, month=month, today=today)
                )
                overdue = reports.overdue_rows(session, today=today)
                position = profits = None
                if self._is_owner:
                    position = financial_position(session, self.current_user.id, year=year, month=month, today=today)
                    profits = reports.profit_by_month(
                        session, start_year=year, end_year=year, owner_user_id=int(self.current_user.id)
                    )
        except (auth.AuthorizationError, ValueError, SQLAlchemyError):
            _LOG.exception("Reports refresh failed")
            events.notify.emit("error", ar.REPORT_PAGE_TITLE, ar.REPORT_LOAD_ERROR, 5000)
            return
        self._apply_collection(collection, overdue)
        if position is not None and profits is not None:
            self._apply_net_money(position)
            self._apply_profits(profits, year, month)

    def _apply_net_money(self, position: FinancialPosition) -> None:
        net = position.net_money
        self.net_money_value.setText(_money(net))
        self.net_money_value.setStyleSheet(f"color: {qcolor('paid' if net >= 0 else 'failed').name()};")
        self.installments_value.setText(_money(position.installments_owe))
        self.credit_value.setText(_money(position.credit_owe))
        self.receivable_value.setText(_money(position.debtors_owe))
        self.payable_value.setText(f"− {_money(position.shop_owes)}")
        self.with_stock_label.setText(ar.NET_MONEY_WITH_STOCK.format(
            stock=_money(position.stock_value), units=position.stock_units, total=_money(position.net),
        ))
        self.receivable_card.set_value(position.debtors_owe, currency=True)
        self.receivable_card.set_subtitle(ar.DASH_OVERDUE_CAPTION.format(amount=_money(position.debtors_overdue)))
        self.payable_card.set_value(position.shop_owes, currency=True)
        self.payable_card.set_subtitle(ar.DASH_OVERDUE_CAPTION.format(amount=_money(position.shop_overdue)))

    def _apply_profits(self, rows: list[reports.ProfitRow], year: int, month: int) -> None:
        this_month = reports.profit_for(rows, year=year, month=month)
        this_year = reports.profit_for(rows, year=year)
        self.month_profit_card.set_value(this_month.profit, currency=True)
        self.month_profit_card.set_subtitle(ar.REPORT_PROFIT_CAPTION.format(total=_money(this_month.total)))
        self.year_profit_card.set_value(this_year.profit, currency=True)
        self.year_profit_card.set_subtitle(ar.REPORT_YEAR_CAPTION.format(year=year, count=this_year.sale_count))
        self.month_sales_card.set_value(this_month.sale_count)
        months = [reports.profit_for(rows, year=year, month=number) for number in range(1, 13)]
        shown = [row for row in months if row.sale_count] or [this_month]
        self.profit_table.setRowCount(len(shown) + 1)
        for index, row in enumerate(shown):
            self._fill(self.profit_table, index, (
                f"{ar.MONTH_NAMES[row.month]} {year}", str(row.sale_count), _money(row.total),
                _money(row.wholesale), _money(row.profit),
            ), highlight=row.month == month)
        self._fill(self.profit_table, len(shown), (
            ar.REPORT_YEAR_TOTAL.format(year=year), str(this_year.sale_count), _money(this_year.total),
            _money(this_year.wholesale), _money(this_year.profit),
        ), bold=True)
        fit_table_to_rows(self.profit_table)

    def _apply_collection(self, totals: reports.CollectionTotals, overdue: list[reports.OverdueRow]) -> None:
        self.due_card.set_value(totals.due, currency=True)
        self.paid_card.set_value(totals.paid, currency=True)
        self.open_card.set_value(totals.remaining, currency=True)
        self.overdue_total.setText(_money(sum(row.remaining for row in overdue)))
        self.overdue_total.setVisible(bool(overdue))
        rows = sorted(overdue, key=lambda row: -row.days_late)[:OVERDUE_LIMIT]
        self.overdue_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            self._fill(self.overdue_table, index, (
                row.customer, row.phone or "—", ltr_text(row.product), row.due_date.strftime("%d/%m/%Y"),
                _money(row.remaining), str(row.days_late),
            ))
        self.overdue_table.setVisible(bool(rows))
        self.overdue_empty.setVisible(not rows)
        if rows:
            fit_table_to_rows(self.overdue_table)

    @staticmethod
    def _fill(table: QTableWidget, row: int, values: tuple[str, ...], *, bold: bool = False,
              highlight: bool = False) -> None:
        for column, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if bold or highlight:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            if highlight:
                item.setForeground(qcolor("highlight"))
            table.setItem(row, column, item)


def _part(parent: QWidget, row: QHBoxLayout, caption: str, tone: str) -> QLabel:
    """One piece of the net-money sum: caption over amount."""
    column = QVBoxLayout()
    column.setSpacing(2)
    label = QLabel(caption, parent)
    label.setObjectName("kpiCaption")
    value = QLabel("—", parent)
    value.setObjectName("kpiValue")
    value.setStyleSheet(f"color: {qcolor(tone).name()};")
    column.addWidget(label)
    column.addWidget(value)
    row.addLayout(column, 1)
    return value


def _money(amount: int) -> str:
    return f"{amount:,} {ar.CURRENCY_SUFFIX}"
