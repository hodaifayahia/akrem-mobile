"""First-purchase section of the "new client" form.

Picking a catalog product fills in its price; the sale type decides which
fields apply (cash: none, installment: months/rate/down payment, credit:
down payment). A live summary shows total, financed amount, monthly
installment and end date using the calculation service, so the screen and
the saved sale always agree.
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


class FirstPurchaseSection(QGroupBox):
    """Checkable group: when checked, the new client is saved with this purchase."""

    def __init__(
        self,
        catalog: list[tuple[str, int, int]],
        rate_presets: dict[int, int],
        *,
        is_owner: bool,
        parent: QWidget | None = None,
    ) -> None:
        """``catalog`` is a list of (product name, cash price, wholesale price)."""
        super().__init__(ar.PURCHASE_SECTION_TITLE, parent)
        self.setCheckable(True)
        self.setChecked(bool(catalog))
        self._catalog = catalog
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
        for name, cash_price, _wholesale in catalog:
            self.product.addItem(f"{name} — {cash_price:,} {ar.CURRENCY_SUFFIX}", name)
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
        self.months.setRange(1, 24)
        self.months.setSuffix(f" {ar.PURCHASE_MONTHS_SUFFIX}")
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
        form.addRow(ar.RATE, self.rate)
        form.addRow(ar.PURCHASE_DOWN_PAYMENT, self.down_payment)
        form.addRow(ar.CUST_COL_PURCHASE_DATE, self.purchase_date)
        self._form = form
        layout.addLayout(form)

        self.summary = QLabel(self)
        self.summary.setObjectName("notificationBody")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)

        self.product.currentIndexChanged.connect(self._product_changed)
        self.sale_type.currentIndexChanged.connect(self._type_changed)
        self.months.valueChanged.connect(self._months_changed)
        for editor in (self.cash_price, self.rate, self.down_payment):
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
    def _product_changed(self, *_args: object) -> None:
        index = self.product.currentIndex()
        if 0 <= index < len(self._catalog):
            self.cash_price.setValue(self._catalog[index][1])
        self._update_summary()

    def _type_changed(self, *_args: object) -> None:
        kind = self.sale_type.currentData()
        self._set_row_visible(self.months, kind == "installment")
        self._set_row_visible(self.rate, kind == "installment")
        self._set_row_visible(self.down_payment, kind != "cash")
        self._months_changed()

    def _months_changed(self, *_args: object) -> None:
        if self.sale_type.currentData() == "installment":
            self.rate.setValue(self._presets.get(self.months.value(), self.rate.value()))
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
            )
        except ValueError:
            return None

    def _wholesale(self) -> int:
        index = self.product.currentIndex()
        return self._catalog[index][2] if 0 <= index < len(self._catalog) else 0

    def _update_summary(self, *_args: object) -> None:
        result = self.calculation()
        if result is None:
            self.summary.setText(ar.PURCHASE_INVALID)
            return
        parts = [ar.PURCHASE_SUMMARY_TOTAL.format(total=_money(result.total))]
        if self.sale_type.currentData() != "cash":
            parts.append(ar.PURCHASE_SUMMARY_FINANCED.format(financed=_money(result.financed)))
        if result.monthly_list:
            parts.append(ar.PURCHASE_SUMMARY_MONTHLY.format(
                monthly=_money(result.monthly_list[0]), months=len(result.monthly_list)
            ))
        if result.end_date is not None:
            parts.append(ar.PURCHASE_SUMMARY_END.format(date=result.end_date.strftime("%d/%m/%Y")))
        self.summary.setText(" · ".join(parts))

    def sale_values(self) -> dict[str, object] | None:
        """Return create_sale keyword arguments, or ``None`` when unchecked."""
        if not self.isChecked() or self.product.currentIndex() < 0:
            return None
        kind = self.sale_type.currentData()
        purchase: date = self.purchase_date.date().toPython()
        return {
            "product": self.product.currentData(),
            "sale_type": kind,
            "wholesale_price": self._wholesale(),
            "cash_price": self.cash_price.value(),
            "rate": self.rate.value() if kind == "installment" else 0,
            "down_payment": self.down_payment.value() if kind != "cash" else 0,
            "months": self.months.value() if kind == "installment" else None,
            "purchase_date": purchase,
        }


def _money(amount: int) -> str:
    return f"{amount:,} {ar.CURRENCY_SUFFIX}"
