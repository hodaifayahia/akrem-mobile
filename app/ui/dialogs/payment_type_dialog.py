"""Owner dialog for changing how a purchase is paid: cash, installment or credit."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import Sale
from app.db.session import session_scope
from app.i18n import ar
from app.services import calc, customers, sales as sales_service
from app.services.settings import get_rate_presets
from app.ui.events import events
from app.ui.widgets.sale_basket import sale_type_label

_TYPES = ("cash", "installment", "credit")


def received_after_purchase(sales: Sequence[Sale]) -> int:
    """What the client paid after the purchase (payments, not the down payment)."""
    total = 0
    for sale in sales:
        total += sum(payment.amount for payment in sale.credit_payments)
        total += sum(
            max(row.amount_paid, sum(payment.amount for payment in row.payments)) for row in sale.installments
        )
    return total


class PaymentTypeDialog(QDialog):
    """Pick the new payment method of one purchase and its plan; the sales service applies it."""

    def __init__(
        self, sales: Sequence[Sale], rate_presets: Mapping[int, int], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(ar.CHANGE_TYPE_TITLE)
        self.setMinimumWidth(440)
        self._presets = dict(rate_presets)
        first = sales[0]
        kinds = list(dict.fromkeys(sale.sale_type for sale in sales))
        installment = next((sale for sale in sales if sale.sale_type == "installment"), None)

        self.current = QLabel(
            ar.CHANGE_TYPE_CURRENT.format(kind=" + ".join(sale_type_label(kind) for kind in kinds)), self
        )
        received = received_after_purchase(sales)
        self.received = QLabel(ar.CHANGE_TYPE_RECEIVED.format(amount=f"{received:,} {ar.CURRENCY_SUFFIX}"), self)
        self.received.setObjectName("sectionHint")
        self.received.setWordWrap(True)
        self.received.setVisible(received > 0)

        self.sale_type = QComboBox(self)
        for kind in _TYPES:
            self.sale_type.addItem(sale_type_label(kind), kind)
        # Start on another method than the current one: that is why the dialog was opened.
        start = next((kind for kind in _TYPES if kind not in kinds), first.sale_type)
        self.sale_type.setCurrentIndex(self.sale_type.findData(start))
        self.months = QSpinBox(self)
        self.months.setRange(1, calc.MAX_PLAN_MONTHS)
        self.months.setValue(installment.months if installment and installment.months else 6)
        self.interval = QSpinBox(self)
        self.interval.setRange(1, self.months.value())
        self.interval.setSuffix(f" {ar.PURCHASE_MONTHS_SUFFIX}")
        self.interval.setSpecialValueText(ar.PLAN_EVERY_1)
        self.interval.setValue((installment.payment_interval or 1) if installment else 1)
        self.rate = QSpinBox(self)
        self.rate.setRange(0, 100)
        self.rate.setValue(installment.rate if installment else self._presets.get(self.months.value(), 0))
        self.months.valueChanged.connect(self._months_changed)
        self.down_payment = QSpinBox(self)
        self.down_payment.setRange(0, 2_000_000_000)
        self.down_payment.setGroupSeparatorShown(True)
        self.down_payment.setValue(sum(sale.down_payment for sale in sales if sale.sale_type != "cash"))
        self.expected_date_enabled = QCheckBox(ar.EXPECTED_PAY_DATE, self)
        expected = next((sale.expected_pay_date for sale in sales if sale.expected_pay_date), None)
        self.expected_date_enabled.setChecked(expected is not None)
        self.expected_date = QDateEdit(self)
        self.expected_date.setCalendarPopup(True)
        self.expected_date.setDisplayFormat("dd/MM/yyyy")
        self.expected_date.setDate(
            QDate(expected.year, expected.month, expected.day) if expected else QDate.currentDate().addDays(30)
        )
        self.expected_date_enabled.toggled.connect(self.expected_date.setEnabled)
        self.expected_date.setEnabled(self.expected_date_enabled.isChecked())
        self.cash_note = QLabel(ar.CHANGE_TYPE_CASH_NOTE, self)
        self.cash_note.setObjectName("sectionHint")
        self.cash_note.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.current)
        layout.addWidget(self.received)
        self.form = QFormLayout()
        self.form.addRow(ar.SALE_TYPE, self.sale_type)
        self.form.addRow(ar.MONTHS, self.months)
        self.form.addRow(ar.PLAN_INTERVAL, self.interval)
        self.form.addRow(ar.RATE, self.rate)
        self.form.addRow(ar.DOWN_PAYMENT, self.down_payment)
        self.form.addRow("", self.expected_date_enabled)
        self.form.addRow("", self.expected_date)
        layout.addLayout(self.form)
        layout.addWidget(self.cash_note)

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

        self.sale_type.currentIndexChanged.connect(self._update_fields)
        self._update_fields()

    def _months_changed(self, months: int) -> None:
        self.interval.setMaximum(months)
        if months in self._presets:
            self.rate.setValue(self._presets[months])

    def _update_fields(self, *_args: object) -> None:
        """Only show the fields the chosen method uses."""
        kind = self.sale_type.currentData()
        installment = kind == "installment"
        credit = kind == "credit"
        for widget, visible in (
            (self.months, installment),
            (self.interval, installment),
            (self.rate, installment),
            (self.down_payment, installment or credit),
            (self.expected_date_enabled, credit),
            (self.expected_date, credit),
        ):
            widget.setVisible(visible)
            label = self.form.labelForField(widget)
            if label is not None:
                label.setVisible(visible)
        self.cash_note.setVisible(kind == "cash")

    def values(self) -> dict[str, object]:
        """Keyword arguments for ``sales.change_sale_type``."""
        kind = str(self.sale_type.currentData())
        installment = kind == "installment"
        return {
            "sale_type": kind,
            "rate": self.rate.value() if installment else 0,
            "months": self.months.value() if installment else None,
            "payment_interval": min(self.interval.value(), self.months.value()) if installment else 1,
            "down_payment": self.down_payment.value() if kind != "cash" else 0,
            "expected_pay_date": (
                self.expected_date.date().toPython()
                if kind == "credit" and self.expected_date_enabled.isChecked()
                else None
            ),
        }


def change_purchase_payment_type(
    parent: QWidget, user_id: int, customer_id: int, sale_ids: Iterable[int], engine: Engine | None = None
) -> bool:
    """Ask for the new payment method of one purchase and apply it; True when it changed.

    Used by the client details window and the client list. Errors are shown
    to the owner; events tell the other pages to refresh.
    """
    ids = set(sale_ids)
    try:
        with session_scope(engine) as session:
            customer = customers.get_customer_details(session, customer_id)
            presets = get_rate_presets(session)
    except (ValueError, SQLAlchemyError):
        QMessageBox.warning(parent, ar.CHANGE_TYPE_TITLE, ar.CHANGE_TYPE_ERROR)
        return False
    chosen = tuple(sale for sale in customer.sales if sale.id in ids)
    if not chosen:
        return False
    dialog = PaymentTypeDialog(chosen, presets, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return False
    try:
        with session_scope(engine) as session:
            sales_service.change_sale_type(session, user_id, [sale.id for sale in chosen], **dialog.values())
    except ValueError as error:
        too_much = "more than the new total" in str(error) or "cannot exceed total" in str(error)
        QMessageBox.warning(parent, ar.CHANGE_TYPE_TITLE, ar.CHANGE_TYPE_TOO_MUCH if too_much else ar.CHANGE_TYPE_ERROR)
        return False
    except (PermissionError, RuntimeError, SQLAlchemyError):
        QMessageBox.warning(parent, ar.CHANGE_TYPE_TITLE, ar.CHANGE_TYPE_ERROR)
        return False
    for sale in chosen:
        events.sale_changed.emit(sale.id)
    events.customer_changed.emit(customer_id)
    events.data_changed.emit()
    events.notify.emit("success", ar.CHANGE_TYPE_TITLE, ar.CHANGE_TYPE_DONE, 4000)
    return True
