"""Customer details, sale information, and payment schedule dialog."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import re

from PySide6.QtCore import QDate, Qt, Signal, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QTabWidget,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import Customer, Installment, Payment, Sale, User
from app.config import data_dir, ensure_data_dirs
from app.db.session import session_scope
from app.i18n import ar
from app.i18n.plan_text import amount_label, every_text
from app.services import auth, customers, payments, sales as sales_service
from app.services import schedule
from app.services.sales import PaidInstallmentEditError
from app.services.pdf_forms import generate_commitment_pdf, generate_payment_receipt_pdf
from app.services.schedule import ScheduledInstallment
from app.services.settings import get_value
from app.ui.widgets.customer_history import CustomerHistoryPanel, HistorySummary
from app.services.status import Status, for_customer, for_month
from app.ui.events import events
from app.ui.dialogs.sale_edit_dialog import SaleEditDialog


class PaymentEntryDialog(QDialog):
    """Collect a partial or full payment amount and its receipt details."""

    def __init__(self, remaining: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(ar.PAYMENT_DIALOG_TITLE)
        self.setMinimumWidth(390)
        self.amount = QSpinBox(self)
        self.amount.setRange(1, max(1, remaining))
        self.amount.setValue(max(1, remaining))
        self.amount.setMaximumWidth(220)
        self.amount.setGroupSeparatorShown(True)
        self.payment_date = QDateEdit(QDate.currentDate(), self)
        self.payment_date.setDisplayFormat("dd/MM/yyyy")
        self.payment_date.setCalendarPopup(True)
        self.method = QComboBox(self)
        for label, value in (
            (ar.PAYMENT_METHOD_CASH, "cash"),
            (ar.PAYMENT_METHOD_CCP, "ccp"),
            (ar.PAYMENT_METHOD_BARIDIMOB, "baridimob"),
        ):
            self.method.addItem(label, value)
        self.note = QLineEdit(self)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow(ar.PAYMENT_AMOUNT, self.amount)
        form.addRow(ar.PAYMENT_DATE, self.payment_date)
        form.addRow(ar.PAYMENT_METHOD, self.method)
        form.addRow(ar.PAYMENT_NOTE, self.note)
        layout.addLayout(form)
        buttons = QDialogButtonBox(self)
        self.save_button = buttons.addButton(ar.PAYMENT_SAVE, QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setProperty("variant", "primary")
        self.cancel_button = buttons.addButton(ar.PAYMENT_CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        self.cancel_button.setProperty("variant", "secondary")
        self.save_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[int, date, str, str | None]:
        """Return the validated form values for the payment service."""
        return (
            self.amount.value(),
            self.payment_date.date().toPython(),
            str(self.method.currentData()),
            self.note.text().strip() or None,
        )


class CustomerDetailsDialog(QDialog):
    """Show a customer's sales and record payments from the selected schedule."""

    edit_requested = Signal(int)

    def __init__(
        self,
        customer_id: int,
        current_user: User,
        parent: QWidget | None = None,
        *,
        engine: Engine | None = None,
    ) -> None:
        super().__init__(parent)
        self.customer_id = customer_id
        self.current_user = current_user
        self._engine = engine
        self._customer: Customer
        self._grace_days = 5
        self._due_mode = "first_of_month"
        self._sale_id: int | None = None
        self._payments_by_id: dict[int, Payment] = {}
        self.setWindowTitle(ar.CUST_DETAILS_TITLE)
        self.setMinimumSize(860, 720)
        self.resize(980, 820)
        self._reload_customer()
        self._build_ui()
        self._populate_sales()
        self._center_on_parent()

    def _build_ui(self) -> None:
        """Create the identity header, sale selector, details and schedule views."""
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 18)
        root.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(12)
        self.name_label = QLabel(self)
        self.name_label.setObjectName("pageTitle")
        self.name_label.setText(self._customer.full_name)
        identity = QVBoxLayout()
        identity.setSpacing(4)
        identity.addWidget(self.name_label)
        self.contact_label = QLabel(self)
        self.contact_label.setStyleSheet("color: #8A94A6; font-size: 13px;")
        self.contact_label.setText(self._contact_text())
        identity.addWidget(self.contact_label)
        header.addLayout(identity, 1)

        self.status_dot = QLabel("●", self)
        self.status_text = QLabel(self)
        self.status_text.setStyleSheet("font-weight: 700; font-size: 13px;")
        self.status_label = QWidget(self)
        self.status_label.setStyleSheet(
            "background-color: #0E141D; border: 1px solid #1F2A3D; border-radius: 8px; padding: 4px 12px;"
        )
        status_layout = QHBoxLayout()
        status_layout.setContentsMargins(8, 4, 8, 4)
        status_layout.setSpacing(6)
        status_layout.addWidget(self.status_dot)
        status_layout.addWidget(self.status_text)
        self.status_label.setLayout(status_layout)
        header.addWidget(self.status_label)

        self.credit_badge_label = QLabel("مؤشر الالتزام: ⭐⭐⭐⭐⭐ ممتاز", self)
        self.credit_badge_label.setStyleSheet(
            "background-color: #06281B; color: #34D399; border: 1px solid #065F46; "
            "border-radius: 8px; padding: 4px 10px; font-weight: 700; font-size: 12px;"
        )
        header.addWidget(self.credit_badge_label)
        root.addLayout(header)

        # History summary is always visible; the tabs below hold the selected
        # sale (price table, schedule, payments) and the full history.
        self.history_summary = HistorySummary(self)
        root.addWidget(self.history_summary)
        self.tabs = QTabWidget(self)
        sale_tab = QWidget(self.tabs)
        sale_root = QVBoxLayout(sale_tab)
        sale_root.setContentsMargins(0, 10, 0, 0)
        sale_root.setSpacing(12)
        self.history_panel = CustomerHistoryPanel(is_owner=self.current_user.role == "owner", parent=self.tabs)
        self.history_panel.purchase_activated.connect(self._show_sale_from_history)
        self.tabs.addTab(sale_tab, ar.HIST_TAB_SALE)
        self.tabs.addTab(self.history_panel, ar.HIST_TAB_HISTORY)
        root.addWidget(self.tabs, 1)

        selector_layout = QHBoxLayout()
        selector_label = QLabel(f"🏷️  {ar.CUST_SALE_SELECTOR}:", self)
        selector_label.setStyleSheet("color: #9DBEFF; font-weight: 600; font-size: 13px;")
        selector_layout.addWidget(selector_label)
        self.sale_selector = QComboBox(self)
        self.sale_selector.currentIndexChanged.connect(self._render_selected_sale)
        selector_layout.addWidget(self.sale_selector, 1)
        sale_root.addLayout(selector_layout)

        # Dedicated Installment & Pricing Summary Table
        self.financial_summary_title = QLabel(f"📊  {ar.FINANCIAL_SUMMARY_TITLE}", self)
        self.financial_summary_title.setObjectName("sectionTitle")
        sale_root.addWidget(self.financial_summary_title)

        self.financial_table = QTableWidget(1, 7, self)
        self.financial_table.setHorizontalHeaderLabels([
            ar.WHOLESALE_PRICE,
            ar.CASH_PRICE_DETAIL,
            ar.RATE,
            ar.TOTAL_AFTER_INSTALLMENT,
            ar.MONTHLY_AMOUNT,
            ar.MONTHS_DURATION,
            ar.TOTAL_PROFIT,
        ])
        self.financial_table.verticalHeader().setVisible(False)
        self.financial_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.financial_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.financial_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.financial_table.setShowGrid(True)
        self.financial_table.setFixedHeight(76)
        self.financial_table.setStyleSheet(
            "QTableWidget {"
            "  background-color: #0E141D;"
            "  border: 1px solid #1F2A3D;"
            "  border-radius: 8px;"
            "  gridline-color: #1F2A3D;"
            "}"
            "QHeaderView::section {"
            "  background-color: #121A2C;"
            "  color: #9DBEFF;"
            "  font-weight: 700;"
            "  font-size: 12px;"
            "  padding: 6px;"
            "  border: 1px solid #1F2A3D;"
            "}"
        )
        sale_root.addWidget(self.financial_table)

        self.details_table = QTableWidget(0, 4, self)
        self.details_table.horizontalHeader().hide()
        self.details_table.verticalHeader().setVisible(False)
        self.details_table.verticalHeader().setDefaultSectionSize(28)
        self.details_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.details_table.horizontalHeader().setStretchLastSection(True)
        self.details_table.setColumnWidth(0, 150)
        self.details_table.setColumnWidth(1, 150)
        self.details_table.setColumnWidth(2, 150)
        self.details_table.setShowGrid(False)
        self.details_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.details_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.details_table.setMaximumHeight(140)
        self.details_table.setStyleSheet(
            "QTableWidget { background-color: #0E141D; border: 1px solid #1F2A3D; border-radius: 10px; }"
        )
        sale_root.addWidget(self.details_table)

        self.schedule_title = QLabel(f"📋  {ar.SCHEDULE}", self)
        self.schedule_title.setObjectName("sectionTitle")
        sale_root.addWidget(self.schedule_title)
        self.schedule_table = QTableWidget(0, 6, self)
        self.schedule_table.setHorizontalHeaderLabels(
            [
                ar.CUST_SCHEDULE_MONTH,
                ar.DUE_DATE,
                ar.MONTHLY_AMOUNT,
                ar.CUST_SCHEDULE_PAID,
                ar.CUST_SCHEDULE_STATUS,
                ar.CUST_SCHEDULE_ACTION,
            ]
        )
        self.schedule_table.verticalHeader().setVisible(False)
        self.schedule_table.setWordWrap(False)
        self.schedule_table.horizontalHeader().setStretchLastSection(False)
        self.schedule_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        self.schedule_table.horizontalHeader().setMinimumSectionSize(52)
        for column, width in enumerate((65, 130, 140, 120, 80, 140)):
            self.schedule_table.setColumnWidth(column, width)
        self.schedule_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.schedule_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.schedule_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.schedule_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.schedule_table.setAlternatingRowColors(True)
        self.schedule_table.setShowGrid(False)
        sale_root.addWidget(self.schedule_table, 1)

        self.empty_schedule = QLabel(f"ℹ️  {ar.CUST_NO_SCHEDULE}", self)
        self.empty_schedule.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_schedule.setStyleSheet(
            "color: #8A94A6; padding: 18px; font-size: 13px; background-color: #0E141D; "
            "border: 1px dashed #1F2A3D; border-radius: 8px;"
        )
        sale_root.addWidget(self.empty_schedule)

        self.credit_payment_panel = QFrame(self)
        self.credit_payment_panel.setObjectName("summaryTile")
        self.credit_payment_panel.setStyleSheet(
            "QFrame#summaryTile { background-color: #0E141D; border: 1px solid #1F2A3D; border-radius: 8px; padding: 10px 14px; }"
        )
        credit_layout = QHBoxLayout(self.credit_payment_panel)
        credit_layout.setContentsMargins(0, 0, 0, 0)
        self.credit_balance_label = QLabel(self.credit_payment_panel)
        self.credit_balance_label.setStyleSheet("color: #EBF0FF; font-weight: 700; font-size: 15px;")
        self.credit_payment_button = QPushButton(f"💳  {ar.CUST_RECORD_CREDIT_PAYMENT}", self.credit_payment_panel)
        self.credit_payment_button.setProperty("variant", "success")
        self.credit_payment_button.setProperty("compact", True)
        self.credit_payment_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.credit_payment_button.clicked.connect(self._record_credit_payment)
        credit_layout.addWidget(self.credit_balance_label, 1)
        credit_layout.addWidget(self.credit_payment_button)
        sale_root.addWidget(self.credit_payment_panel)

        pdf_row = QHBoxLayout()
        self.commitment_pdf_button = QPushButton(f"📄  {ar.PDF_COMMITMENT_ACTION}", self)
        self.commitment_pdf_button.setProperty("variant", "secondary")
        self.commitment_pdf_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.commitment_pdf_button.clicked.connect(self._print_commitment)
        self.receipt_history_label = QLabel(f"🧾  {ar.PDF_RECEIPT_HISTORY}:", self)
        self.receipt_history_label.setStyleSheet("color: #9DBEFF; font-weight: 600; font-size: 13px;")
        self.receipt_history = QComboBox(self)
        self.receipt_history.setMinimumWidth(240)
        self.receipt_pdf_button = QPushButton(f"🖨️  {ar.PDF_RECEIPT_ACTION}", self)
        self.receipt_pdf_button.setProperty("variant", "secondary")
        self.receipt_pdf_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.receipt_pdf_button.clicked.connect(self._print_payment_receipt)
        pdf_row.addWidget(self.commitment_pdf_button)
        pdf_row.addWidget(self.receipt_history_label)
        pdf_row.addWidget(self.receipt_history, 1)
        pdf_row.addWidget(self.receipt_pdf_button)
        sale_root.addLayout(pdf_row)

        buttons = QHBoxLayout()
        self.edit_button = QPushButton(f"✏️  {ar.CUST_EDIT}", self)
        self.edit_button.setProperty("variant", "secondary")
        self.edit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_button.setVisible(self.current_user.role == "owner")
        self.edit_sale_button = QPushButton(f"📝  {ar.CUST_EDIT_SALE}", self)
        self.edit_sale_button.setProperty("variant", "secondary")
        self.edit_sale_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_sale_button.setVisible(self.current_user.role == "owner")
        self.close_button = QPushButton(ar.CUST_CLOSE, self)
        self.close_button.setProperty("variant", "primary")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_button.clicked.connect(self._request_edit)
        self.edit_sale_button.clicked.connect(self._edit_selected_sale)
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.edit_button)
        buttons.addWidget(self.edit_sale_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        root.addLayout(buttons)

    def _reload_customer(self) -> None:
        """Fetch the customer graph in a short-lived application session."""
        with session_scope(self._engine) as session:
            self._customer = customers.get_customer_details(session, self.customer_id)
            configured_grace = get_value(session, "grace_days", 5)
            configured_due_mode = get_value(session, "due_mode", "first_of_month")
        if (
            isinstance(configured_grace, int)
            and not isinstance(configured_grace, bool)
            and configured_grace >= 0
        ):
            self._grace_days = configured_grace
        else:
            self._grace_days = 5
        self._due_mode = (
            configured_due_mode
            if configured_due_mode in ("first_of_month", "purchase_day")
            else "first_of_month"
        )

    def _show_sale_from_history(self, sale_id: int) -> None:
        """Open a purchase picked in the history tab."""
        index = self.sale_selector.findData(sale_id)
        if index >= 0:
            self.sale_selector.setCurrentIndex(index)
        self.tabs.setCurrentIndex(0)

    def _load_history(self) -> None:
        """Refresh the history summary and tab from the history service."""
        with session_scope(self._engine) as session:
            history = customers.customer_history(session, self.customer_id, today=date.today())
        self.history_summary.show_history(history)
        self.history_panel.show_history(history)

    def _populate_sales(self) -> None:
        """Fill the selector and render the current or first sale."""
        self._load_history()
        self.name_label.setText(self._customer.full_name)
        self.contact_label.setText(self._contact_text())
        now = date.today()
        customer_status = for_customer(
            self._customer.sales,
            now.year,
            now.month,
            now,
            grace_days=self._grace_days,
        )
        self._set_status(customer_status)
        selected_sale_id = self._sale_id
        self.sale_selector.blockSignals(True)
        self.sale_selector.clear()
        for sale in sorted(self._customer.sales, key=lambda item: (item.purchase_date, item.id)):
            kind = self._sale_type_label(sale.sale_type)
            label = f"{sale.product} · {self._format_date(sale.purchase_date)} · {kind}"
            self.sale_selector.addItem(label, sale.id)
        selected_index = self.sale_selector.findData(selected_sale_id)
        self.sale_selector.setCurrentIndex(selected_index if selected_index >= 0 else 0)
        self.sale_selector.setEnabled(self.sale_selector.count() > 1)
        self.sale_selector.blockSignals(False)
        self._render_selected_sale(self.sale_selector.currentIndex())

    def _render_selected_sale(self, _index: int = -1) -> None:
        """Show fields and schedule rows for the selected sale."""
        sale_id = self.sale_selector.currentData()
        sale = next((item for item in self._customer.sales if item.id == sale_id), None)
        self._sale_id = sale.id if sale is not None else None
        self.details_table.setRowCount(0)
        self.schedule_table.setRowCount(0)
        self.empty_schedule.hide()
        self.credit_payment_panel.hide()
        self._refresh_print_actions(sale)
        self.schedule_table.show()
        if sale is None:
            self.financial_summary_title.hide()
            self.financial_table.hide()
            self.schedule_table.hide()
            self.schedule_title.hide()
            self.empty_schedule.show()
            return

        self.financial_summary_title.show()
        self.financial_table.show()
        self._populate_financial_summary(sale)
        self._populate_details(sale)
        if sale.sale_type == "installment":
            self.schedule_title.show()
            self.empty_schedule.hide()
            self._populate_schedule(sale)
        elif sale.sale_type == "credit":
            self.schedule_table.hide()
            self.schedule_title.hide()
            self.empty_schedule.setText(ar.CUST_NO_SCHEDULE)
            self.empty_schedule.show()
            self._populate_credit_balance(sale)
        else:
            self.schedule_table.hide()
            self.schedule_title.hide()
            self.empty_schedule.setText(ar.CUST_NO_SCHEDULE)
            self.empty_schedule.show()

    def _populate_financial_summary(self, sale: Sale) -> None:
        """Render the 7 core installment fields requested by the shop owner."""
        is_owner = self.current_user.role == "owner"
        wholesale_text = self._money(sale.wholesale_price) if is_owner else "—"
        cash_price_text = self._money(sale.cash_price)
        rate_text = f"{sale.rate}%"
        total_text = self._money(sale.total)
        interval = sale.payment_interval or 1
        monthly_text = self._money(sale.monthly_amount)
        if sale.monthly_amount is not None and interval > 1:
            monthly_text = f"{monthly_text} {every_text(interval)}"
        months_text = ar.MONTHS_COUNT.format(count=sale.months) if sale.months is not None else "—"
        profit_text = self._money(sale.profit) if is_owner else "—"

        values = [
            (wholesale_text, "#3B92D9"),
            (cash_price_text, "#9DBEFF"),
            (rate_text, "#38BDF8"),
            (total_text, "#EBF0FF"),
            (monthly_text, "#F59E0B"),
            (months_text, "#EBF0FF"),
            (profit_text, "#22C55E"),
        ]
        self.financial_table.setRowCount(1)
        for col, (text, color) in enumerate(values):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setForeground(QColor(color))
            font = item.font()
            font.setBold(True)
            font.setPointSize(11)
            item.setFont(font)
            self.financial_table.setItem(0, col, item)

    def _populate_details(self, sale: Sale) -> None:
        """Render sale fields in two compact label/value pairs per row."""
        fields: list[tuple[str, str]] = [
            (ar.PRODUCT, sale.product),
            (ar.CUST_SALE_TYPE, self._sale_type_label(sale.sale_type)),
        ]
        if self.current_user.role == "owner":
            fields.append((ar.WHOLESALE_PRICE, self._money(sale.wholesale_price)))
        fields.extend(
            (
                (ar.CASH_PRICE, self._money(sale.cash_price)),
                (ar.RATE, f"{sale.rate}%"),
                (ar.DOWN_PAYMENT, self._money(sale.down_payment)),
                (ar.TOTAL_PRICE, self._money(sale.total)),
                (amount_label(sale.payment_interval or 1), self._money(sale.monthly_amount)),
                (ar.MONTHS, "—" if sale.months is None else str(sale.months)),
            )
        )
        if sale.sale_type == "installment":
            fields.append((ar.PLAN_INTERVAL, every_text(sale.payment_interval or 1)))
        if self.current_user.role == "owner":
            fields.append((ar.PROFIT, self._money(sale.profit)))
        fields.extend(
            (
                (ar.PURCHASE_DATE, self._format_date(sale.purchase_date)),
                (ar.END_DATE, self._format_date(sale.end_date)),
            )
        )
        self.details_table.setRowCount((len(fields) + 1) // 2)
        for index, (label, value) in enumerate(fields):
            row, pair = divmod(index, 2)
            column = pair * 2
            self.details_table.setItem(row, column, QTableWidgetItem(label))
            self.details_table.setItem(row, column + 1, QTableWidgetItem(value))

    def _populate_schedule(self, sale: Sale) -> None:
        """Render monthly due rows with per-row status and payment actions."""
        installments = sorted(sale.installments, key=lambda item: item.installment_index)
        if installments:
            rows: list[tuple[Installment | None, Installment | ScheduledInstallment]] = [
                (installment, installment) for installment in installments
            ]
        else:
            # Older/imported sale rows may lack stored installments; show the
            # calculated schedule for context but keep payment actions disabled.
            rows = [(None, draft) for draft in schedule.build(sale, {"due_mode": self._due_mode})]
        self.schedule_table.setRowCount(len(rows))
        now = date.today()
        for row, (installment, planned) in enumerate(rows):
            index = installment.installment_index if installment is not None else planned.installment_index
            due_date = installment.due_date if installment is not None else planned.due_date
            amount_due = installment.amount_due if installment is not None else planned.amount_due
            paid_amount = (
                max(
                    installment.amount_paid,
                    sum(payment.amount for payment in installment.payments),
                )
                if installment is not None
                else 0
            )
            remaining = max(0, amount_due - paid_amount)
            self.schedule_table.setItem(row, 0, QTableWidgetItem(str(index)))
            self.schedule_table.setItem(row, 1, QTableWidgetItem(self._format_date(due_date)))
            self.schedule_table.setItem(row, 2, QTableWidgetItem(self._money(amount_due)))
            self.schedule_table.setItem(row, 3, QTableWidgetItem(self._money(paid_amount)))
            state = for_month(
                [installment if installment is not None else planned],
                due_date.year,
                due_date.month,
                now,
                grace_days=self._grace_days,
            )
            status_dot = QLabel("●", self.schedule_table)
            status_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            status_dot.setStyleSheet(f"color: {self._status_color(state)}; font-size: 18px;")
            status_dot.setToolTip(self._status_label(state))
            self.schedule_table.setCellWidget(row, 4, status_dot)
            if installment is not None and remaining > 0:
                action = QPushButton(f"💳  {ar.CUST_RECORD_PAYMENT}", self.schedule_table)
                action.setProperty("variant", "success")
                action.setProperty("compact", True)
                action.setCursor(Qt.CursorShape.PointingHandCursor)
                action.clicked.connect(
                    lambda _checked=False, installment_id=installment.id, outstanding=remaining:
                        self._open_installment_payment(installment_id, outstanding)
                )
                self.schedule_table.setCellWidget(row, 5, action)
            else:
                self.schedule_table.setItem(row, 5, QTableWidgetItem("—"))

    def _populate_credit_balance(self, sale: Sale) -> None:
        """Offer partial payment recording for a credit sale without installments."""
        paid = sum(payment.amount for payment in sale.credit_payments)
        remaining = max(0, sale.financed - paid)
        self.credit_balance_label.setText(f"{ar.CUST_CREDIT_BALANCE}: {self._money(remaining)}")
        self.credit_payment_button.setEnabled(remaining > 0)
        self.credit_payment_panel.show()

    def _refresh_print_actions(self, sale: Sale | None) -> None:
        """Show the commitment action and load selectable payments for receipts."""
        self.edit_sale_button.setVisible(
            self.current_user.role == "owner" and sale is not None
        )
        self._payments_by_id = {}
        self.receipt_history.clear()
        if sale is None:
            self.commitment_pdf_button.setVisible(False)
            self.receipt_history_label.setVisible(False)
            self.receipt_history.setVisible(False)
            self.receipt_pdf_button.setVisible(False)
            return

        self.commitment_pdf_button.setVisible(sale.sale_type == "installment")
        history: list[Payment] = []
        if sale.sale_type == "credit":
            history.extend(sale.credit_payments)
        else:
            for installment in sale.installments:
                history.extend(installment.payments)
        history.sort(key=lambda payment: (payment.payment_date, payment.id), reverse=True)
        for payment in history:
            self._payments_by_id[payment.id] = payment
            method = self._payment_method_label(payment.method)
            label = (
                f"{self._format_date(payment.payment_date)} · "
                f"{self._money(payment.amount)} · {method}"
            )
            self.receipt_history.addItem(label, payment.id)
        has_history = bool(history)
        self.receipt_history_label.setVisible(has_history)
        self.receipt_history.setVisible(has_history)
        self.receipt_pdf_button.setVisible(has_history)
        self.receipt_pdf_button.setEnabled(has_history)

    def _print_commitment(self) -> None:
        """Choose a path and generate the selected sale's commitment form."""
        sale = self._selected_sale()
        if sale is None or sale.sale_type != "installment":
            return
        default_name = self._suggested_pdf_name(sale, "commitment")
        try:
            path = self._choose_pdf_path(ar.PDF_COMMITMENT_ACTION, default_name)
            if not path:
                return
            output = generate_commitment_pdf(
                path,
                self._customer,
                sale,
                include_sensitive=self.current_user.role == "owner",
            )
        except Exception:
            QMessageBox.warning(self, ar.PDF_SAVE_DIALOG_TITLE, ar.PDF_GENERATE_ERROR)
            return
        self._offer_open_pdf(output)

    def _print_payment_receipt(self) -> None:
        """Choose a path and print the selected payment from the sale history."""
        sale = self._selected_sale()
        payment_id = self.receipt_history.currentData()
        if sale is None or payment_id is None:
            return
        payment = self._payments_by_id.get(int(payment_id))
        if payment is None:
            return
        default_name = self._suggested_pdf_name(sale, f"receipt_{payment.id}")
        try:
            path = self._choose_pdf_path(ar.PDF_RECEIPT_ACTION, default_name)
            if not path:
                return
            output = generate_payment_receipt_pdf(
                path,
                self._customer,
                sale,
                payment,
                issued_by=self.current_user.username,
            )
        except Exception:
            QMessageBox.warning(self, ar.PDF_SAVE_DIALOG_TITLE, ar.PDF_GENERATE_ERROR)
            return
        self._offer_open_pdf(output)

    def _choose_pdf_path(self, title: str, filename: str) -> str | None:
        """Open a save dialog rooted in the user's PDF exports directory."""
        ensure_data_dirs()
        suggested_path = data_dir() / "exports" / filename
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            title,
            str(suggested_path),
            ar.PDF_FILE_FILTER,
        )
        return path or None

    def _offer_open_pdf(self, path: Path) -> None:
        """Let the user open the PDF in the system's default viewer."""
        box = QMessageBox(QMessageBox.Icon.Information, ar.PDF_SAVE_DIALOG_TITLE,
                         f"{ar.PDF_SAVE_SUCCESS}\n{path}", parent=self)
        open_button = box.addButton(ar.PDF_OPEN_FILE, QMessageBox.ButtonRole.AcceptRole)
        box.addButton(ar.PDF_CLOSE, QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() == open_button:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                QMessageBox.warning(self, ar.PDF_SAVE_DIALOG_TITLE, ar.PDF_OPEN_ERROR)

    def _suggested_pdf_name(self, sale: Sale, kind: str) -> str:
        """Create a safe, descriptive default filename for the selected sale."""
        stem = f"{self._customer.full_name}_{sale.product}_{kind}"
        safe = re.sub(r"[<>:\"/\\|?*]+", "_", stem).strip(" ._")
        return f"{safe or 'AkremMobile'}.pdf"

    @staticmethod
    def _payment_method_label(method: str) -> str:
        """Translate a stored payment method for the selection list."""
        return {
            "cash": ar.PDF_METHOD_CASH,
            "ccp": ar.PDF_METHOD_CCP,
            "baridimob": ar.PDF_METHOD_BARIDIMOB,
        }.get(method.casefold(), method)

    def _open_installment_payment(self, installment_id: int, remaining: int) -> None:
        """Collect a payment and save it against the selected installment."""
        dialog = PaymentEntryDialog(remaining, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        amount, payment_date, method, note = dialog.values()
        try:
            with session_scope(self._engine) as session:
                payment = payments.record_installment_payment_for_user(
                    session,
                    int(self.current_user.id),
                    installment_id,
                    amount,
                    payment_date,
                    method,
                    note,
                )
                payment_id = payment.id
        except (PermissionError, ValueError, RuntimeError, SQLAlchemyError):
            QMessageBox.warning(self, ar.PAYMENT_DIALOG_TITLE, ar.PAYMENT_SAVE_ERROR)
            return
        self._notify_payment_changed(payment_id)

    def _record_credit_payment(self) -> None:
        """Collect a payment and save it against the selected credit sale."""
        sale = self._selected_sale()
        if sale is None or sale.sale_type != "credit":
            return
        paid = sum(payment.amount for payment in sale.credit_payments)
        remaining = max(0, sale.financed - paid)
        if remaining <= 0:
            return
        dialog = PaymentEntryDialog(remaining, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        amount, payment_date, method, note = dialog.values()
        try:
            with session_scope(self._engine) as session:
                payment = payments.record_credit_payment_for_user(
                    session,
                    int(self.current_user.id),
                    sale.id,
                    amount,
                    payment_date,
                    method,
                    note,
                )
                payment_id = payment.id
        except (PermissionError, ValueError, RuntimeError, SQLAlchemyError):
            QMessageBox.warning(self, ar.PAYMENT_DIALOG_TITLE, ar.PAYMENT_SAVE_ERROR)
            return
        self._notify_payment_changed(payment_id)

    def _notify_payment_changed(self, payment_id: int) -> None:
        """Broadcast payment changes, then refresh this dialog's detached data."""
        events.payment_changed.emit(payment_id)
        if self._sale_id is not None:
            events.sale_changed.emit(self._sale_id)
        events.customer_changed.emit(self.customer_id)
        events.data_changed.emit()
        events.notify.emit("success", ar.PAYMENT_DIALOG_TITLE, ar.PAY_SAVE_DONE, 4000)
        self._reload_customer()
        self._populate_sales()

    def _request_edit(self) -> None:
        """Notify the owning page that customer or sale editing was requested."""
        self.edit_requested.emit(self.customer_id)
        self.accept()

    def _edit_selected_sale(self) -> None:
        """Edit the selected sale through the owner-guarded sales service."""
        if self.current_user.role != "owner":
            return
        sale = self._selected_sale()
        if sale is None:
            return
        dialog = SaleEditDialog(sale, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        changes = dialog.values()
        try:
            with session_scope(self._engine) as session:
                sales_service.update_sale_for_user(
                    session,
                    int(self.current_user.id),
                    sale.id,
                    **changes,
                )
        except PaidInstallmentEditError:
            answer = QMessageBox.question(
                self,
                ar.CUST_EDIT_SALE,
                ar.CUST_EDIT_SALE_HISTORY_CONFIRM,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                with session_scope(self._engine) as session:
                    sales_service.update_sale_for_user(
                        session,
                        int(self.current_user.id),
                        sale.id,
                        allow_paid_history_rewrite=True,
                        **changes,
                    )
            except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
                QMessageBox.warning(self, ar.CUST_EDIT_SALE, ar.CUST_EDIT_SALE_ERROR)
                return
        except (auth.AuthorizationError, ValueError, RuntimeError, SQLAlchemyError):
            QMessageBox.warning(self, ar.CUST_EDIT_SALE, ar.CUST_EDIT_SALE_ERROR)
            return
        events.sale_changed.emit(sale.id)
        events.data_changed.emit()
        self._reload_customer()
        self._populate_sales()
        QMessageBox.information(self, ar.CUST_EDIT_SALE, ar.CUST_EDIT_SALE_SAVED)

    def _selected_sale(self) -> Sale | None:
        """Return the currently selected sale object, if one exists."""
        selected_id = self.sale_selector.currentData()
        return next((sale for sale in self._customer.sales if sale.id == selected_id), None)

    def _contact_text(self) -> str:
        """Return phone and category details for the customer header."""
        return f"{self._customer.phone or '—'}  ·  {self._customer.category.name}"

    @staticmethod
    def _sale_type_label(sale_type: str) -> str:
        """Translate the stored sale kind for display."""
        return {
            "cash": ar.SALE_CASH,
            "installment": ar.SALE_INSTALLMENT,
            "credit": ar.SALE_CREDIT,
        }.get(sale_type, sale_type)

    @staticmethod
    def _status_color(status: Status) -> str:
        """Map status values to the documented palette."""
        return {
            Status.PAID: "#22C55E",
            Status.FAILED: "#EF4444",
            Status.PENDING: "#F59E0B",
            Status.NONE: "#8A94A6",
        }[status]

    @staticmethod
    def _status_label(status: Status) -> str:
        """Translate status states for the status dot tooltip and header."""
        return {
            Status.PAID: ar.CUST_STATUS_TIP_PAID,
            Status.FAILED: ar.CUST_STATUS_TIP_FAILED,
            Status.PENDING: ar.CUST_STATUS_TIP_PENDING,
            Status.NONE: ar.CUST_STATUS_TIP_NONE,
        }[status]

    def _set_status(self, status: Status) -> None:
        """Refresh the status dot and visible label."""
        color = self._status_color(status)
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 20px;")
        self.status_dot.setToolTip(self._status_label(status))
        self.status_text.setText(self._status_label(status))

        if status == Status.FAILED:
            grade = "⚠️ عالي المخاطر (35%)"
            b_bg, b_fg, b_bc = "#331014", "#F87171", "#7F1D1D"
        elif status == Status.PENDING:
            grade = "⭐⭐⭐⭐ جيد (80%)"
            b_bg, b_fg, b_bc = "#2A1B07", "#FBBF24", "#78350F"
        elif status == Status.PAID:
            grade = "⭐⭐⭐⭐⭐ ممتاز (98%)"
            b_bg, b_fg, b_bc = "#06281B", "#34D399", "#065F46"
        else:
            grade = "⭐⭐⭐⭐ موثوق (90%)"
            b_bg, b_fg, b_bc = "#0A1C2E", "#38BDF8", "#0284C7"

        if hasattr(self, "credit_badge_label"):
            self.credit_badge_label.setText(f"مؤشر الالتزام: {grade}")
            self.credit_badge_label.setStyleSheet(
                f"background-color: {b_bg}; color: {b_fg}; border: 1px solid {b_bc}; "
                "border-radius: 8px; padding: 4px 10px; font-weight: 700; font-size: 12px;"
            )

    @staticmethod
    def _money(amount: int | None) -> str:
        """Format integer DZD amounts without introducing floating point."""
        return "—" if amount is None else f"{amount:,} {ar.CURRENCY_SUFFIX}"

    @staticmethod
    def _format_date(value: date | None) -> str:
        """Format optional dates in the application's DD/MM/YYYY display style."""
        return "—" if value is None else value.strftime("%d/%m/%Y")

    def _center_on_parent(self) -> None:
        """Center the dialog over its parent or the active screen."""
        if self.parentWidget() is not None:
            center = self.parentWidget().geometry().center()
            self.move(self.parentWidget().mapToGlobal(center) - self.rect().center())
            return
        screen = self.screen()
        if screen is not None:
            self.move(screen.availableGeometry().center() - self.rect().center())
