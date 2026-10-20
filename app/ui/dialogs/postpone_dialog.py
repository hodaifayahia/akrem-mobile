"""Choose how to postpone an installment the client cannot pay (تأجيل الدفعة)."""

from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.i18n import ar
from app.services import postponement

QUICK_DAYS = (3, 5, 10)


class PostponeDialog(QDialog):
    """The three ways to postpone: a few days, two parts next month, or one more month."""

    def __init__(
        self,
        installment_index: int,
        due_date: date,
        amount_left: int,
        *,
        can_move_to_next: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(ar.POSTPONE_TITLE)
        self.setMinimumWidth(460)
        self._due_date = due_date
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        heading = QLabel(ar.POSTPONE_HEADING.format(
            n=installment_index, date=due_date.strftime("%d/%m/%Y"),
            amount=f"{amount_left:,} {ar.CURRENCY_SUFFIX}",
        ), self)
        heading.setObjectName("sectionTitle")
        heading.setWordWrap(True)
        layout.addWidget(heading)

        self.choices = QButtonGroup(self)
        self.days_option = self._option(layout, ar.POSTPONE_DAYS, ar.POSTPONE_DAYS_HINT)
        days_row = QHBoxLayout()
        self.days = QSpinBox(self)
        self.days.setRange(1, postponement.MAX_DAYS)
        self.days.setValue(5)
        self.days.setSuffix(f" {ar.POSTPONE_DAYS_UNIT}")
        days_row.addWidget(self.days)
        for value in QUICK_DAYS:
            quick = QPushButton(f"{value} {ar.POSTPONE_DAYS_UNIT}", self)
            quick.setProperty("variant", "secondary")
            quick.setProperty("compact", True)
            quick.clicked.connect(lambda _checked=False, days=value: self._pick_days(days))
            days_row.addWidget(quick)
        days_row.addStretch(1)
        layout.addLayout(days_row)
        self.new_date = QLabel(self)
        self.new_date.setObjectName("sectionHint")
        layout.addWidget(self.new_date)

        self.next_option = self._option(
            layout, ar.POSTPONE_NEXT_MONTH,
            ar.POSTPONE_NEXT_MONTH_HINT.format(amount=f"{amount_left:,} {ar.CURRENCY_SUFFIX}")
            if can_move_to_next else ar.POSTPONE_NEXT_MONTH_LAST,
        )
        self.next_option.setEnabled(can_move_to_next)
        self.month_option = self._option(layout, ar.POSTPONE_EXTRA_MONTH, ar.POSTPONE_EXTRA_MONTH_HINT)

        self.reason = QLineEdit(self)
        self.reason.setPlaceholderText(ar.POSTPONE_REASON)
        layout.addWidget(self.reason)

        buttons = QDialogButtonBox(self)
        self.save_button = buttons.addButton(ar.POSTPONE_SAVE, QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setProperty("variant", "primary")
        cancel = buttons.addButton(ar.PAYMENT_CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setProperty("variant", "secondary")
        self.save_button.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)

        self.days_option.setChecked(True)
        self.days.valueChanged.connect(self._refresh)
        self.choices.buttonToggled.connect(lambda *_args: self._refresh())
        self._refresh()

    def _option(self, layout: QVBoxLayout, title: str, hint: str) -> QRadioButton:
        """A radio choice with a muted explanation under it."""
        separator = QFrame(self)
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setObjectName("divider")
        layout.addWidget(separator)
        option = QRadioButton(title, self)
        self.choices.addButton(option)
        layout.addWidget(option)
        explanation = QLabel(hint, self)
        explanation.setObjectName("sectionHint")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        return option

    def _pick_days(self, days: int) -> None:
        self.days_option.setChecked(True)
        self.days.setValue(days)

    def _refresh(self) -> None:
        by_days = self.days_option.isChecked()
        self.days.setEnabled(by_days)
        self.new_date.setVisible(by_days)
        new_date = self._due_date + timedelta(days=self.days.value())
        self.new_date.setText(ar.POSTPONE_NEW_DATE.format(date=new_date.strftime("%d/%m/%Y")))

    def values(self) -> tuple[str, int | None, str | None]:
        """``(kind, days, reason)`` for ``postponement.postpone_installment``."""
        if self.next_option.isChecked():
            kind = postponement.KIND_NEXT_MONTH
        elif self.month_option.isChecked():
            kind = postponement.KIND_EXTRA_MONTH
        else:
            kind = postponement.KIND_DAYS
        days = self.days.value() if kind == postponement.KIND_DAYS else None
        return kind, days, self.reason.text().strip() or None
