"""First-purchase section of the "new client" form.

Picking a catalog product fills in its price and its default installment
plan (duration, payment every N months, rate); the plan can then be changed
for this client. The sale type decides which fields apply (cash: none,
installment: months/interval/rate/down payment, credit: down payment). A live
summary shows total, financed amount, installment amount and end date using
the calculation service, so the screen and the saved sale always agree.
"""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFormLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.i18n import ar
from app.services import calc
from app.services.products import CatalogEntry, name_key
from app.i18n.plan_text import every_text, plan_description
from app.ui.widgets.phone_details import PhoneDetailsSection


class FirstPurchaseSection(QGroupBox):
    """Checkable group: when checked, the new client is saved with this purchase."""

    def __init__(
        self,
        catalog: list[CatalogEntry] | list[tuple[str, int, int]],
        rate_presets: dict[int, int],
        *,
        is_owner: bool,
        parent: QWidget | None = None,
    ) -> None:
        """``catalog`` holds catalog entries (name, cash price, wholesale price, plan)."""
        super().__init__(ar.PURCHASE_SECTION_TITLE, parent)
        self.setCheckable(True)
        self.setChecked(bool(catalog))
        self._catalog = [CatalogEntry(*entry) for entry in catalog]
        self._presets = rate_presets
        self._is_owner = is_owner

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        hint = QLabel(ar.PURCHASE_SECTION_HINT if catalog else ar.PURCHASE_NO_CATALOG, self)
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        self.product = QComboBox(self)
        # The owner may type a product that is not in the catalog yet; it is
        # added when the client is saved. Sellers pick from the catalog.
        self.product.setEditable(is_owner)
        self.product.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for entry in self._catalog:
            self.product.addItem(entry.name, entry.name)
        self.sale_type = QComboBox(self)
        for label, value in (
            (ar.SALE_CASH, "cash"),
            (ar.SALE_INSTALLMENT, "installment"),
            (ar.SALE_CREDIT, "credit"),
        ):
            self.sale_type.addItem(label, value)
        self.cash_price = self._money()
        self.cash_price.setEnabled(is_owner)  # sellers always sell at the catalog price
        self.months = QSpinBox(self)
        self.months.setRange(1, calc.MAX_PLAN_MONTHS)
        self.months.setSuffix(f" {ar.PURCHASE_MONTHS_SUFFIX}")
        self.interval = QSpinBox(self)
        self.interval.setRange(1, calc.MAX_PLAN_MONTHS)
        self.interval.setSuffix(f" {ar.PURCHASE_MONTHS_SUFFIX}")
        self.interval.setSpecialValueText(ar.PLAN_EVERY_1)
        self.rate = QSpinBox(self)
        self.rate.setRange(0, 100)
        self.rate.setSuffix(" %")
        self.down_payment = self._money()
        self.purchase_date = QDateEdit(self)
        self.purchase_date.setCalendarPopup(True)
        self.purchase_date.setDisplayFormat("dd/MM/yyyy")
        self.purchase_date.setDate(QDate.currentDate())
        form.addRow(ar.PRODUCT, self.product)
        form.addRow(ar.CUST_SALE_TYPE, self.sale_type)
        form.addRow(ar.CASH_PRICE_DETAIL, self.cash_price)
        form.addRow(ar.MONTHS_DURATION, self.months)
        form.addRow(ar.PLAN_INTERVAL, self.interval)
        form.addRow(ar.RATE, self.rate)
        form.addRow(ar.PURCHASE_DOWN_PAYMENT, self.down_payment)
        form.addRow(ar.CUST_COL_PURCHASE_DATE, self.purchase_date)
        self._form = form
        layout.addLayout(form)

        details_title = QLabel(ar.SALE_DETAILS_SECTION, self)
        details_title.setObjectName("sectionHint")
        layout.addWidget(details_title)
        self.details = PhoneDetailsSection(can_create_products=is_owner, parent=self)
        self.details.unit_selected.connect(self._unit_selected)
        layout.addWidget(self.details)

        self.plan_hint = QLabel(self)
        self.plan_hint.setObjectName("sectionHint")
        self.plan_hint.setWordWrap(True)
        layout.addWidget(self.plan_hint)

        self.summary = QLabel(self)
        self.summary.setObjectName("notificationBody")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)

        self.product.currentTextChanged.connect(self._product_changed)
        self.sale_type.currentIndexChanged.connect(self._type_changed)
        self.months.valueChanged.connect(self._months_changed)
        self.months.valueChanged.connect(self.interval.setMaximum)
        for editor in (self.cash_price, self.rate, self.down_payment, self.interval):
            editor.valueChanged.connect(self._update_summary)
        self.purchase_date.dateChanged.connect(self._update_summary)
        default_months = 6 if 6 in rate_presets else (min(rate_presets) if rate_presets else 6)
        self.months.setValue(default_months)
        self.sale_type.setCurrentIndex(self.sale_type.findData("installment"))
        self._product_changed()
        self._type_changed()

    def _money(self) -> QSpinBox:
        editor = QSpinBox(self)
        editor.setRange(0, 2_000_000_000)
        editor.setGroupSeparatorShown(True)
        editor.setSingleStep(500)
        editor.setSuffix(f" {ar.CURRENCY_SUFFIX}")
        return editor

    # ------------------------------------------------------------- reactions
    def _entry(self) -> CatalogEntry | None:
        key = name_key(self.product.currentText())
        return next((entry for entry in self._catalog if name_key(entry.name) == key), None) if key else None

    def _unit_selected(self, unit: dict | None) -> None:
        """A phone picked from stock is sold at its own price."""
        if unit is not None:
            self.cash_price.setValue(unit["cash_price"])
        self._update_summary()

    def _product_changed(self, *_args: object) -> None:
        entry = self._entry()
        self.details.set_product(self.product.currentText())
        if entry is not None:
            self.cash_price.setValue(entry.cash_price)
            self.apply_plan(entry.months, entry.interval)
            self.plan_hint.setText(ar.PLAN_FROM_PRODUCT.format(plan=plan_description(entry.months, entry.interval)))
        self._update_summary()

    def apply_plan(self, months: int, interval: int) -> None:
        """Load a plan into the editors; the rate follows the product or the presets."""
        self.months.setValue(months)
        self.interval.setValue(min(interval, months))
        self._months_changed()

    def _rate_for(self, months: int) -> int:
        entry = self._entry()
        if entry is not None and entry.rate is not None and entry.months == months:
            return entry.rate
        return self._presets.get(months, self.rate.value())

    def _type_changed(self, *_args: object) -> None:
        kind = self.sale_type.currentData()
        self._set_row_visible(self.months, kind == "installment")
        self._set_row_visible(self.interval, kind == "installment")
        self.plan_hint.setVisible(kind == "installment" and bool(self.plan_hint.text()))
        self._set_row_visible(self.rate, kind == "installment")
        self._set_row_visible(self.down_payment, kind != "cash")
        self._months_changed()

    def _months_changed(self, *_args: object) -> None:
        if self.sale_type.currentData() == "installment":
            self.rate.setValue(self._rate_for(self.months.value()))
        self._update_summary()

    def _set_row_visible(self, field: QWidget, visible: bool) -> None:
        self._form.setRowVisible(field, visible)

    # -------------------------------------------------------------- values
    def calculation(self) -> calc.SaleCalculation | None:
        """Return the live calculation, or ``None`` when inputs are invalid."""
        kind = self.sale_type.currentData()
        try:
            return calc.compute_sale(
                cash_price=self.cash_price.value(),
                rate=self.rate.value() if kind == "installment" else 0,
                down_payment=self.down_payment.value() if kind != "cash" else 0,
                months=self.months.value() if kind == "installment" else None,
                wholesale=self._wholesale(),
                sale_type=kind,
                purchase_date=self.purchase_date.date().toPython(),
                interval=self._interval() if kind == "installment" else 1,
            )
        except ValueError:
            return None

    def _wholesale(self) -> int:
        unit = self.details.selected_unit()
        if unit is not None:
            return unit["wholesale_price"]
        entry = self._entry()
        return entry.wholesale_price if entry is not None else 0

    def _interval(self) -> int:
        return min(self.interval.value(), self.months.value())

    def _update_summary(self, *_args: object) -> None:
        result = self.calculation()
        if result is None:
            self.summary.setText(ar.PURCHASE_INVALID)
            return
        parts = [ar.PURCHASE_SUMMARY_TOTAL.format(total=_money(result.total))]
        if self.sale_type.currentData() != "cash":
            parts.append(ar.PURCHASE_SUMMARY_FINANCED.format(financed=_money(result.financed)))
        if result.monthly_list:
            parts.append(ar.PURCHASE_SUMMARY_PLAN.format(
                amount=_money(result.monthly_list[0]),
                every=every_text(result.payment_interval),
                count=result.payment_count,
            ))
        if result.end_date is not None:
            parts.append(ar.PURCHASE_SUMMARY_END.format(date=result.end_date.strftime("%d/%m/%Y")))
        self.summary.setText(" · ".join(parts))

    def sale_values(self) -> dict[str, object] | None:
        """Return create_sale keyword arguments, or ``None`` when unchecked."""
        if not self.isChecked() or not self.product.currentText().strip():
            return None
        kind = self.sale_type.currentData()
        purchase: date = self.purchase_date.date().toPython()
        return {
            "product": self.product.currentText().strip(),
            "sale_type": kind,
            "wholesale_price": self._wholesale(),
            "cash_price": self.cash_price.value(),
            "rate": self.rate.value() if kind == "installment" else 0,
            "down_payment": self.down_payment.value() if kind != "cash" else 0,
            "months": self.months.value() if kind == "installment" else None,
            "payment_interval": self._interval() if kind == "installment" else 1,
            "purchase_date": purchase,
            **self.details.values(),
        }


def _money(amount: int) -> str:
    return f"{amount:,} {ar.CURRENCY_SUFFIX}"
