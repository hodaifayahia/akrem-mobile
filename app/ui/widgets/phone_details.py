"""The phone's details on a sale form: pick a unit from stock, or type them.

Used by the cash and installment modules of the New sale page and by the
"new client" purchase section, so every sale (cash, installment or credit)
records the same fields as the shop's stock sheet: battery, colour, IMEI and
REF. Choosing a unit fills those fields and its prices; saving the sale then
marks that phone as sold to the client.

The quantity says how many phones of the product the client takes; saving
the sale takes that many units out of the stock (the chosen one first, then
the oldest ones), so a product with 5 in stock sold 2 at a time has 3 left.

It also tells the user whether the typed product name already exists in the
catalog (it will be linked) or is new (it will be added).

Phones already in the sale basket (see ``set_reserved``) are not offered
again, and the quantity cannot exceed what the basket leaves in stock.

A barcode scanner (or typing) in the scan box finds the phone by its IMEI
or REF: ``scanned`` tells the form which product to show, and the form then
calls ``select_unit``. An IMEI that is not in stock is written in the IMEI
field instead.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import session_scope
from app.i18n import ar
from app.i18n.stock_text import unit_label
from app.services import products, stock

_LOG = logging.getLogger(__name__)


class PhoneDetailsSection(QWidget):
    """Stock unit picker plus battery / colour / IMEI / REF fields."""

    unit_selected = Signal(object)  # dict with the unit's prices, or None when cleared
    quantity_changed = Signal(int)
    scanned = Signal(str, int)  # product name, stock unit id

    def __init__(self, *, can_create_products: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._can_create = can_create_products
        self._units: dict[int, dict[str, Any]] = {}
        self._product_name = ""
        self._catalog_prices: tuple[int, int] | None = None
        self._reserved_ids: set[int] = set()
        self._reserved_counts: dict[str, int] = {}
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)

        self.scan = QLineEdit(self)
        self.scan.setPlaceholderText(ar.SCAN_PLACEHOLDER)
        self.scan.setClearButtonEnabled(True)
        self.scan.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.scan.returnPressed.connect(self._scan_entered)
        self.scan_state = QLabel(self)
        self.scan_state.setObjectName("sectionHint")
        self.scan_state.setWordWrap(True)
        self.product_state = QLabel(self)
        self.product_state.setObjectName("sectionHint")
        self.product_state.setWordWrap(True)
        self.unit = QComboBox(self)
        self.unit.addItem(ar.SALE_UNIT_NONE, None)
        self.quantity = QSpinBox(self)
        self.quantity.setRange(1, 1)
        self.quantity.setMaximumWidth(140)
        self.quantity.valueChanged.connect(self.quantity_changed.emit)
        self.availability = QLabel(self)
        self.availability.setObjectName("sectionHint")
        self.availability.setWordWrap(True)
        self.is_new = QCheckBox(ar.STOCK_IS_NEW, self)
        self.battery = QSpinBox(self)
        self.battery.setRange(-1, 100)
        self.battery.setSuffix(" %")
        self.battery.setSpecialValueText(ar.STOCK_BATTERY_UNKNOWN)
        self.battery.setValue(-1)
        self.is_new.toggled.connect(lambda checked: self.battery.setEnabled(not checked))
        self.color = QComboBox(self)
        self.color.setEditable(True)
        self.imei = QLineEdit(self)
        self.imei.setMaxLength(40)
        self.imei.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.reference = QLineEdit(self)
        self.reference.setMaxLength(40)
        self.reference.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

        form.addRow(ar.SCAN_LABEL, self.scan)
        form.addRow("", self.scan_state)
        form.addRow("", self.product_state)
        form.addRow(ar.SALE_UNIT_PICK, self.unit)
        form.addRow(ar.SALE_QUANTITY, self.quantity)
        form.addRow("", self.availability)
        form.addRow("", self.is_new)
        form.addRow(ar.STOCK_BATTERY, self.battery)
        form.addRow(ar.PROD_TPL_COLOR, self.color)
        form.addRow(ar.PROD_TPL_IMEI, self.imei)
        form.addRow(ar.PROD_TPL_REF, self.reference)
        self.unit.currentIndexChanged.connect(self._unit_changed)
        self._load_colors()

    # ---------------------------------------------------------------- product
    def set_reserved(self, unit_ids: set[int], counts_by_product: dict[str, int]) -> None:
        """Phones the sale basket already holds, and how many more of each product it will take."""
        self._reserved_ids = set(unit_ids)
        self._reserved_counts = dict(counts_by_product)
        self.set_product(self._product_name)

    def set_product(self, name: str) -> None:
        """Show whether ``name`` is in the catalog and list its phones in stock."""
        name = (name or "").strip()
        current = self.unit.currentData()
        self.unit.blockSignals(True)
        while self.unit.count() > 1:
            self.unit.removeItem(1)
        self._units = {}
        product = None
        try:
            with session_scope() as session:
                product = products.find_by_name(session, name) if name else None
                self._catalog_prices = (
                    (product.wholesale_price, product.cash_price) if product is not None else None
                )
                if product is not None:
                    units = [item for item in stock.available_units(session, product.id)
                             if item.id not in self._reserved_ids]
                    # Basket lines without a picked phone take the oldest ones: offer the rest.
                    held = self._reserved_counts.get(products.name_key(product.name), 0)
                    for item in units[held:]:
                        self._units[item.id] = {
                            "id": item.id,
                            "cash_price": item.cash_price,
                            "wholesale_price": item.wholesale_price,
                            "color": item.color,
                            "battery_health": item.battery_health,
                            "is_new": item.is_new,
                            "imei": item.imei,
                            "reference": item.reference,
                        }
                        self.unit.addItem(unit_label(item), item.id)
        except SQLAlchemyError:
            _LOG.exception("Could not load stock for %s", name)
        index = self.unit.findData(current) if current is not None else 0
        self.unit.setCurrentIndex(max(index, 0))
        self.unit.blockSignals(False)
        # Several phones can only be sold from stock; a new product starts at 1.
        same_product = name == self._product_name
        before = self.quantity.value()
        self.quantity.blockSignals(True)
        if not same_product:
            self.quantity.setValue(1)
        self.quantity.setMaximum(max(1, len(self._units)))
        self.quantity.blockSignals(False)
        self._product_name = name
        if same_product and self.quantity.value() != before:
            self.quantity_changed.emit(self.quantity.value())  # fewer phones left than asked
        if not name:
            self.product_state.setText("")
        elif product is not None:
            self.product_state.setText(ar.SALE_PRODUCT_EXISTS)
        else:
            self.product_state.setText(ar.SALE_PRODUCT_NEW if self._can_create else "")
        self.availability.setText(
            ar.SALE_UNITS_AVAILABLE.format(count=len(self._units)) if self._units
            else (ar.SALE_NO_UNITS if product is not None else "")
        )
        self._unit_changed()

    def _load_colors(self) -> None:
        try:
            with session_scope() as session:
                colors = stock.colors(session)
        except SQLAlchemyError:
            colors = []
        self.color.addItems(colors)
        self.color.setCurrentText("")

    # ------------------------------------------------------------------ scanner
    def _scan_entered(self) -> None:
        """A code was scanned (the scanner presses Enter): find the phone in stock."""
        code = self.scan.text().strip()
        self.scan.clear()
        if not code:
            return
        found: tuple[str, int] | None = None
        try:
            with session_scope() as session:
                unit = stock.find_by_scan(session, code)
                if unit is not None:
                    found = (unit.product.name, unit.id)
        except SQLAlchemyError:
            _LOG.exception("Scan lookup failed")
        if found is not None and found[1] in self._reserved_ids:
            self.scan_state.setText(ar.SCAN_IN_BASKET)
            return
        if found is not None:
            self.scan_state.setText(ar.SCAN_FOUND.format(product=found[0]))
            self.scanned.emit(*found)
            return
        imei = stock.normalize_imei(code)
        if imei is not None:
            self.unit.setCurrentIndex(0)
            self.imei.setText(imei)
            self.scan_state.setText(ar.SCAN_NOT_IN_STOCK)
        else:
            self.scan_state.setText(ar.SCAN_NOT_FOUND.format(code=code))

    def select_unit(self, unit_id: int) -> bool:
        """Pick a phone of the product shown (after a scan); False when it is not listed."""
        index = self.unit.findData(unit_id)
        if index < 0:
            return False
        self.unit.setCurrentIndex(index)
        return True

    # ------------------------------------------------------------------ unit
    def selected_unit(self) -> dict[str, Any] | None:
        """The chosen stock unit (as a plain dict), or None."""
        unit_id = self.unit.currentData()
        return self._units.get(unit_id) if unit_id is not None else None

    def unit_prices(self) -> tuple[int, int] | None:
        """``(wholesale, cash)`` price of one phone: the chosen unit's, else the catalog's."""
        unit = self.selected_unit()
        if unit is not None:
            return unit["wholesale_price"], unit["cash_price"]
        return self._catalog_prices

    def _unit_changed(self, *_args: object) -> None:
        unit = self.selected_unit()
        locked = unit is not None
        for editor in (self.imei, self.reference):
            editor.setReadOnly(locked)
        if unit is not None:
            self.is_new.setChecked(bool(unit["is_new"]))
            self.battery.setValue(-1 if unit["battery_health"] is None else unit["battery_health"])
            self.color.setCurrentText(unit["color"] or "")
            self.imei.setText(unit["imei"] or "")
            self.reference.setText(unit["reference"] or "")
            self.availability.setText(ar.SALE_UNIT_SOLD_HINT)
        self.unit_selected.emit(unit)

    # ---------------------------------------------------------------- values
    def values(self) -> dict[str, Any]:
        """``create_sale`` keyword arguments for the phone's details."""
        unit = self.selected_unit()
        battery = self.battery.value()
        return {
            "stock_item_id": unit["id"] if unit is not None else None,
            "quantity": self.quantity.value(),
            "color": self.color.currentText().strip() or None,
            "battery_health": None if battery < 0 or self.is_new.isChecked() else battery,
            "is_new": self.is_new.isChecked(),
            "imei": self.imei.text().strip() or None,
            "reference": self.reference.text().strip() or None,
        }

    def error_message(self) -> str | None:
        """A problem with the typed details, or None."""
        text = self.imei.text().strip()
        if text and stock.normalize_imei(text) is None:
            return ar.STOCK_IMEI_INVALID
        return None

    def clear(self) -> None:
        """Back to an empty form (after a sale was saved)."""
        self.scan.clear()
        self.scan_state.clear()
        self.unit.setCurrentIndex(0)
        self.quantity.setValue(1)
        self.is_new.setChecked(False)
        self.battery.setValue(-1)
        self.color.setCurrentText("")
        self.imei.clear()
        self.reference.clear()
