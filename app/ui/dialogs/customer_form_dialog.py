"""Add/edit client form, shared by the clients page and the new-sale page.

The fields scroll inside the dialog while Save and Cancel stay at the bottom, and the dialog opens no larger than the
screen it appears on, so every field and button can be reached on a laptop.
"""

from __future__ import annotations

from PySide6.QtCore import QDate
from PySide6.QtGui import QIntValidator, QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.i18n import ar
from app.services.categories import ClientType
from app.services.customers import CustomerSummary
from app.ui.screen_fit import SCREEN_HEIGHT_SHARE, SCREEN_WIDTH_SHARE, fit_to_screen

__all__ = ["CustomerFormDialog", "SCREEN_HEIGHT_SHARE", "SCREEN_WIDTH_SHARE"]


class CustomerFormDialog(QDialog):
    """Add/edit customer profile details without performing database work."""

    def __init__(
        self,
        category_rows: list[tuple[ClientType, int]],
        customer: CustomerSummary | None,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(ar.CUST_ADD if customer is None else ar.CUST_EDIT)
        self.setMinimumWidth(520)
        self.customer = customer.customer if customer is not None else None
        self._fitted = False

        self.full_name = QLineEdit(self)
        self.phone = QLineEdit(self)
        self.phone.setMaxLength(20)
        self.phone.setPlaceholderText("05xxxxxxxx")
        self.category = QComboBox(self)
        for category, count in category_rows:
            self.category.addItem(f"{category.name} ({count})", category.id)

        self.id_number = QLineEdit(self)
        self.issue_date_enabled = QCheckBox(ar.CUST_ID_ISSUE_DATE, self)
        self.issue_date = QDateEdit(self)
        self.issue_date.setCalendarPopup(True)
        self.issue_date.setDisplayFormat("dd/MM/yyyy")
        self.issue_date.setDate(QDate.currentDate())
        self.issue_date.setEnabled(False)
        self.issue_date_enabled.toggled.connect(self.issue_date.setEnabled)
        self.issue_place = QLineEdit(self)
        self.address = QLineEdit(self)
        self.profession = QLineEdit(self)
        self.ccp_number = QLineEdit(self)
        self.cheques_enabled = QCheckBox(ar.CUST_CHEQUES_COUNT, self)
        self.cheques_count = QLineEdit(self)
        self.cheques_count.setValidator(QIntValidator(0, 2_000_000_000, self))
        self.cheques_count.setEnabled(False)
        self.cheques_enabled.toggled.connect(self.cheques_count.setEnabled)
        self.notes = QPlainTextEdit(self)
        self.notes.setMaximumHeight(100)

        form = QFormLayout()
        form.addRow(ar.CUST_COL_NAME, self.full_name)
        form.addRow(ar.CUST_COL_PHONE, self.phone)
        form.addRow(ar.CUST_COL_CATEGORY, self.category)
        form.addRow(ar.CUST_ID_NUMBER, self.id_number)
        issue_date_row = QWidget(self)
        issue_date_layout = QHBoxLayout(issue_date_row)
        issue_date_layout.setContentsMargins(0, 0, 0, 0)
        issue_date_layout.addWidget(self.issue_date_enabled)
        issue_date_layout.addWidget(self.issue_date, 1)
        form.addRow("", issue_date_row)
        form.addRow(ar.CUST_ID_ISSUE_PLACE, self.issue_place)
        form.addRow(ar.CUST_ADDRESS, self.address)
        form.addRow(ar.CUST_PROFESSION, self.profession)
        form.addRow(ar.CUST_CCP_NUMBER, self.ccp_number)
        cheque_row = QWidget(self)
        cheque_layout = QHBoxLayout(cheque_row)
        cheque_layout.setContentsMargins(0, 0, 0, 0)
        cheque_layout.addWidget(self.cheques_enabled)
        cheque_layout.addWidget(self.cheques_count, 1)
        form.addRow("", cheque_row)
        form.addRow(ar.CUST_NOTES, self.notes)

        buttons = QHBoxLayout()
        self.cancel_button = QPushButton(ar.CUST_CANCEL, self)
        self.cancel_button.setProperty("variant", "secondary")
        self.save_button = QPushButton(ar.CUST_SAVE, self)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self.accept)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)

        self.body = QWidget(self)
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addLayout(form)
        body_layout.addStretch(1)
        self.scroll = QScrollArea(self)
        self.scroll.setWidget(self.body)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.addWidget(self.scroll, 1)
        layout.addLayout(buttons)
        self._load_customer()

    def values(self) -> dict[str, object]:
        """Return validated-shape form values; service validation remains authoritative."""
        cheque_text = self.cheques_count.text().strip()
        return {
            "full_name": self.full_name.text(),
            "phone": self.phone.text(),
            "category_id": self.category.currentData(),
            "id_number": self._optional_text(self.id_number.text()),
            "id_issue_date": (
                self.issue_date.date().toPython() if self.issue_date_enabled.isChecked() else None
            ),
            "id_issue_place": self._optional_text(self.issue_place.text()),
            "address": self._optional_text(self.address.text()),
            "profession": self._optional_text(self.profession.text()),
            "ccp_number": self._optional_text(self.ccp_number.text()),
            "cheques_count": int(cheque_text) if self.cheques_enabled.isChecked() and cheque_text else None,
            "notes": self._optional_text(self.notes.toPlainText()),
        }

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt event override
        """Fit the dialog to its screen the first time it opens."""
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            self.fit_to_screen()

    def fit_to_screen(self) -> None:
        """Size to the content, never past the free screen area, and center over the parent."""
        # Everything around the scroll area (margins, buttons) plus the whole
        # body, with room for a vertical scroll bar when the body is cut short.
        chrome = self.sizeHint() - self.scroll.sizeHint()
        wanted = self.body.sizeHint() + chrome
        wanted.setWidth(wanted.width() + self.scroll.verticalScrollBar().sizeHint().width())
        fit_to_screen(self, wanted)

    def _load_customer(self) -> None:
        """Populate the form when editing an existing row."""
        if self.customer is None:
            return
        customer = self.customer
        self.full_name.setText(customer.full_name)
        self.phone.setText(customer.phone or "")
        category_index = self.category.findData(customer.category_id)
        if category_index >= 0:
            self.category.setCurrentIndex(category_index)
        self.id_number.setText(customer.id_number or "")
        if customer.id_issue_date is not None:
            self.issue_date_enabled.setChecked(True)
            self.issue_date.setDate(QDate(customer.id_issue_date.year, customer.id_issue_date.month,
                                          customer.id_issue_date.day))
        self.issue_place.setText(customer.id_issue_place or "")
        self.address.setText(customer.address or "")
        self.profession.setText(customer.profession or "")
        self.ccp_number.setText(customer.ccp_number or "")
        if customer.cheques_count is not None:
            self.cheques_enabled.setChecked(True)
            self.cheques_count.setText(str(customer.cheques_count))
        self.notes.setPlainText(customer.notes or "")

    @staticmethod
    def _optional_text(value: str) -> str | None:
        return value.strip() or None
