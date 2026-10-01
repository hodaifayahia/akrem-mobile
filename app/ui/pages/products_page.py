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
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import Product, User
from app.db.session import session_scope
from app.i18n import ar
from app.services import auth, calc, product_sheet, products, stock
from app.services.customers import normalize_search_text
from app.services.settings import get_rate_presets
from app.ui import icons
from app.ui.dialogs.auth_dialogs import InlineError
from app.ui.events import events
from app.ui.pages.stock_panel import StockPanel
from app.i18n.plan_text import every_text, plan_description, plan_summary
from app.i18n.stock_text import battery_text, ltr_text
from app.ui.theme import qcolor, repolish

_LOG = logging.getLogger(__name__)


class ProductDialog(QDialog):
    """Name and selling price (required), optional wholesale price and default plan."""

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
        self.plan_months = QSpinBox(self)
        self.plan_months.setRange(1, calc.MAX_PLAN_MONTHS)
        self.plan_months.setSuffix(f" {ar.PURCHASE_MONTHS_SUFFIX}")
        self.plan_interval = QSpinBox(self)
        self.plan_interval.setRange(1, calc.MAX_PLAN_MONTHS)
        self.plan_interval.setSuffix(f" {ar.PURCHASE_MONTHS_SUFFIX}")
        self.plan_interval.setSpecialValueText(ar.PLAN_EVERY_1)
        self.plan_rate = QSpinBox(self)
        self.plan_rate.setRange(-1, 100)
        self.plan_rate.setSuffix(" %")
        self.plan_rate.setSpecialValueText(ar.PLAN_RATE_AUTO)
        self.plan_months.valueChanged.connect(self.plan_interval.setMaximum)
        plan = products.plan_of(product) if product is not None else products.ProductPlan()
        self.plan_months.setValue(plan.months)
        self.plan_interval.setValue(plan.interval)
        self.plan_rate.setValue(-1 if plan.rate is None else plan.rate)
        form = QFormLayout()
        form.addRow(f"{ar.SET_PRODUCT_NAME} *", self.name)
        form.addRow(f"{ar.SET_PRODUCT_CASH} *", self.cash_price)
        form.addRow(f"{ar.SET_PRODUCT_WHOLESALE} ({ar.PROD_OPTIONAL})", self.wholesale_price)
        layout.addLayout(form)
        plan_title = QLabel(ar.PLAN_SECTION, self)
        plan_title.setObjectName("sectionTitle")
        layout.addWidget(plan_title)
        plan_hint = QLabel(ar.PLAN_SECTION_HINT, self)
        plan_hint.setObjectName("sectionHint")
        plan_hint.setWordWrap(True)
        layout.addWidget(plan_hint)
        plan_form = QFormLayout()
        plan_form.addRow(ar.PLAN_MONTHS, self.plan_months)
        plan_form.addRow(ar.PLAN_INTERVAL, self.plan_interval)
        plan_form.addRow(ar.PLAN_RATE, self.plan_rate)
        layout.addLayout(plan_form)
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

    def plan(self) -> products.ProductPlan:
        """The default installment plan entered in the dialog."""
        rate = self.plan_rate.value()
        return products.ProductPlan(
            months=self.plan_months.value(),
            interval=min(self.plan_interval.value(), self.plan_months.value()),
            rate=None if rate < 0 else rate,
        )

    def _money_input(self) -> QSpinBox:
        editor = QSpinBox(self)
        editor.setRange(0, 2_000_000_000)
        editor.setGroupSeparatorShown(True)
        editor.setSuffix(f" {ar.CURRENCY_SUFFIX}")
        return editor


class ProductsPage(QWidget):
    """Browse and (for the owner) manage the product catalog."""

    COLUMNS_OWNER = ("name", "wholesale", "cash", "margin", "plan", "quote", "stock", "sold", "last_sold", "state")
    COLUMNS_SELLER = ("name", "cash", "plan", "quote", "stock", "sold", "state")

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self._is_owner = current_user.role == "owner"
        self._columns = self.COLUMNS_OWNER if self._is_owner else self.COLUMNS_SELLER
        self._rows: list[Product] = []
        self._stock: dict[int, tuple[int, int]] = {}
        self._stock_totals = (0, 0)
        self._build_ui()
        events.data_changed.connect(self.refresh)
        events.data_changed.connect(self.stock_panel.refresh)
        self.refresh()

    # ------------------------------------------------------------------ build
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(12)

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
        self.export_button = self._action(ar.STOCK_EXPORT, "import", "secondary")
        self.export_button.clicked.connect(self._export_stock)
        for button in (self.template_button, self.upload_button, self.export_button):
            button.setVisible(self._is_owner)
            header.addWidget(button)
        layout.addLayout(header)

        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.stock_panel = StockPanel(self.current_user, self.tabs)
        self.stock_panel.counts_changed.connect(self._stock_counts_changed)
        self._stock_totals = self.stock_panel.last_counts
        self.tabs.addTab(self.stock_panel, icons.icon("tag", "text-muted", 16), ar.PROD_TAB_STOCK)
        catalog = QWidget(self.tabs)
        self.tabs.addTab(catalog, icons.icon("cart", "text-muted", 16), ar.PROD_TAB_CATALOG)
        layout.addWidget(self.tabs, 1)

        catalog_layout = QVBoxLayout(catalog)
        catalog_layout.setContentsMargins(0, 12, 0, 0)
        catalog_layout.setSpacing(10)
        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.search = QLineEdit(catalog)
        self.search.setPlaceholderText(ar.PROD_SEARCH)
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icons.icon("search", "text-muted", 16), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._render)
        filters.addWidget(self.search, 1)
        self.state_filter = QComboBox(catalog)
        self.state_filter.addItem(ar.PROD_FILTER_ACTIVE, "active")
        if self._is_owner:
            self.state_filter.addItem(ar.PROD_FILTER_ARCHIVED, "archived")
            self.state_filter.addItem(ar.PROD_FILTER_ALL, "all")
        self.state_filter.addItem(ar.PROD_FILTER_IN_STOCK, "in_stock")
        self.state_filter.addItem(ar.PROD_FILTER_OUT_OF_STOCK, "out_of_stock")
        self.state_filter.currentIndexChanged.connect(self._render)
        filters.addWidget(self.state_filter)
        self.edit_button = self._action(ar.SET_PRODUCT_EDIT, "edit", "secondary")
        self.edit_button.clicked.connect(self._edit_selected)
        self.archive_button = self._action(ar.SET_PRODUCT_ARCHIVE, "tray", "secondary")
        self.archive_button.clicked.connect(self._toggle_selected)
        self.add_button = self._action(ar.SET_PRODUCT_ADD, "plus", None)
        self.add_button.clicked.connect(self._create)
        for button in (self.archive_button, self.edit_button, self.add_button):
            button.setVisible(self._is_owner)
            filters.addWidget(button)
        catalog_layout.addLayout(filters)

        self.table = QTableWidget(0, len(self._columns), catalog)
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
        catalog_layout.addWidget(self.table, 1)

        self.empty_card = QFrame(catalog)
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
        catalog_layout.addWidget(self.empty_card)
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
            "plan": ar.PROD_COL_PLAN,
            "quote": ar.INSTALLMENT_AMOUNT,
            "stock": ar.PROD_COL_STOCK,
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
                self._stock = stock.stock_counts(session)
                self._presets = get_rate_presets(session)
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
            in_stock = self._stock.get(product.id, (0, 0))[0]
            if state in ("in_stock", "out_of_stock") and (not product.active or (in_stock > 0) != (state == "in_stock")):
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
                if key == "quote" and self._plan_rate(product) is None:
                    item.setToolTip(ar.PLAN_NO_RATE.format(months=products.plan_of(product).months))
                self.table.setItem(row, column, item)
        active = sum(1 for product in getattr(self, "_catalog", []) if product.active)
        self._active_count = active
        self._update_count_label()
        self.empty_card.setVisible(not self._rows)
        self.table.setVisible(bool(self._rows))
        self._update_actions()

    def _cell(self, product: Product, key: str) -> str:
        if key == "name":
            return ltr_text(product.name)
        if key == "wholesale":
            return _money(product.wholesale_price) if product.wholesale_price else "—"
        if key == "cash":
            return _money(product.cash_price)
        if key == "margin":
            return _money(product.cash_price - product.wholesale_price) if product.wholesale_price else "—"
        if key == "plan":
            plan = products.plan_of(product)
            text = plan_description(plan.months, plan.interval)
            rate = self._plan_rate(product)
            return text if rate is None else f"{text} · {rate}%"
        if key == "quote":
            return self._quote(product)
        if key == "stock":
            return str(self._stock.get(product.id, (0, 0))[0])
        if key == "sold":
            return str(self._sold.get(product.name, (0, None))[0])
        if key == "last_sold":
            last = self._sold.get(product.name, (0, None))[1]
            return last.strftime("%d/%m/%Y") if last else "—"
        return ar.SET_USER_ACTIVE if product.active else ar.SET_PRODUCT_ARCHIVED

    def _plan_rate(self, product: Product) -> int | None:
        return products.plan_of(product).resolved_rate(getattr(self, "_presets", {}))

    def _quote(self, product: Product) -> str:
        """Installment amount of the product's default plan, with no down payment."""
        rate = self._plan_rate(product)
        if rate is None:
            return "—"
        plan = products.plan_of(product)
        result = calc.compute_sale(
            cash_price=product.cash_price, rate=rate, down_payment=0, months=plan.months,
            wholesale=product.wholesale_price, sale_type="installment", interval=plan.interval,
        )
        return plan_summary(result.monthly_list[0], plan.interval, result.payment_count)

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
            "plan": dialog.plan(),
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

    def _stock_counts_changed(self, available: int, sold: int) -> None:
        self._stock_totals = (available, sold)
        self._update_count_label()

    def _update_count_label(self) -> None:
        available, sold = self._stock_totals
        self.count_label.setText(
            ar.PROD_COUNT.format(count=getattr(self, "_active_count", 0))
            + " · " + ar.STOCK_COUNT.format(available=available, sold=sold)
        )

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

    def _export_stock(self) -> None:
        selected, _filter = QFileDialog.getSaveFileName(
            self, ar.STOCK_EXPORT, ar.STOCK_EXPORT_FILE, ar.IMP_TEMPLATE_FILTER
        )
        if selected:
            self.export_stock(selected)

    def export_stock(self, destination: str | Path) -> Path | None:
        """Write the stock list as shown (current filters) in the re-importable layout."""
        path = Path(destination)
        if path.suffix.casefold() != ".xlsx":
            path = path.with_suffix(".xlsx")
        panel = self.stock_panel
        try:
            with session_scope() as session:
                units = stock.list_units(
                    session,
                    status=panel.status_filter.currentData(),
                    product_id=panel.product_filter.currentData(),
                    color=panel.color_filter.currentData(),
                    condition=panel.condition_filter.currentData(),
                    search=panel.search.text(),
                )
                product_sheet.export_stock_workbook(session, path, units)
        except (OSError, SQLAlchemyError):
            _LOG.exception("Stock export failed")
            QMessageBox.warning(self, ar.ERROR_TITLE, ar.PROD_TEMPLATE_ERROR)
            return None
        events.notify.emit("success", ar.NAV_PRODUCTS, ar.STOCK_EXPORT_DONE.format(path=path), 5000)
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
        if dialog.preview.is_stock:
            message = ar.PROD_UPLOAD_DONE_STOCK.format(
                created=result.created, added=result.units_added, updated=result.units_updated,
            )
        else:
            message = ar.PROD_UPLOAD_DONE.format(
                created=result.created, updated=result.updated, restored=result.restored,
            )
        events.notify.emit("success", ar.NAV_PRODUCTS, message, 5000)
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
    """Row-by-row preview of an uploaded products/stock sheet before anything is saved."""

    CATALOG_COLUMNS = ("row", "name", "cash", "wholesale", "result", "notes", "plan")
    STOCK_COLUMNS = ("row", "name", "wholesale", "cash", "battery", "color", "imei", "reference",
                     "result", "stock", "notes")

    def __init__(self, preview: products.ProductImportPreview, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.preview = preview
        self.columns = self.STOCK_COLUMNS if preview.is_stock else self.CATALOG_COLUMNS
        self.setWindowTitle(ar.PROD_UPLOAD_TITLE)
        self.setMinimumSize(980 if preview.is_stock else 760, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(8)
        heading = QLabel(ar.PROD_UPLOAD_TITLE, self)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        source = QLabel(ar.PROD_UPLOAD_FILE.format(name=preview.path.name, sheet=preview.sheet_name), self)
        source.setObjectName("sectionHint")
        layout.addWidget(source)
        if preview.is_stock:
            summary = ar.PROD_UPLOAD_STOCK_SUMMARY.format(
                create=preview.count(products.ACTION_CREATE),
                units=preview.units_to_add,
                updated=preview.unit_count(products.UNIT_UPDATE),
                in_stock=preview.unit_count(products.UNIT_IN_STOCK),
                invalid=preview.count(products.ACTION_INVALID),
            )
            hint_text = ar.PROD_UPLOAD_STOCK_HINT
        else:
            summary = ar.PROD_UPLOAD_SUMMARY.format(
                create=preview.count(products.ACTION_CREATE),
                update=preview.count(products.ACTION_UPDATE),
                restore=preview.count(products.ACTION_RESTORE),
                unchanged=preview.count(products.ACTION_UNCHANGED),
                invalid=preview.count(products.ACTION_INVALID),
            )
            hint_text = ar.PROD_UPLOAD_HINT
        self.summary = QLabel(summary, self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        hint = QLabel(hint_text, self)
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(len(preview.rows), len(self.columns), self)
        self.table.setHorizontalHeaderLabels([_preview_header(key) for key in self.columns])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.columns.index("name"), QHeaderView.ResizeMode.Stretch)
        notes_column = self.columns.index("notes")
        header.setSectionResizeMode(notes_column, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(notes_column, 230)
        header.setStretchLastSection(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        for row, item in enumerate(preview.rows):
            for column, key in enumerate(self.columns):
                cell = QTableWidgetItem(_preview_cell(item, key))
                if key not in ("name", "notes"):
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if key == "notes":
                    cell.setToolTip(cell.text())
                if key == "result" or (key == "notes" and item.errors):
                    cell.setForeground(qcolor(_ACTION_TONES[item.action]))
                if key == "stock" and item.unit_action == products.UNIT_ADD:
                    cell.setForeground(qcolor("paid"))
                self.table.setItem(row, column, cell)
        layout.addWidget(self.table, 1)

        self.error = InlineError(self)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(self)
        if preview.is_stock:
            label = ar.PROD_UPLOAD_CONFIRM_STOCK.format(units=preview.units_to_add)
        else:
            importable = sum(
                1 for item in preview.rows
                if item.action in (products.ACTION_CREATE, products.ACTION_UPDATE, products.ACTION_RESTORE)
            )
            label = ar.PROD_UPLOAD_CONFIRM.format(count=importable)
        self.import_button = buttons.addButton(label, QDialogButtonBox.ButtonRole.AcceptRole)
        self.import_button.setIcon(icons.icon("upload", "#FFFFFF", 16))
        self.import_button.setEnabled(preview.has_changes)
        cancel = buttons.addButton(ar.USER_CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setProperty("variant", "secondary")
        repolish(cancel)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)
        if not preview.has_changes:
            self.error.show_message(ar.PROD_UPLOAD_NOTHING)

    def cell_text(self, row: int, key: str) -> str:
        """Text shown for ``key`` on a preview row (used by tests)."""
        return self.table.item(row, self.columns.index(key)).text()


def _preview_header(key: str) -> str:
    return {
        "row": ar.PROD_UPLOAD_COL_ROW,
        "name": ar.SET_PRODUCT_NAME,
        "cash": ar.SET_PRODUCT_CASH,
        "wholesale": ar.SET_PRODUCT_WHOLESALE,
        "battery": ar.PROD_TPL_BATTERY,
        "color": ar.PROD_TPL_COLOR,
        "imei": ar.PROD_TPL_IMEI,
        "reference": ar.PROD_TPL_REF,
        "result": ar.PROD_UPLOAD_COL_RESULT,
        "stock": ar.PROD_UPLOAD_COL_STOCK,
        "notes": ar.PROD_UPLOAD_COL_NOTES,
        "plan": ar.PROD_COL_PLAN,
    }[key]


def _preview_cell(item: products.ProductImportRow, key: str) -> str:
    if key == "row":
        return str(item.row_number)
    if key == "name":
        return ltr_text(item.name) if item.name else "—"
    if key == "cash":
        return _money(item.cash_price) if item.cash_price is not None else "—"
    if key == "wholesale":
        return _money(item.wholesale_price) if item.wholesale_price is not None else "—"
    if key == "battery":
        return battery_text(item.is_new, item.battery_health)
    if key == "color":
        return ltr_text(item.color) if item.color else "—"
    if key == "imei":
        return ltr_text(item.imei) if item.imei else "—"
    if key == "reference":
        if item.reference:
            return ltr_text(item.reference)
        return ar.STOCK_REF_AUTO if item.unit_action == products.UNIT_ADD else "—"
    if key == "result":
        return action_label(item.action)
    if key == "stock":
        return unit_action_label(item)
    if key == "plan":
        return _row_plan_text(item)
    notes = [_problem_text(code) for code in item.errors + item.warnings]
    if item.is_stock and item.note:
        notes.append(item.note)
    return " · ".join(notes)


def unit_action_label(item: products.ProductImportRow) -> str:
    """Localized stock outcome of one stock row."""
    if not item.valid or item.unit_action is None:
        return "—"
    if item.unit_action == products.UNIT_ADD:
        return ar.PROD_UNIT_ADD.format(count=item.units_to_add)
    return {
        products.UNIT_UPDATE: ar.PROD_UNIT_UPDATE,
        products.UNIT_IN_STOCK: ar.PROD_UNIT_IN_STOCK,
        products.UNIT_SOLD: ar.PROD_UNIT_SOLD,
    }[item.unit_action]


_ACTION_TONES = {
    products.ACTION_CREATE: "paid",
    products.ACTION_UPDATE: "primary-glow",
    products.ACTION_RESTORE: "primary-glow",
    products.ACTION_UNCHANGED: "text-muted",
    products.ACTION_EXISTING: "text-muted",
    products.ACTION_INVALID: "failed",
}


def action_label(action: str) -> str:
    """Localized result of importing one row."""
    return {
        products.ACTION_CREATE: ar.PROD_ACTION_CREATE,
        products.ACTION_UPDATE: ar.PROD_ACTION_UPDATE,
        products.ACTION_RESTORE: ar.PROD_ACTION_RESTORE,
        products.ACTION_UNCHANGED: ar.PROD_ACTION_UNCHANGED,
        products.ACTION_EXISTING: ar.PROD_ACTION_EXISTING,
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
        products.ERROR_MONTHS_INVALID: ar.PROD_ERR_MONTHS_INVALID,
        products.ERROR_INTERVAL_INVALID: ar.PROD_ERR_INTERVAL_INVALID,
        products.ERROR_RATE_INVALID: ar.PROD_ERR_RATE_INVALID,
        products.ERROR_NAME_INVALID: ar.PROD_ERR_NAME_INVALID,
        products.ERROR_QUANTITY_INVALID: ar.PROD_ERR_QUANTITY_INVALID,
        products.ERROR_IMEI_DUPLICATE: ar.PROD_ERR_IMEI_DUPLICATE,
        products.WARNING_BATTERY_TEXT: ar.PROD_WARN_BATTERY_TEXT,
        products.WARNING_STATUS_TEXT: ar.PROD_WARN_STATUS_TEXT,
        products.WARNING_REF_DUPLICATE: ar.PROD_WARN_REF_DUPLICATE,
        products.WARNING_NO_WHOLESALE: ar.PROD_WARN_NO_WHOLESALE,
        products.WARNING_BELOW_WHOLESALE: ar.PROD_WARN_BELOW_WHOLESALE,
    }.get(code, code)


def _row_plan_text(row: products.ProductImportRow) -> str:
    """The plan cells a row fills in, or a dash when it leaves the plan alone."""
    parts = []
    if row.months is not None:
        parts.append(ar.MONTHS_COUNT.format(count=row.months))
    if row.interval is not None:
        parts.append(every_text(row.interval))
    if row.rate is not None:
        parts.append(f"{row.rate}%")
    return " · ".join(parts) or "—"


def _money(amount: int) -> str:
    return f"{amount:,} {ar.CURRENCY_SUFFIX}"
