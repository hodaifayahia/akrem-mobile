"""Remind everyone due this week on WhatsApp, one client after the other.

WhatsApp only lets an app open a drafted message, so "Send next" opens the
next ticked client in WhatsApp with the message ready and the operator
presses Send there. Each opened reminder is logged; clients already reminded
today start unticked so nobody gets the same message twice.
"""

from __future__ import annotations

import logging
from datetime import date

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import User
from app.db.session import session_scope
from app.i18n import ar
from app.services import reminders
from app.services.reminders import ReminderTarget
from app.ui.theme import qcolor, repolish

_LOG = logging.getLogger(__name__)
COLUMNS = ("send", "name", "phone", "amount", "due", "last")


class WeeklyRemindersDialog(QDialog):
    """The week's reminder list with a "send next" button."""

    def __init__(self, current_user: User, parent: QWidget | None = None, *, today: date | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self.today = today or date.today()
        self.targets: list[ReminderTarget] = []
        self.sent: set[int] = set()
        self.opener = lambda url: QDesktopServices.openUrl(QUrl(url))
        self.setWindowTitle(ar.REMIND_TITLE)
        self.setMinimumSize(760, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        heading = QLabel(ar.REMIND_TITLE, self)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        hint = QLabel(ar.REMIND_HINT, self)
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        options = QHBoxLayout()
        self.include_late = QCheckBox(ar.REMIND_INCLUDE_LATE, self)
        self.include_late.setChecked(True)
        self.include_late.toggled.connect(self.reload)
        self.summary = QLabel(self)
        self.summary.setObjectName("sectionHint")
        options.addWidget(self.include_late)
        options.addStretch(1)
        options.addWidget(self.summary)
        layout.addLayout(options)

        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels([
            "", ar.CUST_COL_NAME, ar.CUST_COL_PHONE, ar.PAY_COL_REMAINING, ar.REMIND_COL_DUE, ar.REMIND_COL_LAST,
        ])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.currentCellChanged.connect(lambda *_args: self._show_message())
        self.table.itemChanged.connect(lambda _item: self._update_buttons())
        layout.addWidget(self.table, 1)

        preview_title = QLabel(ar.REMIND_PREVIEW, self)
        preview_title.setObjectName("sectionHint")
        layout.addWidget(preview_title)
        self.preview = QPlainTextEdit(self)
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(80)
        layout.addWidget(self.preview)

        buttons = QHBoxLayout()
        self.next_button = QPushButton(ar.REMIND_SEND_NEXT, self)
        self.next_button.setProperty("variant", "success")
        self.next_button.clicked.connect(self.send_next)
        close = QPushButton(ar.CUST_CLOSE, self)
        close.setProperty("variant", "secondary")
        close.clicked.connect(self.accept)
        for button in (self.next_button, close):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            repolish(button)
        buttons.addWidget(close)
        buttons.addStretch(1)
        buttons.addWidget(self.next_button)
        layout.addLayout(buttons)
        self.reload()

    # ------------------------------------------------------------------ data
    def reload(self, *_args: object) -> None:
        try:
            with session_scope() as session:
                self.targets = reminders.weekly_targets(
                    session, today=self.today, include_late=self.include_late.isChecked()
                )
        except SQLAlchemyError:
            _LOG.exception("Weekly reminders could not be loaded")
            self.targets = []
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.targets))
        for row, target in enumerate(self.targets):
            tick = QTableWidgetItem()
            reminded = target.reminded_on(self.today) or target.customer_id in self.sent
            if target.can_send:
                tick.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                tick.setCheckState(Qt.CheckState.Unchecked if reminded else Qt.CheckState.Checked)
            else:
                tick.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                tick.setToolTip(ar.PAYT_NO_PHONE)
            self.table.setItem(row, 0, tick)
            last = target.last_reminded.strftime("%d/%m/%Y %H:%M") if target.last_reminded else "—"
            due = target.first_due.strftime("%d/%m/%Y") + (f"  · {ar.REMIND_LATE}" if target.late else "")
            texts = (target.customer_name, target.phone or "—", f"{target.amount:,} {ar.CURRENCY_SUFFIX}", due, last)
            for column, text in enumerate(texts, start=1):
                item = QTableWidgetItem(text)
                if column >= 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column == 4 and target.late:
                    item.setForeground(qcolor("failed"))
                self.table.setItem(row, column, item)
        self.table.blockSignals(False)
        if self.targets:
            self.table.setCurrentCell(0, 1)
        self._update_buttons()
        self._show_message()

    def ticked_rows(self) -> list[int]:
        return [
            row for row in range(self.table.rowCount())
            if self.table.item(row, 0).checkState() == Qt.CheckState.Checked
        ]

    # ------------------------------------------------------------------ actions
    def send_next(self) -> int | None:
        """Open WhatsApp for the next ticked client, log it and untick the row; the row sent, or None."""
        rows = self.ticked_rows()
        if not rows:
            return None
        row = rows[0]
        target = self.targets[row]
        link = reminders.link_for(target)
        if link is None:
            return None
        self.opener(link)
        try:
            with session_scope() as session:
                reminders.record_reminder(
                    session, int(self.current_user.id), target.customer_id, amount=target.amount
                )
        except SQLAlchemyError:
            _LOG.exception("Reminder could not be logged")
        self.sent.add(target.customer_id)
        self.table.blockSignals(True)
        self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        self.table.item(row, 5).setText(ar.REMIND_SENT_NOW)
        self.table.item(row, 5).setForeground(qcolor("paid"))
        self.table.blockSignals(False)
        following = self.ticked_rows()
        if following:
            self.table.setCurrentCell(following[0], 1)
        self._update_buttons()
        return row

    def _update_buttons(self) -> None:
        count = len(self.ticked_rows())
        sendable = sum(1 for target in self.targets if target.can_send)
        self.next_button.setEnabled(count > 0)
        self.next_button.setText(ar.REMIND_SEND_NEXT_COUNT.format(count=count) if count else ar.REMIND_ALL_DONE)
        self.summary.setText(ar.REMIND_SUMMARY.format(
            clients=len(self.targets), sendable=sendable,
            total=f"{sum(target.amount for target in self.targets):,} {ar.CURRENCY_SUFFIX}",
        ))

    def _show_message(self) -> None:
        row = self.table.currentRow()
        if not 0 <= row < len(self.targets):
            self.preview.setPlainText("")
            return
        self.preview.setPlainText(reminders.message_for(self.targets[row]))
