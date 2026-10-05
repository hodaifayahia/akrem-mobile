"""Customer directory: client-type chips, status and search filters, owner actions.

Every sale is its own row, except products bought together in one basket,
which share a row (a client who bought three phones on three visits shows
three rows); a client without sales shows one row. Ticking a row selects the
client, so delete and "assign type" act on clients.
"""

from __future__ import annotations

import logging
from datetime import date

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QDate, QRectF, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDateEdit,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QSizePolicy,
    QWidget,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import User
from app.db.session import session_scope
from app.i18n import ar
from app.i18n.stock_text import ltr_text
from app.services import auth, backup, categories, customers
from app.services.categories import ClientType, ClientTypeError
from app.services.customers import CustomerSummary, DuplicateCustomerPhoneError
from app.services.status import Status
from app.ui import icons
from app.ui.theme import qcolor
from app.ui.dialogs.client_types_dialog import AssignTypeDialog, error_text
from app.ui.dialogs.customer_form_dialog import CustomerFormDialog
from app.ui.events import events
from app.ui.widgets import row_selection
from app.ui.widgets.client_types import TypeFilterBar, TypeTagDelegate
from app.ui.widgets.row_selection import SelectionBar

_STATUS_COLORS = {
    Status.PAID: "#22C55E",
    Status.FAILED: "#EF4444",
    Status.PENDING: "#F59E0B",
    Status.NONE: "#8A94A6",
}
_LOG = logging.getLogger(__name__)
TYPE_COLUMN = 6
PAYMENT_COLUMN = 7
MONTHLY_COLUMN = 8
REMAINING_COLUMN = 9


def _status_tooltips() -> dict[Status, str]:
    """Status tooltips in the active language (evaluated at call time)."""
    return {
        Status.PAID: ar.CUST_STATUS_TIP_PAID,
        Status.FAILED: ar.CUST_STATUS_TIP_FAILED,
        Status.PENDING: ar.CUST_STATUS_TIP_PENDING,
        Status.NONE: ar.CUST_STATUS_TIP_NONE,
    }


def _headers() -> tuple[str, ...]:
    """Column titles in the active language (evaluated at call time)."""
    return (
        ar.CUST_COL_STATUS,
        ar.CUST_COL_NAME,
        ar.CUST_COL_PHONE,
        ar.CUST_COL_PRODUCTS,
        ar.CUST_COL_PURCHASE_DATE,
        ar.CUST_COL_END_DATE,
        ar.CUST_COL_CATEGORY,
        ar.CUST_COL_PAYMENT_KIND,
        ar.CUST_COL_MONTHLY_DUE,
        ar.CUST_COL_REMAINING,
    )


class _CustomerTableModel(QAbstractTableModel):
    """Read-only model: one service summary per sale (or per client without sales)."""

    SUMMARY_ROLE = Qt.ItemDataRole.UserRole + 1
    TYPE_COLOR_ROLE = Qt.ItemDataRole.UserRole + 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: list[CustomerSummary] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(_headers())

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> str | None:  # noqa: N802 - Qt API
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        headers = _headers()
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(headers):
            return headers[section]
        if orientation == Qt.Orientation.Vertical:
            return str(section + 1)
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802 - Qt API
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        summary = self.rows[index.row()]
        column = index.column()
        if role == self.SUMMARY_ROLE:
            return summary
        if role == self.TYPE_COLOR_ROLE:
            return getattr(summary.customer.category, "color", None)
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == 0:
                return _status_tooltips()[summary.status]
            if column == 1:
                return ar.CUST_NAME_TOOLTIP.format(name=summary.full_name)
            if column == 3:
                return "\n".join(ltr_text(product) for product in summary.products)
            return self.data(index, Qt.ItemDataRole.DisplayRole)
        if role == Qt.ItemDataRole.UserRole:
            return self._sort_value(summary, column)
        if role == Qt.ItemDataRole.TextAlignmentRole and column in {0, 4, 5, MONTHLY_COLUMN, REMAINING_COLUMN}:
            return int(Qt.AlignmentFlag.AlignCenter)
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if column == 0:
            return ""
        if column == 1:
            return summary.full_name
        if column == 2:
            return summary.phone or ""
        if column == 3:
            # Products bought together share the row (the tooltip lists one per line).
            return " + ".join(ltr_text(product) for product in summary.products)
        if column == 4:
            return self._date_text(summary.purchase_date)
        if column == 5:
            return self._date_text(summary.end_date)
        if column == 6:
            return summary.category_name
        if column == PAYMENT_COLUMN:
            return " · ".join(_payment_label(kind) for kind in summary.sale_types)
        if column == MONTHLY_COLUMN:
            return self._money_text(summary.monthly_amount)
        if column == REMAINING_COLUMN:
            return self._money_text(summary.remaining_balance)
        return None

    def summary_at(self, row: int) -> CustomerSummary | None:
        """Return the row summary for a valid source-model index."""
        if 0 <= row < len(self.rows):
            return self.rows[row]
        return None

    def replace_rows(self, rows: list[CustomerSummary]) -> None:
        """Replace all directory rows after a service query."""
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    @staticmethod
    def _date_text(value: date | None) -> str:
        return value.strftime("%d/%m/%Y") if value is not None else ""

    @staticmethod
    def _money_text(value: int) -> str:
        return f"{value:,} {ar.CURRENCY_SUFFIX}"

    @staticmethod
    def _sort_value(summary: CustomerSummary, column: int):
        if column == 0:
            return {
                Status.FAILED: 0,
                Status.PENDING: 1,
                Status.PAID: 2,
                Status.NONE: 3,
            }[summary.status]
        if column == 1:
            return summary.full_name.casefold()
        if column == 2:
            return summary.phone or ""
        if column == 3:
            return ", ".join(summary.products).casefold()
        if column == 4:
            return summary.purchase_date or date.min
        if column == 5:
            return summary.end_date or date.min
        if column == 6:
            return summary.category_name.casefold()
        if column == PAYMENT_COLUMN:
            return summary.payment_profile + ",".join(summary.sale_types)
        if column == MONTHLY_COLUMN:
            return summary.monthly_amount
        if column == REMAINING_COLUMN:
            return summary.remaining_balance
        return ""


_PAYMENT_TONES = {"cash": "paid", "installment": "primary-glow", "credit": "pending"}


def _payment_label(kind: str) -> str:
    return {"cash": ar.SALE_CASH, "installment": ar.SALE_INSTALLMENT, "credit": ar.SALE_CREDIT}.get(kind, kind)


class _PaymentKindDelegate(QStyledItemDelegate):
    """Draw one small coloured pill per sale type: cash, installment, credit."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        base = QStyleOptionViewItem(option)
        self.initStyleOption(base, index)
        base.text = ""
        style = base.widget.style() if base.widget is not None else self.parent().style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, base, painter, base.widget)
        summary = index.data(_CustomerTableModel.SUMMARY_ROLE)
        if summary is None:
            return
        rtl = option.direction == Qt.LayoutDirection.RightToLeft
        metrics = option.fontMetrics
        x = option.rect.right() - 6 if rtl else option.rect.left() + 6
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for kind in summary.sale_types:
            text = _payment_label(kind)
            width = metrics.horizontalAdvance(text) + 16
            left = x - width if rtl else x
            rect = QRectF(left, option.rect.center().y() - 10, width, 20)
            color = qcolor(_PAYMENT_TONES.get(kind, "text-muted"))
            fill = QColor(color)
            fill.setAlpha(38)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, 10, 10)
            painter.setPen(color)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
            x = left - 4 if rtl else left + width + 4
        painter.restore()


class _StatusDotDelegate(QStyledItemDelegate):
    """Draw a glowing status indicator dot in the status column."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        style_option = QStyleOptionViewItem(option)
        self.initStyleOption(style_option, index)
        style_option.text = ""
        style = style_option.widget.style() if style_option.widget else self.parent().style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, style_option, painter, style_option.widget)

        summary = index.data(_CustomerTableModel.SUMMARY_ROLE)
        if summary is None:
            return
        color = QColor(_STATUS_COLORS[summary.status])
        center = option.rect.center()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        # Outer soft glow ring
        halo = QColor(color)
        halo.setAlpha(45)
        painter.setBrush(halo)
        painter.drawEllipse(center.x() - 10, center.y() - 10, 20, 20)
        # Inner solid dot
        painter.setBrush(color)
        painter.drawEllipse(center.x() - 5, center.y() - 5, 10, 10)
        painter.restore()


class CustomersPage(QWidget):
    """Search and manage customers, with service-owned queries and mutations."""

    customer_selected = Signal(int)
    sale_selected = Signal(int, int)  # customer id, sale id: open the client on that sale
    manage_types_requested = Signal()

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self._types: list[ClientType] = []
        self._category_rows: list[tuple[ClientType, int]] = []
        self._model = _CustomerTableModel(self)
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self._proxy.setDynamicSortFilter(True)
        self._build_ui()
        events.data_changed.connect(self.refresh)
        self.refresh()

    @property
    def _is_owner(self) -> bool:
        return self.current_user.role == "owner"

    # ------------------------------------------------------------------ build
    def _build_ui(self) -> None:
        """Create the header, type chips, filters, directory table and empty state."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(14)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        heading = QLabel(ar.CUST_PAGE_TITLE, self)
        heading.setObjectName("pageTitle")
        self.count_label = QLabel(self)
        self.count_label.setObjectName("pageSubtitle")
        self.count_label.setWordWrap(True)
        titles.addWidget(heading)
        titles.addWidget(self.count_label)
        title_row.addLayout(titles, 1)

        self.assign_type_button = QPushButton(ar.CT_ASSIGN, self)
        self.assign_type_button.setProperty("variant", "secondary")
        self.assign_type_button.setIcon(icons.icon("tag", "text", 16))
        self.assign_type_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.assign_type_button.setVisible(self._is_owner)
        self.assign_type_button.setEnabled(False)
        self.assign_type_button.clicked.connect(self._assign_type_to_selection)
        title_row.addWidget(self.assign_type_button)

        self.add_customer_button = QPushButton(ar.CUST_ADD, self)
        self.add_customer_button.setIcon(icons.icon("plus", "#FFFFFF", 16))
        self.add_customer_button.setVisible(self._is_owner)
        self.add_customer_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_customer_button.clicked.connect(self._add_customer)
        title_row.addWidget(self.add_customer_button)
        layout.addLayout(title_row)

        self.type_filter = TypeFilterBar(self)
        self.type_filter.type_selected.connect(lambda _type_id: self.refresh())
        if self._is_owner:
            self.type_filter.manage_requested.connect(self._manage_types)
        layout.addWidget(self.type_filter)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(ar.CUST_SEARCH)
        self.search.addAction(icons.icon("search", "text-muted", 16), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._schedule_refresh)
        filter_row.addWidget(self.search, 1)
        status_label = QLabel(ar.CUST_STATUS_FILTER, self)
        status_label.setObjectName("sectionHint")
        filter_row.addWidget(status_label)
        self.status_filter = QComboBox(self)
        for label, value in (
            (ar.CUST_FILTER_ALL, None),
            (ar.CUST_FILTER_PAID, Status.PAID.value),
            (ar.CUST_FILTER_FAILED, Status.FAILED.value),
            (ar.CUST_FILTER_PENDING, Status.PENDING.value),
            (ar.CUST_FILTER_NONE, Status.NONE.value),
        ):
            self.status_filter.addItem(label, value)
        self.status_filter.currentIndexChanged.connect(self.refresh)
        filter_row.addWidget(self.status_filter)
        payment_label = QLabel(ar.CUST_PAYMENT_FILTER, self)
        payment_label.setObjectName("sectionHint")
        filter_row.addWidget(payment_label)
        self.payment_filter = QComboBox(self)
        for label, value in (
            (ar.CUST_PAY_ALL, None),
            (ar.CUST_PAY_CASH, "cash"),
            (ar.CUST_PAY_FACILITIES, "facilities"),
            (ar.CUST_PAY_INSTALLMENT, "installment"),
            (ar.CUST_PAY_CREDIT, "credit"),
        ):
            self.payment_filter.addItem(label, value)
        self.payment_filter.currentIndexChanged.connect(self.refresh)
        filter_row.addWidget(self.payment_filter)
        balance_label = QLabel(ar.CUST_BALANCE_FILTER, self)
        balance_label.setObjectName("sectionHint")
        filter_row.addWidget(balance_label)
        self.balance_filter = QComboBox(self)
        for label, value in (
            (ar.CUST_BALANCE_ALL, None),
            (ar.CUST_BALANCE_OPEN, "open"),
            (ar.CUST_BALANCE_SETTLED, "settled"),
        ):
            self.balance_filter.addItem(label, value)
        self.balance_filter.currentIndexChanged.connect(self.refresh)
        filter_row.addWidget(self.balance_filter)
        month_label = QLabel(ar.DASHBOARD_MONTH, self)
        month_label.setObjectName("sectionHint")
        filter_row.addWidget(month_label)
        self.month_selector = QDateEdit(self)
        self.month_selector.setCalendarPopup(True)
        self.month_selector.setDisplayFormat("MM/yyyy")
        self.month_selector.setDate(QDate.currentDate())
        self.month_selector.dateChanged.connect(self.refresh)
        filter_row.addWidget(self.month_selector)
        layout.addLayout(filter_row)

        # Filters on the installment sheet's purchase and end-of-deduction dates.
        date_row = QHBoxLayout()
        date_row.setSpacing(10)
        purchased_label = QLabel(ar.CUST_PURCHASED_FILTER, self)
        purchased_label.setObjectName("sectionHint")
        date_row.addWidget(purchased_label)
        self.purchased_filter = QComboBox(self)
        for label, value in (
            (ar.CUST_PURCHASED_ANY, None),
            (ar.CUST_PURCHASED_THIS_MONTH, "this_month"),
            (ar.CUST_PURCHASED_LAST_MONTH, "last_month"),
            (ar.CUST_PURCHASED_LAST_3_MONTHS, "last_3_months"),
            (ar.CUST_PURCHASED_THIS_YEAR, "this_year"),
        ):
            self.purchased_filter.addItem(label, value)
        self.purchased_filter.currentIndexChanged.connect(self.refresh)
        date_row.addWidget(self.purchased_filter)
        deduction_label = QLabel(ar.CUST_DEDUCTION_FILTER, self)
        deduction_label.setObjectName("sectionHint")
        date_row.addWidget(deduction_label)
        self.deduction_filter = QComboBox(self)
        for label, value in (
            (ar.CUST_DEDUCTION_ANY, None),
            (ar.CUST_DEDUCTION_THIS_MONTH, "ends_this_month"),
            (ar.CUST_DEDUCTION_NEXT_MONTH, "ends_next_month"),
            (ar.CUST_DEDUCTION_ONGOING, "ongoing"),
            (ar.CUST_DEDUCTION_FINISHED, "finished"),
        ):
            self.deduction_filter.addItem(label, value)
        self.deduction_filter.currentIndexChanged.connect(self.refresh)
        date_row.addWidget(self.deduction_filter)
        date_row.addSpacing(16)
        # One-click sale-type filter (كاش / بالتقسيط / كريدي), kept in step
        # with the "payment" drop-down above.
        kind_label = QLabel(ar.CUST_KIND_FILTER, self)
        kind_label.setObjectName("sectionHint")
        date_row.addWidget(kind_label)
        self.kind_chips: dict[str | None, QPushButton] = {}
        for label, value in (
            (ar.CUST_PAY_ALL, None),
            (ar.SALE_CASH, "cash"),
            (ar.SALE_INSTALLMENT, "installment"),
            (ar.SALE_CREDIT, "credit"),
        ):
            chip = QPushButton(label, self)
            chip.setProperty("chip", True)
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _checked=False, kind=value: self.set_payment_filter(kind))
            self.kind_chips[value] = chip
            date_row.addWidget(chip)
        self.payment_filter.currentIndexChanged.connect(self._sync_kind_chips)
        self._sync_kind_chips()
        date_row.addStretch(1)
        layout.addLayout(date_row)

        # A fixed-height strip: when the list is empty the free space must go
        # to the empty-state card, not stretch the status pills.
        legend_bar = QWidget(self)
        legend_bar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        legend = QHBoxLayout(legend_bar)
        legend.setContentsMargins(0, 0, 0, 0)
        legend.setSpacing(8)
        for text, pill in (
            (ar.CUST_FILTER_PAID, "paid"),
            (ar.CUST_FILTER_PENDING, "pending"),
            (ar.CUST_FILTER_FAILED, "failed"),
        ):
            badge = QLabel(f"●  {text}", legend_bar)  # same dot as the status column
            badge.setProperty("pill", pill)
            badge.setMinimumHeight(24)
            badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            legend.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
        legend.addStretch(1)
        hint = QLabel(ar.CUST_CLICK_HINT, legend_bar)
        hint.setObjectName("sectionHint")
        legend.addWidget(hint, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(legend_bar)

        self.table = QTableView(self)
        self.table.setModel(self._proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QTableView.SelectionMode.ExtendedSelection if self._is_owner
            else QTableView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setMouseTracking(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(46)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(48)
        header.setSectionResizeMode(0, header.ResizeMode.Fixed)
        header.setSectionResizeMode(1, header.ResizeMode.Stretch)
        # A basket row lists several products, so this column shares the spare width with the name.
        header.setSectionResizeMode(3, header.ResizeMode.Stretch)
        for column, width in {0: 52, 2: 108, 4: 92, 5: 100, 6: 122, 7: 140, 8: 108, 9: 112}.items():
            self.table.setColumnWidth(column, width)
        self.table.setItemDelegateForColumn(0, _StatusDotDelegate(self.table))
        self.table.setItemDelegateForColumn(
            TYPE_COLUMN, TypeTagDelegate(_CustomerTableModel.TYPE_COLOR_ROLE, self.table)
        )
        self.table.setItemDelegateForColumn(PAYMENT_COLUMN, _PaymentKindDelegate(self.table))
        self.table.clicked.connect(self._cell_clicked)
        self.table.doubleClicked.connect(self._open_row)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._customer_context_menu)
        layout.addWidget(self.table, 1)

        # Owner: a checkbox per row, "select all" and "delete selected" at the
        # start of the status legend strip; the click hint becomes its tooltip.
        self.selection: SelectionBar | None = None
        if self._is_owner:
            self.selection = SelectionBar(self.table, self._key_at, legend_bar)
            self.selection.delete_requested.connect(self._delete_selected)
            self.selection.changed.connect(self._selection_changed)
            legend.insertWidget(0, self.selection, 0, Qt.AlignmentFlag.AlignVCenter)
            legend.insertSpacing(1, 16)
            hint.hide()
            legend_bar.setToolTip(ar.CUST_CLICK_HINT)

        self.empty_card = QFrame(self)
        self.empty_card.setObjectName("emptyStateCard")
        empty_layout = QVBoxLayout(self.empty_card)
        empty_layout.setContentsMargins(20, 36, 20, 36)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.setSpacing(8)
        empty_icon = QLabel(self.empty_card)
        empty_icon.setPixmap(icons.pixmap("search", "text-muted", 32, stroke=1.5))
        empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title = QLabel(ar.CUST_EMPTY_TITLE, self.empty_card)
        self.empty_title.setObjectName("emptyStateTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_sub = QLabel(ar.CUST_EMPTY_SUB, self.empty_card)
        self.empty_sub.setObjectName("emptyStateSub")
        self.empty_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reset_filter_btn = QPushButton(ar.CUST_RESET_FILTERS, self.empty_card)
        self.reset_filter_btn.setProperty("variant", "secondary")
        self.reset_filter_btn.setProperty("compact", True)
        self.reset_filter_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_filter_btn.clicked.connect(self._reset_filters)
        empty_layout.addWidget(empty_icon)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_sub)
        empty_layout.addWidget(self.reset_filter_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_card)
        self.empty_card.hide()

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self.refresh)

    # ------------------------------------------------------------------ data
    def refresh(self, *_args: object) -> None:
        """Reload client types and customer summaries through the services."""
        preferred_type = self.type_filter.selected_type_id()
        selected = self.month_selector.date()
        try:
            with session_scope() as session:
                types = categories.list_client_types(session)
                summaries = customers.list_customers(
                    session,
                    year=selected.year(),
                    month=selected.month(),
                    today=date.today(),
                    category_id=preferred_type,
                    status=self.status_filter.currentData(),
                    search=self.search.text().strip() or None,
                    payment_kind=self.payment_filter.currentData(),
                    balance=self.balance_filter.currentData(),
                    purchased=self.purchased_filter.currentData(),
                    deduction=self.deduction_filter.currentData(),
                    per_sale=True,
                )
        except (ValueError, RuntimeError, SQLAlchemyError):
            # Refreshes run in response to events; a modal box here could
            # stack up or outlive the page, so report it as a toast instead.
            _LOG.exception("Customer list refresh failed")
            events.notify.emit("error", ar.ERROR_TITLE, ar.CUST_DETAILS_LOAD_ERROR, 5000)
            return
        self._types = types
        self._category_rows = [(item, item.customer_count) for item in types]
        self.type_filter.set_types(types, preferred_type)
        self._model.replace_rows(summaries)
        header = self.table.horizontalHeader()
        self.table.sortByColumn(header.sortIndicatorSection(), header.sortIndicatorOrder())
        self.count_label.setText(ar.CUST_SUMMARY_SALES.format(
            sales=sum(len(item.sales) for item in summaries if item.sale_id is not None),
            count=len({item.id for item in summaries}),
            monthly=_CustomerTableModel._money_text(sum(item.monthly_amount for item in summaries)),
            remaining=_CustomerTableModel._money_text(sum(item.remaining_balance for item in summaries)),
        ))
        is_empty = len(summaries) == 0
        self.empty_card.setVisible(is_empty)
        self.table.setVisible(not is_empty)
        if self.selection is not None:
            self.selection.refresh()
        self._selection_changed()

    def set_status_filter(self, status: Status | str | None) -> None:
        """Select a status filter (used when arriving from a dashboard card)."""
        value = status.value if isinstance(status, Status) else status
        if value in (None, "", "ALL"):
            self.status_filter.setCurrentIndex(0)
            return
        index = self.status_filter.findData(value)
        if index >= 0:
            self.status_filter.setCurrentIndex(index)

    def set_payment_filter(self, kind: str | None) -> None:
        """Select a payment filter (``cash``, ``facilities``, ...); ``None`` for all."""
        index = self.payment_filter.findData(kind) if kind else 0
        if index >= 0:
            self.payment_filter.setCurrentIndex(index)
        self._sync_kind_chips()

    def _sync_kind_chips(self, *_args: object) -> None:
        """Check the sale-type chip that matches the payment drop-down."""
        current = self.payment_filter.currentData()
        for kind, chip in self.kind_chips.items():
            chip.setChecked(kind == current)

    def visible_summaries(self) -> list[CustomerSummary]:
        """Return the rows in their on-screen (sorted) order."""
        return [
            summary for row in range(self._proxy.rowCount())
            if (summary := self._summary_from_proxy_row(row)) is not None
        ]

    def checked_summaries(self) -> list[CustomerSummary]:
        """The rows ticked with their checkbox, in on-screen order."""
        if self.selection is None:
            return []
        wanted = set(self.selection.checked_keys())
        return _one_per_client(summary for summary in self.visible_summaries() if summary.id in wanted)

    def edit_customer_by_id(self, customer_id: int) -> None:
        """Open the owner edit form for a customer from another screen."""
        if not self._is_owner:
            return
        selected = self.month_selector.date()
        with session_scope() as session:
            rows = customers.list_customers(
                session, year=selected.year(), month=selected.month(), today=date.today()
            )
        summary = next((row for row in rows if row.id == customer_id), None)
        if summary is not None:
            self._edit_customer(summary)

    def _reset_filters(self) -> None:
        """Clear the search, status and type filters."""
        self.search.clear()
        self.status_filter.setCurrentIndex(0)
        self.payment_filter.setCurrentIndex(0)
        self.balance_filter.setCurrentIndex(0)
        self.purchased_filter.setCurrentIndex(0)
        self.deduction_filter.setCurrentIndex(0)
        self.type_filter.set_types(self._types, None)
        self.refresh()

    def _schedule_refresh(self, _text: str) -> None:
        """Debounce service searches while the user is typing."""
        self._search_timer.start()

    # --------------------------------------------------------------- selection
    def _key_at(self, proxy_row: int) -> int | None:
        """The customer id shown on a table row (the row's checkbox key)."""
        summary = self._summary_from_proxy_row(proxy_row)
        return summary.id if summary is not None else None

    def _selected_summaries(self) -> list[CustomerSummary]:
        """The ticked customers, or else the highlighted rows."""
        if self.selection is not None:
            wanted = set(self.selection.target_keys())
            return _one_per_client(summary for summary in self.visible_summaries() if summary.id in wanted)
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        return _one_per_client(summary for row in sorted(rows) if (summary := self._summary_from_proxy_row(row)))

    def _selection_changed(self, *_args: object) -> None:
        count = len(self._selected_summaries())
        self.assign_type_button.setEnabled(count > 0)
        self.assign_type_button.setText(f"{ar.CT_ASSIGN} ({count})" if count > 1 else ar.CT_ASSIGN)

    def _cell_clicked(self, proxy_index: QModelIndex) -> None:
        """A plain click on the name opens the customer; modifier clicks only select."""
        if not proxy_index.isValid():
            return
        modifiers = QApplication.keyboardModifiers()
        if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return
        if proxy_index.column() == 1 or not self._is_owner:
            self._open_row(proxy_index)

    def _open_row(self, proxy_index: QModelIndex) -> None:
        summary = self._summary_from_proxy_row(proxy_index.row()) if proxy_index.isValid() else None
        if summary is None:
            return
        if summary.sale_id is not None:
            self.sale_selected.emit(summary.id, summary.sale_id)
        else:
            self.customer_selected.emit(summary.id)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt event override
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            indexes = self.table.selectionModel().selectedRows()
            if indexes:
                self._open_row(indexes[0])
                return
        super().keyPressEvent(event)

    def _summary_from_proxy_row(self, proxy_row: int) -> CustomerSummary | None:
        """Map a sorted/filtered table row back to the domain summary."""
        source_index = self._proxy.mapToSource(self._proxy.index(proxy_row, 0))
        return self._model.summary_at(source_index.row())

    def _customer_context_menu(self, position) -> None:
        """Show owner-only actions for the row (or selection) under the pointer."""
        if not self._is_owner:
            return
        proxy_index = self.table.indexAt(position)
        if not proxy_index.isValid():
            return
        if not self.table.selectionModel().isRowSelected(proxy_index.row(), QModelIndex()):
            self.table.selectRow(proxy_index.row())
        summary = self._summary_from_proxy_row(proxy_index.row())
        if summary is None:
            return
        # The ticked/highlighted rows when the clicked row is one of them, else just this row.
        targets = self._selected_summaries()
        if summary.id not in {item.id for item in targets}:
            targets = [summary]
        menu = QMenu(self)
        edit_action = menu.addAction(icons.icon("edit", "text", 16), ar.CUST_EDIT)
        assign_action = menu.addAction(icons.icon("tag", "text", 16), ar.CT_ASSIGN)
        menu.addSeparator()
        delete_text = ar.CUST_DELETE if len(targets) == 1 else f"{ar.SEL_DELETE} ({len(targets)})"
        delete_action = menu.addAction(icons.icon("trash", "failed", 16), delete_text)
        chosen = menu.exec(self.table.viewport().mapToGlobal(position))
        if chosen == edit_action:
            self._edit_customer(summary)
        elif chosen == assign_action:
            self._assign_type(targets)
        elif chosen == delete_action:
            self._delete_summaries(targets)

    # ----------------------------------------------------------- client types
    def _manage_types(self) -> None:
        """Client types are managed on their own page (sidebar)."""
        if self._is_owner:
            self.manage_types_requested.emit()

    def set_type_filter(self, type_id: int | None) -> None:
        """Show only customers of one client type (``None`` for all)."""
        self.type_filter.set_types(self._types, type_id)
        self.refresh()

    def _assign_type_to_selection(self) -> None:
        self._assign_type(self._selected_summaries())

    def _assign_type(self, selection: list[CustomerSummary]) -> None:
        if not self._is_owner:
            return
        if not selection:
            self._show_error(ar.CT_SELECT_CUSTOMERS)
            return
        dialog = AssignTypeDialog(self._types, len(selection), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        choice = dialog.selected()
        if choice is None:
            return
        type_id, type_name = choice
        try:
            with session_scope() as session:
                categories.assign_client_type(
                    session, self.current_user.id, [item.id for item in selection], type_id
                )
        except (ClientTypeError, auth.AuthorizationError, SQLAlchemyError) as error:
            self._show_error(error_text(error))
            return
        events.data_changed.emit()
        events.notify.emit(
            "success", ar.CT_TITLE,
            ar.CT_ASSIGNED_TOAST.format(name=type_name, count=len(selection)), 3500,
        )

    # -------------------------------------------------------------- customers
    def _add_customer(self) -> None:
        """Open the new-client form and save the client (purchases are made on the New sale page)."""
        if not self._category_rows:
            self._show_error(ar.CUST_SAVE_ERROR)
            return
        dialog = CustomerFormDialog(self._category_rows, None, self)
        preferred = self.type_filter.selected_type_id()
        if preferred is not None:
            dialog.category.setCurrentIndex(max(0, dialog.category.findData(preferred)))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.save_new_customer(dialog.values())

    def save_new_customer(self, values: dict[str, object]) -> int | None:
        """Save a new client, asking first when the phone number is already used."""
        try:
            customer_id = self._save_customer(values, allow_duplicate_phone=False)
        except DuplicateCustomerPhoneError as error:
            names = ", ".join(customer.full_name for customer in error.matches)
            if not self._confirm(ar.CUST_DUP_PHONE_CONFIRM + "\n" + names, ar.CUST_ADD, ar.CUST_SAVE):
                return None
            try:
                customer_id = self._save_customer(values, allow_duplicate_phone=True)
            except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
                self._show_error(ar.CUST_SAVE_ERROR)
                return None
        except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.CUST_SAVE_ERROR)
            return None
        self._emit_customer_change(customer_id)
        return customer_id

    def _edit_customer(self, summary: CustomerSummary) -> None:
        """Edit customer profile fields through the customer service."""
        dialog = CustomerFormDialog(self._category_rows, summary, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            self._update_customer(summary.id, values, allow_duplicate_phone=False)
        except DuplicateCustomerPhoneError as error:
            names = ", ".join(customer.full_name for customer in error.matches)
            if not self._confirm(ar.CUST_DUP_PHONE_CONFIRM + "\n" + names, ar.CUST_EDIT, ar.CUST_SAVE):
                return
            try:
                self._update_customer(summary.id, values, allow_duplicate_phone=True)
            except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
                self._show_error(ar.CUST_SAVE_ERROR)
                return
        except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.CUST_SAVE_ERROR)
            return
        self._emit_customer_change(summary.id)

    def _save_customer(self, values: dict[str, object], *, allow_duplicate_phone: bool) -> int:
        """Create a customer through the customer service."""
        customer_values = {
            "full_name": str(values["full_name"]),
            "category_id": int(values["category_id"]),
            **{key: value for key, value in values.items() if key not in {"full_name", "category_id"}},
        }
        with session_scope() as session:
            self._require_owner(session)
            customer = customers.create_customer(
                session, allow_duplicate_phone=allow_duplicate_phone, **customer_values
            )
            return customer.id

    def _update_customer(
        self,
        customer_id: int,
        values: dict[str, object],
        *,
        allow_duplicate_phone: bool,
    ) -> None:
        """Update customer values in a caller-committed session."""
        with session_scope() as session:
            self._require_owner(session)
            customers.update_customer(
                session, customer_id, allow_duplicate_phone=allow_duplicate_phone, **values
            )

    def _delete_selected(self) -> None:
        """Delete the ticked customers (or the highlighted ones) after confirmation."""
        self._delete_summaries(self._selected_summaries())

    def _delete_summaries(self, chosen: list[CustomerSummary]) -> None:
        """Confirm, then delete; customers with sales need an explicit "with sales" choice."""
        if not chosen or not self._is_owner:
            return
        message = ar.CUST_DELETE_MANY_CONFIRM.format(count=len(chosen)) + "\n\n" + row_selection.names_preview(
            [summary.full_name for summary in chosen]
        )
        with_sales = [summary for summary in chosen if summary.sales]
        if with_sales:
            choices = [("with_sales", ar.CUST_DELETE_WITH_SALES)]
            if len(with_sales) < len(chosen):
                choices.append(("without_sales", ar.CUST_DELETE_WITHOUT_SALES.format(
                    count=len(chosen) - len(with_sales)
                )))
            details = ar.CUST_DELETE_HAS_SALES.format(count=len(with_sales)) + "\n" + ar.SEL_BACKUP_NOTE
            answer = row_selection.ask_delete(self, ar.CUST_DELETE, message, choices, details=details)
        else:
            answer = row_selection.ask_delete(self, ar.CUST_DELETE, message)
        if answer is None:
            return
        self.delete_customers([summary.id for summary in chosen], with_sales=answer == "with_sales")

    def delete_customers(self, customer_ids: list[int], *, with_sales: bool = False) -> bool:
        """Delete customers; with ``with_sales`` their sales go too (after a safety backup)."""
        if with_sales:
            backup.create_safety_backup()
        try:
            with session_scope() as session:
                self._require_owner(session)
                result = customers.delete_customers(
                    session, self.current_user.id, customer_ids, with_sales=with_sales
                )
        except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
            _LOG.exception("Customer delete failed")
            self._show_error(ar.CUST_DELETE_FAILED)
            return False
        if self.selection is not None:
            self.selection.clear()
        events.data_changed.emit()
        message = ar.CUST_DELETED_TOAST.format(count=result.deleted)
        if result.kept:
            message += " " + ar.CUST_DELETE_KEPT.format(count=len(result.kept))
        events.notify.emit("success", ar.CUST_PAGE_TITLE, message, 4000)
        return True

    def _require_owner(self, session: Session) -> None:
        """Enforce current owner status before any write action."""
        if not self._is_owner:
            raise auth.AuthorizationError("Owner role required")
        auth.require_owner(session, self.current_user.id)

    def _emit_customer_change(self, customer_id: int) -> None:
        """Publish refresh events and a confirmation after a committed change."""
        events.data_changed.emit()
        events.customer_changed.emit(customer_id)
        events.notify.emit("success", ar.CUST_PAGE_TITLE, ar.CUST_SAVED_TOAST, 3500)

    def _confirm(self, message: str, title: str, confirm_text: str) -> bool:
        """Show a localized confirmation with explicit button labels."""
        box = QMessageBox(QMessageBox.Icon.Question, title, message, parent=self)
        confirm = box.addButton(confirm_text, QMessageBox.ButtonRole.AcceptRole)
        box.addButton(ar.CUST_CANCEL, QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() == confirm

    def _show_error(self, message: str) -> None:
        """Display a localized page-level error."""
        QMessageBox.warning(self, ar.ERROR_TITLE, message)


def _one_per_client(rows) -> list[CustomerSummary]:
    """The first row of each client: actions such as delete act on clients, not on sale rows."""
    seen: set[int] = set()
    result = []
    for summary in rows:
        if summary.id not in seen:
            seen.add(summary.id)
            result.append(summary)
    return result
