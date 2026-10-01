"""Customer directory: client-type chips, status and search filters, owner actions."""

from __future__ import annotations

import logging
from datetime import date

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QDate, QRectF, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIntValidator, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.models import Category, User
from app.db.session import session_scope
from app.i18n import ar
from app.config import data_dir
from app.services import auth, categories, customers, reports
from app.services.categories import ClientType, ClientTypeError
from app.services.customers import CustomerSummary, DuplicateCustomerPhoneError
from app.services.status import Status
from app.ui import icons
from app.ui.theme import qcolor
from app.ui.dialogs.client_types_dialog import AssignTypeDialog, ClientTypesDialog, error_text
from app.ui.events import events
from app.ui.widgets.client_types import TypeFilterBar, TypeTagDelegate

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
    """Read-only model containing exactly one service summary per customer."""

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
            return ", ".join(summary.products)
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


class _CustomerFormDialog(QDialog):
    """Add/edit customer profile details without performing database work."""

    def __init__(
        self,
        category_rows: list[tuple[Category, int]],
        customer: CustomerSummary | None,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(ar.CUST_ADD if customer is None else ar.CUST_EDIT)
        self.setMinimumWidth(520)
        self.customer = customer.customer if customer is not None else None

        self.full_name = QLineEdit(self)
        self.phone = QLineEdit(self)
        self.phone.setMaxLength(20)
        self.phone.setPlaceholderText("05xxxxxxxx")
        self.category = QComboBox(self)
        for category, count in category_rows:
            self.category.addItem(f"{category.name} ({count})", category.id)

        self.id_number = QLineEdit(self)
        self.issue_date_enabled = QCheckBox(ar.CUST_ID_ISSUE_DATE, self)
        self.issue_date = QDateEdit(self)
        self.issue_date.setCalendarPopup(True)
        self.issue_date.setDisplayFormat("dd/MM/yyyy")
        self.issue_date.setDate(QDate.currentDate())
        self.issue_date.setEnabled(False)
        self.issue_date_enabled.toggled.connect(self.issue_date.setEnabled)
        self.issue_place = QLineEdit(self)
        self.address = QLineEdit(self)
        self.profession = QLineEdit(self)
        self.ccp_number = QLineEdit(self)
        self.cheques_enabled = QCheckBox(ar.CUST_CHEQUES_COUNT, self)
        self.cheques_count = QLineEdit(self)
        self.cheques_count.setValidator(QIntValidator(0, 2_000_000_000, self))
        self.cheques_count.setEnabled(False)
        self.cheques_enabled.toggled.connect(self.cheques_count.setEnabled)
        self.notes = QPlainTextEdit(self)
        self.notes.setMaximumHeight(100)

        form = QFormLayout()
        form.addRow(ar.CUST_COL_NAME, self.full_name)
        form.addRow(ar.CUST_COL_PHONE, self.phone)
        form.addRow(ar.CUST_COL_CATEGORY, self.category)
        form.addRow(ar.CUST_ID_NUMBER, self.id_number)
        issue_date_row = QWidget(self)
        issue_date_layout = QHBoxLayout(issue_date_row)
        issue_date_layout.setContentsMargins(0, 0, 0, 0)
        issue_date_layout.addWidget(self.issue_date_enabled)
        issue_date_layout.addWidget(self.issue_date, 1)
        form.addRow("", issue_date_row)
        form.addRow(ar.CUST_ID_ISSUE_PLACE, self.issue_place)
        form.addRow(ar.CUST_ADDRESS, self.address)
        form.addRow(ar.CUST_PROFESSION, self.profession)
        form.addRow(ar.CUST_CCP_NUMBER, self.ccp_number)
        cheque_row = QWidget(self)
        cheque_layout = QHBoxLayout(cheque_row)
        cheque_layout.setContentsMargins(0, 0, 0, 0)
        cheque_layout.addWidget(self.cheques_enabled)
        cheque_layout.addWidget(self.cheques_count, 1)
        form.addRow("", cheque_row)
        form.addRow(ar.CUST_NOTES, self.notes)

        buttons = QHBoxLayout()
        self.cancel_button = QPushButton(ar.CUST_CANCEL, self)
        self.cancel_button.setProperty("variant", "secondary")
        self.save_button = QPushButton(ar.CUST_SAVE, self)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self.accept)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(buttons)
        self._load_customer()

    def values(self) -> dict[str, object]:
        """Return validated-shape form values; service validation remains authoritative."""
        cheque_text = self.cheques_count.text().strip()
        return {
            "full_name": self.full_name.text(),
            "phone": self.phone.text(),
            "category_id": self.category.currentData(),
            "id_number": self._optional_text(self.id_number.text()),
            "id_issue_date": (
                self.issue_date.date().toPython() if self.issue_date_enabled.isChecked() else None
            ),
            "id_issue_place": self._optional_text(self.issue_place.text()),
            "address": self._optional_text(self.address.text()),
            "profession": self._optional_text(self.profession.text()),
            "ccp_number": self._optional_text(self.ccp_number.text()),
            "cheques_count": int(cheque_text) if self.cheques_enabled.isChecked() and cheque_text else None,
            "notes": self._optional_text(self.notes.toPlainText()),
        }

    def _load_customer(self) -> None:
        """Populate the form when editing an existing row."""
        if self.customer is None:
            return
        customer = self.customer
        self.full_name.setText(customer.full_name)
        self.phone.setText(customer.phone or "")
        category_index = self.category.findData(customer.category_id)
        if category_index >= 0:
            self.category.setCurrentIndex(category_index)
        self.id_number.setText(customer.id_number or "")
        if customer.id_issue_date is not None:
            self.issue_date_enabled.setChecked(True)
            self.issue_date.setDate(QDate(customer.id_issue_date.year, customer.id_issue_date.month,
                                          customer.id_issue_date.day))
        self.issue_place.setText(customer.id_issue_place or "")
        self.address.setText(customer.address or "")
        self.profession.setText(customer.profession or "")
        self.ccp_number.setText(customer.ccp_number or "")
        if customer.cheques_count is not None:
            self.cheques_enabled.setChecked(True)
            self.cheques_count.setText(str(customer.cheques_count))
        self.notes.setPlainText(customer.notes or "")

    @staticmethod
    def _optional_text(value: str) -> str | None:
        return value.strip() or None




class CustomersPage(QWidget):
    """Search and manage customers, with service-owned queries and mutations."""

    customer_selected = Signal(int)

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
        titles.addWidget(heading)
        titles.addWidget(self.count_label)
        title_row.addLayout(titles, 1)

        self.export_button = QPushButton(ar.CUST_EXPORT, self)
        self.export_button.setProperty("variant", "secondary")
        self.export_button.setIcon(icons.icon("import", "text", 16))
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_button.clicked.connect(self._export_list)
        title_row.addWidget(self.export_button)

        self.assign_type_button = QPushButton(ar.CT_ASSIGN, self)
        self.assign_type_button.setProperty("variant", "secondary")
        self.assign_type_button.setIcon(icons.icon("tag", "text", 16))
        self.assign_type_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.assign_type_button.setVisible(self._is_owner)
        self.assign_type_button.setEnabled(False)
        self.assign_type_button.clicked.connect(self._assign_type_to_selection)
        title_row.addWidget(self.assign_type_button)

        self.manage_types_button = QPushButton(ar.CT_MANAGE, self)
        self.manage_types_button.setProperty("variant", "secondary")
        self.manage_types_button.setIcon(icons.icon("settings", "text", 16))
        self.manage_types_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.manage_types_button.setVisible(self._is_owner)
        self.manage_types_button.clicked.connect(self._manage_types)
        title_row.addWidget(self.manage_types_button)

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

        legend = QHBoxLayout()
        legend.setSpacing(8)
        for text, pill in (
            (ar.CUST_FILTER_PAID, "paid"),
            (ar.CUST_FILTER_PENDING, "pending"),
            (ar.CUST_FILTER_FAILED, "failed"),
        ):
            badge = QLabel(text, self)
            badge.setProperty("pill", pill)
            legend.addWidget(badge)
        legend.addStretch(1)
        hint = QLabel(ar.CUST_CLICK_HINT, self)
        hint.setObjectName("sectionHint")
        legend.addWidget(hint)
        layout.addLayout(legend)

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
        for column, width in {0: 52, 2: 108, 3: 120, 4: 92, 5: 100, 6: 122, 7: 100, 8: 108, 9: 112}.items():
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
        self.count_label.setText(ar.CUST_SUMMARY.format(
            count=len(summaries),
            monthly=_CustomerTableModel._money_text(sum(item.monthly_amount for item in summaries)),
            remaining=_CustomerTableModel._money_text(sum(item.remaining_balance for item in summaries)),
        ))
        is_empty = len(summaries) == 0
        self.empty_card.setVisible(is_empty)
        self.table.setVisible(not is_empty)
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

    def visible_summaries(self) -> list[CustomerSummary]:
        """Return the rows in their on-screen (sorted) order."""
        return [
            summary for row in range(self._proxy.rowCount())
            if (summary := self._summary_from_proxy_row(row)) is not None
        ]

    def _export_list(self) -> None:
        """Save the customers currently listed (with all filters) as an Excel file."""
        default = str(data_dir() / "exports" / ar.CUST_EXPORT_FILENAME)
        selected, _filter = QFileDialog.getSaveFileName(
            self, ar.CUST_EXPORT_TITLE, default, ar.IMP_TEMPLATE_FILTER
        )
        if not selected:
            return
        self.export_to(selected)

    def export_to(self, destination: str) -> bool:
        """Write the visible list to ``destination``; returns success."""
        rows = self.visible_summaries()
        path = destination if destination.casefold().endswith(".xlsx") else destination + ".xlsx"
        try:
            reports.export_customers_xlsx(rows, path)
        except OSError:
            _LOG.exception("Customer export failed")
            self._show_error(ar.CUST_EXPORT_ERROR)
            return False
        events.notify.emit("success", ar.CUST_EXPORT_TITLE, ar.CUST_EXPORT_DONE.format(count=len(rows)), 4000)
        return True

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
        self.type_filter.set_types(self._types, None)
        self.refresh()

    def _schedule_refresh(self, _text: str) -> None:
        """Debounce service searches while the user is typing."""
        self._search_timer.start()

    # --------------------------------------------------------------- selection
    def _selected_summaries(self) -> list[CustomerSummary]:
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        return [summary for row in sorted(rows) if (summary := self._summary_from_proxy_row(row))]

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
        if summary is not None:
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
        menu = QMenu(self)
        edit_action = menu.addAction(icons.icon("edit", "text", 16), ar.CUST_EDIT)
        assign_action = menu.addAction(icons.icon("tag", "text", 16), ar.CT_ASSIGN)
        menu.addSeparator()
        delete_action = menu.addAction(icons.icon("trash", "failed", 16), ar.CUST_DELETE)
        chosen = menu.exec(self.table.viewport().mapToGlobal(position))
        if chosen == edit_action:
            self._edit_customer(summary)
        elif chosen == assign_action:
            self._assign_type_to_selection()
        elif chosen == delete_action:
            self._delete_customer(summary)

    # ----------------------------------------------------------- client types
    def _manage_types(self) -> None:
        if not self._is_owner:
            return
        ClientTypesDialog(self.current_user.id, self).exec()

    def _assign_type_to_selection(self) -> None:
        selection = self._selected_summaries()
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
        """Open the customer form and save through the customer service."""
        if not self._category_rows:
            self._show_error(ar.CUST_SAVE_ERROR)
            return
        dialog = _CustomerFormDialog(self._category_rows, None, self)
        preferred = self.type_filter.selected_type_id()
        if preferred is not None:
            dialog.category.setCurrentIndex(max(0, dialog.category.findData(preferred)))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            customer_id = self._save_customer(values, allow_duplicate_phone=False)
        except DuplicateCustomerPhoneError as error:
            names = ", ".join(customer.full_name for customer in error.matches)
            if not self._confirm(ar.CUST_DUP_PHONE_CONFIRM + "\n" + names, ar.CUST_ADD, ar.CUST_SAVE):
                return
            try:
                customer_id = self._save_customer(values, allow_duplicate_phone=True)
            except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
                self._show_error(ar.CUST_SAVE_ERROR)
                return
        except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.CUST_SAVE_ERROR)
            return
        self._emit_customer_change(customer_id)

    def _edit_customer(self, summary: CustomerSummary) -> None:
        """Edit customer profile fields through the customer service."""
        dialog = _CustomerFormDialog(self._category_rows, summary, self)
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
        """Create a customer in a caller-committed session."""
        with session_scope() as session:
            self._require_owner(session)
            customer = customers.create_customer(
                session,
                full_name=str(values["full_name"]),
                category_id=int(values["category_id"]),
                phone=values["phone"],
                allow_duplicate_phone=allow_duplicate_phone,
                **{key: value for key, value in values.items()
                   if key not in {"full_name", "category_id", "phone"}},
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

    def _delete_customer(self, summary: CustomerSummary) -> None:
        """Confirm and delete a customer only when the service allows it."""
        if not self._confirm(
            ar.CUST_CONFIRM_DELETE + "\n" + summary.full_name, ar.CUST_DELETE, ar.CUST_DELETE
        ):
            return
        try:
            with session_scope() as session:
                self._require_owner(session)
                customers.delete_customer(session, summary.id)
        except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.CUST_DELETE_ERROR)
            return
        self._emit_customer_change(summary.id)

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
