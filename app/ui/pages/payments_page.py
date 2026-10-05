"""Payments: one searchable table of every installment and credit balance.

Each row is one amount to collect (an installment, or a credit sale's open
balance) with its status: overdue, due (by the end of this month), upcoming
or paid. Search and filters narrow the table; the status tiles above it count
the rows and filter by status when clicked. Collecting a payment or sending a
WhatsApp reminder happens from the row itself.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QEvent,
    QModelIndex,
    QRectF,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QColor, QDesktopServices, QMouseEvent, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from app.db.models import Sale, User
from app.db.session import session_scope
from app.i18n import ar
from app.i18n.stock_text import ltr_text
from app.services import backup, categories, collections
from app.services import payments as payment_service
from app.services import sales as sale_service
from app.services.collections import CollectionFilter, CollectionRow
from app.services.reminders import whatsapp_link
from app.ui import icons
from app.ui.events import events
from app.ui.widgets import row_selection
from app.ui.widgets.row_selection import SelectionBar

_LOG = logging.getLogger(__name__)
PAGE_NEW_SALE = 2
STATUS_COLORS = {"overdue": "#EF4444", "due": "#F59E0B", "upcoming": "#3B92D9", "paid": "#22C55E"}
COL_STATUS, COL_CUSTOMER, COL_PHONE, COL_PRODUCT, COL_NUMBER, COL_DUE = 0, 1, 2, 3, 4, 5
COL_AMOUNT, COL_PAID, COL_REMAINING, COL_LAST, COL_PAY, COL_REMIND = 6, 7, 8, 9, 10, 11


def _payment_methods() -> tuple[tuple[str, str], ...]:
    """Labels in the active language (evaluated at call time)."""
    return (
        (ar.PAY_METHOD_CASH, "cash"),
        (ar.PAY_METHOD_CCP, "CCP"),
        (ar.PAY_METHOD_BARIDIMOB, "BaridiMob"),
    )


def _status_label(status: str) -> str:
    return {
        "overdue": ar.PAYT_STATUS_OVERDUE,
        "due": ar.PAYT_STATUS_DUE,
        "upcoming": ar.PAYT_STATUS_UPCOMING,
        "paid": ar.PAYT_STATUS_PAID,
    }[status]


class _CollectionModel(QAbstractTableModel):
    """Collection rows for the table; ``UserRole`` holds each cell's sort key."""

    ROW_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[CollectionRow] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 - Qt API
        return 0 if parent.isValid() else 12

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal or role != Qt.ItemDataRole.DisplayRole:
            return None
        return (
            ar.PAYT_COL_STATUS, ar.PAY_COL_CUSTOMER, ar.PAYT_COL_PHONE, ar.PAY_COL_PRODUCT,
            ar.PAYT_COL_NUMBER, ar.PAYT_COL_DUE, ar.PAYT_COL_AMOUNT, ar.PAYT_COL_PAID,
            ar.PAY_COL_REMAINING, ar.PAYT_COL_LAST_PAYMENT, ar.PAYT_COL_PAY, ar.PAYT_COL_REMIND,
        )[section]

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802 - Qt API
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()
        if role == self.ROW_ROLE:
            return row
        if role == Qt.ItemDataRole.DisplayRole:
            return self._text(row, column)
        if role == Qt.ItemDataRole.UserRole:
            return self._sort_key(row, column)
        if role == Qt.ItemDataRole.ForegroundRole:
            if column == COL_STATUS:
                return QColor(STATUS_COLORS[row.status])
            if column == COL_REMAINING and row.remaining > 0:
                return QColor(STATUS_COLORS["overdue"] if row.status == "overdue" else "#EBF0FF")
            return None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column in (COL_CUSTOMER, COL_PRODUCT):
                return int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeading)
            return int(Qt.AlignmentFlag.AlignCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == COL_STATUS and row.status == "overdue":
                return ar.PAYT_DAYS_LATE.format(days=row.days_late)
            return self._text(row, column) or None
        return None

    def row_at(self, row: int) -> CollectionRow | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def replace_rows(self, rows: list[CollectionRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    @staticmethod
    def _text(row: CollectionRow, column: int) -> str:
        if column == COL_STATUS:
            return f"●  {_status_label(row.status)}"
        if column == COL_CUSTOMER:
            return row.customer_name
        if column == COL_PHONE:
            return row.customer_phone or "—"
        if column == COL_PRODUCT:
            return ltr_text(row.product)
        if column == COL_NUMBER:
            if row.kind == "credit":
                return ar.PAYT_CREDIT
            return ar.PAYT_NUMBER.format(number=row.number, count=row.count)
        if column == COL_DUE:
            return _format_date(row.due_date) if row.due_date else ar.PAYT_NO_DATE
        if column == COL_AMOUNT:
            return _format_money(row.amount_due)
        if column == COL_PAID:
            return _format_money(row.paid)
        if column == COL_REMAINING:
            return _format_money(row.remaining)
        if column == COL_LAST:
            return _format_date(row.last_payment_date) if row.last_payment_date else "—"
        return ""

    @staticmethod
    def _sort_key(row: CollectionRow, column: int):
        order = {"overdue": 0, "due": 1, "upcoming": 2, "paid": 3}
        due = row.due_date.toordinal() if row.due_date else date.max.toordinal()
        return {
            COL_STATUS: (order[row.status], -row.days_late, due),
            COL_CUSTOMER: row.customer_name,
            COL_PHONE: row.customer_phone or "",
            COL_PRODUCT: row.product,
            COL_NUMBER: (row.kind, row.number or 0),
            COL_DUE: due,
            COL_AMOUNT: row.amount_due,
            COL_PAID: row.paid,
            COL_REMAINING: row.remaining,
            COL_LAST: row.last_payment_date.toordinal() if row.last_payment_date else 0,
        }.get(column, 0)


class _SortProxy(QSortFilterProxyModel):
    """Sort on the model's ``UserRole`` keys (tuples, numbers and text)."""

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:  # noqa: N802 - Qt API
        a = left.data(Qt.ItemDataRole.UserRole)
        b = right.data(Qt.ItemDataRole.UserRole)
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


class _RowButtonDelegate(QStyledItemDelegate):
    """Draw a small pill button in a cell and call ``on_click`` when it is pressed."""

    def __init__(self, label, color: str, enabled, on_click, parent: QWidget) -> None:
        super().__init__(parent)
        self._label = label
        self._color = color
        self._enabled = enabled
        self._on_click = on_click

    def _row(self, index: QModelIndex) -> CollectionRow | None:
        return index.data(_CollectionModel.ROW_ROLE)

    def _button_rect(self, option: QStyleOptionViewItem) -> QRectF:
        text_width = option.fontMetrics.horizontalAdvance(self._label()) + 26
        width = min(option.rect.width() - 10, text_width)
        return QRectF(
            option.rect.center().x() - width / 2, option.rect.center().y() - 14, width, 28
        )

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        base = QStyleOptionViewItem(option)
        self.initStyleOption(base, index)
        base.text = ""
        style = base.widget.style() if base.widget is not None else self.parent().style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, base, painter, base.widget)
        row = self._row(index)
        if row is None or not self._enabled(row):
            return
        rect = self._button_rect(option)
        color = QColor(self._color)
        fill = QColor(color)
        fill.setAlpha(46)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(color)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 8, 8)
        painter.setPen(QColor("#EBF0FF"))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._label())
        painter.restore()

    def editorEvent(self, event: QEvent, model, option: QStyleOptionViewItem, index: QModelIndex) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.MouseButtonRelease or not isinstance(event, QMouseEvent):
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        row = self._row(index)
        if row is None or not self._enabled(row):
            return False
        if not self._button_rect(option).contains(event.position()):
            return False
        # Run after the click finishes: the action opens a modal dialog.
        QTimer.singleShot(0, lambda: self._on_click(row))
        return True


class _StatusTile(QFrame):
    """A coloured count of one status; clicking it filters the table by that status."""

    clicked = Signal(str)

    def __init__(self, status: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.status = status
        color = STATUS_COLORS[status]
        self.setObjectName("statusTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            f"QFrame#statusTile {{ background-color: #0E141D; border: 1px solid #1F2A3D; "
            f"border-top: 3px solid {color}; border-radius: 12px; }}"
            f"QFrame#statusTile[active='true'] {{ background-color: #121A2C; border-color: {color}; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        self.title = QLabel(_status_label(status), self)
        self.title.setStyleSheet(f"color: {color}; font-weight: 700; font-size: 13px;")
        self.value = QLabel(self)
        self.value.setStyleSheet("color: #EBF0FF; font-weight: 700; font-size: 16px;")
        layout.addWidget(self.title)
        layout.addWidget(self.value)

    def set_values(self, count: int, amount: int) -> None:
        self.value.setText(ar.PAYT_TILE.format(count=count, amount=_format_money(amount)))

    def set_active(self, active: bool) -> None:
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.status)
        super().mouseReleaseEvent(event)


class PaymentsPage(QWidget):
    """One table of every installment and credit balance, with search and filters."""

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self._rows: list[CollectionRow] = []
        self._model = _CollectionModel(self)
        self._proxy = _SortProxy(self)
        self._proxy.setSourceModel(self._model)
        self._build_ui()
        events.data_changed.connect(self.refresh)
        events.payment_changed.connect(self.refresh)
        self.refresh()

    @property
    def _is_owner(self) -> bool:
        return getattr(self.current_user, "role", None) == "owner"

    # ------------------------------------------------------------------ build
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(14)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        heading = QLabel(ar.PAY_PAGE_TITLE, self)
        heading.setObjectName("pageTitle")
        self.count_label = QLabel(self)
        self.count_label.setObjectName("pageSubtitle")
        titles.addWidget(heading)
        titles.addWidget(self.count_label)
        title_row.addLayout(titles, 1)

        self.undo_button = self._tool_button(f"↩️  {ar.PAY_UNDO_LAST}", None, self._undo_last_payment)
        self.undo_button.setVisible(self._is_owner)
        # Everyone due this week (and the late ones) on WhatsApp, one after the other.
        self.remind_week_button = self._tool_button(f"📲  {ar.REMIND_WEEK_BUTTON}", None, self.open_weekly_reminders)
        self.new_sale_button = QPushButton(ar.TOPBAR_NEW_SALE, self)
        self.new_sale_button.setIcon(icons.icon("plus", "#FFFFFF", 16))
        self.new_sale_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_sale_button.clicked.connect(lambda: events.navigate_to.emit(PAGE_NEW_SALE))
        for button in (self.undo_button, self.remind_week_button, self.new_sale_button):
            title_row.addWidget(button)
        layout.addLayout(title_row)

        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self.tiles: dict[str, _StatusTile] = {}
        for status in ("overdue", "due", "upcoming", "paid"):
            tile = _StatusTile(status, self)
            tile.clicked.connect(self._tile_clicked)
            self.tiles[status] = tile
            tiles.addWidget(tile, 1)
        layout.addLayout(tiles)

        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(ar.PAYT_SEARCH)
        self.search.addAction(icons.icon("search", "text-muted", 16), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _text: self._search_timer.start())
        filters.addWidget(self.search, 1)
        self.status_filter = self._filter_combo(filters, ar.PAYT_FILTER_STATUS, (
            (ar.PAYT_ALL, None),
            (ar.PAYT_STATUS_UNPAID, "unpaid"),
            (ar.PAYT_STATUS_OVERDUE, "overdue"),
            (ar.PAYT_STATUS_DUE, "due"),
            (ar.PAYT_STATUS_UPCOMING, "upcoming"),
            (ar.PAYT_STATUS_PAID, "paid"),
        ))
        self.kind_filter = self._filter_combo(filters, ar.PAYT_FILTER_KIND, (
            (ar.PAYT_ALL, None),
            (ar.SALE_INSTALLMENT, "installment"),
            (ar.SALE_CREDIT, "credit"),
        ))
        self.category_filter = self._filter_combo(filters, ar.PAYT_FILTER_CATEGORY, ((ar.PAYT_ALL, None),))
        self.period_filter = self._filter_combo(filters, ar.PAYT_FILTER_PERIOD, (
            (ar.PAYT_PERIOD_UNTIL, "until_this_month"),
            (ar.PAYT_PERIOD_THIS, "this_month"),
            (ar.PAYT_PERIOD_NEXT, "next_month"),
            (ar.PAYT_PERIOD_MONTH, "month"),
            (ar.PAYT_PERIOD_ALL, "all"),
        ))
        self.month_selector = QDateEdit(self)
        self.month_selector.setCalendarPopup(True)
        self.month_selector.setDisplayFormat("MM/yyyy")
        self.month_selector.setDate(QDate.currentDate())
        self.month_selector.setMinimumWidth(120)
        self.month_selector.dateChanged.connect(self._apply_filters)
        filters.addWidget(self.month_selector)
        layout.addLayout(filters)
        # Default view: everything still unpaid up to the end of this month.
        self.status_filter.blockSignals(True)
        self.status_filter.setCurrentIndex(self.status_filter.findData("unpaid"))
        self.status_filter.blockSignals(False)
        self._update_month_selector()

        self.table = QTableView(self)
        self.table.setModel(self._proxy)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setMouseTracking(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(48)
        header.setSectionResizeMode(header.ResizeMode.Interactive)
        for column, width in {
            COL_STATUS: 92, COL_NUMBER: 76, COL_DUE: 100, COL_AMOUNT: 100,
            COL_PAID: 96, COL_REMAINING: 100, COL_PAY: 104, COL_REMIND: 88,
        }.items():
            self.table.setColumnWidth(column, width)
        header.setSectionResizeMode(COL_CUSTOMER, header.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_PRODUCT, header.ResizeMode.Stretch)
        # Phone and last payment stay searchable; the table keeps the columns
        # needed to collect, so it fits a laptop screen.
        self.table.setColumnHidden(COL_PHONE, True)
        self.table.setColumnHidden(COL_LAST, True)
        # Keep the service order (late first) until a column header is clicked.
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.table.setSortingEnabled(True)
        self.pay_delegate = _RowButtonDelegate(
            lambda: ar.PAY_RECORD, STATUS_COLORS["paid"], lambda row: row.remaining > 0,
            self.record_payment, self.table,
        )
        self.remind_delegate = _RowButtonDelegate(
            lambda: ar.PAYT_COL_REMIND, "#25D366",
            lambda row: row.remaining > 0 and bool(row.customer_phone), self.send_reminder, self.table,
        )
        self.table.setItemDelegateForColumn(COL_PAY, self.pay_delegate)
        self.table.setItemDelegateForColumn(COL_REMIND, self.remind_delegate)
        self.table.doubleClicked.connect(self._open_row)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)

        # Owner: a checkbox per row, "select all" and "delete selected".
        self.selection: SelectionBar | None = None
        if self._is_owner:
            self.selection = SelectionBar(self.table, self._key_at, self)
            self.selection.delete_requested.connect(self._delete_selected)
            selection_row = QHBoxLayout()
            selection_row.addWidget(self.selection)
            selection_row.addStretch(1)
            layout.addLayout(selection_row)
        layout.addWidget(self.table, 1)

        self.empty_card = QFrame(self)
        self.empty_card.setObjectName("emptyStateCard")
        empty_layout = QVBoxLayout(self.empty_card)
        empty_layout.setContentsMargins(20, 36, 20, 36)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_icon = QLabel(self.empty_card)
        empty_icon.setPixmap(icons.pixmap("wallet", "text-muted", 32, stroke=1.5))
        empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title = QLabel(ar.PAYT_EMPTY_TITLE, self.empty_card)
        empty_title.setObjectName("emptyStateTitle")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_sub = QLabel(ar.PAYT_EMPTY_SUB, self.empty_card)
        empty_sub.setObjectName("emptyStateSub")
        empty_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        reset = QPushButton(ar.CUST_RESET_FILTERS, self.empty_card)
        reset.setProperty("variant", "secondary")
        reset.setProperty("compact", True)
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.clicked.connect(self.reset_filters)
        for widget in (empty_icon, empty_title, empty_sub):
            empty_layout.addWidget(widget)
        empty_layout.addWidget(reset, alignment=Qt.AlignmentFlag.AlignCenter)
        self.empty_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.empty_card, 1)
        self.empty_card.hide()

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._apply_filters)

    def _tool_button(self, text: str, icon_name: str | None, slot) -> QPushButton:
        button = QPushButton(text, self)
        button.setProperty("variant", "secondary")
        if icon_name:
            button.setIcon(icons.icon(icon_name, "text", 16))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    def _filter_combo(self, row: QHBoxLayout, label: str, items) -> QComboBox:
        caption = QLabel(label, self)
        caption.setObjectName("sectionHint")
        row.addWidget(caption)
        combo = QComboBox(self)
        for text, value in items:
            combo.addItem(text, value)
        combo.currentIndexChanged.connect(self._filters_changed)
        row.addWidget(combo)
        return combo

    # ------------------------------------------------------------------ data
    def refresh(self, *_args: object) -> None:
        """Reload every collection row and the client types, then re-apply the filters."""
        try:
            with session_scope() as session:
                self._rows = collections.collection_rows(session, today=date.today())
                types = categories.list_client_types(session)
        except (ValueError, RuntimeError, SQLAlchemyError):
            _LOG.exception("Payments refresh failed")
            events.notify.emit("error", ar.ERROR_TITLE, ar.ERROR_BODY, 5000)
            return
        selected = self.category_filter.currentData()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem(ar.PAYT_ALL, None)
        for client_type in types:
            self.category_filter.addItem(client_type.name, client_type.id)
        index = self.category_filter.findData(selected) if selected is not None else 0
        self.category_filter.setCurrentIndex(max(0, index))
        self.category_filter.blockSignals(False)
        self._apply_filters()

    def current_filter(self) -> CollectionFilter:
        """The filter chosen in the controls above the table."""
        period = self.period_filter.currentData() or "all"
        return CollectionFilter(
            search=self.search.text(),
            status=self.status_filter.currentData(),
            kind=self.kind_filter.currentData(),
            category_id=self.category_filter.currentData(),
            period=period,
            month=self.month_selector.date().toPython() if period == "month" else None,
        )

    def visible_rows(self) -> list[CollectionRow]:
        """The rows in their on-screen (sorted) order."""
        rows = []
        for proxy_row in range(self._proxy.rowCount()):
            source = self._proxy.mapToSource(self._proxy.index(proxy_row, 0))
            row = self._model.row_at(source.row())
            if row is not None:
                rows.append(row)
        return rows

    def _key_at(self, proxy_row: int) -> tuple[str, int] | None:
        """The checkbox key of a table row: the installment, or the credit sale."""
        row = self._row_at(self._proxy.index(proxy_row, 0))
        return _row_key(row) if row is not None else None

    def checked_rows(self) -> list[CollectionRow]:
        """The rows ticked with their checkbox, in on-screen order."""
        if self.selection is None:
            return []
        wanted = set(self.selection.checked_keys())
        return [row for row in self.visible_rows() if _row_key(row) in wanted]

    def target_rows(self) -> list[CollectionRow]:
        """The ticked rows, or else the highlighted ones."""
        if self.selection is None:
            return []
        wanted = set(self.selection.target_keys())
        return [row for row in self.visible_rows() if _row_key(row) in wanted]

    def show_status(self, status: str | None) -> None:
        """Filter by one status (``overdue``, ``due``, ``unpaid``...); ``None`` for all."""
        index = self.status_filter.findData(status) if status else 0
        self.status_filter.setCurrentIndex(max(0, index))

    def reset_filters(self) -> None:
        """Back to the default view: everything unpaid up to the end of this month."""
        for combo in (self.kind_filter, self.category_filter, self.period_filter):
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._update_month_selector()
        self.show_status("unpaid")
        self._apply_filters()

    def _filters_changed(self, *_args: object) -> None:
        self._update_month_selector()
        self._apply_filters()

    def _update_month_selector(self) -> None:
        self.month_selector.setEnabled(self.period_filter.currentData() == "month")

    def _apply_filters(self, *_args: object) -> None:
        today = date.today()
        wanted = self.current_filter()
        rows = collections.filter_rows(self._rows, wanted, today=today)
        self._model.replace_rows(rows)
        # The tiles count every status within the other filters, so a click
        # on one of them shows what it counts.
        others = collections.filter_rows(
            self._rows,
            CollectionFilter(wanted.search, None, wanted.kind, wanted.category_id, wanted.period, wanted.month),
            today=today,
        )
        summary = collections.totals(others)
        for status, tile in self.tiles.items():
            tile.set_values(getattr(summary, f"{status}_count"), getattr(summary, f"{status}_amount"))
            tile.set_active(wanted.status == status)
        self.count_label.setText(ar.PAYT_ROWS.format(
            count=len(rows), remaining=_format_money(collections.totals(rows).remaining),
        ))
        empty = not rows
        self.empty_card.setVisible(empty)
        self.table.setVisible(not empty)
        if self.selection is not None:
            self.selection.refresh()

    def _tile_clicked(self, status: str) -> None:
        self.show_status(None if self.status_filter.currentData() == status else status)

    # --------------------------------------------------------------- actions
    def record_payment(self, row: CollectionRow) -> None:
        """Collect a payment on an installment or a credit balance."""
        if row.remaining <= 0:
            return
        if row.kind == "credit":
            self._record_credit_payment(row.sale_id, row.remaining)
        elif row.installment_id is not None:
            self._record_installment_payment(row)

    def open_weekly_reminders(self) -> None:
        """The week's WhatsApp reminder list."""
        from app.ui.dialogs.reminders_dialog import WeeklyRemindersDialog

        WeeklyRemindersDialog(self.current_user, self).exec()
        self.refresh()

    def send_reminder(self, row: CollectionRow) -> None:
        """Open WhatsApp with a drafted reminder for the operator to review and send."""
        if not row.customer_phone:
            QMessageBox.information(self, ar.PAY_PAGE_TITLE, ar.PAYT_NO_PHONE)
            return
        message = ar.PAYT_REMINDER.format(
            customer=row.customer_name,
            amount=f"{row.remaining:,}",
            due=_format_date(row.due_date) if row.due_date else ar.PAYT_NO_DATE,
        )
        link = whatsapp_link(row.customer_phone, message)
        if link is not None:
            QDesktopServices.openUrl(QUrl(link))

    def _row_at(self, proxy_index: QModelIndex) -> CollectionRow | None:
        if not proxy_index.isValid():
            return None
        return self._model.row_at(self._proxy.mapToSource(proxy_index).row())

    def _open_row(self, proxy_index: QModelIndex) -> None:
        if proxy_index.column() in (COL_PAY, COL_REMIND):
            return
        row = self._row_at(proxy_index)
        if row is not None:
            events.open_customer.emit(row.customer_id)

    def _context_menu(self, position) -> None:
        row = self._row_at(self.table.indexAt(position))
        if row is None:
            return
        menu = QMenu(self)
        pay = menu.addAction(icons.icon("cash", "text", 16), ar.PAY_RECORD)
        pay.setEnabled(row.remaining > 0)
        pay.triggered.connect(lambda: self.record_payment(row))
        remind = menu.addAction(icons.icon("message", "text", 16), ar.PAY_REMINDERS_ACTION)
        remind.setEnabled(row.remaining > 0 and bool(row.customer_phone))
        remind.triggered.connect(lambda: self.send_reminder(row))
        if row.kind == "credit":
            history = menu.addAction(icons.icon("clock", "text", 16), ar.PAY_COL_HISTORY)
            history.triggered.connect(lambda: self._show_credit_history(row.sale_id))
        menu.addSeparator()
        open_client = menu.addAction(icons.icon("user", "text", 16), ar.PAYT_OPEN_CUSTOMER)
        open_client.triggered.connect(lambda: events.open_customer.emit(row.customer_id))
        if self.selection is not None:
            # The ticked/highlighted rows when the clicked row is one of them, else just this row.
            targets = self.target_rows()
            if _row_key(row) not in {_row_key(item) for item in targets}:
                targets = [row]
            menu.addSeparator()
            delete_text = ar.SEL_DELETE_BUTTON if len(targets) == 1 else f"{ar.SEL_DELETE} ({len(targets)})"
            delete = menu.addAction(icons.icon("trash", "failed", 16), delete_text)
            delete.triggered.connect(lambda: self._delete_rows(targets))
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _record_installment_payment(self, row: CollectionRow) -> None:
        """Collect any amount on an installment sale (oldest unpaid installment first).

        The client may pay more or less than the installment, up to what is
        left on the whole sale.
        """
        sale_remaining = sum(
            item.remaining for item in self._rows if item.kind == "installment" and item.sale_id == row.sale_id
        )
        sale_remaining = max(sale_remaining, row.remaining)
        dialog = PaymentDialog(sale_remaining, suggested=row.remaining, parent=self)
        dialog.setWindowTitle(ar.PAY_DIALOG_TITLE)
        dialog.set_hint(ar.PAY_FLEXIBLE_HINT.format(remaining=_format_money(sale_remaining)))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        amount, paid_date, method, note = dialog.values()
        try:
            with session_scope() as session:
                recorded = payment_service.record_sale_payment_for_user(
                    session, int(self.current_user.id), row.sale_id, amount, paid_date, method, note,
                )
                payment_id = recorded[-1].id
        except (PermissionError, ValueError, RuntimeError, SQLAlchemyError):
            QMessageBox.warning(self, ar.PAY_PAGE_TITLE, ar.PAY_SAVE_ERROR)
            return
        self._payment_saved(payment_id)

    def _record_credit_payment(self, sale_id: int, remaining: int) -> None:
        """Open the payment dialog and persist a credit-sale collection."""
        dialog = PaymentDialog(remaining, parent=self)
        dialog.setWindowTitle(ar.PAY_CREDIT_DIALOG_TITLE)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        amount, paid_date, method, note = dialog.values()
        try:
            with session_scope() as session:
                payment = payment_service.record_credit_payment_for_user(
                    session, int(self.current_user.id), sale_id, amount, paid_date, method, note,
                )
                payment_id = payment.id
        except (PermissionError, ValueError, RuntimeError, SQLAlchemyError):
            QMessageBox.warning(self, ar.PAY_PAGE_TITLE, ar.PAY_SAVE_ERROR)
            return
        self._payment_saved(payment_id)

    def _payment_saved(self, payment_id: int) -> None:
        """Notify dependent screens and confirm the collection."""
        events.payment_changed.emit(payment_id)
        events.data_changed.emit()
        events.notify.emit("success", ar.PAY_PAGE_TITLE, ar.PAY_SAVE_DONE, 4000)

    def _undo_last_payment(self) -> None:
        """Undo the latest payment with explicit confirmation and a reason."""
        if not self._is_owner:
            return
        answer = QMessageBox.question(
            self,
            ar.PAY_UNDO_LAST,
            ar.PAY_UNDO_CONFIRM,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        reason, accepted = QInputDialog.getMultiLineText(self, ar.PAY_UNDO_LAST, ar.PAY_UNDO_REASON)
        if not accepted or not reason.strip():
            QMessageBox.warning(self, ar.PAY_UNDO_LAST, ar.PAY_UNDO_ERROR)
            return
        try:
            with session_scope() as session:
                audit = payment_service.undo_last_payment(session, int(self.current_user.id), reason)
                payment_id = audit.entity_id
        except (PermissionError, ValueError, RuntimeError, SQLAlchemyError):
            QMessageBox.warning(self, ar.PAY_UNDO_LAST, ar.PAY_UNDO_ERROR)
            return
        events.payment_changed.emit(payment_id)
        events.data_changed.emit()
        events.notify.emit("warning", ar.PAY_UNDO_LAST, ar.PAY_UNDO_DONE, 4000)

    def _delete_selected(self) -> None:
        """Delete from the ticked rows (or the highlighted ones)."""
        self._delete_rows(self.target_rows())

    def _delete_rows(self, rows: list[CollectionRow]) -> None:
        """Ask what to delete from some rows: their paid amounts, or their whole sales."""
        if not rows or not self._is_owner:
            return
        paid = sum(row.paid for row in rows)
        sale_ids = list(dict.fromkeys(row.sale_id for row in rows))
        choices: list[tuple[str, str]] = []
        details: list[str] = []
        if paid > 0:
            choices.append(("payments", ar.PAYT_DELETE_PAYMENTS))
            details.append(ar.PAYT_DELETE_EXPLAIN_PAYMENTS.format(amount=_format_money(paid)))
        choices.append(("sales", ar.PAYT_DELETE_SALES))
        details.append(ar.PAYT_DELETE_EXPLAIN_SALES.format(count=len(sale_ids)))
        details.append(ar.SEL_BACKUP_NOTE)
        answer = row_selection.ask_delete(
            self, ar.PAYT_DELETE_TITLE, ar.PAYT_DELETE_QUESTION.format(count=len(rows)), choices,
            details="\n".join(details),
        )
        if answer == "payments":
            self.delete_paid_amounts(rows)
        elif answer == "sales":
            self.delete_sales(sale_ids)

    def delete_paid_amounts(self, rows: list[CollectionRow]) -> int | None:
        """Cancel the payments recorded on some rows; the amounts become unpaid again."""
        backup.create_safety_backup()
        try:
            with session_scope() as session:
                removed = payment_service.delete_payments(
                    session, int(self.current_user.id),
                    installment_ids=[
                        row.installment_id for row in rows
                        if row.kind == "installment" and row.installment_id is not None and row.paid > 0
                    ],
                    credit_sale_ids=[row.sale_id for row in rows if row.kind == "credit" and row.paid > 0],
                )
        except (PermissionError, ValueError, RuntimeError, SQLAlchemyError):
            _LOG.exception("Payment delete failed")
            QMessageBox.warning(self, ar.PAYT_DELETE_TITLE, ar.PAYT_DELETE_ERROR)
            return None
        self._deleted(ar.PAYT_DELETED_PAYMENTS.format(count=removed))
        return removed

    def delete_sales(self, sale_ids: list[int]) -> int | None:
        """Delete whole sales (all their installments and payments)."""
        backup.create_safety_backup()
        try:
            with session_scope() as session:
                removed = sale_service.delete_sales(session, int(self.current_user.id), sale_ids)
        except (PermissionError, ValueError, RuntimeError, SQLAlchemyError):
            _LOG.exception("Sale delete failed")
            QMessageBox.warning(self, ar.PAYT_DELETE_TITLE, ar.PAYT_DELETE_ERROR)
            return None
        self._deleted(ar.PAYT_DELETED_SALES.format(count=removed))
        return removed

    def _deleted(self, message: str) -> None:
        if self.selection is not None:
            self.selection.clear()
        events.data_changed.emit()
        events.notify.emit("warning", ar.PAY_PAGE_TITLE, message, 4000)

    def _show_credit_history(self, sale_id: int) -> None:
        """Show payment dates, amounts, methods, and notes for one credit sale."""
        with session_scope() as session:
            sale = session.scalar(
                select(Sale).where(Sale.id == sale_id).options(selectinload(Sale.credit_payments))
            )
            payments = tuple(
                sorted(sale.credit_payments, key=lambda payment: (payment.payment_date, payment.id or 0))
            ) if sale is not None else ()
        dialog = QDialog(self)
        dialog.setWindowTitle(ar.PAY_HISTORY_TITLE)
        layout = QVBoxLayout(dialog)
        history = QListWidget(dialog)
        if not payments:
            history.addItem(ar.PAY_HISTORY_EMPTY)
        for payment in payments:
            label = ar.PAY_HISTORY_ITEM.format(
                date=_format_date(payment.payment_date),
                amount=_format_money(payment.amount),
                method=_method_label(payment.method),
            )
            history.addItem(f"{label} — {payment.note}" if payment.note else label)
        layout.addWidget(history)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(ar.PAY_CANCEL)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(500, 360)
        dialog.exec()


class PaymentDialog(QDialog):
    """Collect a whole-dinar amount, payment date, method, and optional note."""

    def __init__(self, remaining: int, suggested: int | None = None, parent: QWidget | None = None) -> None:
        """``remaining`` is the most that can be paid; ``suggested`` fills the amount (default: all)."""
        super().__init__(parent)
        self.amount_input = QSpinBox(self)
        self.amount_input.setRange(1, max(1, remaining))
        self.amount_input.setValue(max(1, min(remaining, suggested if suggested is not None else remaining)))
        self.amount_input.setGroupSeparatorShown(True)
        self.hint = QLabel(self)
        self.hint.setObjectName("sectionHint")
        self.hint.setWordWrap(True)
        self.hint.hide()

        self.date_input = QDateEdit(self)
        self.date_input.setCalendarPopup(True)
        self.date_input.setDisplayFormat("dd/MM/yyyy")
        self.date_input.setDate(QDate.currentDate())

        self.method_input = QComboBox(self)
        for label, value in _payment_methods():
            self.method_input.addItem(label, value)
        self.note_input = QLineEdit(self)

        layout = QFormLayout(self)
        layout.addRow(ar.PAY_AMOUNT, self.amount_input)
        layout.addRow("", self.hint)
        layout.addRow(ar.PAY_DATE, self.date_input)
        layout.addRow(ar.PAY_METHOD, self.method_input)
        layout.addRow(ar.PAY_NOTE, self.note_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(ar.PAY_SAVE)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(ar.PAY_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.setWindowTitle(ar.PAY_DIALOG_TITLE)

    def set_hint(self, text: str) -> None:
        """Show a line under the amount (what can be paid and how it is applied)."""
        self.hint.setText(text)
        self.hint.setVisible(bool(text))

    def values(self) -> tuple[int, date, str, str | None]:
        """Return the dialog values in the payment service's expected format."""
        return (
            self.amount_input.value(),
            self.date_input.date().toPython(),
            str(self.method_input.currentData()),
            self.note_input.text().strip() or None,
        )


def _row_key(row: CollectionRow) -> tuple[str, int]:
    """A row's stable identity: its installment, or its credit sale."""
    if row.kind == "credit" or row.installment_id is None:
        return ("credit", row.sale_id)
    return ("installment", row.installment_id)


def _format_money(amount: int) -> str:
    """Format an integer dinar amount with Western-digit grouping."""
    return f"{amount:,} {ar.CURRENCY_SUFFIX}"


def _format_date(value: date | datetime | None) -> str:
    """Format dates using the app's documented day/month/year order."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime("%d/%m/%Y")


def _method_label(method: str) -> str:
    """Map stored method codes to the localized label."""
    for label, value in _payment_methods():
        if method.casefold() == value.casefold():
            return label
    return method
