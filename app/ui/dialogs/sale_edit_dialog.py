"""Owner dialog for editing a stored sale and its calculation inputs."""

from __future__ import annotations

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.db.models import Sale
from app.i18n import ar
from app.services import calc


class SaleEditDialog(QDialog):
    """Edit the inputs for one existing sale; persistence stays in its service."""

    def __init__(self, sale: Sale, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(ar.CUST_EDIT_SALE)
        self.setMinimumWidth(440)
        self.sale = sale

        self.product = QLineEdit(sale.product, self)
        self.sale_type = QComboBox(self)
        for label, value in (
            (ar.SALE_CASH, "cash"),
            (ar.SALE_INSTALLMENT, "installment"),
            (ar.SALE_CREDIT, "credit"),
        ):
            self.sale_type.addItem(label, value)
        self.sale_type.setCurrentIndex(self.sale_type.findData(sale.sale_type))
        self.wholesale_price = self._money_input(self, sale.wholesale_price)
        self.cash_price = self._money_input(self, sale.cash_price)
        self.rate = QSpinBox(self)
        self.rate.setRange(0, 100)
        self.rate.setValue(sale.rate)
        self.down_payment = self._money_input(self, sale.down_payment)
        self.months = QSpinBox(self)
        self.months.setRange(1, max(calc.MAX_PLAN_MONTHS, sale.months or 0))
        self.months.setValue(sale.months or 6)
        self.interval = QSpinBox(self)
        self.interval.setRange(1, self.months.value())
        self.interval.setSuffix(f" {ar.PURCHASE_MONTHS_SUFFIX}")
        self.interval.setSpecialValueText(ar.PLAN_EVERY_1)
        self.interval.setValue(sale.payment_interval or 1)
        self.months.valueChanged.connect(self.interval.setMaximum)
        self.purchase_date = QDateEdit(self)
        self.purchase_date.setCalendarPopup(True)
        self.purchase_date.setDisplayFormat("dd/MM/yyyy")
        self.purchase_date.setDate(QDate(sale.purchase_date.year, sale.purchase_date.month,
                                         sale.purchase_date.day))
        self.expected_date_enabled = QCheckBox(ar.EXPECTED_PAY_DATE, self)
        self.expected_date_enabled.setChecked(sale.expected_pay_date is not None)
        expected = sale.expected_pay_date or sale.purchase_date
        self.expected_date = QDateEdit(self)
        self.expected_date.setCalendarPopup(True)
        self.expected_date.setDisplayFormat("dd/MM/yyyy")
        self.expected_date.setDate(QDate(expected.year, expected.month, expected.day))

        layout = QVBoxLayout(self)
        self.form = QFormLayout()
        self.form.addRow(ar.PRODUCT, self.product)
        self.form.addRow(ar.SALE_TYPE, self.sale_type)
        self.form.addRow(ar.WHOLESALE_PRICE, self.wholesale_price)
        self.form.addRow(ar.CASH_PRICE, self.cash_price)
        self.form.addRow(ar.RATE, self.rate)
        self.form.addRow(ar.DOWN_PAYMENT, self.down_payment)
        self.form.addRow(ar.MONTHS, self.months)
        self.form.addRow(ar.PLAN_INTERVAL, self.interval)
        self.form.addRow(ar.PURCHASE_DATE, self.purchase_date)
        self.form.addRow("", self.expected_date_enabled)
        self.form.addRow(ar.EXPECTED_PAY_DATE, self.expected_date)
        self._labels = {
            "rate": self.form.labelForField(self.rate),
            "down_payment": self.form.labelForField(self.down_payment),
            "months": self.form.labelForField(self.months),
            "interval": self.form.labelForField(self.interval),
            "expected_date": self.form.labelForField(self.expected_date),
        }
        layout.addLayout(self.form)

        buttons = QDialogButtonBox(self)
        save = buttons.addButton(ar.CUST_SAVE, QDialogButtonBox.ButtonRole.AcceptRole)
        save.setProperty("variant", "primary")
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel = buttons.addButton(ar.CUST_CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setProperty("variant", "secondary")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        save.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)

        self.sale_type.currentIndexChanged.connect(self._update_sale_type)
        self.expected_date_enabled.toggled.connect(self.expected_date.setEnabled)
        self.expected_date.setEnabled(self.expected_date_enabled.isChecked())
        self._update_sale_type()

    @staticmethod
    def _money_input(parent: QWidget, value: int) -> QSpinBox:
        """Create an integer-only whole-dinar editor."""
        control = QSpinBox(parent)
        control.setRange(0, 2_000_000_000)
        control.setGroupSeparatorShown(True)
        control.setValue(value)
        return control

    def _update_sale_type(self, *_args: object) -> None:
        """Only show fields used by the selected sale kind."""
        sale_type = self.sale_type.currentData()
        installment = sale_type == "installment"
        credit = sale_type == "credit"
        self.rate.setVisible(installment)
        self.months.setVisible(installment)
        self.interval.setVisible(installment)
        self.down_payment.setVisible(installment or credit)
        self.expected_date_enabled.setVisible(credit)
        self.expected_date.setVisible(credit)
        for name, visible in (
            ("rate", installment),
            ("months", installment),
            ("interval", installment),
            ("down_payment", installment or credit),
            ("expected_date", credit),
        ):
            label = self._labels[name]
            if label is not None:
                label.setVisible(visible)

    def values(self) -> dict[str, object]:
        """Return normalized inputs for the sale service."""
        sale_type = str(self.sale_type.currentData())
        if sale_type == "installment":
            rate = self.rate.value()
        elif sale_type == "credit" and self.sale.sale_type == "credit":
            rate = self.sale.rate
        else:
            rate = 0
        return {
            "product": self.product.text().strip(),
            "sale_type": sale_type,
            "wholesale_price": self.wholesale_price.value(),
            "cash_price": self.cash_price.value(),
            "rate": rate,
            "down_payment": self.down_payment.value() if sale_type != "cash" else 0,
            "months": self.months.value() if sale_type == "installment" else None,
            "payment_interval": min(self.interval.value(), self.months.value()) if sale_type == "installment" else 1,
            "purchase_date": self.purchase_date.date().toPython(),
            "expected_pay_date": (
                self.expected_date.date().toPython()
                if sale_type == "credit" and self.expected_date_enabled.isChecked()
                else None
            ),
        }
