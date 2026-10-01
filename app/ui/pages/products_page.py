"""Product catalog: prices, an installment quote, sales count, archive state.

Everyone can browse active products and their selling price (sellers use
them to quote customers). Wholesale prices, margins, editing and archiving
are owner-only, and the product service re-checks the owner role.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
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

from app.db.models import Product, User
from app.db.session import session_scope
from app.i18n import ar
from app.services import auth, calc, products
from app.services.customers import normalize_search_text
from app.services.settings import get_rate_presets
from app.ui import icons
from app.ui.dialogs.auth_dialogs import InlineError
from app.ui.events import events

_LOG = logging.getLogger(__name__)
QUOTE_MONTHS = 6


class ProductDialog(QDialog):
    """Name, wholesale price and selling price of a catalog product."""

    def __init__(self, *, title: str, product: Product | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        heading = QLabel(title, self)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        self.name = QLineEdit(self)
        self.name.setMaxLength(180)
        self.wholesale_price = self._money_input()
        self.cash_price = self._money_input()
        if product is not None:
            self.name.setText(product.name)
            self.wholesale_price.setValue(product.wholesale_price)
            self.cash_price.setValue(product.cash_price)
        form = QFormLayout()
        form.addRow(ar.SET_PRODUCT_NAME, self.name)
        form.addRow(ar.SET_PRODUCT_WHOLESALE, self.wholesale_price)
        form.addRow(ar.SET_PRODUCT_CASH, self.cash_price)
        layout.addLayout(form)
        self.error = InlineError(self)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(self)
        self.save_button = buttons.addButton(ar.USER_SAVE, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(ar.USER_CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setProperty("variant", "secondary")
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self.name.setFocus()

    def _money_input(self) -> QSpinBox:
        editor = QSpinBox(self)
        editor.setRange(0, 2_000_000_000)
        editor.setGroupSeparatorShown(True)
        editor.setSuffix(f" {ar.CURRENCY_SUFFIX}")
        return editor


class ProductsPage(QWidget):
    """Browse and (for the owner) manage the product catalog."""

    COLUMNS_OWNER = ("name", "wholesale", "cash", "margin", "quote", "sold", "last_sold", "state")
    COLUMNS_SELLER = ("name", "cash", "quote", "sold", "state")

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self._is_owner = current_user.role == "owner"
        self._columns = self.COLUMNS_OWNER if self._is_owner else self.COLUMNS_SELLER
        self._rows: list[Product] = []
        self._build_ui()
        events.data_changed.connect(self.refresh)
        self.refresh()

    # ------------------------------------------------------------------ build
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(14)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel(ar.NAV_PRODUCTS, self)
        title.setObjectName("pageTitle")
        self.count_label = QLabel(self)
        self.count_label.setObjectName("pageSubtitle")
        titles.addWidget(title)
        titles.addWidget(self.count_label)
        header.addLayout(titles, 1)
        self.edit_button = self._action(ar.SET_PRODUCT_EDIT, "edit", "secondary")
        self.edit_button.clicked.connect(self._edit_selected)
        self.archive_button = self._action(ar.SET_PRODUCT_ARCHIVE, "tray", "secondary")
        self.archive_button.clicked.connect(self._toggle_selected)
        self.add_button = self._action(ar.SET_PRODUCT_ADD, "plus", None)
        self.add_button.clicked.connect(self._create)
        for button in (self.edit_button, self.archive_button, self.add_button):
            button.setVisible(self._is_owner)
            header.addWidget(button)
        layout.addLayout(header)

        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(ar.PROD_SEARCH)
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icons.icon("search", "text-muted", 16), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._render)
        filters.addWidget(self.search, 1)
        self.state_filter = QComboBox(self)
        self.state_filter.addItem(ar.PROD_FILTER_ACTIVE, "active")
        if self._is_owner:
            self.state_filter.addItem(ar.PROD_FILTER_ARCHIVED, "archived")
            self.state_filter.addItem(ar.PROD_FILTER_ALL, "all")
        self.state_filter.currentIndexChanged.connect(self._render)
        filters.addWidget(self.state_filter)
        layout.addLayout(filters)

        self.table = QTableWidget(0, len(self._columns), self)
        self.table.setHorizontalHeaderLabels([self._header(key) for key in self._columns])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSortingEnabled(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(self._columns)):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
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
        empty_text = QLabel(ar.SET_PRODUCT_EMPTY, self.empty_card)
        empty_text.setObjectName("emptyStateTitle")
        empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_icon)
        empty_layout.addWidget(empty_text)
        layout.addWidget(self.empty_card)
        self.empty_card.hide()
        self._update_actions()

    def _action(self, text: str, icon: str, variant: str | None) -> QPushButton:
        button = QPushButton(text, self)
        if variant:
            button.setProperty("variant", variant)
        button.setIcon(icons.icon(icon, "#FFFFFF" if variant is None else "text", 16))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    @staticmethod
    def _header(key: str) -> str:
        return {
            "name": ar.SET_PRODUCT_NAME,
            "wholesale": ar.SET_PRODUCT_WHOLESALE,
            "cash": ar.SET_PRODUCT_CASH,
            "margin": ar.PROD_COL_MARGIN,
            "quote": ar.PROD_COL_INSTALLMENT.format(months=QUOTE_MONTHS),
            "sold": ar.PROD_COL_SOLD,
            "last_sold": ar.PROD_COL_LAST_SOLD,
            "state": ar.SET_PRODUCT_ACTIVE,
        }[key]

    # ------------------------------------------------------------------ data
    def refresh(self, *_args: object) -> None:
        """Reload the catalog, sales counts and the installment quote rate."""
        try:
            with session_scope() as session:
                catalog = products.list_products(session, include_inactive=self._is_owner)
                self._sold = products.sales_by_product(session)
                self._quote_rate = get_rate_presets(session).get(QUOTE_MONTHS)
        except SQLAlchemyError:
            _LOG.exception("Product catalog refresh failed")
            return
        self._catalog = catalog
        self._render()

    def _visible(self) -> list[Product]:
        state = self.state_filter.currentData()
        key = normalize_search_text(self.search.text())
        result = []
        for product in getattr(self, "_catalog", []):
            if state == "active" and not product.active:
                continue
            if state == "archived" and product.active:
                continue
            if key and key not in normalize_search_text(product.name):
                continue
            result.append(product)
        return sorted(result, key=lambda item: (item.name.casefold(), item.id))

    def _render(self, *_args: object) -> None:
        self._rows = self._visible()
        self.table.setRowCount(len(self._rows))
        for row, product in enumerate(self._rows):
            for column, key in enumerate(self._columns):
                item = QTableWidgetItem(self._cell(product, key))
                if key != "name":
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if key == "state" and not product.active:
                    item.setForeground(self.palette().placeholderText())
                self.table.setItem(row, column, item)
        active = sum(1 for product in getattr(self, "_catalog", []) if product.active)
        self.count_label.setText(ar.PROD_COUNT.format(count=active))
        self.empty_card.setVisible(not self._rows)
        self.table.setVisible(bool(self._rows))
        self._update_actions()

    def _cell(self, product: Product, key: str) -> str:
        if key == "name":
            return product.name
        if key == "wholesale":
            return _money(product.wholesale_price)
        if key == "cash":
            return _money(product.cash_price)
        if key == "margin":
            return _money(product.cash_price - product.wholesale_price)
        if key == "quote":
            return self._quote(product)
        if key == "sold":
            return str(self._sold.get(product.name, (0, None))[0])
        if key == "last_sold":
            last = self._sold.get(product.name, (0, None))[1]
            return last.strftime("%d/%m/%Y") if last else "—"
        return ar.SET_USER_ACTIVE if product.active else ar.SET_PRODUCT_ARCHIVED

    def _quote(self, product: Product) -> str:
        """Monthly amount for a 6-month installment with no down payment."""
        if self._quote_rate is None:
            return "—"
        result = calc.compute_sale(
            cash_price=product.cash_price, rate=self._quote_rate, down_payment=0,
            months=QUOTE_MONTHS, wholesale=product.wholesale_price, sale_type="installment",
        )
        return ar.PROD_INSTALLMENT_VALUE.format(monthly=_money(result.monthly_list[0]))

    # --------------------------------------------------------------- actions
    def selected_product(self) -> Product | None:
        """Return the product on the selected row."""
        row = self.table.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _update_actions(self) -> None:
        product = self.selected_product()
        self.edit_button.setEnabled(product is not None)
        self.archive_button.setEnabled(product is not None)
        if product is not None:
            self.archive_button.setText(
                ar.SET_PRODUCT_ARCHIVE if product.active else ar.SET_PRODUCT_ACTIVATE
            )

    def _create(self) -> None:
        dialog = ProductDialog(title=ar.SET_PRODUCT_ADD, parent=self)
        dialog.save_button.clicked.connect(lambda: self.save_dialog(dialog))
        dialog.exec()

    def _edit_selected(self) -> None:
        product = self.selected_product()
        if product is None or not self._is_owner:
            return
        dialog = ProductDialog(title=ar.SET_PRODUCT_EDIT, product=product, parent=self)
        dialog.save_button.clicked.connect(lambda: self.save_dialog(dialog, product.id))
        dialog.exec()

    def save_dialog(self, dialog: ProductDialog, product_id: int | None = None) -> bool:
        """Create or update from a filled dialog; errors stay inline."""
        values = {
            "name": dialog.name.text(),
            "wholesale_price": dialog.wholesale_price.value(),
            "cash_price": dialog.cash_price.value(),
        }
        try:
            with session_scope() as session:
                if product_id is None:
                    products.create_product(session, self.current_user.id, **values)
                else:
                    products.update_product(session, self.current_user.id, product_id, **values)
        except auth.AuthorizationError:
            dialog.error.show_message(ar.SET_OWNER_ONLY)
            return False
        except (ValueError, SQLAlchemyError):
            dialog.error.show_message(ar.SET_PRODUCT_ERROR)
            return False
        dialog.accept()
        events.data_changed.emit()
        events.notify.emit("success", ar.NAV_PRODUCTS, ar.SET_PRODUCT_SAVED, 3000)
        return True

    def _toggle_selected(self) -> None:
        product = self.selected_product()
        if product is None:
            return
        try:
            with session_scope() as session:
                products.set_product_active(session, self.current_user.id, product.id, not product.active)
        except (ValueError, auth.AuthorizationError, SQLAlchemyError):
            QMessageBox.warning(self, ar.ERROR_TITLE, ar.SET_PRODUCT_ERROR)
            return
        events.data_changed.emit()


def _money(amount: int) -> str:
    return f"{amount:,} {ar.CURRENCY_SUFFIX}"
