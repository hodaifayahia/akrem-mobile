"""Customer directory with category tabs, status, search, and owner actions."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QDate, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIntValidator, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabBar,
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
from app.services import auth, categories, customers
from app.services.customers import CustomerSummary, DuplicateCustomerPhoneError
from app.services.status import Status
from app.ui.events import events

_STATUS_COLORS = {
    Status.PAID: "#22C55E",
    Status.FAILED: "#EF4444",
    Status.PENDING: "#F59E0B",
    Status.NONE: "#8A94A6",
}
_STATUS_TOOLTIPS = {
    Status.PAID: ar.CUST_STATUS_TIP_PAID,
    Status.FAILED: ar.CUST_STATUS_TIP_FAILED,
    Status.PENDING: ar.CUST_STATUS_TIP_PENDING,
    Status.NONE: ar.CUST_STATUS_TIP_NONE,
}
_HEADERS = (
    ar.CUST_COL_STATUS,
    ar.CUST_COL_NAME,
    ar.CUST_COL_PHONE,
    ar.CUST_COL_PRODUCTS,
    ar.CUST_COL_PURCHASE_DATE,
    ar.CUST_COL_END_DATE,
    ar.CUST_COL_CATEGORY,
    ar.CUST_COL_MONTHLY_DUE,
    ar.CUST_COL_REMAINING,
)


class _CustomerTableModel(QAbstractTableModel):
    """Read-only model containing exactly one service summary per customer."""

    SUMMARY_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: list[CustomerSummary] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> str | None:  # noqa: N802 - Qt API
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(_HEADERS):
            return _HEADERS[section]
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
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == 0:
                return _STATUS_TOOLTIPS[summary.status]
            if column == 1:
                return f"{summary.full_name} · اضغط لعرض تفاصيل التقسيط والأسعار"
            return self.data(index, Qt.ItemDataRole.DisplayRole)
        if role == Qt.ItemDataRole.UserRole:
            return self._sort_value(summary, column)
        if role == Qt.ItemDataRole.TextAlignmentRole and column in {0, 4, 5, 7, 8}:
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
        if column == 7:
            return self._money_text(summary.monthly_amount)
        if column == 8:
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
        if column == 7:
            return summary.monthly_amount
        if column == 8:
            return summary.remaining_balance
        return ""


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
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
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


class _CategoryChoiceDialog(QDialog):
    """Choose an existing customer category for one customer."""

    def __init__(
        self,
        category_rows: list[tuple[Category, int]],
        selected_category_id: int,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(ar.CUST_CHANGE_CATEGORY)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.category = QComboBox(self)
        for category, count in category_rows:
            self.category.addItem(f"{category.name} ({count})", category.id)
        index = self.category.findData(selected_category_id)
        if index >= 0:
            self.category.setCurrentIndex(index)
        buttons = QHBoxLayout()
        cancel = QPushButton(ar.CUST_CANCEL, self)
        cancel.setProperty("variant", "secondary")
        save = QPushButton(ar.CUST_SAVE, self)
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(ar.CUST_COL_CATEGORY, self))
        layout.addWidget(self.category)
        layout.addLayout(buttons)


class CustomersPage(QWidget):
    """Search and manage customers, with service-owned queries and mutations."""

    customer_selected = Signal(int)

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self._category_rows: list[tuple[Category, int]] = []
        self._model = _CustomerTableModel(self)
        self._proxy = QSortFilterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self._proxy.setDynamicSortFilter(True)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._build_ui()
        events.data_changed.connect(self.refresh)
        self.refresh()

    def _build_ui(self) -> None:
        """Create category tabs, filters, directory table, and owner actions."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(14)

        title_row = QHBoxLayout()
        heading = QLabel(ar.CUST_PAGE_TITLE, self)
        heading.setObjectName("pageTitle")
        title_row.addWidget(heading)
        title_row.addStretch(1)
        self.add_customer_button = QPushButton(f"➕  {ar.CUST_ADD}", self)
        self.add_customer_button.setVisible(self._is_owner)
        self.add_customer_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_customer_button.clicked.connect(self._add_customer)
        title_row.addWidget(self.add_customer_button)
        layout.addLayout(title_row)

        category_row = QHBoxLayout()
        self.category_tabs = QTabBar(self)
        self.category_tabs.setExpanding(False)
        self.category_tabs.setMovable(False)
        self.category_tabs.setTabsClosable(False)
        self.category_tabs.setCursor(Qt.CursorShape.PointingHandCursor)
        self.category_tabs.currentChanged.connect(self.refresh)
        self.category_tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.category_tabs.customContextMenuRequested.connect(self._category_context_menu)
        category_row.addWidget(self.category_tabs, 1)
        self.add_category_button = QPushButton(f"📁  {ar.CUST_ADD_CATEGORY}", self)
        self.add_category_button.setProperty("variant", "secondary")
        self.add_category_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_category_button.setVisible(self._is_owner)
        self.add_category_button.clicked.connect(self._add_category)
        category_row.addWidget(self.add_category_button)
        layout.addLayout(category_row)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(f"🔍  {ar.CUST_SEARCH}...")
        self.search.textChanged.connect(self._schedule_refresh)
        filter_row.addWidget(self.search, 1)

        filter_label = QLabel(f"⚡  {ar.CUST_STATUS_FILTER}:", self)
        filter_label.setStyleSheet("color: #9DBEFF; font-weight: 600; font-size: 13px;")
        filter_row.addWidget(filter_label)
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

        month_label = QLabel(f"📅  {ar.DASHBOARD_MONTH}:", self)
        month_label.setStyleSheet("color: #9DBEFF; font-weight: 600; font-size: 13px;")
        filter_row.addWidget(month_label)
        self.month_selector = QDateEdit(self)
        self.month_selector.setCalendarPopup(True)
        self.month_selector.setDisplayFormat("MM/yyyy")
        self.month_selector.setDate(QDate.currentDate())
        self.month_selector.dateChanged.connect(self.refresh)
        filter_row.addWidget(self.month_selector)
        layout.addLayout(filter_row)

        # Quick Status Visual Indicator Bar
        legend_layout = QHBoxLayout()
        legend_layout.setContentsMargins(4, 0, 4, 0)
        legend_layout.setSpacing(16)

        status_paid_badge = QLabel("🟢  مكتمل (تم الدفع)", self)
        status_paid_badge.setStyleSheet("color: #22C55E; font-size: 12px; font-weight: 700;")

        status_pending_badge = QLabel("🟠  قيد الانتظار", self)
        status_pending_badge.setStyleSheet("color: #F59E0B; font-size: 12px; font-weight: 700;")

        status_failed_badge = QLabel("🔴  فشلت (فات تاريخ الدفع ولم يدفع)", self)
        status_failed_badge.setStyleSheet("color: #EF4444; font-size: 12px; font-weight: 700;")

        click_hint = QLabel("💡 انقر على اسم الزبون لعرض جدول تفاصيل التقسيط والأسعار", self)
        click_hint.setStyleSheet("color: #8A94A6; font-size: 11px;")

        legend_layout.addWidget(status_paid_badge)
        legend_layout.addWidget(status_pending_badge)
        legend_layout.addWidget(status_failed_badge)
        legend_layout.addStretch(1)
        legend_layout.addWidget(click_hint)
        layout.addLayout(legend_layout)

        self.table = QTableView(self)
        self.table.setModel(self._proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setMinimumSectionSize(48)
        self.table.horizontalHeader().setSectionResizeMode(
            0, self.table.horizontalHeader().ResizeMode.Fixed
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, self.table.horizontalHeader().ResizeMode.Stretch
        )
        for column, width in {
            0: 56,
            2: 110,
            3: 135,
            4: 100,
            5: 100,
            6: 105,
            7: 120,
            8: 120,
        }.items():
            self.table.setColumnWidth(column, width)
        self.table.setItemDelegateForColumn(0, _StatusDotDelegate(self.table))
        self.table.clicked.connect(self._cell_clicked)
        self.table.doubleClicked.connect(self._cell_clicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._customer_context_menu)
        layout.addWidget(self.table, 1)

        # Empty State Card
        self.empty_card = QFrame(self)
        self.empty_card.setObjectName("emptyStateCard")
        empty_layout = QVBoxLayout(self.empty_card)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.setSpacing(8)
        empty_icon = QLabel("🔍", self.empty_card)
        empty_icon.setStyleSheet("font-size: 28px;")
        empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title = QLabel("لا يوجد زبائن مطابقين للبحث", self.empty_card)
        self.empty_title.setObjectName("emptyStateTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_sub = QLabel("جرب تغيير نص البحث، التصنيف، أو حالة الاقتطاع", self.empty_card)
        self.empty_sub.setObjectName("emptyStateSub")
        self.empty_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reset_filter_btn = QPushButton("إعادة ضبط الفلاتر", self.empty_card)
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

    def _reset_filters(self) -> None:
        """Clear search term and reset status dropdown."""
        self.search.clear()
        self.status_filter.setCurrentIndex(0)
        if self.category_tabs.count() > 0:
            self.category_tabs.setCurrentIndex(0)

    @property
    def _is_owner(self) -> bool:
        return self.current_user.role == "owner"

    def refresh(self, *_args: object) -> None:
        """Reload category counts and customer summaries through service APIs."""
        preferred_category = self._selected_category_id()
        selected = self.month_selector.date()
        today = date.today()
        try:
            with session_scope() as session:
                category_rows = categories.list_categories(session)
                summaries = customers.list_customers(
                    session,
                    year=selected.year(),
                    month=selected.month(),
                    today=today,
                    category_id=preferred_category,
                    status=self.status_filter.currentData(),
                    search=self.search.text().strip() or None,
                )
            self._category_rows = category_rows
            self._replace_category_tabs(preferred_category)
            self._model.replace_rows(summaries)
            self.table.sortByColumn(self.table.horizontalHeader().sortIndicatorSection(),
                                    self.table.horizontalHeader().sortIndicatorOrder())
            is_empty = len(summaries) == 0
            self.empty_card.setVisible(is_empty)
            self.table.setVisible(not is_empty)
        except (ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.CUST_SAVE_ERROR)

    def set_status_filter(self, status: Status | str | None) -> None:
        """Select a status filter, useful for dashboard navigation integration."""
        value = status.value if isinstance(status, Status) else status
        index = self.status_filter.findData(value)
        if index >= 0:
            self.status_filter.setCurrentIndex(index)

    def edit_customer_by_id(self, customer_id: int) -> None:
        """Open the owner edit form for a customer from another screen."""
        if not self._is_owner:
            return
        selected = self.month_selector.date()
        with session_scope() as session:
            rows = customers.list_customers(
                session,
                year=selected.year(),
                month=selected.month(),
                today=date.today(),
            )
        summary = next((row for row in rows if row.id == customer_id), None)
        if summary is not None:
            self._edit_customer(summary)

    def _replace_category_tabs(self, selected_category_id: int | None) -> None:
        """Rebuild category tabs with customer counts and restore selection."""
        self.category_tabs.blockSignals(True)
        while self.category_tabs.count():
            self.category_tabs.removeTab(self.category_tabs.count() - 1)
        total_count = sum(count for _category, count in self._category_rows)
        self.category_tabs.addTab(f"{ar.CUST_FILTER_ALL} ({total_count})")
        self.category_tabs.setTabData(0, None)
        selected_index = 0
        for category, count in self._category_rows:
            index = self.category_tabs.addTab(f"{category.name} ({count})")
            self.category_tabs.setTabData(index, category.id)
            if category.id == selected_category_id:
                selected_index = index
        self.category_tabs.setCurrentIndex(selected_index)
        self.category_tabs.blockSignals(False)

    def _selected_category_id(self) -> int | None:
        """Return the category chosen by the current tab, if any."""
        index = self.category_tabs.currentIndex()
        if index < 0:
            return None
        value = self.category_tabs.tabData(index)
        return int(value) if value is not None else None

    def _schedule_refresh(self, _text: str) -> None:
        """Debounce service searches while the user is typing."""
        self._search_timer.start()

    def _cell_clicked(self, proxy_index: QModelIndex) -> None:
        """Emit the selected customer's ID when any cell in its row is clicked."""
        if not proxy_index.isValid():
            return
        source_index = self._proxy.mapToSource(proxy_index)
        summary = self._model.summary_at(source_index.row())
        if summary is not None:
            self.customer_selected.emit(summary.id)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt event override
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            indexes = self.table.selectionModel().selectedRows()
            if indexes:
                self._cell_clicked(indexes[0])
                return
        super().keyPressEvent(event)

    def _customer_context_menu(self, position) -> None:
        """Show owner-only customer actions for the row under the pointer."""
        if not self._is_owner:
            return
        proxy_index = self.table.indexAt(position)
        if not proxy_index.isValid():
            return
        self.table.selectRow(proxy_index.row())
        summary = self._summary_from_proxy_row(proxy_index.row())
        if summary is None:
            return
        menu = QMenu(self)
        edit_action = menu.addAction(ar.CUST_EDIT)
        category_action = menu.addAction(ar.CUST_CHANGE_CATEGORY)
        delete_action = menu.addAction(ar.CUST_DELETE)
        selected_action = menu.exec(self.table.viewport().mapToGlobal(position))
        if selected_action == edit_action:
            self._edit_customer(summary)
        elif selected_action == category_action:
            self._change_customer_category(summary)
        elif selected_action == delete_action:
            self._delete_customer(summary)

    def _summary_from_proxy_row(self, proxy_row: int) -> CustomerSummary | None:
        """Map a sorted/filtered table row back to the domain summary."""
        source_index = self._proxy.mapToSource(self._proxy.index(proxy_row, 0))
        return self._model.summary_at(source_index.row())

    def _category_context_menu(self, position) -> None:
        """Show category rename/delete actions on a category tab."""
        if not self._is_owner:
            return
        index = self.category_tabs.tabAt(position)
        if index <= 0:
            return
        category_id = self.category_tabs.tabData(index)
        if category_id is None:
            return
        menu = QMenu(self)
        rename_action = menu.addAction(ar.CUST_RENAME_CATEGORY)
        delete_action = menu.addAction(ar.CUST_DELETE_CATEGORY)
        selected_action = menu.exec(self.category_tabs.mapToGlobal(position))
        if selected_action == rename_action:
            category = self._category_by_id(int(category_id))
            if category is not None:
                self._rename_category(category)
        elif selected_action == delete_action:
            category = self._category_by_id(int(category_id))
            if category is not None:
                self._delete_category(category)

    def _category_by_id(self, category_id: int) -> Category | None:
        return next((item for item, _count in self._category_rows if item.id == category_id), None)

    def _add_customer(self) -> None:
        """Open the customer form and save through the customer service."""
        if not self._category_rows:
            self._show_error(ar.CUST_SAVE_ERROR)
            return
        dialog = _CustomerFormDialog(self._category_rows, None, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        try:
            customer_id = self._save_customer(values, allow_duplicate_phone=False)
        except DuplicateCustomerPhoneError as error:
            names = ", ".join(customer.full_name for customer in error.matches)
            if self._confirm(ar.CUST_DUP_PHONE_CONFIRM + "\n" + names, ar.CUST_ADD, ar.CUST_SAVE):
                try:
                    customer_id = self._save_customer(values, allow_duplicate_phone=True)
                except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
                    self._show_error(ar.CUST_SAVE_ERROR)
                    return
            else:
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
            if self._confirm(ar.CUST_DUP_PHONE_CONFIRM + "\n" + names, ar.CUST_EDIT, ar.CUST_SAVE):
                try:
                    self._update_customer(summary.id, values, allow_duplicate_phone=True)
                except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
                    self._show_error(ar.CUST_SAVE_ERROR)
                    return
            else:
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
                session,
                customer_id,
                allow_duplicate_phone=allow_duplicate_phone,
                **values,
            )

    def _change_customer_category(self, summary: CustomerSummary) -> None:
        """Change the category of a selected customer."""
        dialog = _CategoryChoiceDialog(self._category_rows, summary.category_id, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        category_id = dialog.category.currentData()
        if category_id is None or int(category_id) == summary.category_id:
            return
        try:
            with session_scope() as session:
                self._require_owner(session)
                customers.update_customer(session, summary.id, category_id=int(category_id))
        except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.CUST_SAVE_ERROR)
            return
        self._emit_customer_change(summary.id)

    def _delete_customer(self, summary: CustomerSummary) -> None:
        """Confirm and delete a customer only when the service allows it."""
        if not self._confirm(
            ar.CUST_CONFIRM_DELETE + "\n" + summary.full_name,
            ar.CUST_DELETE,
            ar.CUST_DELETE,
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

    def _add_category(self) -> None:
        """Create a category through the category service."""
        if not self._is_owner:
            return
        from PySide6.QtWidgets import QInputDialog

        name, accepted = QInputDialog.getText(self, ar.CUST_ADD_CATEGORY, ar.CUST_CATEGORY_NAME)
        if not accepted:
            return
        try:
            with session_scope() as session:
                self._require_owner(session)
                categories.create_category(session, name)
        except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.CUST_SAVE_ERROR)
            return
        events.data_changed.emit()

    def _rename_category(self, category: Category) -> None:
        """Rename a category through the category service."""
        from PySide6.QtWidgets import QInputDialog

        name, accepted = QInputDialog.getText(
            self, ar.CUST_RENAME_CATEGORY, ar.CUST_CATEGORY_NAME, text=category.name
        )
        if not accepted:
            return
        try:
            with session_scope() as session:
                self._require_owner(session)
                categories.rename_category(session, category.id, name)
        except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.CUST_SAVE_ERROR)
            return
        events.data_changed.emit()

    def _delete_category(self, category: Category) -> None:
        """Confirm and delete an unused category through the category service."""
        if not self._confirm(
            ar.CUST_CONFIRM_DELETE_CATEGORY + "\n" + category.name,
            ar.CUST_DELETE_CATEGORY,
            ar.CUST_DELETE_CATEGORY,
        ):
            return
        try:
            with session_scope() as session:
                self._require_owner(session)
                categories.delete_category(session, category.id)
        except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.CUST_SAVE_ERROR)
            return
        events.data_changed.emit()

    def _require_owner(self, session: Session) -> None:
        """Enforce current owner status before any write action."""
        if not self._is_owner:
            raise auth.AuthorizationError("Owner role required")
        auth.require_owner(session, self.current_user.id)

    def _emit_customer_change(self, customer_id: int) -> None:
        """Publish page refresh events after a customer mutation commits."""
        events.data_changed.emit()
        events.customer_changed.emit(customer_id)
        events.notify.emit("success", ar.CUSTOMERS_TITLE, "تم تحديث بيانات الزبون بنجاح", 3500)

    def _confirm(self, message: str, title: str, confirm_text: str) -> bool:
        """Show a localized confirmation with explicit button labels."""
        box = QMessageBox(QMessageBox.Icon.Question, title, message, parent=self)
        confirm = box.addButton(confirm_text, QMessageBox.ButtonRole.AcceptRole)
        box.addButton(ar.CUST_CANCEL, QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() == confirm

    def _show_error(self, message: str) -> None:
        """Display a localized page-level validation error."""
        QMessageBox.warning(self, ar.ERROR_TITLE, message)
