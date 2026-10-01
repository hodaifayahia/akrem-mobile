"""A customer's history: what they bought, how they pay, and every payment.

The summary strip answers "who is this customer for us?" at a glance:
purchase count, money bought/paid/remaining, and whether they pay cash or
little by little. The purchase table lists each sale with its type tag and
current status; the payment table is the full payment timeline.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.i18n import ar
from app.services.customers import CustomerHistory, PurchaseRecord
from app.services.status import Status
from app.i18n.plan_text import plan_summary
from app.ui.theme import qcolor

_TYPE_TONES = {"cash": "paid", "installment": "primary-glow", "credit": "pending"}
_STATUS_TONES = {Status.PAID: "paid", Status.PENDING: "pending", Status.FAILED: "failed", Status.NONE: "text-muted"}


def type_label(kind: str) -> str:
    """Localized sale type."""
    return {"cash": ar.SALE_CASH, "installment": ar.SALE_INSTALLMENT, "credit": ar.SALE_CREDIT}.get(kind, kind)


def status_label(status: Status) -> str:
    """Localized monthly status."""
    return {
        Status.PAID: ar.CUST_STATUS_TIP_PAID,
        Status.PENDING: ar.CUST_STATUS_TIP_PENDING,
        Status.FAILED: ar.CUST_STATUS_TIP_FAILED,
        Status.NONE: ar.HIST_STATUS_NONE,
    }[status]


def money(amount: int | None) -> str:
    """Whole dinars with separators, or a dash."""
    return "—" if amount is None else f"{amount:,} {ar.CURRENCY_SUFFIX}"


class HistorySummary(QFrame):
    """Headline numbers for one customer."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        grid = QGridLayout(self)
        grid.setContentsMargins(16, 12, 16, 12)
        grid.setHorizontalSpacing(22)
        grid.setVerticalSpacing(2)
        self.values: dict[str, QLabel] = {}
        for column, (key, caption) in enumerate((
            ("count", ar.HIST_PURCHASES),
            ("bought", ar.HIST_TOTAL_BOUGHT),
            ("paid", ar.HIST_TOTAL_PAID),
            ("remaining", ar.HIST_REMAINING),
        )):
            caption_label = QLabel(caption, self)
            caption_label.setObjectName("kpiCaption")
            value = QLabel("—", self)
            value.setObjectName("moneyValue")
            grid.addWidget(caption_label, 0, column)
            grid.addWidget(value, 1, column)
            self.values[key] = value
        self.profile = QLabel(self)
        self.mix = QLabel(self)
        self.mix.setObjectName("kpiCaption")
        profile_box = QVBoxLayout()
        profile_box.setSpacing(2)
        profile_box.addWidget(self.profile, 0, Qt.AlignmentFlag.AlignLeading)
        profile_box.addWidget(self.mix)
        grid.addLayout(profile_box, 0, 4, 2, 1)
        grid.setColumnStretch(4, 1)

    def show_history(self, history: CustomerHistory) -> None:
        """Fill the strip from a history record."""
        self.values["count"].setText(str(history.purchase_count))
        self.values["bought"].setText(money(history.total_bought))
        self.values["paid"].setText(money(history.total_paid))
        self.values["remaining"].setText(money(history.remaining))
        self.values["remaining"].setStyleSheet(
            f"color: {qcolor('failed' if history.overdue_installments else 'text').name()};"
        )
        profile = history.payment_profile
        self.profile.setText({
            "cash": ar.HIST_PROFILE_CASH,
            "facilities": ar.HIST_PROFILE_FACILITIES,
            "none": ar.HIST_PROFILE_NONE,
        }[profile])
        self.profile.setProperty("pill", {"cash": "paid", "facilities": "info", "none": "pending"}[profile])
        self.profile.style().unpolish(self.profile)
        self.profile.style().polish(self.profile)
        mix = ar.HIST_MIX.format(
            cash=history.cash_purchases,
            installment=history.installment_purchases,
            credit=history.credit_purchases,
        )
        if history.overdue_installments:
            mix += " · " + ar.HIST_OVERDUE.format(count=history.overdue_installments)
        self.mix.setText(mix)


class CustomerHistoryPanel(QWidget):
    """Purchases and payment timeline; double-click a purchase to open it."""

    purchase_activated = Signal(int)

    def __init__(self, *, is_owner: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.is_owner = is_owner
        self._purchases: tuple[PurchaseRecord, ...] = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()
        purchases_title = QLabel(ar.HIST_PURCHASES_TITLE, self)
        purchases_title.setObjectName("sectionTitle")
        header.addWidget(purchases_title, 1)
        self.dates_label = QLabel(self)
        self.dates_label.setObjectName("sectionHint")
        header.addWidget(self.dates_label)
        layout.addLayout(header)
        self.purchases_table = self._table([
            ar.HIST_COL_DATE, ar.PRODUCT, ar.HIST_COL_TYPE, ar.HIST_COL_TOTAL, ar.HIST_COL_PAID,
            ar.HIST_REMAINING, ar.HIST_COL_PLAN, ar.HIST_COL_STATUS,
        ])
        self.purchases_table.cellDoubleClicked.connect(self._activate)
        layout.addWidget(self.purchases_table, 3)

        payments_title = QLabel(ar.HIST_PAYMENTS_TITLE, self)
        payments_title.setObjectName("sectionTitle")
        layout.addWidget(payments_title)
        self.payments_table = self._table([
            ar.HIST_COL_DATE, ar.HIST_COL_AMOUNT, ar.HIST_COL_METHOD, ar.PRODUCT,
            ar.HIST_COL_INSTALLMENT, ar.CUST_NOTES,
        ])
        layout.addWidget(self.payments_table, 2)
        self.empty_payments = QLabel(ar.HIST_NO_PAYMENTS, self)
        self.empty_payments.setObjectName("emptyStateSub")
        self.empty_payments.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_payments)

    def _table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers), self)
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(36)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        return table

    def show_history(self, history: CustomerHistory) -> None:
        """Fill both tables."""
        self._purchases = history.purchases
        self.purchases_table.setRowCount(len(history.purchases))
        for row, purchase in enumerate(history.purchases):
            plan = (
                plan_summary(purchase.monthly_amount or 0, purchase.payment_interval, purchase.payment_count)
                if purchase.sale_type == "installment" and purchase.months
                else ar.HIST_PLAN_CASH if purchase.sale_type == "cash"
                else ar.HIST_PLAN_CREDIT
            )
            cells = [
                (purchase.purchase_date.strftime("%d/%m/%Y"), None),
                (purchase.product, None),
                (type_label(purchase.sale_type), _TYPE_TONES.get(purchase.sale_type)),
                (money(purchase.total), None),
                (money(purchase.paid), "paid" if purchase.paid else None),
                (money(purchase.remaining), "failed" if purchase.overdue_installments else None),
                (plan, None),
                (status_label(purchase.status), _STATUS_TONES[purchase.status]),
            ]
            self._fill_row(self.purchases_table, row, cells)
            self.purchases_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, purchase.sale_id)

        self.payments_table.setRowCount(len(history.payments))
        for row, payment in enumerate(history.payments):
            installment = (
                ar.HIST_INSTALLMENT_N.format(n=payment.installment_index)
                if payment.installment_index is not None
                else type_label(payment.sale_type)
            )
            self._fill_row(self.payments_table, row, [
                (payment.payment_date.strftime("%d/%m/%Y"), None),
                (money(payment.amount), "paid"),
                (payment.method, None),
                (payment.product, None),
                (installment, None),
                (payment.note or "", None),
            ])
        self.empty_payments.setVisible(not history.payments)
        self.payments_table.setVisible(bool(history.payments))
        first = history.first_purchase.strftime("%d/%m/%Y") if history.first_purchase else "—"
        last_payment = history.last_payment.strftime("%d/%m/%Y") if history.last_payment else "—"
        self.dates_label.setText(ar.HIST_DATES.format(first=first, last_payment=last_payment))

    @staticmethod
    def _fill_row(table: QTableWidget, row: int, cells: list[tuple[str, str | None]]) -> None:
        for column, (text, tone) in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if tone is not None:
                item.setForeground(QColor(qcolor(tone)))
            table.setItem(row, column, item)

    def _activate(self, row: int, _column: int) -> None:
        if 0 <= row < len(self._purchases):
            self.purchase_activated.emit(self._purchases[row].sale_id)
