"""Monthly installment collection and credit-payment screen."""

from __future__ import annotations

import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from PySide6.QtCore import QDate, QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload, selectinload

from app.db.models import Installment, Payment, Sale, Setting, User
from app.db.session import session_scope
from app.i18n import ar
from app.services import payments as payment_service
from app.services.reminders import whatsapp_link
from app.services.status import Status, for_month
from app.ui.events import events


def _payment_methods() -> tuple[tuple[str, str], ...]:
    """Labels in the active language (evaluated at call time)."""
    return (
        (ar.PAY_METHOD_CASH, "cash"),
        (ar.PAY_METHOD_CCP, "CCP"),
        (ar.PAY_METHOD_BARIDIMOB, "BaridiMob"),
    )


@dataclass(frozen=True, slots=True)
class ReminderItem:
    """One installment or credit balance that needs a customer reminder."""

    customer: str
    phone: str | None
    product: str
    due_date: date
    remaining: int


class PaymentsPage(QWidget):
    """Show monthly installment groups, credit balances, and payment actions."""

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self._build_ui()
        events.data_changed.connect(self.refresh)
        events.payment_changed.connect(self.refresh)
        self.refresh()

    def _build_ui(self) -> None:
        """Build a month selector and scrollable status groups."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(14)

        heading_row = QHBoxLayout()
        heading = QLabel(ar.PAY_PAGE_TITLE, self)
        heading.setObjectName("pageTitle")
        heading_row.addWidget(heading)
        heading_row.addStretch(1)

        month_container = QWidget(self)
        month_layout = QHBoxLayout(month_container)
        month_layout.setContentsMargins(0, 0, 0, 0)
        month_layout.setSpacing(8)
        month_label = QLabel(f"📅  {ar.PAY_MONTH}:", month_container)
        month_label.setStyleSheet("color: #9DBEFF; font-weight: 600; font-size: 13px;")
        month_layout.addWidget(month_label)
        self.month_selector = QDateEdit(month_container)
        self.month_selector.setCalendarPopup(True)
        self.month_selector.setDisplayFormat("MM/yyyy")
        self.month_selector.setDate(QDate.currentDate())
        self.month_selector.setMinimumWidth(130)
        self.month_selector.dateChanged.connect(self.refresh)
        month_layout.addWidget(self.month_selector)
        heading_row.addWidget(month_container)

        self.undo_button = QPushButton(f"↩️  {ar.PAY_UNDO_LAST}", self)
        self.undo_button.setProperty("variant", "secondary")
        self.undo_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_button.clicked.connect(self._undo_last_payment)
        self.undo_button.setVisible(self.current_user.role == "owner")
        heading_row.addWidget(self.undo_button)
        layout.addLayout(heading_row)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.content = QWidget(self.scroll_area)
        self.sections_layout = QVBoxLayout(self.content)
        self.sections_layout.setContentsMargins(0, 0, 0, 0)
        self.sections_layout.setSpacing(16)

        self.failed_section, self.failed_table, self.failed_empty = self._make_section(
            ar.PAY_FAILED_GROUP
        )
        self.failed_section.setStyleSheet(
            "QGroupBox { border-color: #EF444455; border-top: 2px solid #EF4444; } "
            "QGroupBox::title { color: #EF4444; border-color: #EF444455; }"
        )
        self.pending_section, self.pending_table, self.pending_empty = self._make_section(
            ar.PAY_PENDING_GROUP
        )
        self.pending_section.setStyleSheet(
            "QGroupBox { border-color: #F59E0B55; border-top: 2px solid #F59E0B; } "
            "QGroupBox::title { color: #F59E0B; border-color: #F59E0B55; }"
        )
        self.paid_section, self.paid_table, self.paid_empty = self._make_section(
            ar.PAY_PAID_GROUP
        )
        self.paid_section.setStyleSheet(
            "QGroupBox { border-color: #22C55E55; border-top: 2px solid #22C55E; } "
            "QGroupBox::title { color: #22C55E; border-color: #22C55E55; }"
        )
        self.reminders_section, self.reminders_table, self.reminders_empty = (
            self._make_reminders_section()
        )
        self.reminders_section.setStyleSheet(
            "QGroupBox { border-color: #3B92D955; border-top: 2px solid #3B92D9; } "
            "QGroupBox::title { color: #9DBEFF; border-color: #3B92D955; }"
        )
        self.credit_section, self.credit_table, self.credit_empty = self._make_credit_section()
        self.credit_section.setStyleSheet(
            "QGroupBox { border-color: #A78BFA55; border-top: 2px solid #A78BFA; } "
            "QGroupBox::title { color: #C4B5FD; border-color: #A78BFA55; }"
        )
        for section in (
            self.reminders_section,
            self.failed_section,
            self.pending_section,
            self.paid_section,
            self.credit_section,
        ):
            self.sections_layout.addWidget(section)
        self.sections_layout.addStretch(1)
        self.scroll_area.setWidget(self.content)
        layout.addWidget(self.scroll_area, 1)

    def _make_section(
        self, title: str
    ) -> tuple[QGroupBox, QTableWidget, QLabel]:
        """Create a status group with its installment table and empty hint."""
        box = QGroupBox(title, self.content)
        box_layout = QVBoxLayout(box)
        box_layout.setSpacing(10)
        empty = QLabel(f"ℹ️  {ar.PAY_EMPTY}", box)
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setStyleSheet(
            "color: #8A94A6; padding: 18px; font-size: 13px; background-color: #0E141D; "
            "border: 1px dashed #1F2A3D; border-radius: 8px;"
        )
        table = QTableWidget(0, 7, box)
        table.setHorizontalHeaderLabels(
            (
                ar.PAY_COL_CUSTOMER,
                ar.PAY_COL_PRODUCT,
                ar.PAY_COL_DUE_DATE,
                ar.PAY_COL_AMOUNT_DUE,
                ar.PAY_COL_AMOUNT_PAID,
                ar.PAY_COL_REMAINING,
                ar.PAY_COL_ACTION,
            )
        )
        self._configure_table(table)
        box_layout.addWidget(empty)
        box_layout.addWidget(table)
        return box, table, empty

    def _make_reminders_section(self) -> tuple[QGroupBox, QTableWidget, QLabel]:
        """Create a list of overdue and near-due customer reminders."""
        box = QGroupBox(f"🔔  {ar.PAY_REMINDERS_TITLE}", self.content)
        box_layout = QVBoxLayout(box)
        box_layout.setSpacing(10)
        empty = QLabel(f"🎉  {ar.PAY_REMINDERS_EMPTY}", box)
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setStyleSheet(
            "color: #8A94A6; padding: 18px; font-size: 13px; background-color: #0E141D; "
            "border: 1px dashed #1F2A3D; border-radius: 8px;"
        )
        table = QTableWidget(0, 5, box)
        table.setHorizontalHeaderLabels(
            (
                ar.PAY_COL_CUSTOMER,
                ar.PAY_COL_PRODUCT,
                ar.PAY_COL_DUE_DATE,
                ar.PAY_COL_REMAINING,
                ar.PAY_REMINDERS_ACTION,
            )
        )
        self._configure_table(table)
        table.setMaximumHeight(250)
        box_layout.addWidget(empty)
        box_layout.addWidget(table)
        return box, table, empty

    def _make_credit_section(self) -> tuple[QGroupBox, QTableWidget, QLabel]:
        """Create the credit balance and payment-history group."""
        box = QGroupBox(f"💳  {ar.PAY_CREDIT_GROUP}", self.content)
        box_layout = QVBoxLayout(box)
        box_layout.setSpacing(10)
        empty = QLabel(f"ℹ️  {ar.PAY_EMPTY_CREDIT}", box)
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setStyleSheet(
            "color: #8A94A6; padding: 18px; font-size: 13px; background-color: #0E141D; "
            "border: 1px dashed #1F2A3D; border-radius: 8px;"
        )
        table = QTableWidget(0, 7, box)
        table.setHorizontalHeaderLabels(
            (
                ar.PAY_COL_CUSTOMER,
                ar.PAY_COL_PRODUCT,
                ar.PAY_COL_AMOUNT_DUE,
                ar.PAY_COL_AMOUNT_PAID,
                ar.PAY_COL_REMAINING,
                ar.PAY_COL_EXPECTED_DATE,
                ar.PAY_COL_HISTORY,
            )
        )
        self._configure_table(table)
        box_layout.addWidget(empty)
        box_layout.addWidget(table)
        return box, table, empty

    @staticmethod
    def _configure_table(table: QTableWidget) -> None:
        """Apply consistent read-only table behavior."""
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setMinimumSectionSize(55)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        if table.columnCount() == 5:
            widths = (160, 130, 105, 115, 135)
        elif table.horizontalHeaderItem(2).text() == ar.PAY_COL_DUE_DATE:
            widths = (160, 130, 105, 115, 105, 110, 115)
        else:
            widths = (155, 125, 115, 105, 110, 120, 130)
        for column, width in enumerate(widths):
            table.setColumnWidth(column, width)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        if table.columnCount() > 1:
            table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.setMinimumHeight(90)

    def refresh(self, *_args: object) -> None:
        """Reload selected-month installments and all credit balances."""
        selected = self.month_selector.date()
        year, month = selected.year(), selected.month()
        today = date.today()
        month_end = date(year, month, monthrange(year, month)[1])
        reminder_cutoff = today + timedelta(days=3)
        with session_scope() as session:
            grace_days = _grace_days(session)
            installments = session.scalars(
                select(Installment)
                .options(
                    selectinload(Installment.payments),
                    joinedload(Installment.sale).joinedload(Sale.customer),
                )
                .where(
                    Installment.due_date <= month_end,
                )
                .order_by(Installment.due_date, Installment.sale_id, Installment.installment_index)
            ).all()
            credit_sales = session.scalars(
                select(Sale)
                .options(
                    joinedload(Sale.customer),
                    selectinload(Sale.credit_payments),
                )
                .where(Sale.sale_type == "credit")
                .order_by(Sale.expected_pay_date, Sale.purchase_date, Sale.id)
            ).all()
            reminder_installments = session.scalars(
                select(Installment)
                .options(
                    selectinload(Installment.payments),
                    joinedload(Installment.sale).joinedload(Sale.customer),
                )
                .where(Installment.due_date <= reminder_cutoff)
                .order_by(Installment.due_date, Installment.sale_id, Installment.installment_index)
            ).all()
            reminders: list[ReminderItem] = []
            for installment in reminder_installments:
                remaining = installment.amount_due - _installment_paid(installment)
                due = installment.due_date
                if remaining <= 0 or not _needs_reminder(due, today, reminder_cutoff, grace_days):
                    continue
                customer = installment.sale.customer
                reminders.append(ReminderItem(
                    customer.full_name if customer else "",
                    customer.phone if customer else None,
                    installment.sale.product,
                    due,
                    remaining,
                ))
            for sale in credit_sales:
                due = sale.expected_pay_date
                if due is None or not _needs_reminder(due, today, reminder_cutoff, grace_days):
                    continue
                remaining = sale.financed - sum(
                    payment.amount for payment in sale.credit_payments
                )
                if remaining <= 0:
                    continue
                customer = sale.customer
                reminders.append(ReminderItem(
                    customer.full_name if customer else "",
                    customer.phone if customer else None,
                    sale.product,
                    due,
                    remaining,
                ))
            reminders.sort(key=lambda item: (item.due_date, item.customer, item.product))
            self._render_reminders(reminders)

            groups: dict[Status, list[Installment]] = {
                Status.FAILED: [],
                Status.PENDING: [],
                Status.PAID: [],
            }
            for installment in installments:
                installment_status = for_month(
                    (installment,), year, month, today, grace_days=grace_days
                )
                if installment_status in groups:
                    groups[installment_status].append(installment)

            self._render_installments(
                self.failed_section,
                self.failed_table,
                self.failed_empty,
                groups[Status.FAILED],
                allow_payment=True,
            )
            self._render_installments(
                self.pending_section,
                self.pending_table,
                self.pending_empty,
                groups[Status.PENDING],
                allow_payment=True,
            )
            self._render_installments(
                self.paid_section,
                self.paid_table,
                self.paid_empty,
                groups[Status.PAID],
                allow_payment=False,
            )
            self._render_credit(credit_sales)

    def _render_reminders(self, reminders: list[ReminderItem]) -> None:
        """Display unpaid obligations due within three days or earlier."""
        self.reminders_table.setRowCount(len(reminders))
        self.reminders_empty.setVisible(not reminders)
        self.reminders_table.setVisible(bool(reminders))
        for row_index, reminder in enumerate(reminders):
            values = (
                reminder.customer,
                reminder.product,
                _format_date(reminder.due_date),
                _format_money(reminder.remaining),
            )
            for column, value in enumerate(values):
                self._set_cell(self.reminders_table, row_index, column, value)
            button = QPushButton(f"💬  {ar.PAY_REMINDERS_ACTION}", self.reminders_table)
            button.setProperty("variant", "whatsapp")
            button.setProperty("compact", True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setEnabled(bool(reminder.phone))
            if reminder.phone:
                button.clicked.connect(
                    lambda _checked=False, number=reminder.phone, name=reminder.customer,
                    amount=reminder.remaining, due=reminder.due_date:
                    self._open_whatsapp_reminder(number, name, amount, due)
                )
            self.reminders_table.setCellWidget(row_index, 4, button)
        self.reminders_table.resizeRowsToContents()

    @staticmethod
    def _open_whatsapp_reminder(phone: str, customer_name: str, amount: int, due_date: date) -> None:
        """Open WhatsApp with a drafted reminder for the operator to review and send."""
        message = ar.PAY_REMINDERS_MESSAGE.format(
            customer=customer_name,
            amount=_format_money(amount),
            due=_format_date(due_date),
        )
        link = whatsapp_link(phone, message)
        if link is not None:
            QDesktopServices.openUrl(QUrl(link))

    def _render_installments(
        self,
        section: QGroupBox,
        table: QTableWidget,
        empty_hint: QLabel,
        installments: list[Installment],
        *,
        allow_payment: bool,
    ) -> None:
        """Populate one of the three monthly installment groups."""
        section.setTitle(
            ar.PAY_GROUP_HEADER.format(
                title=section.title().split(" (", 1)[0],
                count=len(installments),
                amount=_format_money(sum(item.amount_due for item in installments)),
            )
        )
        table.setRowCount(len(installments))
        empty_hint.setVisible(not installments)
        table.setVisible(bool(installments))
        for row_index, installment in enumerate(installments):
            sale = installment.sale
            customer_name = sale.customer.full_name if sale.customer else ""
            paid = _installment_paid(installment)
            remaining = max(0, installment.amount_due - paid)
            values = (
                customer_name,
                sale.product,
                _format_date(installment.due_date),
                _format_money(installment.amount_due),
                _format_money(paid),
                _format_money(remaining),
            )
            for column, value in enumerate(values):
                self._set_cell(table, row_index, column, value)
            if allow_payment and remaining > 0:
                button = QPushButton(f"💳  {ar.PAY_RECORD}", table)
                button.setProperty("variant", "success")
                button.setProperty("compact", True)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.clicked.connect(
                    lambda _checked=False, item_id=installment.id, max_amount=remaining:
                    self._record_installment_payment(item_id, max_amount)
                )
                table.setCellWidget(row_index, 6, button)
            else:
                self._set_cell(table, row_index, 6, f"✅  {ar.PAY_STATE_PAID}")
        table.resizeRowsToContents()
        table.setMinimumHeight(max(90, table.rowHeight(0) * len(installments) + 36))

    def _render_credit(self, sales: list[Sale]) -> None:
        """Populate credit balances with recent-payment history actions."""
        self.credit_table.setRowCount(len(sales))
        self.credit_empty.setVisible(not sales)
        self.credit_table.setVisible(bool(sales))
        for row_index, sale in enumerate(sales):
            payments = sorted(
                sale.credit_payments,
                key=lambda payment: (payment.payment_date, payment.id or 0),
            )
            paid = sum(payment.amount for payment in payments)
            remaining = max(0, sale.financed - paid)
            values = (
                sale.customer.full_name if sale.customer else "",
                sale.product,
                _format_money(sale.financed),
                _format_money(paid),
                _format_money(remaining),
                _format_date(sale.expected_pay_date) if sale.expected_pay_date else "—",
            )
            for column, value in enumerate(values):
                self._set_cell(self.credit_table, row_index, column, value)
            actions = QWidget(self.credit_table)
            action_layout = QHBoxLayout(actions)
            action_layout.setContentsMargins(2, 0, 2, 0)
            action_layout.setSpacing(6)
            if remaining > 0:
                record_button = QPushButton(f"💳  {ar.PAY_RECORD_CREDIT}", actions)
                record_button.setProperty("variant", "success")
                record_button.setProperty("compact", True)
                record_button.setCursor(Qt.CursorShape.PointingHandCursor)
                record_button.clicked.connect(
                    lambda _checked=False, sale_id=sale.id, balance=remaining:
                    self._record_credit_payment(sale_id, balance)
                )
                action_layout.addWidget(record_button)
            history_button = QPushButton(f"📜  {ar.PAY_COL_HISTORY}", actions)
            history_button.setProperty("variant", "secondary")
            history_button.setProperty("compact", True)
            history_button.setCursor(Qt.CursorShape.PointingHandCursor)
            history_button.clicked.connect(
                lambda _checked=False, entries=tuple(payments):
                self._show_payment_history(entries)
            )
            action_layout.addWidget(history_button)
            self.credit_table.setCellWidget(row_index, 6, actions)
        self.credit_table.resizeRowsToContents()
        self.credit_table.setMinimumHeight(max(90, self.credit_table.rowHeight(0) * len(sales) + 36))

    def _record_installment_payment(self, installment_id: int, remaining: int) -> None:
        """Open the payment dialog and persist an installment collection."""
        dialog = PaymentDialog(remaining, parent=self)
        dialog.setWindowTitle(ar.PAY_DIALOG_TITLE)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        amount, paid_date, method, note = dialog.values()
        try:
            with session_scope() as session:
                payment = payment_service.record_installment_payment_for_user(
                    session,
                    int(self.current_user.id),
                    installment_id,
                    amount,
                    paid_date,
                    method,
                    note,
                )
                payment_id = payment.id
        except (PermissionError, ValueError, RuntimeError, SQLAlchemyError):
            QMessageBox.warning(self, ar.PAY_PAGE_TITLE, ar.PAY_SAVE_ERROR)
            return
        self._payment_saved(payment_id)

    def _record_credit_payment(self, sale_id: int, remaining: int) -> None:
        """Open the payment dialog and persist a credit-sale collection."""
        dialog = PaymentDialog(remaining, parent=self)
        dialog.setWindowTitle(ar.PAY_CREDIT_DIALOG_TITLE)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        amount, paid_date, method, note = dialog.values()
        try:
            with session_scope() as session:
                payment = payment_service.record_credit_payment_for_user(
                    session,
                    int(self.current_user.id),
                    sale_id,
                    amount,
                    paid_date,
                    method,
                    note,
                )
                payment_id = payment.id
        except (PermissionError, ValueError, RuntimeError, SQLAlchemyError):
            QMessageBox.warning(self, ar.PAY_PAGE_TITLE, ar.PAY_SAVE_ERROR)
            return
        self._payment_saved(payment_id)

    def _payment_saved(self, payment_id: int) -> None:
        """Notify dependent screens and confirm the collection."""
        events.payment_changed.emit(payment_id)
        events.data_changed.emit()
        events.notify.emit("success", ar.PAY_PAGE_TITLE, ar.PAY_SAVE_DONE, 4000)
        QMessageBox.information(self, ar.PAY_PAGE_TITLE, ar.PAY_SAVE_DONE)

    def _undo_last_payment(self) -> None:
        """Undo the latest payment with explicit confirmation and a reason."""
        if self.current_user.role != "owner":
            return
        answer = QMessageBox.question(
            self,
            ar.PAY_UNDO_LAST,
            ar.PAY_UNDO_CONFIRM,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        reason, accepted = QInputDialog.getMultiLineText(
            self, ar.PAY_UNDO_LAST, ar.PAY_UNDO_REASON
        )
        if not accepted or not reason.strip():
            QMessageBox.warning(self, ar.PAY_UNDO_LAST, ar.PAY_UNDO_ERROR)
            return
        try:
            with session_scope() as session:
                audit = payment_service.undo_last_payment(
                    session, int(self.current_user.id), reason
                )
                payment_id = audit.entity_id
        except (PermissionError, ValueError, RuntimeError, SQLAlchemyError):
            QMessageBox.warning(self, ar.PAY_UNDO_LAST, ar.PAY_UNDO_ERROR)
            return
        events.payment_changed.emit(payment_id)
        events.data_changed.emit()
        events.notify.emit("warning", ar.PAY_UNDO_LAST, ar.PAY_UNDO_DONE, 4000)
        QMessageBox.information(self, ar.PAY_UNDO_LAST, ar.PAY_UNDO_DONE)

    def _show_payment_history(self, payments: tuple[Payment, ...]) -> None:
        """Show payment dates, amounts, methods, and notes for one credit sale."""
        dialog = QDialog(self)
        dialog.setWindowTitle(ar.PAY_HISTORY_TITLE)
        layout = QVBoxLayout(dialog)
        history = QListWidget(dialog)
        if not payments:
            history.addItem(ar.PAY_HISTORY_EMPTY)
        for payment in payments:
            method = _method_label(payment.method)
            amount = _format_money(payment.amount)
            label = ar.PAY_HISTORY_ITEM.format(
                date=_format_date(payment.payment_date),
                amount=amount,
                method=method,
            )
            if payment.note:
                label = f"{label} — {payment.note}"
            history.addItem(label)
        layout.addWidget(history)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(ar.PAY_CANCEL)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.resize(500, 360)
        dialog.exec()

    @staticmethod
    def _set_cell(table: QTableWidget, row: int, column: int, value: str) -> None:
        """Place centered, read-only text in a table cell."""
        item = QTableWidgetItem(value)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setToolTip(value)
        table.setItem(row, column, item)


class PaymentDialog(QDialog):
    """Collect a whole-dinar amount, payment date, method, and optional note."""

    def __init__(self, remaining: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.amount_input = QSpinBox(self)
        self.amount_input.setRange(1, max(1, remaining))
        self.amount_input.setValue(max(1, remaining))
        self.amount_input.setGroupSeparatorShown(True)

        self.date_input = QDateEdit(self)
        self.date_input.setCalendarPopup(True)
        self.date_input.setDisplayFormat("dd/MM/yyyy")
        self.date_input.setDate(QDate.currentDate())

        self.method_input = QComboBox(self)
        for label, value in _payment_methods():
            self.method_input.addItem(label, value)
        self.note_input = QLineEdit(self)

        layout = QFormLayout(self)
        layout.addRow(ar.PAY_AMOUNT, self.amount_input)
        layout.addRow(ar.PAY_DATE, self.date_input)
        layout.addRow(ar.PAY_METHOD, self.method_input)
        layout.addRow(ar.PAY_NOTE, self.note_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(ar.PAY_SAVE)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(ar.PAY_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.setWindowTitle(ar.PAY_DIALOG_TITLE)

    def values(self) -> tuple[int, date, str, str | None]:
        """Return the dialog values in the payment service's expected format."""
        return (
            self.amount_input.value(),
            self.date_input.date().toPython(),
            str(self.method_input.currentData()),
            self.note_input.text().strip() or None,
        )


def _grace_days(session) -> int:
    """Read a valid grace-days setting, falling back to the business default."""
    setting = session.get(Setting, "grace_days")
    if setting is None:
        return 5
    try:
        value = json.loads(setting.value)
    except (TypeError, json.JSONDecodeError):
        return 5
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 5
    return value


def _installment_paid(installment: Installment) -> int:
    """Return the larger of the cached amount and its payment-ledger total."""
    return max(
        installment.amount_paid,
        sum(payment.amount for payment in installment.payments),
    )


def _needs_reminder(due: date, today: date, cutoff: date, grace_days: int) -> bool:
    """Include obligations due in three days or overdue beyond the grace period."""
    return due <= cutoff and (due >= today or today > due + timedelta(days=grace_days))


def _format_money(amount: int) -> str:
    """Format an integer dinar amount with Western-digit grouping."""
    return f"{amount:,} {ar.CURRENCY_SUFFIX}"


def _format_date(value: date | datetime | None) -> str:
    """Format dates using the app's documented day/month/year order."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime("%d/%m/%Y")


def _method_label(method: str) -> str:
    """Map stored method codes to the localized label."""
    for label, value in _payment_methods():
        if method.casefold() == value.casefold():
            return label
    return method
