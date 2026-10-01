"""Product catalog: prices, an installment quote, sales count, archive state.

Everyone can browse active products and their selling price (sellers use
them to quote customers). Wholesale prices, margins, editing, archiving and
the Excel template/upload are owner-only, and the product service re-checks
the owner role. A product needs only a name and a selling price.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
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
from app.ui.theme import qcolor, repolish

_LOG = logging.getLogger(__name__)
QUOTE_MONTHS = 6


class ProductDialog(QDialog):
    """Name and selling price (required) plus an optional wholesale price."""

    def __init__(self, *, title: str, product: Product | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        heading = QLabel(title, self)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        hint = QLabel(ar.PROD_REQUIRED_HINT, self)
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.name = QLineEdit(self)
        self.name.setMaxLength(180)
        self.cash_price = self._money_input()
        self.wholesale_price = self._money_input()
        self.wholesale_price.setSpecialValueText(ar.PROD_OPTIONAL)
        if product is not None:
            self.name.setText(product.name)
            self.wholesale_price.setValue(product.wholesale_price)
            self.cash_price.setValue(product.cash_price)
        form = QFormLayout()
        form.addRow(f"{ar.SET_PRODUCT_NAME} *", self.name)
        form.addRow(f"{ar.SET_PRODUCT_CASH} *", self.cash_price)
        form.addRow(f"{ar.SET_PRODUCT_WHOLESALE} ({ar.PROD_OPTIONAL})", self.wholesale_price)
        layout.addLayout(form)
        self.error = InlineError(self)
        layout.addWidget(self.error)
        self.name.textEdited.connect(self.error.clear)
        self.cash_price.valueChanged.connect(self.error.clear)
        buttons = QDialogButtonBox(self)
        self.save_button = buttons.addButton(ar.USER_SAVE, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(ar.USER_CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setProperty("variant", "secondary")
        repolish(cancel)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self.name.setFocus()

    def missing_field_message(self) -> str | None:
        """The message for the first empty required field, or None when complete."""
        if not self.name.text().strip():
            return ar.PROD_NAME_REQUIRED
        if self.cash_price.value() <= 0:
            return ar.PROD_PRICE_REQUIRED
        return None

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
        self.template_button = self._action(ar.PROD_TEMPLATE_BUTTON, "sheet", "secondary")
        self.template_button.setToolTip(ar.PROD_TEMPLATE_TOOLTIP)
        self.template_button.clicked.connect(self._download_template)
        self.upload_button = self._action(ar.PROD_UPLOAD_BUTTON, "upload", "secondary")
        self.upload_button.setToolTip(ar.PROD_UPLOAD_TOOLTIP)
        self.upload_button.clicked.connect(self._upload)
        self.edit_button = self._action(ar.SET_PRODUCT_EDIT, "edit", "secondary")
        self.edit_button.clicked.connect(self._edit_selected)
        self.archive_button = self._action(ar.SET_PRODUCT_ARCHIVE, "tray", "secondary")
        self.archive_button.clicked.connect(self._toggle_selected)
        self.add_button = self._action(ar.SET_PRODUCT_ADD, "plus", None)
        self.add_button.clicked.connect(self._create)
        for button in (
            self.template_button, self.upload_button, self.edit_button, self.archive_button, self.add_button,
        ):
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
            return _money(product.wholesale_price) if product.wholesale_price else "—"
        if key == "cash":
            return _money(product.cash_price)
        if key == "margin":
            return _money(product.cash_price - product.wholesale_price) if product.wholesale_price else "—"
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
        missing = dialog.missing_field_message()
        if missing is not None:
            dialog.error.show_message(missing)
            (dialog.name if missing == ar.PROD_NAME_REQUIRED else dialog.cash_price).setFocus()
            return False
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
        except products.ProductNameTakenError:
            dialog.error.show_message(ar.PROD_DUPLICATE_NAME)
            return False
        except (ValueError, SQLAlchemyError):
            dialog.error.show_message(ar.SET_PRODUCT_ERROR)
            return False
        dialog.accept()
        events.data_changed.emit()
        events.notify.emit("success", ar.NAV_PRODUCTS, ar.SET_PRODUCT_SAVED, 3000)
        return True

    # ------------------------------------------------------------ Excel
    def _download_template(self) -> None:
        selected, _filter = QFileDialog.getSaveFileName(
            self, ar.PROD_TEMPLATE_BUTTON, ar.PROD_TEMPLATE_FILE, ar.IMP_TEMPLATE_FILTER
        )
        if selected:
            self.save_template(selected)

    def save_template(self, destination: str | Path) -> Path | None:
        """Write the product template; returns its path, or None after showing the error."""
        path = Path(destination)
        if path.suffix.casefold() != ".xlsx":
            path = path.with_suffix(".xlsx")
        try:
            products.write_product_template(path)
        except OSError:
            _LOG.exception("Product template could not be written")
            QMessageBox.warning(self, ar.ERROR_TITLE, ar.PROD_TEMPLATE_ERROR)
            return None
        events.notify.emit("success", ar.NAV_PRODUCTS, ar.PROD_TEMPLATE_SAVED.format(path=path), 5000)
        return path

    def _upload(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(self, ar.PROD_UPLOAD_CHOOSE, "", ar.IMP_TEMPLATE_FILTER)
        if not selected:
            return
        preview = self.preview_upload(selected)
        if preview is None:
            return
        dialog = ProductImportDialog(preview, parent=self)
        dialog.import_button.clicked.connect(lambda: self.import_preview(dialog))
        dialog.exec()

    def preview_upload(self, source: str | Path) -> products.ProductImportPreview | None:
        """Read and check an uploaded sheet; shows why when it cannot be used."""
        try:
            with session_scope() as session:
                return products.preview_product_workbook(session, source)
        except products.ProductWorkbookError as error:
            message = {
                "unreadable": ar.PROD_UPLOAD_UNREADABLE,
                "no_header": ar.PROD_UPLOAD_NO_HEADER,
                "empty": ar.PROD_UPLOAD_EMPTY,
                "too_many_rows": ar.PROD_UPLOAD_TOO_MANY.format(limit=products.MAX_IMPORT_ROWS),
            }.get(error.code, ar.PROD_UPLOAD_UNREADABLE)
        except SQLAlchemyError:
            _LOG.exception("Product upload preview failed")
            message = ar.PROD_UPLOAD_FAILED
        QMessageBox.warning(self, ar.PROD_UPLOAD_TITLE, message)
        return None

    def import_preview(self, dialog: "ProductImportDialog") -> products.ProductImportResult | None:
        """Save the valid rows shown in the preview dialog; all or nothing."""
        try:
            with session_scope() as session:
                result = products.import_products(session, self.current_user.id, dialog.preview.rows)
        except auth.AuthorizationError:
            dialog.error.show_message(ar.SET_OWNER_ONLY)
            return None
        except (ValueError, SQLAlchemyError):
            _LOG.exception("Product upload failed")
            dialog.error.show_message(ar.PROD_UPLOAD_FAILED)
            return None
        dialog.accept()
        events.data_changed.emit()
        events.notify.emit(
            "success",
            ar.NAV_PRODUCTS,
            ar.PROD_UPLOAD_DONE.format(created=result.created, updated=result.updated, restored=result.restored),
            5000,
        )
        return result

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


class ProductImportDialog(QDialog):
    """Row-by-row preview of an uploaded product sheet before anything is saved."""

    def __init__(self, preview: products.ProductImportPreview, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.preview = preview
        self.setWindowTitle(ar.PROD_UPLOAD_TITLE)
        self.setMinimumSize(760, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(8)
        heading = QLabel(ar.PROD_UPLOAD_TITLE, self)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        source = QLabel(ar.PROD_UPLOAD_FILE.format(name=preview.path.name, sheet=preview.sheet_name), self)
        source.setObjectName("sectionHint")
        layout.addWidget(source)
        self.summary = QLabel(ar.PROD_UPLOAD_SUMMARY.format(
            create=preview.count(products.ACTION_CREATE),
            update=preview.count(products.ACTION_UPDATE),
            restore=preview.count(products.ACTION_RESTORE),
            unchanged=preview.count(products.ACTION_UNCHANGED),
            invalid=preview.count(products.ACTION_INVALID),
        ), self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        hint = QLabel(ar.PROD_UPLOAD_HINT, self)
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        headers = [
            ar.PROD_UPLOAD_COL_ROW, ar.SET_PRODUCT_NAME, ar.SET_PRODUCT_CASH, ar.SET_PRODUCT_WHOLESALE,
            ar.PROD_UPLOAD_COL_RESULT, ar.PROD_UPLOAD_COL_NOTES,
        ]
        self.table = QTableWidget(len(preview.rows), len(headers), self)
        self.table.setHorizontalHeaderLabels(headers)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(True)
        for row, item in enumerate(preview.rows):
            tone = _ACTION_TONES[item.action]
            notes = [_problem_text(code) for code in item.errors + item.warnings]
            cells = (
                str(item.row_number),
                item.name or "—",
                _money(item.cash_price) if item.cash_price is not None else "—",
                _money(item.wholesale_price) if item.wholesale_price is not None else "—",
                action_label(item.action),
                " · ".join(notes),
            )
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if column != 1 and column != 5:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column == 4 or (column == 5 and item.errors):
                    cell.setForeground(qcolor(tone))
                self.table.setItem(row, column, cell)
        layout.addWidget(self.table, 1)

        self.error = InlineError(self)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(self)
        importable = sum(
            1 for item in preview.rows
            if item.action in (products.ACTION_CREATE, products.ACTION_UPDATE, products.ACTION_RESTORE)
        )
        self.import_button = buttons.addButton(
            ar.PROD_UPLOAD_CONFIRM.format(count=importable), QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.import_button.setIcon(icons.icon("upload", "#FFFFFF", 16))
        self.import_button.setEnabled(preview.has_changes)
        cancel = buttons.addButton(ar.USER_CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setProperty("variant", "secondary")
        repolish(cancel)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)
        if not preview.has_changes:
            self.error.show_message(ar.PROD_UPLOAD_NOTHING)


_ACTION_TONES = {
    products.ACTION_CREATE: "paid",
    products.ACTION_UPDATE: "primary-glow",
    products.ACTION_RESTORE: "primary-glow",
    products.ACTION_UNCHANGED: "text-muted",
    products.ACTION_INVALID: "failed",
}


def action_label(action: str) -> str:
    """Localized result of importing one row."""
    return {
        products.ACTION_CREATE: ar.PROD_ACTION_CREATE,
        products.ACTION_UPDATE: ar.PROD_ACTION_UPDATE,
        products.ACTION_RESTORE: ar.PROD_ACTION_RESTORE,
        products.ACTION_UNCHANGED: ar.PROD_ACTION_UNCHANGED,
        products.ACTION_INVALID: ar.PROD_ACTION_INVALID,
    }[action]


def _problem_text(code: str) -> str:
    """Localized row error or warning."""
    return {
        products.ERROR_NAME_MISSING: ar.PROD_ERR_NAME_MISSING,
        products.ERROR_NAME_TOO_LONG: ar.PROD_ERR_NAME_TOO_LONG,
        products.ERROR_PRICE_MISSING: ar.PROD_ERR_PRICE_MISSING,
        products.ERROR_PRICE_INVALID: ar.PROD_ERR_PRICE_INVALID,
        products.ERROR_WHOLESALE_INVALID: ar.PROD_ERR_WHOLESALE_INVALID,
        products.ERROR_DUPLICATE: ar.PROD_ERR_DUPLICATE,
        products.WARNING_NO_WHOLESALE: ar.PROD_WARN_NO_WHOLESALE,
        products.WARNING_BELOW_WHOLESALE: ar.PROD_WARN_BELOW_WHOLESALE,
    }.get(code, code)


def _money(amount: int) -> str:
    return f"{amount:,} {ar.CURRENCY_SUFFIX}"
