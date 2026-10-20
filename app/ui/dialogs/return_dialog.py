"""Owner dialog for a product the client brought back: which products, the refund, the date."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.db.models import Sale
from app.i18n import ar
from app.i18n.stock_text import ltr_text
from app.services import customers


class ReturnDialog(QDialog):
    """Pick the products brought back and what the shop gives back; ``sales.return_sales`` applies it."""

    def __init__(self, sales: Sequence[Sale], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(ar.RETURN_TITLE)
        self.setMinimumWidth(440)
        self._sales = [sale for sale in sales if sale.returned_at is None]
        self._received = {sale.id: customers.sale_paid(sale) for sale in self._sales}

        layout = QVBoxLayout(self)
        title = QLabel(ar.RETURN_PRODUCTS, self)
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.products: dict[int, QCheckBox] = {}
        for sale in self._sales:
            name = ltr_text(sale.product) + (f" ×{sale.quantity}" if (sale.quantity or 1) > 1 else "")
            box = QCheckBox(name, self)
            box.setChecked(len(self._sales) == 1)
            box.toggled.connect(self._selection_changed)
            self.products[sale.id] = box
            layout.addWidget(box)
        self.received = QLabel(self)
        self.received.setObjectName("sectionHint")
        layout.addWidget(self.received)

        self.refund = QSpinBox(self)
        self.refund.setGroupSeparatorShown(True)
        self.refund.setSuffix(f" {ar.CURRENCY_SUFFIX}")
        self.return_date = QDateEdit(QDate.currentDate(), self)
        self.return_date.setDisplayFormat("dd/MM/yyyy")
        self.return_date.setCalendarPopup(True)
        self.reason = QLineEdit(self)
        self.reason.setMaxLength(500)
        form = QFormLayout()
        form.addRow(ar.RETURN_REFUND, self.refund)
        form.addRow(ar.RETURN_DATE, self.return_date)
        form.addRow(ar.RETURN_REASON, self.reason)
        layout.addLayout(form)
        hint = QLabel(ar.RETURN_HINT, self)
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(self)
        self.save_button: QPushButton = buttons.addButton(ar.RETURN_ACTION, QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setProperty("variant", "primary")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel = buttons.addButton(ar.CUST_CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setProperty("variant", "secondary")
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self._selection_changed()

    def selected_ids(self) -> list[int]:
        return [sale_id for sale_id, box in self.products.items() if box.isChecked()]

    def _selection_changed(self, *_args: object) -> None:
        """The refund starts at everything the client paid for the ticked products."""
        paid = sum(self._received[sale_id] for sale_id in self.selected_ids())
        self.received.setText(ar.RETURN_RECEIVED.format(amount=f"{paid:,} {ar.CURRENCY_SUFFIX}"))
        self.refund.setRange(0, paid)
        self.refund.setValue(paid)
        self.save_button.setEnabled(bool(self.selected_ids()))

    def values(self) -> tuple[list[int], int, date, str | None]:
        """``(sale ids, refund, return date, reason)`` for ``sales.return_sales``."""
        return (
            self.selected_ids(),
            self.refund.value(),
            self.return_date.date().toPython(),
            self.reason.text().strip() or None,
        )
