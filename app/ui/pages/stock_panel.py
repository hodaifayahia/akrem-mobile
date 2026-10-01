"""Stock tab of the Products page: one row per phone, like the shop's Excel sheet.

Columns follow "Gros et Détail": product, wholesale, cash price, battery,
colour, IMEI, REF, notes, plus the sale status and the client it was sold
to. Filters: status, product, colour and condition (new / used / unknown
battery / battery under 85 %). The owner adds, edits and removes phones;
sellers see the list without wholesale prices.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import StockItem, User
from app.db.session import session_scope
from app.i18n import ar
from app.i18n.stock_text import battery_text, ltr_text, status_text
from app.services import auth, products, stock
from app.ui import icons
from app.ui.dialogs.auth_dialogs import InlineError
from app.ui.events import events
from app.ui.theme import qcolor, repolish

_LOG = logging.getLogger(__name__)


class StockUnitDialog(QDialog):
    """Add or edit one phone: product, prices, battery, colour, IMEI, REF, note."""

    def __init__(
        self,
        *,
        title: str,
        catalog: list[tuple[int, str, int, int]],
        colors: list[str],
        unit: StockItem | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """``catalog`` holds (product id, name, cash price, wholesale price)."""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        self._catalog = {name: (product_id, cash, wholesale) for product_id, name, cash, wholesale in catalog}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        heading = QLabel(title, self)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        hint = QLabel(ar.PROD_REQUIRED_HINT, self)
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.product = QComboBox(self)
        self.product.setEditable(True)
        self.product.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.product.addItems([name for _id, name, _cash, _wholesale in catalog])
        self.product.setCurrentIndex(-1)
        self.product_state = QLabel(self)
        self.product_state.setObjectName("sectionHint")
        self.cash_price = _money_input(self)
        self.wholesale_price = _money_input(self)
        self.wholesale_price.setSpecialValueText(ar.PROD_OPTIONAL)
        self.is_new = QCheckBox(ar.STOCK_IS_NEW, self)
        self.battery = QSpinBox(self)
        self.battery.setRange(-1, 100)
        self.battery.setSuffix(" %")
        self.battery.setSpecialValueText(ar.STOCK_BATTERY_UNKNOWN)
        self.battery.setValue(-1)
        self.is_new.toggled.connect(lambda checked: self.battery.setEnabled(not checked))
        self.color = QComboBox(self)
        self.color.setEditable(True)
        self.color.addItems(colors)
        self.color.setCurrentText("")
        self.imei = QLineEdit(self)
        self.imei.setMaxLength(40)
        self.imei.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.reference = QLineEdit(self)
        self.reference.setMaxLength(40)
        self.reference.setPlaceholderText(ar.STOCK_REF_AUTO)
        self.reference.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.note = QLineEdit(self)
        self.note.setMaxLength(500)
        self.quantity = QSpinBox(self)
        self.quantity.setRange(1, 50)

        form = QFormLayout()
        form.addRow(f"{ar.STOCK_PRODUCT} *", self.product)
        form.addRow("", self.product_state)
        form.addRow(f"{ar.SET_PRODUCT_CASH} *", self.cash_price)
        form.addRow(f"{ar.SET_PRODUCT_WHOLESALE} ({ar.PROD_OPTIONAL})", self.wholesale_price)
        form.addRow("", self.is_new)
        form.addRow(ar.STOCK_BATTERY, self.battery)
        form.addRow(ar.PROD_TPL_COLOR, self.color)
        form.addRow(ar.PROD_TPL_IMEI, self.imei)
        form.addRow(ar.PROD_TPL_REF, self.reference)
        form.addRow(ar.PROD_TPL_NOTE, self.note)
        form.addRow(ar.STOCK_QUANTITY, self.quantity)
        self._form = form
        layout.addLayout(form)
        self.error = InlineError(self)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(self)
        self.save_button = buttons.addButton(ar.USER_SAVE, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(ar.USER_CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setProperty("variant", "secondary")
        repolish(cancel)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self.product.currentTextChanged.connect(self._product_changed)

        if unit is not None:
            self.product.setCurrentText(unit.product.name)
            self.cash_price.setValue(unit.cash_price)
            self.wholesale_price.setValue(unit.wholesale_price)
            self.is_new.setChecked(unit.is_new)
            self.battery.setValue(-1 if unit.battery_health is None else unit.battery_health)
            self.color.setCurrentText(unit.color or "")
            self.imei.setText(unit.imei or "")
            self.reference.setText(unit.reference or "")
            self.note.setText(unit.note or "")
            form.setRowVisible(self.quantity, False)
        self._product_changed(self.product.currentText())

    def _product_changed(self, text: str) -> None:
        """Fill the catalog prices and say whether the name exists or will be created."""
        name = text.strip()
        known = None
        if name:
            key = products.name_key(name)
            known = next((value for label, value in self._catalog.items() if products.name_key(label) == key), None)
        if known is not None:
            _product_id, cash, wholesale = known
            if self.cash_price.value() == 0:
                self.cash_price.setValue(cash)
            if self.wholesale_price.value() == 0:
                self.wholesale_price.setValue(wholesale)
            self.product_state.setText(ar.SALE_PRODUCT_EXISTS)
        else:
            self.product_state.setText(ar.SALE_PRODUCT_NEW if name else "")
        self.error.clear()

    def details(self) -> stock.UnitDetails:
        """The phone's descriptive fields."""
        battery = self.battery.value()
        return stock.UnitDetails(
            color=self.color.currentText(),
            battery_health=None if battery < 0 or self.is_new.isChecked() else battery,
            is_new=self.is_new.isChecked(),
            imei=self.imei.text().strip() or None,
            reference=self.reference.text().strip() or None,
            note=self.note.text(),
        )

    def missing_field_message(self) -> str | None:
        """The first missing required field, or None."""
        if not self.product.currentText().strip():
            return ar.STOCK_PRODUCT_REQUIRED
        if self.cash_price.value() <= 0:
            return ar.PROD_PRICE_REQUIRED
        if self.imei.text().strip() and stock.normalize_imei(self.imei.text()) is None:
            return ar.STOCK_IMEI_INVALID
        return None


class StockPanel(QWidget):
    """The stock list with its filters and owner actions."""

    counts_changed = Signal(int, int)  # available, sold

    COLUMNS_OWNER = ("reference", "product", "wholesale", "cash", "battery", "color", "imei", "note", "status", "sold_to")
    COLUMNS_SELLER = ("reference", "product", "cash", "battery", "color", "imei", "note", "status", "sold_to")

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self._is_owner = current_user.role == "owner"
        self._columns = self.COLUMNS_OWNER if self._is_owner else self.COLUMNS_SELLER
        self._units: list[StockItem] = []
        self.last_counts = (0, 0)
        self._build()
        self.refresh()

    # ----------------------------------------------------------------- build
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)

        tools = QHBoxLayout()
        tools.setSpacing(10)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(ar.STOCK_SEARCH)
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icons.icon("search", "text-muted", 16), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self.refresh)
        tools.addWidget(self.search, 1)
        self.add_button = _action(self, ar.STOCK_ADD, "plus", None)
        self.add_button.clicked.connect(self._add)
        self.edit_button = _action(self, ar.STOCK_EDIT, "edit", "secondary")
        self.edit_button.clicked.connect(self._edit_selected)
        self.delete_button = _action(self, ar.STOCK_DELETE, "trash", "secondary")
        self.delete_button.clicked.connect(self._delete_selected)
        for button in (self.delete_button, self.edit_button, self.add_button):
            button.setVisible(self._is_owner)
            tools.addWidget(button)
        layout.addLayout(tools)

        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.status_filter = _combo(self, (
            (ar.STOCK_FILTER_AVAILABLE, stock.STATUS_AVAILABLE),
            (ar.STOCK_FILTER_SOLD, stock.STATUS_SOLD),
            (ar.STOCK_FILTER_ANY_STATUS, None),
        ))
        self.product_filter = _combo(self, ((ar.STOCK_FILTER_ANY_PRODUCT, None),))
        self.color_filter = _combo(self, ((ar.STOCK_FILTER_ANY_COLOR, None),))
        self.condition_filter = _combo(self, (
            (ar.STOCK_FILTER_ANY_CONDITION, None),
            (ar.STOCK_FILTER_NEW, stock.CONDITION_NEW),
            (ar.STOCK_FILTER_USED, stock.CONDITION_USED),
            (ar.STOCK_FILTER_LOW_BATTERY, stock.CONDITION_LOW_BATTERY),
            (ar.STOCK_FILTER_UNKNOWN, stock.CONDITION_UNKNOWN),
        ))
        for combo in (self.status_filter, self.product_filter, self.color_filter, self.condition_filter):
            combo.currentIndexChanged.connect(self.refresh)
            filters.addWidget(combo)
        filters.addStretch(1)
        layout.addLayout(filters)

        self.table = QTableWidget(0, len(self._columns), self)
        self.table.setHorizontalHeaderLabels([self._header(key) for key in self._columns])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self._columns.index("product"), QHeaderView.ResizeMode.Stretch)
        note_column = self._columns.index("note")
        header.setSectionResizeMode(note_column, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(note_column, 170)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.itemSelectionChanged.connect(self._update_actions)
        if self._is_owner:
            self.table.itemDoubleClicked.connect(lambda _item: self._edit_selected())
        layout.addWidget(self.table, 1)

        self.empty_card = QFrame(self)
        self.empty_card.setObjectName("emptyStateCard")
        empty_layout = QVBoxLayout(self.empty_card)
        empty_layout.setContentsMargins(20, 36, 20, 36)
        empty_icon = QLabel(self.empty_card)
        empty_icon.setPixmap(icons.pixmap("tag", "text-muted", 32, stroke=1.5))
        empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_text = QLabel(ar.STOCK_EMPTY, self.empty_card)
        empty_text.setObjectName("emptyStateTitle")
        empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_text.setWordWrap(True)
        empty_layout.addWidget(empty_icon)
        empty_layout.addWidget(empty_text)
        layout.addWidget(self.empty_card)
        self.empty_card.hide()
        self._update_actions()

    @staticmethod
    def _header(key: str) -> str:
        return {
            "reference": ar.PROD_TPL_REF,
            "product": ar.STOCK_PRODUCT,
            "wholesale": ar.SET_PRODUCT_WHOLESALE,
            "cash": ar.SET_PRODUCT_CASH,
            "battery": ar.PROD_TPL_BATTERY,
            "color": ar.PROD_TPL_COLOR,
            "imei": ar.PROD_TPL_IMEI,
            "note": ar.PROD_TPL_NOTE,
            "status": ar.STOCK_COL_STATUS,
            "sold_to": ar.STOCK_COL_SOLD_TO,
        }[key]

    # ------------------------------------------------------------------ data
    def refresh(self, *_args: object) -> None:
        """Reload units for the current filters (and the filter choices)."""
        try:
            with session_scope() as session:
                self._fill_choices(session)
                self._units = stock.list_units(
                    session,
                    status=self.status_filter.currentData(),
                    product_id=self.product_filter.currentData(),
                    color=self.color_filter.currentData(),
                    condition=self.condition_filter.currentData(),
                    search=self.search.text(),
                )
                counts = stock.stock_counts(session)
                for unit in self._units:  # load lazily-read attributes inside the session
                    _ = unit.product.name, unit.sale.customer.full_name if unit.sale else None
        except SQLAlchemyError:
            _LOG.exception("Stock refresh failed")
            return
        self._render()
        available = sum(pair[0] for pair in counts.values())
        sold = sum(pair[1] for pair in counts.values())
        self.last_counts = (available, sold)
        self.counts_changed.emit(available, sold)

    def _fill_choices(self, session) -> None:
        """Keep the product and colour filters in step with the data, preserving the choice."""
        for combo, values in (
            (self.product_filter, [(product.name, product.id) for product in products.list_products(session, include_inactive=True)]),
            (self.color_filter, [(color, color) for color in stock.colors(session)]),
        ):
            current = combo.currentData()
            combo.blockSignals(True)
            while combo.count() > 1:
                combo.removeItem(1)
            for label, data in values:
                combo.addItem(label, data)
            index = combo.findData(current) if current is not None else 0
            combo.setCurrentIndex(max(0, index))
            combo.blockSignals(False)

    def _render(self) -> None:
        self.table.setRowCount(len(self._units))
        for row, unit in enumerate(self._units):
            for column, key in enumerate(self._columns):
                item = QTableWidgetItem(self._cell(unit, key))
                if key not in ("product", "note", "sold_to"):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if key in ("note", "product"):
                    item.setToolTip(item.text())
                if key == "status":
                    item.setForeground(qcolor("pending" if unit.status == stock.STATUS_SOLD else "paid"))
                if key == "battery" and unit.battery_health is not None and unit.battery_health < stock.LOW_BATTERY_BELOW:
                    item.setForeground(qcolor("failed"))
                self.table.setItem(row, column, item)
        self.empty_card.setVisible(not self._units)
        self.table.setVisible(bool(self._units))
        self._update_actions()

    @staticmethod
    def _cell(unit: StockItem, key: str) -> str:
        if key == "reference":
            return ltr_text(unit.reference) if unit.reference else "—"
        if key == "product":
            return ltr_text(unit.product.name)
        if key == "wholesale":
            return _money(unit.wholesale_price) if unit.wholesale_price else "—"
        if key == "cash":
            return _money(unit.cash_price)
        if key == "battery":
            return battery_text(unit.is_new, unit.battery_health)
        if key == "color":
            return ltr_text(unit.color) if unit.color else "—"
        if key == "imei":
            return ltr_text(unit.imei) if unit.imei else "—"
        if key == "note":
            return unit.note or ""
        if key == "status":
            return status_text(unit.status)
        sale = unit.sale
        return sale.customer.full_name if sale is not None else ""

    # --------------------------------------------------------------- actions
    def selected_unit(self) -> StockItem | None:
        """The unit on the selected row."""
        selected = self.table.selectionModel().selectedRows()
        row = selected[0].row() if selected else self.table.currentRow()
        return self._units[row] if 0 <= row < len(self._units) else None

    def _update_actions(self) -> None:
        unit = self.selected_unit()
        self.edit_button.setEnabled(unit is not None)
        self.delete_button.setEnabled(unit is not None and unit.status == stock.STATUS_AVAILABLE)

    def new_dialog(self, unit: StockItem | None = None) -> StockUnitDialog:
        """Build the add/edit dialog with the current catalog and colours."""
        with session_scope() as session:
            catalog = [
                (product.id, product.name, product.cash_price, product.wholesale_price)
                for product in products.list_products(session)
            ]
            colors = stock.colors(session)
            if unit is not None:
                unit = session.get(StockItem, unit.id)
                _ = unit.product.name
        title = ar.STOCK_EDIT if unit is not None else ar.STOCK_ADD
        return StockUnitDialog(title=title, catalog=catalog, colors=colors, unit=unit, parent=self)

    def _add(self) -> None:
        dialog = self.new_dialog()
        dialog.save_button.clicked.connect(lambda: self.save_dialog(dialog))
        dialog.exec()

    def _edit_selected(self) -> None:
        unit = self.selected_unit()
        if unit is None or not self._is_owner:
            return
        dialog = self.new_dialog(unit)
        dialog.save_button.clicked.connect(lambda: self.save_dialog(dialog, unit.id))
        dialog.exec()

    def save_dialog(self, dialog: StockUnitDialog, unit_id: int | None = None) -> bool:
        """Add or update from a filled dialog; the product is linked by name or created."""
        missing = dialog.missing_field_message()
        if missing is not None:
            dialog.error.show_message(missing)
            return False
        try:
            with session_scope() as session:
                product, _created = products.find_or_create_product(
                    session, self.current_user.id, dialog.product.currentText(),
                    cash_price=dialog.cash_price.value(), wholesale_price=dialog.wholesale_price.value(),
                )
                if unit_id is None:
                    stock.add_units(
                        session, self.current_user.id, product_id=product.id, details=dialog.details(),
                        cash_price=dialog.cash_price.value(), wholesale_price=dialog.wholesale_price.value(),
                        quantity=dialog.quantity.value(),
                    )
                else:
                    stock.update_unit(
                        session, self.current_user.id, unit_id, details=dialog.details(),
                        cash_price=dialog.cash_price.value(), wholesale_price=dialog.wholesale_price.value(),
                        product_id=product.id,
                    )
        except auth.AuthorizationError:
            dialog.error.show_message(ar.SET_OWNER_ONLY)
            return False
        except stock.StockError as error:
            dialog.error.show_message(ar.STOCK_IMEI_TAKEN if error.code == "imei_taken" else ar.STOCK_REF_TAKEN)
            return False
        except (ValueError, SQLAlchemyError):
            _LOG.exception("Stock unit save failed")
            dialog.error.show_message(ar.STOCK_ERROR)
            return False
        dialog.accept()
        events.data_changed.emit()
        events.notify.emit("success", ar.PROD_TAB_STOCK, ar.STOCK_SAVED, 3000)
        return True

    def _delete_selected(self) -> None:
        unit = self.selected_unit()
        if unit is None:
            return
        answer = QMessageBox.question(self, ar.STOCK_DELETE, ar.STOCK_DELETE_CONFIRM)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.delete_unit(unit.id)

    def delete_unit(self, unit_id: int) -> bool:
        """Remove an unsold unit."""
        try:
            with session_scope() as session:
                stock.delete_unit(session, self.current_user.id, unit_id)
        except stock.StockError:
            QMessageBox.warning(self, ar.ERROR_TITLE, ar.STOCK_SOLD_LOCKED)
            return False
        except (ValueError, auth.AuthorizationError, SQLAlchemyError):
            QMessageBox.warning(self, ar.ERROR_TITLE, ar.STOCK_ERROR)
            return False
        events.data_changed.emit()
        return True


def _combo(parent: QWidget, items: tuple[tuple[str, object], ...]) -> QComboBox:
    combo = QComboBox(parent)
    for label, data in items:
        combo.addItem(label, data)
    return combo


def _action(parent: QWidget, text: str, icon: str, variant: str | None) -> QPushButton:
    button = QPushButton(text, parent)
    if variant:
        button.setProperty("variant", variant)
    button.setIcon(icons.icon(icon, "#FFFFFF" if variant is None else "text", 16))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _money_input(parent: QWidget) -> QSpinBox:
    editor = QSpinBox(parent)
    editor.setRange(0, 2_000_000_000)
    editor.setGroupSeparatorShown(True)
    editor.setSuffix(f" {ar.CURRENCY_SUFFIX}")
    return editor


def _money(amount: int) -> str:
    return f"{amount:,} {ar.CURRENCY_SUFFIX}"
