"""Debt book pages: "ديون لي" (people who owe the shop) and "ديون عليّ" (what the shop owes).

Both pages share :class:`DebtsPage`; only the direction and the wording
differ. Each debt records the person (first and last name, e-mail, national
ID card number, phone), the amount and reason, and how it is repaid: in one
payment (optionally before a date) or by facility (a number of months, one
payment every N months). Payments are recorded as they come, partial
payments included; the details window shows the repayment schedule with what
is already covered. The page is owner-only.
"""

from __future__ import annotations

import logging
from datetime import date

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import User
from app.db.session import session_scope
from app.i18n import ar
from app.i18n.plan_text import plan_summary
from app.services import auth, calc, debts
from app.services.debts import DebtError, DebtSummary, DebtTerms, PersonDetails
from app.services.settings import get_grace_days, get_value
from app.ui import icons
from app.ui.dialogs.auth_dialogs import InlineError
from app.ui.events import events
from app.ui.theme import qcolor, repolish

_LOG = logging.getLogger(__name__)
_STATUS_TONES = {
    debts.STATUS_OVERDUE: "failed",
    debts.STATUS_DUE_SOON: "pending",
    debts.STATUS_ON_TRACK: "primary-glow",
    debts.STATUS_PAID: "paid",
}


def status_label(status: str) -> str:
    """Localized debt status."""
    return {
        debts.STATUS_OVERDUE: ar.DEBT_STATUS_OVERDUE,
        debts.STATUS_DUE_SOON: ar.DEBT_STATUS_DUE_SOON,
        debts.STATUS_ON_TRACK: ar.DEBT_STATUS_ON_TRACK,
        debts.STATUS_PAID: ar.DEBT_STATUS_PAID,
    }[status]


def plan_label(summary: DebtSummary) -> str:
    """"دفعة واحدة — قبل 15/11/2026" or "20,000 دج كل شهرين × 3"."""
    if summary.plan == debts.PLAN_INSTALLMENTS and summary.months:
        count = len(calc.payment_offsets(summary.months, summary.interval))
        base = summary.amount // count
        return plan_summary(base, summary.interval, count)
    if summary.due_date is not None:
        return ar.DEBT_PLAN_SINGLE_BEFORE.format(date=_date(summary.due_date))
    return ar.DEBT_PLAN_SINGLE


def error_text(error: DebtError) -> str:
    """Localized message for a refused debt change."""
    return {
        "name": ar.DEBT_ERR_NAME,
        "email": ar.DEBT_ERR_EMAIL,
        "national_id": ar.DEBT_ERR_NATIONAL_ID,
        "national_id_taken": ar.DEBT_ERR_NATIONAL_ID_TAKEN,
        "phone": ar.DEBT_ERR_PHONE,
        "amount": ar.DEBT_ERR_AMOUNT,
        "plan": ar.DEBT_ERR_PLAN,
        "overpaid": ar.DEBT_ERR_OVERPAID,
        "below_paid": ar.DEBT_ERR_BELOW_PAID,
    }.get(error.code, ar.ERROR_BODY)


# ------------------------------------------------------------------ dialogs
class DebtDialog(QDialog):
    """Add or edit a debt: the person, the amount and the repayment plan."""

    def __init__(self, direction: str, *, summary: DebtSummary | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.direction = direction
        receivable = direction == debts.RECEIVABLE
        title = (ar.DEBT_EDIT if summary else (ar.DEBT_ADD_IN if receivable else ar.DEBT_ADD_OUT))
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(8)
        heading = QLabel(title, self)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)

        person_title = QLabel(ar.DEBT_PERSON_IN if receivable else ar.DEBT_PERSON_OUT, self)
        person_title.setObjectName("sectionHint")
        layout.addWidget(person_title)
        self.first_name = _line(self, 80)
        self.last_name = _line(self, 80)
        self.email = _line(self, 120, ltr=True)
        self.email.setPlaceholderText("name@example.com")
        self.national_id = _line(self, 30, ltr=True)
        self.phone = _line(self, 20, ltr=True)
        person = QFormLayout()
        person.addRow(f"{ar.DEBT_FIRST_NAME} *", self.first_name)
        person.addRow(f"{ar.DEBT_LAST_NAME} *", self.last_name)
        person.addRow(ar.DEBT_EMAIL, self.email)
        person.addRow(ar.DEBT_NATIONAL_ID, self.national_id)
        person.addRow(ar.DEBT_PHONE, self.phone)
        layout.addLayout(person)

        debt_title = QLabel(ar.DEBT_TERMS, self)
        debt_title.setObjectName("sectionHint")
        layout.addWidget(debt_title)
        self.amount = QSpinBox(self)
        self.amount.setRange(0, 2_000_000_000)
        self.amount.setGroupSeparatorShown(True)
        self.amount.setSingleStep(1000)
        self.amount.setSuffix(f" {ar.CURRENCY_SUFFIX}")
        self.reason = _line(self, 200)
        self.reason.setPlaceholderText(ar.DEBT_REASON_HINT)
        self.debt_date = _date_edit(self, QDate.currentDate())
        self.plan = QComboBox(self)
        self.plan.addItem(ar.DEBT_PLAN_SINGLE, debts.PLAN_SINGLE)
        self.plan.addItem(ar.DEBT_PLAN_FACILITY, debts.PLAN_INSTALLMENTS)
        self.has_due_date = QCheckBox(ar.DEBT_HAS_DUE_DATE, self)
        self.due_date = _date_edit(self, QDate.currentDate().addMonths(1))
        self.has_due_date.toggled.connect(self.due_date.setEnabled)
        self.due_date.setEnabled(False)
        self.months = QSpinBox(self)
        self.months.setRange(1, calc.MAX_PLAN_MONTHS)
        self.months.setValue(6)
        self.months.setSuffix(f" {ar.PURCHASE_MONTHS_SUFFIX}")
        self.interval = QSpinBox(self)
        self.interval.setRange(1, calc.MAX_PLAN_MONTHS)
        self.interval.setSuffix(f" {ar.PURCHASE_MONTHS_SUFFIX}")
        self.interval.setSpecialValueText(ar.PLAN_EVERY_1)
        self.months.valueChanged.connect(self.interval.setMaximum)
        terms = QFormLayout()
        terms.addRow(f"{ar.DEBT_AMOUNT} *", self.amount)
        terms.addRow(ar.DEBT_REASON, self.reason)
        terms.addRow(ar.DEBT_DATE, self.debt_date)
        terms.addRow(ar.DEBT_PLAN, self.plan)
        terms.addRow("", self.has_due_date)
        terms.addRow(ar.DEBT_DUE_DATE, self.due_date)
        terms.addRow(ar.PLAN_MONTHS, self.months)
        terms.addRow(ar.PLAN_INTERVAL, self.interval)
        self._terms = terms
        layout.addLayout(terms)
        self.preview = QLabel(self)
        self.preview.setObjectName("notificationBody")
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)

        self.error = InlineError(self)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(self)
        self.save_button = buttons.addButton(ar.USER_SAVE, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(ar.USER_CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setProperty("variant", "secondary")
        repolish(cancel)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)

        for signal in (self.plan.currentIndexChanged, self.amount.valueChanged, self.months.valueChanged,
                       self.interval.valueChanged, self.debt_date.dateChanged):
            signal.connect(self._update)
        if summary is not None:
            self._load(summary)
        self._update()
        self.first_name.setFocus()

    def _load(self, summary: DebtSummary) -> None:
        self.first_name.setText(summary.first_name)
        self.last_name.setText(summary.last_name)
        self.email.setText(summary.email or "")
        self.national_id.setText(summary.national_id or "")
        self.phone.setText(summary.phone or "")
        self.amount.setValue(summary.amount)
        self.reason.setText(summary.reason or "")
        self.debt_date.setDate(QDate(summary.debt_date.year, summary.debt_date.month, summary.debt_date.day))
        self.plan.setCurrentIndex(self.plan.findData(summary.plan))
        if summary.due_date is not None:
            self.has_due_date.setChecked(True)
            self.due_date.setDate(QDate(summary.due_date.year, summary.due_date.month, summary.due_date.day))
        if summary.months:
            self.months.setValue(summary.months)
            self.interval.setValue(summary.interval)

    def _update(self, *_args: object) -> None:
        facility = self.plan.currentData() == debts.PLAN_INSTALLMENTS
        for widget in (self.months, self.interval):
            self._terms.setRowVisible(widget, facility)
        for widget in (self.has_due_date, self.due_date):
            self._terms.setRowVisible(widget, not facility)
        if facility and self.amount.value() > 0:
            count = len(calc.payment_offsets(self.months.value(), min(self.interval.value(), self.months.value())))
            self.preview.setText(ar.DEBT_PREVIEW.format(
                plan=plan_summary(self.amount.value() // count, self.interval.value(), count),
            ))
        else:
            self.preview.setText("")
        self.error.clear()

    def person(self) -> PersonDetails:
        return PersonDetails(
            first_name=self.first_name.text(), last_name=self.last_name.text(),
            email=self.email.text() or None, national_id=self.national_id.text() or None,
            phone=self.phone.text() or None,
        )

    def terms(self) -> DebtTerms:
        facility = self.plan.currentData() == debts.PLAN_INSTALLMENTS
        return DebtTerms(
            amount=self.amount.value(),
            debt_date=self.debt_date.date().toPython(),
            reason=self.reason.text(),
            plan=self.plan.currentData(),
            due_date=self.due_date.date().toPython() if not facility and self.has_due_date.isChecked() else None,
            months=self.months.value() if facility else None,
            interval=min(self.interval.value(), self.months.value()) if facility else 1,
        )


class PaymentDialog(QDialog):
    """Record money received from (or paid to) the person of a debt."""

    def __init__(self, summary: DebtSummary, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        receivable = summary.direction == debts.RECEIVABLE
        title = ar.DEBT_RECEIVE if receivable else ar.DEBT_PAY
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        heading = QLabel(title, self)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        info = QLabel(ar.DEBT_PAYMENT_INFO.format(name=summary.full_name, remaining=_money(summary.remaining)), self)
        info.setObjectName("sectionHint")
        info.setWordWrap(True)
        layout.addWidget(info)
        self.amount = QSpinBox(self)
        self.amount.setRange(1, max(1, summary.remaining))
        self.amount.setGroupSeparatorShown(True)
        self.amount.setSuffix(f" {ar.CURRENCY_SUFFIX}")
        self.amount.setValue(summary.next_due_amount or summary.remaining)
        self.payment_date = _date_edit(self, QDate.currentDate())
        self.method = QComboBox(self)
        for method in debts.METHODS:
            self.method.addItem({"cash": ar.DEBT_METHOD_CASH, "bank": ar.DEBT_METHOD_BANK}.get(method, method), method)
        self.note = _line(self, 300)
        form = QFormLayout()
        form.addRow(ar.DEBT_PAYMENT_AMOUNT, self.amount)
        form.addRow(ar.DEBT_PAYMENT_DATE, self.payment_date)
        form.addRow(ar.DEBT_PAYMENT_METHOD, self.method)
        form.addRow(ar.DEBT_PAYMENT_NOTE, self.note)
        layout.addLayout(form)
        self.error = InlineError(self)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(self)
        self.save_button = buttons.addButton(ar.USER_SAVE, QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(ar.USER_CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setProperty("variant", "secondary")
        repolish(cancel)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)


class DebtDetailsDialog(QDialog):
    """A debt's person, repayment schedule and payment history."""

    def __init__(self, page: DebtsPage, debt_id: int) -> None:
        super().__init__(page)
        self.page = page
        self.debt_id = debt_id
        self.setMinimumSize(720, 560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(8)
        self.heading = QLabel(self)
        self.heading.setObjectName("sectionTitle")
        layout.addWidget(self.heading)
        self.person = QLabel(self)
        self.person.setObjectName("sectionHint")
        self.person.setWordWrap(True)
        layout.addWidget(self.person)
        self.totals = QLabel(self)
        self.totals.setWordWrap(True)
        layout.addWidget(self.totals)
        schedule_title = QLabel(ar.DEBT_SCHEDULE, self)
        schedule_title.setObjectName("sectionTitle")
        layout.addWidget(schedule_title)
        self.schedule_table = _table(self, [ar.DEBT_COL_INDEX, ar.DEBT_COL_DUE, ar.DEBT_AMOUNT,
                                            ar.DEBT_COL_COVERED, ar.DEBT_COL_STATE])
        layout.addWidget(self.schedule_table, 2)
        payments_title = QLabel(ar.DEBT_PAYMENTS, self)
        payments_title.setObjectName("sectionTitle")
        layout.addWidget(payments_title)
        self.payments_table = _table(self, [ar.DEBT_PAYMENT_DATE, ar.DEBT_PAYMENT_AMOUNT,
                                            ar.DEBT_PAYMENT_METHOD, ar.DEBT_PAYMENT_NOTE])
        layout.addWidget(self.payments_table, 2)
        buttons = QHBoxLayout()
        self.pay_button = _button(self, ar.DEBT_RECEIVE if page.direction == debts.RECEIVABLE else ar.DEBT_PAY, "wallet", None)
        self.pay_button.clicked.connect(self._pay)
        self.undo_button = _button(self, ar.DEBT_UNDO_PAYMENT, "trash", "secondary")
        self.undo_button.clicked.connect(self._undo)
        close = _button(self, ar.DEBT_CLOSE, "close", "secondary")
        close.clicked.connect(self.accept)
        buttons.addWidget(self.pay_button)
        buttons.addWidget(self.undo_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.reload()

    def reload(self) -> None:
        """Re-read the debt and fill both tables."""
        with session_scope() as session:
            debt = debts.get_debt(session, self.debt_id)
            due_mode = get_value(session, "due_mode", "first_of_month")
            summary = debts.summarize(debt, date.today(), grace_days=get_grace_days(session), due_mode=due_mode)
            rows = debts.schedule(debt, due_mode=due_mode)
            payments = [(p.id, p.payment_date, p.amount, p.method, p.note) for p in debt.payments]
        self.summary = summary
        self.setWindowTitle(summary.full_name)
        self.heading.setText(f"{summary.full_name} — {status_label(summary.status)}")
        details = [value for value in (summary.national_id and f"{ar.DEBT_NATIONAL_ID}: {summary.national_id}",
                                       summary.email, summary.phone, summary.reason) if value]
        self.person.setText(" · ".join(details))
        self.totals.setText(ar.DEBT_TOTALS_LINE.format(
            amount=_money(summary.amount), paid=_money(summary.paid), remaining=_money(summary.remaining),
            plan=plan_label(summary),
        ))
        today = date.today()
        self.schedule_table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            if item.open_amount == 0:
                state, tone = ar.DEBT_STATUS_PAID, "paid"
            elif item.due_date < today and summary.next_due_date is not None:
                state, tone = ar.DEBT_STATUS_OVERDUE, "failed"
            else:
                state, tone = ar.DEBT_STATE_OPEN, "text-muted"
            _fill(self.schedule_table, row, [
                (str(item.index), None), (_date(item.due_date), None), (_money(item.amount), None),
                (_money(item.covered), "paid" if item.covered else None), (state, tone),
            ])
        self._payment_ids = [payment[0] for payment in payments]
        self.payments_table.setRowCount(len(payments))
        for row, (_pid, paid_on, amount, method, note) in enumerate(payments):
            _fill(self.payments_table, row, [
                (_date(paid_on), None), (_money(amount), "paid"), (method, None), (note or "", None),
            ])
        self.pay_button.setEnabled(summary.remaining > 0)
        self.undo_button.setEnabled(bool(payments))

    def _pay(self) -> None:
        if self.page.record_payment_for(self.summary):
            self.reload()

    def _undo(self) -> None:
        if not self._payment_ids:
            return
        if QMessageBox.question(self, ar.DEBT_UNDO_PAYMENT, ar.DEBT_UNDO_CONFIRM) != QMessageBox.StandardButton.Yes:
            return
        if self.page.undo_payment(self._payment_ids[-1]):
            self.reload()


# -------------------------------------------------------------------- page
class DebtsPage(QWidget):
    """One side of the debt book with totals, filters and actions."""

    direction = debts.RECEIVABLE

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self._rows: list[DebtSummary] = []
        self._build()
        events.data_changed.connect(self.refresh)
        self.refresh()

    @property
    def receivable(self) -> bool:
        return self.direction == debts.RECEIVABLE

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(12)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel(ar.NAV_DEBTS_IN if self.receivable else ar.NAV_DEBTS_OUT, self)
        title.setObjectName("pageTitle")
        subtitle = QLabel(ar.PAGE_SUBTITLE_DEBTS_IN if self.receivable else ar.PAGE_SUBTITLE_DEBTS_OUT, self)
        subtitle.setObjectName("pageSubtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        self.delete_button = _button(self, ar.DEBT_DELETE, "trash", "secondary")
        self.delete_button.clicked.connect(self._delete_selected)
        self.edit_button = _button(self, ar.DEBT_EDIT, "edit", "secondary")
        self.edit_button.clicked.connect(self._edit_selected)
        self.pay_button = _button(self, ar.DEBT_RECEIVE if self.receivable else ar.DEBT_PAY, "wallet", "secondary")
        self.pay_button.clicked.connect(self._pay_selected)
        self.add_button = _button(self, ar.DEBT_ADD_IN if self.receivable else ar.DEBT_ADD_OUT, "plus", None)
        self.add_button.clicked.connect(self._add)
        for button in (self.delete_button, self.edit_button, self.pay_button, self.add_button):
            header.addWidget(button)
        layout.addLayout(header)

        cards = QGridLayout()
        cards.setHorizontalSpacing(12)
        self.cards: dict[str, QLabel] = {}
        for column, (key, caption, tone) in enumerate((
            ("remaining", ar.DEBT_CARD_REMAINING_IN if self.receivable else ar.DEBT_CARD_REMAINING_OUT,
             "primary-glow" if self.receivable else "pending"),
            ("overdue", ar.DEBT_CARD_OVERDUE, "failed"),
            ("paid", ar.DEBT_CARD_PAID_IN if self.receivable else ar.DEBT_CARD_PAID_OUT, "paid"),
            ("amount", ar.DEBT_CARD_TOTAL, "text"),
        )):
            card = QFrame(self)
            card.setObjectName("card")
            box = QVBoxLayout(card)
            box.setContentsMargins(16, 12, 16, 12)
            label = QLabel(caption, card)
            label.setObjectName("kpiCaption")
            value = QLabel("—", card)
            value.setObjectName("moneyValue")
            value.setStyleSheet(f"color: {qcolor(tone).name()};")
            box.addWidget(label)
            box.addWidget(value)
            cards.addWidget(card, 0, column)
            self.cards[key] = value
        layout.addLayout(cards)

        filters = QHBoxLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(ar.DEBT_SEARCH)
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icons.icon("search", "text-muted", 16), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self.refresh)
        filters.addWidget(self.search, 1)
        self.status_filter = QComboBox(self)
        self.status_filter.addItem(ar.DEBT_FILTER_OPEN, "open")
        for status in debts.STATUSES:
            self.status_filter.addItem(status_label(status), status)
        self.status_filter.addItem(ar.DEBT_FILTER_ALL, None)
        self.status_filter.currentIndexChanged.connect(self.refresh)
        filters.addWidget(self.status_filter)
        layout.addLayout(filters)

        self.columns = ("name", "national_id", "contact", "amount", "remaining", "plan", "next", "status")
        self.table = _table(self, [_header(key) for key in self.columns])
        header_view = self.table.horizontalHeader()
        header_view.setStretchLastSection(False)
        # Names are short and must always show; long e-mails give way instead.
        header_view.setSectionResizeMode(self.columns.index("contact"), QHeaderView.ResizeMode.Stretch)
        header_view.setMinimumSectionSize(70)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.table.itemDoubleClicked.connect(lambda _item: self.open_details())
        layout.addWidget(self.table, 1)

        self.empty_card = QFrame(self)
        self.empty_card.setObjectName("emptyStateCard")
        empty_layout = QVBoxLayout(self.empty_card)
        empty_layout.setContentsMargins(20, 36, 20, 36)
        empty_icon = QLabel(self.empty_card)
        empty_icon.setPixmap(icons.pixmap("debt-in" if self.receivable else "debt-out", "text-muted", 32, stroke=1.5))
        empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_text = QLabel(ar.DEBT_EMPTY_IN if self.receivable else ar.DEBT_EMPTY_OUT, self.empty_card)
        empty_text.setObjectName("emptyStateTitle")
        empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_text.setWordWrap(True)
        empty_layout.addWidget(empty_icon)
        empty_layout.addWidget(empty_text)
        layout.addWidget(self.empty_card, 1)
        self.empty_card.hide()
        self._update_actions()

    # ------------------------------------------------------------------ data
    def refresh(self, *_args: object) -> None:
        """Reload the debts and the totals."""
        choice = self.status_filter.currentData()
        try:
            with session_scope() as session:
                rows = debts.list_debts(
                    session, self.direction, today=date.today(),
                    status=choice if choice not in (None, "open") else None,
                    search=self.search.text(),
                )
                totals = debts.totals(session, self.direction, today=date.today())
        except SQLAlchemyError:
            _LOG.exception("Debt list refresh failed")
            return
        if choice == "open":
            rows = [row for row in rows if row.remaining > 0]
        self._rows = rows
        self.cards["remaining"].setText(_money(totals.remaining))
        self.cards["overdue"].setText(
            _money(totals.overdue) + (f"  ({totals.overdue_count})" if totals.overdue_count else "")
        )
        self.cards["paid"].setText(_money(totals.paid))
        self.cards["amount"].setText(_money(totals.amount))
        self.table.setRowCount(len(rows))
        for row, summary in enumerate(rows):
            _fill(self.table, row, [self._cell(summary, key) for key in self.columns], start_aligned=(0,))
            tip = ar.DEBT_ROW_TIP.format(
                reason=summary.reason or "—", paid=_money(summary.paid), date=_date(summary.debt_date),
            )
            for column in range(len(self.columns)):
                self.table.item(row, column).setToolTip(tip)
        self.table.setVisible(bool(rows))
        self.empty_card.setVisible(not rows)
        self._update_actions()

    @staticmethod
    def _cell(summary: DebtSummary, key: str) -> tuple[str, str | None]:
        if key == "name":
            return summary.full_name, None
        if key == "national_id":
            return summary.national_id or "—", None
        if key == "contact":
            return " · ".join(value for value in (summary.phone, summary.email) if value) or "—", None
        if key == "amount":
            return _money(summary.amount), None
        if key == "remaining":
            return _money(summary.remaining), "failed" if summary.overdue_amount else None
        if key == "plan":
            return plan_label(summary), None
        if key == "next":
            if summary.next_due_date is None:
                return "—", None
            return f"{_date(summary.next_due_date)} · {_money(summary.next_due_amount)}", None
        return status_label(summary.status), _STATUS_TONES[summary.status]

    # --------------------------------------------------------------- actions
    def selected(self) -> DebtSummary | None:
        """The debt on the selected row."""
        selection = self.table.selectionModel().selectedRows()
        row = selection[0].row() if selection else self.table.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _update_actions(self) -> None:
        summary = self.selected()
        self.edit_button.setEnabled(summary is not None)
        self.delete_button.setEnabled(summary is not None)
        self.pay_button.setEnabled(summary is not None and summary.remaining > 0)

    def new_dialog(self, summary: DebtSummary | None = None) -> DebtDialog:
        return DebtDialog(self.direction, summary=summary, parent=self)

    def _add(self) -> None:
        dialog = self.new_dialog()
        dialog.save_button.clicked.connect(lambda: self.save_dialog(dialog))
        dialog.exec()

    def _edit_selected(self) -> None:
        summary = self.selected()
        if summary is None:
            return
        dialog = self.new_dialog(summary)
        dialog.save_button.clicked.connect(lambda: self.save_dialog(dialog, summary.id))
        dialog.exec()

    def save_dialog(self, dialog: DebtDialog, debt_id: int | None = None) -> bool:
        """Create or update a debt from a filled dialog; problems stay inline."""
        if not dialog.first_name.text().strip() or not dialog.last_name.text().strip():
            dialog.error.show_message(ar.DEBT_ERR_NAME)
            return False
        if dialog.amount.value() <= 0:
            dialog.error.show_message(ar.DEBT_ERR_AMOUNT)
            return False
        try:
            with session_scope() as session:
                if debt_id is None:
                    debts.create_debt(session, self.current_user.id, direction=self.direction,
                                      person=dialog.person(), terms=dialog.terms())
                else:
                    debts.update_debt(session, self.current_user.id, debt_id,
                                      person=dialog.person(), terms=dialog.terms())
        except DebtError as error:
            dialog.error.show_message(error_text(error))
            return False
        except auth.AuthorizationError:
            dialog.error.show_message(ar.SET_OWNER_ONLY)
            return False
        except SQLAlchemyError:
            _LOG.exception("Debt save failed")
            dialog.error.show_message(ar.ERROR_BODY)
            return False
        dialog.accept()
        events.data_changed.emit()
        events.notify.emit("success", ar.NAV_DEBTS_IN if self.receivable else ar.NAV_DEBTS_OUT, ar.DEBT_SAVED, 3000)
        return True

    def _pay_selected(self) -> None:
        summary = self.selected()
        if summary is not None:
            self.record_payment_for(summary)

    def record_payment_for(self, summary: DebtSummary) -> bool:
        """Open the payment dialog for a debt; True when a payment was saved."""
        dialog = PaymentDialog(summary, self)
        saved: list[bool] = []
        dialog.save_button.clicked.connect(lambda: saved.append(self.save_payment(dialog, summary.id)))
        dialog.exec()
        return any(saved)

    def save_payment(self, dialog: PaymentDialog, debt_id: int) -> bool:
        """Record the payment typed in a payment dialog."""
        try:
            with session_scope() as session:
                debts.record_payment(
                    session, self.current_user.id, debt_id, amount=dialog.amount.value(),
                    payment_date=dialog.payment_date.date().toPython(), method=dialog.method.currentData(),
                    note=dialog.note.text(),
                )
        except DebtError as error:
            dialog.error.show_message(error_text(error))
            return False
        except (auth.AuthorizationError, SQLAlchemyError):
            dialog.error.show_message(ar.ERROR_BODY)
            return False
        dialog.accept()
        events.data_changed.emit()
        events.notify.emit("success", ar.NAV_DEBTS_IN if self.receivable else ar.NAV_DEBTS_OUT,
                           ar.DEBT_PAYMENT_SAVED, 3000)
        return True

    def undo_payment(self, payment_id: int) -> bool:
        """Remove a payment recorded by mistake."""
        try:
            with session_scope() as session:
                debts.delete_payment(session, self.current_user.id, payment_id)
        except (DebtError, auth.AuthorizationError, SQLAlchemyError):
            QMessageBox.warning(self, ar.ERROR_TITLE, ar.ERROR_BODY)
            return False
        events.data_changed.emit()
        return True

    def _delete_selected(self) -> None:
        summary = self.selected()
        if summary is None:
            return
        if QMessageBox.question(self, ar.DEBT_DELETE, ar.DEBT_DELETE_CONFIRM.format(name=summary.full_name)) \
                != QMessageBox.StandardButton.Yes:
            return
        self.delete_debt(summary.id)

    def delete_debt(self, debt_id: int) -> bool:
        try:
            with session_scope() as session:
                debts.delete_debt(session, self.current_user.id, debt_id)
        except (DebtError, auth.AuthorizationError, SQLAlchemyError):
            QMessageBox.warning(self, ar.ERROR_TITLE, ar.ERROR_BODY)
            return False
        events.data_changed.emit()
        return True

    def open_details(self) -> DebtDetailsDialog | None:
        """Show the selected debt's schedule and payments."""
        summary = self.selected()
        if summary is None:
            return None
        dialog = DebtDetailsDialog(self, summary.id)
        dialog.open()
        return dialog


class ReceivablesPage(DebtsPage):
    """"ديون لي": people who must pay the shop back."""

    direction = debts.RECEIVABLE


class PayablesPage(DebtsPage):
    """"ديون عليّ": what the shop must pay to others."""

    direction = debts.PAYABLE


# ------------------------------------------------------------------ helpers
def _header(key: str) -> str:
    return {
        "name": ar.DEBT_COL_NAME,
        "national_id": ar.DEBT_NATIONAL_ID,
        "contact": ar.DEBT_COL_CONTACT,
        "amount": ar.DEBT_AMOUNT,
        "remaining": ar.DEBT_COL_REMAINING,
        "plan": ar.DEBT_PLAN,
        "next": ar.DEBT_COL_NEXT,
        "status": ar.DEBT_COL_STATUS,
    }[key]


def _table(parent: QWidget, headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers), parent)
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(40)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _fill(table: QTableWidget, row: int, cells: list[tuple[str, str | None]], start_aligned: tuple[int, ...] = ()) -> None:
    for column, (text, tone) in enumerate(cells):
        item = QTableWidgetItem(text)
        if column not in start_aligned:
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        if tone is not None:
            item.setForeground(qcolor(tone))
        table.setItem(row, column, item)


def _button(parent: QWidget, text: str, icon: str, variant: str | None) -> QPushButton:
    button = QPushButton(text, parent)
    if variant:
        button.setProperty("variant", variant)
    button.setIcon(icons.icon(icon, "#FFFFFF" if variant is None else "text", 16))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _line(parent: QWidget, limit: int, *, ltr: bool = False) -> QLineEdit:
    editor = QLineEdit(parent)
    editor.setMaxLength(limit)
    if ltr:
        editor.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    return editor


def _date_edit(parent: QWidget, value: QDate) -> QDateEdit:
    editor = QDateEdit(value, parent)
    editor.setDisplayFormat("dd/MM/yyyy")
    editor.setCalendarPopup(True)
    return editor


def _money(amount: int) -> str:
    return f"{amount:,} {ar.CURRENCY_SUFFIX}"


def _date(value: date) -> str:
    return value.strftime("%d/%m/%Y")


__all__ = ["DebtsPage", "ReceivablesPage", "PayablesPage", "DebtDialog", "PaymentDialog", "DebtDetailsDialog"]
