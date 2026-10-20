"""Two-module sales page: Dedicated Cash Checkout vs. Installment Contracts.

Both modules have a basket: a client can take several products in one visit.
"Add to basket" keeps the product in the form (with its own type, price,
plan and phone) and empties the form for the next one; saving records every
product as its own sale of the same client, in one transaction.

Prices follow the catalog and the phones in stock: when the owner edits a
product, the forms pick up the new price unless a price was typed by hand.

Every sale (cash, credit or installment, alone or in a basket) takes its
phones out of the stock, so the product lists only offer products with a
phone in stock; a product sold out disappears from them after the sale.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QDate, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import data_dir
from app.db.models import Category, Customer, User
from app.db.session import session_scope
from app.i18n import ar
from app.services import calc, categories, customers, print_templates, sales, stock
from app.services.customers import DuplicateCustomerPhoneError
from app.services.pdf_forms import ReceiptItem, generate_cash_sale_receipt_pdf
from app.services import products as product_service
from app.services import schedule
from app.services.settings import get_rate_presets, get_value
from app.ui.dialogs.customer_form_dialog import CustomerFormDialog
from app.ui.dialogs.owner_approval_dialog import ask_owner_approval
from app.ui.events import events
from app.ui.screen_fit import fit_table_to_rows
from app.ui.widgets.phone_details import PhoneDetailsSection
from app.ui.widgets.sale_basket import SaleBasket, sale_type_label
from app.i18n.stock_text import ltr_text
from app.i18n.plan_text import amount_label, plan_description


_PLAN_ROLE = Qt.ItemDataRole.UserRole + 3  # (months, interval, rate) of a catalog product


def _scrolling(widget: QWidget) -> QScrollArea:
    """Let a long form scroll instead of squeezing on small screens."""
    area = QScrollArea(widget.parentWidget())
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return area


class NewSalePage(QWidget):
    """Overhauled sales page with isolated Cash and Installment interfaces."""

    customer_details_requested = Signal(int)

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        if hasattr(current_user, "id"):
            self.current_user = SimpleNamespace(
                id=int(current_user.id),
                role=str(getattr(current_user, "role", "seller")),
                username=str(getattr(current_user, "username", "")),
                display_name=str(getattr(current_user, "display_name", "")),
            )
        else:
            self.current_user = current_user
        self._customer_id: int | None = None
        self._rate_presets: dict[int, int] = {}
        self._cash_status = "paid"  # 'paid' or 'credit'
        self._last_saved_cash_sale_id: int | None = None
        self._last_saved_cash_sale_ids: list[int] = []
        # Prices / plan last filled in automatically; a different value was typed by hand.
        self._auto_prices: dict[str, tuple[int, int]] = {"sale": (0, 0), "cash": (0, 0)}
        self._auto_plan: tuple[int, int] | None = None
        self._refreshing_products = False

        self._build_ui()
        self._refresh_customers()
        self._refresh_products()
        self._month_changed(self.months.value())
        self._update_sale_type()
        self._update_preview()
        self._update_cash_calculations()
        events.data_changed.connect(self._refresh_products)

    def _build_ui(self) -> None:
        """Build top-level mode switcher, Cash flow view, and Installment view."""
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 20, 28, 20)
        root.setSpacing(12)

        # Header Title
        title_row = QHBoxLayout()
        title = QLabel(ar.SIDEBAR_ITEMS[2], self)
        title.setObjectName("pageTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        root.addLayout(title_row)

        # Module Switcher (Segmented Tab Bar: Cash vs. Installments)
        mode_bar = QFrame(self)
        mode_bar.setObjectName("salesModeBar")
        mode_bar_layout = QHBoxLayout(mode_bar)
        mode_bar_layout.setContentsMargins(4, 4, 4, 4)
        mode_bar_layout.setSpacing(8)

        self.tab_cash_btn = QPushButton(f"💵  {ar.CASH_TAB_TITLE}", mode_bar)
        self.tab_installment_btn = QPushButton(f"📅  {ar.INSTALLMENT_TAB_TITLE}", mode_bar)

        for btn in (self.tab_cash_btn, self.tab_installment_btn):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(40)
            btn.setStyleSheet(
                "QPushButton { font-size: 13px; font-weight: 700; border-radius: 8px; padding: 0 20px; } "
                "QPushButton[active='false'] { background-color: #0E141D; color: #8A94A6; border: 1px solid #1F2E47; } "
                "QPushButton[active='false']:hover { background-color: #151D2A; color: #FFFFFF; } "
                "QPushButton[active='true'] { background-color: #0758CD; color: #FFFFFF; border: 1px solid #3B92D9; }"
            )

        self.tab_cash_btn.clicked.connect(lambda: self._switch_module(0))
        self.tab_installment_btn.clicked.connect(lambda: self._switch_module(1))

        mode_bar_layout.addWidget(self.tab_cash_btn, 1)
        mode_bar_layout.addWidget(self.tab_installment_btn, 1)
        root.addWidget(mode_bar)

        # Stack holding Cash Module (0) and Installment Module (1)
        self.module_stack = QStackedWidget(self)
        self.cash_module = self._build_cash_module()
        self.installment_module = self._build_installment_module()
        self.module_stack.addWidget(self.cash_module)
        self.module_stack.addWidget(self.installment_module)
        root.addWidget(self.module_stack, 1)

        self._switch_module(0)

    def _switch_module(self, index: int) -> None:
        """Switch between Cash module (0) and Installment module (1)."""
        self.module_stack.setCurrentIndex(index)
        self.tab_cash_btn.setProperty("active", index == 0)
        self.tab_installment_btn.setProperty("active", index == 1)
        self.tab_cash_btn.style().unpolish(self.tab_cash_btn)
        self.tab_cash_btn.style().polish(self.tab_cash_btn)
        self.tab_installment_btn.style().unpolish(self.tab_installment_btn)
        self.tab_installment_btn.style().polish(self.tab_installment_btn)

        if index == 0:
            if hasattr(self, "sale_type"):
                self.sale_type.setCurrentIndex(
                    self.sale_type.findData("credit" if self._cash_status == "credit" else "cash")
                )
        else:
            if hasattr(self, "sale_type"):
                self.sale_type.setCurrentIndex(self.sale_type.findData("installment"))

    # =========================================================================
    # 1. CASH FLOW MODULE (Spot Transactions & Partial/Credit Flow)
    # =========================================================================
    def _build_cash_module(self) -> QWidget:
        """Build the dedicated Cash & Spot Billing interface."""
        page = QWidget(self)
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(18)

        # ---------------------------------------------------------------------
        # Left Side: Entry Form, Status Toggle, & Customer Association
        # ---------------------------------------------------------------------
        form_card = QFrame(page)
        form_card.setObjectName("summaryTile")
        form_card.setStyleSheet(
            "QFrame#summaryTile { background-color: #0E141D; border: 1px solid #1F2A3D; border-radius: 12px; padding: 18px; }"
        )
        form_layout = QVBoxLayout(form_card)
        form_layout.setSpacing(12)

        heading = QLabel("💵  تسجيل بيع نقدي فوري وتسوية الصندوق", form_card)
        heading.setStyleSheet("color: #FFFFFF; font-size: 15px; font-weight: 700;")
        form_layout.addWidget(heading)

        # Status Toggle (Fully Paid vs. Partial Payment / Credit)
        status_box = QFrame(form_card)
        status_box.setStyleSheet(
            "background-color: #0A0E17; border: 1px solid #1F2E47; border-radius: 10px; padding: 6px;"
        )
        status_layout = QHBoxLayout(status_box)
        status_layout.setContentsMargins(4, 4, 4, 4)
        status_layout.setSpacing(6)

        self.cash_btn_fully_paid = QPushButton(f"🟢  {ar.CASH_STATUS_FULLY_PAID}", status_box)
        self.cash_btn_partial_credit = QPushButton(f"🟠  {ar.CASH_STATUS_PARTIAL_CREDIT}", status_box)

        for btn in (self.cash_btn_fully_paid, self.cash_btn_partial_credit):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(36)
            btn.setStyleSheet(
                "QPushButton { font-size: 12px; font-weight: 700; border-radius: 6px; padding: 0 14px; } "
                "QPushButton[active='false'] { background-color: #0E1522; color: #8A94A6; border: 1px solid #1F2E47; } "
                "QPushButton[active='false']:hover { background-color: #162032; color: #FFFFFF; } "
                "QPushButton[active='true'] { background-color: #0758CD; color: #FFFFFF; border: 1px solid #3B92D9; }"
            )

        self.cash_btn_fully_paid.clicked.connect(lambda: self._set_cash_status("paid"))
        self.cash_btn_partial_credit.clicked.connect(lambda: self._set_cash_status("credit"))

        status_layout.addWidget(self.cash_btn_fully_paid, 1)
        status_layout.addWidget(self.cash_btn_partial_credit, 1)
        form_layout.addWidget(status_box)

        # Debt Warning Banner (Shown only for partial payment / credit)
        self.cash_debt_warning = QFrame(form_card)
        self.cash_debt_warning.setStyleSheet(
            "background-color: #241400; border: 1px solid #F59E0B; border-radius: 8px; padding: 10px;"
        )
        debt_warn_layout = QHBoxLayout(self.cash_debt_warning)
        debt_warn_layout.setContentsMargins(6, 4, 6, 4)
        debt_warn_icon = QLabel("⚠️", self.cash_debt_warning)
        debt_warn_icon.setStyleSheet("font-size: 16px;")
        debt_warn_text = QLabel(ar.CASH_CREDIT_CUSTOMER_REQUIRED, self.cash_debt_warning)
        debt_warn_text.setStyleSheet("color: #FCD34D; font-size: 11px; font-weight: 600;")
        debt_warn_text.setWordWrap(True)
        debt_warn_layout.addWidget(debt_warn_icon)
        debt_warn_layout.addWidget(debt_warn_text, 1)
        self.cash_debt_warning.setVisible(False)
        form_layout.addWidget(self.cash_debt_warning)

        # Customer Association Box
        cust_box = QFrame(form_card)
        cust_box.setStyleSheet(
            "background-color: #0A0E17; border: 1px solid #1F2E47; border-radius: 8px; padding: 10px;"
        )
        cust_box_layout = QVBoxLayout(cust_box)
        cust_box_layout.setContentsMargins(6, 6, 6, 6)
        cust_box_layout.setSpacing(6)

        self.cash_walkin_check = QCheckBox(ar.CASH_WALKIN_CUSTOMER, cust_box)
        self.cash_walkin_check.setChecked(True)
        self.cash_walkin_check.setStyleSheet("color: #9DBEFF; font-weight: 600; font-size: 12px;")
        self.cash_walkin_check.toggled.connect(self._toggle_cash_walkin)
        cust_box_layout.addWidget(self.cash_walkin_check)

        self.cash_cust_select_container = QWidget(cust_box)
        cust_sel_layout = QVBoxLayout(self.cash_cust_select_container)
        cust_sel_layout.setContentsMargins(0, 0, 0, 0)
        cust_sel_layout.setSpacing(6)

        search_row = QHBoxLayout()
        search_lbl = QLabel("🔍", self.cash_cust_select_container)
        search_lbl.setStyleSheet("color: #3B92D9;")
        self.cash_customer_search = QLineEdit(self.cash_cust_select_container)
        self.cash_customer_search.setPlaceholderText("ابحث عن الزبون بالاسم أو الهاتف...")
        self.cash_add_customer_button = QPushButton(f"➕  {ar.ADD_CUSTOMER}", self.cash_cust_select_container)
        self.cash_add_customer_button.setProperty("variant", "secondary")
        self.cash_add_customer_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cash_add_customer_button.clicked.connect(self.add_new_customer)
        search_row.addWidget(search_lbl)
        search_row.addWidget(self.cash_customer_search, 1)
        search_row.addWidget(self.cash_add_customer_button)
        cust_sel_layout.addLayout(search_row)

        self.cash_customer_combo = QComboBox(self.cash_cust_select_container)
        self.cash_customer_combo.currentIndexChanged.connect(
            lambda _index: self._sync_customer("cash_customer_combo", "customer_combo")
        )
        cust_sel_layout.addWidget(self.cash_customer_combo)
        cust_box_layout.addWidget(self.cash_cust_select_container)
        self.cash_cust_select_container.setVisible(False)

        form_layout.addWidget(cust_box)

        # Financial Inputs Form
        inputs_form = QFormLayout()
        inputs_form.setSpacing(10)

        self.cash_product = QComboBox(form_card)
        self.cash_product.setEditable(self.current_user.role == "owner")
        self.cash_product.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.cash_product.currentTextChanged.connect(self._on_cash_product_changed)

        self.cash_wholesale = self._money_input(form_card)
        self.cash_wholesale.valueChanged.connect(self._update_cash_calculations)
        self.cash_total = self._money_input(form_card)
        self.cash_total.valueChanged.connect(self._update_cash_calculations)

        self.cash_amount_paid = self._money_input(form_card)
        self.cash_amount_paid.valueChanged.connect(self._update_cash_calculations)

        self.cash_date = QDateEdit(QDate.currentDate(), form_card)
        self.cash_date.setDisplayFormat("dd/MM/yyyy")
        self.cash_date.setCalendarPopup(True)

        self.cash_expected_date = QDateEdit(QDate.currentDate().addDays(30), form_card)
        self.cash_expected_date.setDisplayFormat("dd/MM/yyyy")
        self.cash_expected_date.setCalendarPopup(True)

        inputs_form.addRow(ar.PRODUCT, self.cash_product)
        inputs_form.addRow(ar.CASH_TOTAL_INVOICE, self.cash_total)
        if self.current_user.role == "owner":
            inputs_form.addRow(ar.WHOLESALE_PRICE, self.cash_wholesale)
        inputs_form.addRow(ar.CASH_AMOUNT_PAID, self.cash_amount_paid)

        self.cash_expected_date_label = inputs_form.labelForField(self.cash_expected_date)
        inputs_form.addRow(ar.CASH_EXPECTED_PAY_DATE, self.cash_expected_date)
        inputs_form.addRow(ar.PURCHASE_DATE, self.cash_date)

        form_layout.addLayout(inputs_form)
        cash_details_title = QLabel(ar.SALE_DETAILS_SECTION, form_card)
        cash_details_title.setObjectName("sectionTitle")
        form_layout.addWidget(cash_details_title)
        self.cash_details = PhoneDetailsSection(parent=form_card)
        self.cash_details.unit_selected.connect(self._on_cash_unit_selected)
        self.cash_details.quantity_changed.connect(self._on_cash_quantity_changed)
        self.cash_details.scanned.connect(
            lambda name, unit_id: self._show_scanned(self.cash_product, self.cash_details, name, unit_id)
        )
        form_layout.addWidget(self.cash_details)
        form_layout.addStretch(1)

        layout.addWidget(_scrolling(form_card), 3)

        # ---------------------------------------------------------------------
        # Right Side: Register Reconciliation & Receipt Generator
        # ---------------------------------------------------------------------
        summary_card = QFrame(page)
        summary_card.setObjectName("summaryTile")
        summary_card.setStyleSheet(
            "QFrame#summaryTile { background-color: #0A0E17; border: 1px solid #1F2A3D; border-radius: 12px; padding: 18px; }"
        )
        summary_layout = QVBoxLayout(summary_card)
        summary_layout.setSpacing(14)

        sum_heading = QLabel(f"📊  {ar.CASH_SUMMARY_TITLE}", summary_card)
        sum_heading.setObjectName("sectionTitle")
        summary_layout.addWidget(sum_heading)

        # 4-Tile Grid for Cash Flow
        grid = QGridLayout()
        grid.setSpacing(10)

        self.c_tile_total = self._make_stat_tile("إجمالي الفاتورة", "0 دج", "#3B92D9", summary_card)
        self.c_tile_collected = self._make_stat_tile("المقبوض في الصندوق", "0 دج", "#22C55E", summary_card)
        self.c_tile_debt = self._make_stat_tile("الدين المتبقي", "0 دج", "#64748B", summary_card)
        self.c_tile_profit = self._make_stat_tile("ربح العملية", "0 دج", "#9DBEFF", summary_card)

        grid.addWidget(self.c_tile_total[0], 0, 0)
        grid.addWidget(self.c_tile_collected[0], 0, 1)
        grid.addWidget(self.c_tile_debt[0], 1, 0)
        if self.current_user.role == "owner":
            grid.addWidget(self.c_tile_profit[0], 1, 1)

        summary_layout.addLayout(grid)

        # Payment Status Badge
        self.cash_status_pill = QLabel("🟢  مدفوع بالكامل نقداً", summary_card)
        self.cash_status_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cash_status_pill.setStyleSheet(
            "background-color: #0D2E1E; color: #4ADE80; border: 1px solid #14532D; "
            "border-radius: 8px; padding: 8px; font-weight: 700; font-size: 13px;"
        )
        summary_layout.addWidget(self.cash_status_pill)

        # Several products for the same client: kept here until the sale is saved.
        self.cash_basket = SaleBasket(summary_card)
        self.cash_basket.changed.connect(self._cash_basket_changed)
        summary_layout.addWidget(self.cash_basket)

        summary_layout.addStretch(1)

        # Action Buttons
        btn_col = QVBoxLayout()
        btn_col.setSpacing(8)

        self.cash_add_to_basket_button = QPushButton(f"➕  {ar.BASKET_ADD}", summary_card)
        self.cash_add_to_basket_button.setProperty("variant", "secondary")
        self.cash_add_to_basket_button.setFixedHeight(38)
        self.cash_add_to_basket_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cash_add_to_basket_button.setToolTip(ar.BASKET_ADD_TOOLTIP)
        self.cash_add_to_basket_button.clicked.connect(self._add_cash_to_basket)
        btn_col.addWidget(self.cash_add_to_basket_button)

        self.cash_save_btn = QPushButton("💾  تأكيد وقبض البيع", summary_card)
        self.cash_save_btn.setProperty("variant", "primary")
        self.cash_save_btn.setFixedHeight(42)
        self.cash_save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cash_save_btn.clicked.connect(self._save_cash_sale)
        btn_col.addWidget(self.cash_save_btn)

        self.cash_receipt_btn = QPushButton("🧾  طباعة وتصدير الوصل PDF", summary_card)
        self.cash_receipt_btn.setProperty("variant", "secondary")
        self.cash_receipt_btn.setFixedHeight(38)
        self.cash_receipt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cash_receipt_btn.clicked.connect(self._export_cash_receipt)
        btn_col.addWidget(self.cash_receipt_btn)

        summary_layout.addLayout(btn_col)
        layout.addWidget(summary_card, 2)

        # Connect search filter for cash customer combo
        self._cash_search_timer = QTimer(self)
        self._cash_search_timer.setSingleShot(True)
        self._cash_search_timer.setInterval(250)
        self._cash_search_timer.timeout.connect(self._refresh_cash_customers)
        self.cash_customer_search.textChanged.connect(lambda _text: self._cash_search_timer.start())

        self._set_cash_status("paid")
        return page

    def _make_stat_tile(self, title: str, value: str, color: str, parent: QWidget) -> tuple[QFrame, QLabel]:
        card = QFrame(parent)
        card.setObjectName("summaryTile")
        card.setStyleSheet(
            "QFrame#summaryTile { background-color: #0E141D; border: 1px solid #1F2A3D; border-radius: 10px; padding: 10px; }"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(3)
        t_label = QLabel(title, card)
        t_label.setObjectName("summaryTileTitle")
        v_label = QLabel(value, card)
        v_label.setObjectName("summaryTileValue")
        v_label.setStyleSheet(f"color: {color}; font-size: 16px; font-weight: 700; font-family: 'Rajdhani', sans-serif;")
        layout.addWidget(t_label)
        layout.addWidget(v_label)
        return card, v_label

    def _set_cash_status(self, status: str) -> None:
        """Toggle between Fully Paid (paid) and Partial/Credit (credit)."""
        self._cash_status = status
        self.cash_btn_fully_paid.setProperty("active", status == "paid")
        self.cash_btn_partial_credit.setProperty("active", status == "credit")
        self.cash_btn_fully_paid.style().unpolish(self.cash_btn_fully_paid)
        self.cash_btn_fully_paid.style().polish(self.cash_btn_fully_paid)
        self.cash_btn_partial_credit.style().unpolish(self.cash_btn_partial_credit)
        self.cash_btn_partial_credit.style().polish(self.cash_btn_partial_credit)

        if status == "paid":
            self.cash_debt_warning.setVisible(False)
            self.cash_walkin_check.setEnabled(True)
            self.cash_amount_paid.setEnabled(False)
            self.cash_amount_paid.setValue(self.cash_total.value())
            self.cash_expected_date.setVisible(False)
            if self.cash_expected_date_label:
                self.cash_expected_date_label.setVisible(False)
            self.cash_status_pill.setText("🟢  مدفوع بالكامل نقداً (Payé intégralement)")
            self.cash_status_pill.setStyleSheet(
                "background-color: #0D2E1E; color: #4ADE80; border: 1px solid #14532D; "
                "border-radius: 8px; padding: 8px; font-weight: 700; font-size: 13px;"
            )
        else:
            self.cash_debt_warning.setVisible(True)
            self.cash_walkin_check.setChecked(False)
            self.cash_walkin_check.setEnabled(False)
            self.cash_cust_select_container.setVisible(True)
            self.cash_amount_paid.setEnabled(True)
            self.cash_expected_date.setVisible(True)
            if self.cash_expected_date_label:
                self.cash_expected_date_label.setVisible(True)
            self.cash_status_pill.setText("🟠  دفع جزئي / دين مسجل على الزبون (Crédit)")
            self.cash_status_pill.setStyleSheet(
                "background-color: #331A00; color: #FBBF24; border: 1px solid #78350F; "
                "border-radius: 8px; padding: 8px; font-weight: 700; font-size: 13px;"
            )

        self._update_cash_calculations()

    def _toggle_cash_walkin(self, checked: bool) -> None:
        """Show or hide customer search combo in cash view."""
        self.cash_cust_select_container.setVisible(not checked)

    def _update_cash_calculations(self, *_args: object) -> None:
        """Compute live cash flow amounts, remaining debt, and profit."""
        total = self.cash_total.value()
        wholesale = self.cash_wholesale.value()

        if self._cash_status == "paid":
            amount_paid = total
            self.cash_amount_paid.blockSignals(True)
            self.cash_amount_paid.setValue(total)
            self.cash_amount_paid.blockSignals(False)
            remaining_balance = 0
        else:
            amount_paid = self.cash_amount_paid.value()
            if amount_paid > total:
                amount_paid = total
                self.cash_amount_paid.setValue(total)
            remaining_balance = max(0, total - amount_paid)

        profit = total - wholesale
        if hasattr(self, "cash_basket"):
            # The summary covers the whole visit: the basket plus the product in the form.
            for line in self.cash_basket.lines():
                price = int(line.values["cash_price"])
                paid = price if line.values["sale_type"] == "cash" else int(line.values["down_payment"])
                total += price
                amount_paid += paid
                remaining_balance += price - paid
                profit += price - int(line.values["wholesale_price"])

        self.c_tile_total[1].setText(f"{total:,} {ar.CURRENCY_SUFFIX}")
        self.c_tile_collected[1].setText(f"{amount_paid:,} {ar.CURRENCY_SUFFIX}")
        self.c_tile_debt[1].setText(f"{remaining_balance:,} {ar.CURRENCY_SUFFIX}")
        self.c_tile_debt[1].setStyleSheet(
            "color: #F59E0B; font-size: 16px; font-weight: 700; font-family: 'Rajdhani', sans-serif;"
            if remaining_balance > 0
            else "color: #64748B; font-size: 16px; font-weight: 700; font-family: 'Rajdhani', sans-serif;"
        )
        self.c_tile_profit[1].setText(f"{profit:,} {ar.CURRENCY_SUFFIX}")

    def _on_cash_product_changed(self, name: str, *, keep_typed: bool = False) -> None:
        """List the product's phones and fill one phone's price times the quantity."""
        if hasattr(self, "cash_details"):
            self.cash_details.set_product(name)
            prices = self.cash_details.unit_prices()
            quantity = self.cash_details.quantity.value()
        else:
            prices, quantity = self._product_prices(name), 1
        if prices is not None:
            self._apply_prices("cash", (prices[0] * quantity, prices[1] * quantity), keep_typed=keep_typed)
        elif self.cash_product.isEditable() and self.cash_product.currentIndex() < 0:
            self._apply_prices("cash", (0, 0), keep_typed=keep_typed)
        self._update_cash_calculations()

    def _on_cash_unit_selected(self, unit: dict | None) -> None:
        """A phone picked from stock is sold at its own price."""
        if unit is None or self._refreshing_products:
            return  # a refresh fills prices itself, keeping hand-typed ones
        self._on_cash_quantity_changed(self.cash_details.quantity.value())

    def _on_cash_quantity_changed(self, quantity: int) -> None:
        """Prices follow the quantity: one phone's price times the number sold."""
        prices = self.cash_details.unit_prices()
        if prices is None:
            return
        self._apply_prices("cash", (prices[0] * quantity, prices[1] * quantity))
        self._update_cash_calculations()

    @staticmethod
    def _show_scanned(combo: QComboBox, details: PhoneDetailsSection, name: str, unit_id: int) -> None:
        """A scanned phone: show its product (filling prices and phones), then pick that phone."""
        if combo.currentText() != name:
            combo.setCurrentText(name)
        details.select_unit(unit_id)

    def _apply_prices(self, kind: str, prices: tuple[int, int], *, keep_typed: bool = False) -> None:
        """Fill (wholesale, selling) price of the ``"sale"`` or ``"cash"`` form.

        With ``keep_typed`` (a refresh after data changed elsewhere) a price
        the user typed by hand is left alone; an automatic one is updated.
        """
        wholesale_box, price_box = (
            (self.wholesale_price, self.cash_price) if kind == "sale" else (self.cash_wholesale, self.cash_total)
        )
        current = (wholesale_box.value(), price_box.value())
        if keep_typed and current != self._auto_prices.get(kind, (0, 0)):
            return
        wholesale_box.setValue(prices[0])
        price_box.setValue(prices[1])
        self._auto_prices[kind] = (wholesale_box.value(), price_box.value())

    def _refresh_cash_customers(self) -> None:
        """Refresh customer list for cash view search (keeps the picked client when still listed)."""
        selected = self.cash_customer_combo.currentData() if self.cash_customer_combo.count() else None
        query = self.cash_customer_search.text().strip() or None
        with session_scope() as session:
            rows = customers.list_customers(
                session,
                year=date.today().year,
                month=date.today().month,
                today=date.today(),
                search=query,
            )
        self.cash_customer_combo.blockSignals(True)
        self.cash_customer_combo.clear()
        for row in rows:
            phone = row.phone or "—"
            self.cash_customer_combo.addItem(f"{row.full_name}  ·  {phone}", row.id)
        index = self.cash_customer_combo.findData(selected) if selected is not None else -1
        if index >= 0:
            self.cash_customer_combo.setCurrentIndex(index)
        self.cash_customer_combo.blockSignals(False)

    def _get_or_create_walkin_customer(self, session: Session) -> Customer:
        """Return or create a system walk-in customer for spot cash sales."""
        cust = session.scalar(select(Customer).where(Customer.full_name == "زبون نقدي مباشر"))
        if cust is None:
            default_cat = session.scalar(select(Category).order_by(Category.id))
            cat_id = default_cat.id if default_cat else 1
            cust = Customer(
                full_name="زبون نقدي مباشر",
                category_id=cat_id,
                phone=None,
                notes="حساب تلقائي لمبيعات الكاش الفورية",
            )
            session.add(cust)
            session.flush()
        return cust

    def _cash_line_values(self) -> dict[str, object] | None:
        """The product in the cash form as sale values; None (after a message) when incomplete."""
        product_name = self.cash_product.currentText().strip()
        if not product_name:
            self._show_error("يرجى إدخال أو تحديد اسم المنتج.")
            return None
        if not self.cash_details.in_stock():
            self._show_error(ar.SALE_OUT_OF_STOCK)
            return None
        total = self.cash_total.value()
        if total <= 0:
            self._show_error("يرجى إدخال مبلغ صحيح للبيع.")
            return None
        details_error = self.cash_details.error_message()
        if details_error is not None:
            self._show_error(details_error)
            return None
        wholesale = (
            self.cash_wholesale.value()
            if self.current_user.role == "owner"
            else (self._product_prices(product_name) or (0, 0))[0]
        )
        sale_type = "credit" if self._cash_status == "credit" else "cash"
        amount_paid = total if sale_type == "cash" else min(self.cash_amount_paid.value(), total)
        return {
            "product": product_name,
            "sale_type": sale_type,
            "wholesale_price": wholesale,
            "cash_price": total,
            "rate": 0,
            "down_payment": amount_paid if sale_type == "credit" else 0,
            "months": None,
            "purchase_date": self.cash_date.date().toPython(),
            "expected_pay_date": self.cash_expected_date.date().toPython() if sale_type == "credit" else None,
            **self.cash_details.values(),
        }

    def _cash_form_has_product(self) -> bool:
        return bool(self.cash_product.currentText().strip()) and self.cash_total.value() > 0

    def _add_cash_to_basket(self) -> None:
        """Keep the product in the cash form and empty the form for the next one."""
        values = self._cash_line_values()
        if values is None:
            return
        self._clear_cash_product()
        self.cash_basket.add(values)

    def _cash_basket_changed(self) -> None:
        self.cash_details.set_reserved(self.cash_basket.reserved_units(), self.cash_basket.reserved_counts())
        self._update_cash_calculations()
        count = self.cash_basket.count()
        self.cash_save_btn.setText(ar.BASKET_SAVE_MANY.format(count=count) if count else "💾  تأكيد وقبض البيع")

    def _clear_cash_product(self, *, keep_product: bool = False) -> None:
        """Empty the product part of the cash form (the client and status stay).

        ``keep_product`` (after a save) leaves the product picked, ready for the
        next client, while it still has a phone in stock.
        """
        listed = self.cash_product.findText(self.cash_product.currentText().strip()) >= 0
        if self.cash_product.isEditable() and not (keep_product and listed):
            self.cash_product.setCurrentText("")
        self.cash_details.clear()
        self.cash_details.set_product(self.cash_product.currentText())
        self._apply_prices("cash", (0, 0))
        self.cash_amount_paid.setValue(0)
        self._update_cash_calculations()

    def _save_cash_sale(self) -> None:
        """Persist the basket and the product in the form: one cash or credit sale per product."""
        lines = [line.values for line in self.cash_basket.lines()]
        if self._cash_form_has_product() or not lines:
            current = self._cash_line_values()
            if current is None:
                return
            lines.append(current)
        if len(lines) > 1 and not self._confirm_basket(lines):
            return
        all_cash = all(line["sale_type"] == "cash" for line in lines)
        if not (all_cash and self.cash_walkin_check.isChecked()) and self.cash_customer_combo.currentData() is None:
            self._show_error(ar.CASH_CREDIT_CUSTOMER_REQUIRED)
            return

        def customer_for(session: Session) -> int:
            if all_cash and self.cash_walkin_check.isChecked():
                return self._get_or_create_walkin_customer(session).id
            return int(self.cash_customer_combo.currentData())

        try:
            saved_batch = self._create_batch(customer_for, lines)
        except stock.StockError as error:
            self._show_error(self._stock_error_text(error))
            return
        except Exception as error:
            self._show_error(f"{ar.FORM_SAVE_ERROR}\n{error}")
            return
        if saved_batch is None:
            return
        customer_id, sale_ids = saved_batch
        self._last_saved_cash_sale_ids = sale_ids
        self._last_saved_cash_sale_id = sale_ids[-1]

        events.data_changed.emit()
        for sale_id in sale_ids:
            events.sale_changed.emit(sale_id)
        events.customer_changed.emit(customer_id)

        total = sum(int(line["cash_price"]) for line in lines)
        paid = sum(int(line["cash_price"]) if line["sale_type"] == "cash" else int(line["down_payment"])
                   for line in lines)
        remaining = total - paid
        if remaining <= 0:
            events.notify.emit("success", "تم تسجيل البيع بنجاح 🎉", f"تم قبض {total:,} دج نقداً في الخزينة", 4500)
            QMessageBox.information(self, "تسجيل بيع نقدي", f"تم تسجيل البيع وقبض {total:,} دج بنجاح.")
        else:
            events.notify.emit("warning", "تم تسجيل بيع بالدين (كريدي) ⚠️", f"تم تقييد دين بقيمة {remaining:,} دج في كشف حساب الزبون", 5000)
            QMessageBox.information(
                self,
                "تسجيل بيع بالدين",
                f"تم قبض تسبيق بقيمة {paid:,} دج، وتقييد دين متبقي قدره {remaining:,} دج على حساب الزبون.",
            )

        answer = QMessageBox.question(
            self,
            "طباعة الوصل",
            "هل ترغب في تصدير وطباعة وصل المعاملة الآن؟",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        # Reset the form first: the receipt then reads the saved sales.
        self.cash_basket.clear()
        self._clear_cash_product(keep_product=True)
        if answer == QMessageBox.StandardButton.Yes:
            self._export_cash_receipt()

    @staticmethod
    def _stock_error_text(error: stock.StockError) -> str:
        """The message for a sale the stock refused."""
        return {
            "out_of_stock": ar.SALE_OUT_OF_STOCK,
            "not_enough": ar.SALE_NOT_ENOUGH_STOCK,
        }.get(error.code, ar.FORM_SAVE_ERROR)

    def _create_batch(
        self, customer_for: Callable[[Session], int], lines: list[dict[str, object]]
    ) -> tuple[int, list[int]] | None:
        """Save the lines for the client ``customer_for`` picks; ``(client, sale ids)``, or None when cancelled.

        A seller's discount above the owner's limit opens the owner-approval
        box; once the owner typed their password the save is repeated with
        that approval. Other errors are raised to the caller.
        """
        approved_by: int | None = None
        while True:
            try:
                with session_scope() as session:
                    customer_id = customer_for(session)
                    saved = sales.create_sales_batch(
                        session, int(self.current_user.id), customer_id=customer_id, lines=lines,
                        approved_by_owner_id=approved_by,
                    )
                    return customer_id, [sale.id for sale in saved]
            except sales.DiscountApprovalRequired as needed:
                approved_by = ask_owner_approval(needed.percent, needed.limit, self)
                if approved_by is None:
                    return None

    def _confirm_basket(self, lines: list[dict[str, object]]) -> bool:
        """Ask before saving several products: one sale each, sharing one row in the client list."""
        listing = "\n".join(
            f"• {ltr_text(str(line['product']))} — {sale_type_label(str(line['sale_type']))} — "
            f"{int(line['cash_price']):,} {ar.CURRENCY_SUFFIX}"
            for line in lines
        )
        answer = QMessageBox.question(
            self,
            ar.BASKET_TITLE,
            ar.BASKET_CONFIRM.format(count=len(lines)) + "\n\n" + listing,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _export_cash_receipt(self) -> None:
        """Export an A5 PDF receipt for the spot sale."""
        product_name = self.cash_product.currentText().strip() or "سلعة هواتف"
        total = self.cash_total.value()
        amount_paid = self.cash_amount_paid.value() if self._cash_status == "credit" else total
        remaining = max(0, total - amount_paid)
        customer_name = "زبون نقدي مباشر"
        customer_phone = None
        sale_type = "credit" if self._cash_status == "credit" else "cash"
        purchase_date = self.cash_date.date().toPython()
        expected_pay_date = self.cash_expected_date.date().toPython() if self._cash_status == "credit" else None
        templates = print_templates.default_templates()
        items: list[ReceiptItem] | None = None
        receipt_number: int | None = None

        try:
            from app.db.models import Sale
            with session_scope() as session:
                templates = print_templates.load(session)
                saved = [
                    sale for sale_id in self._last_saved_cash_sale_ids
                    if (sale := session.get(Sale, sale_id)) is not None
                ]
                if total <= 0 and saved:
                    # The last saved basket: one receipt for all its products, one line each.
                    product_name = " + ".join(
                        f"{sale.product} ×{sale.quantity}" if sale.quantity > 1 else sale.product for sale in saved
                    )
                    items = [ReceiptItem(sale.product, sale.quantity or 1, sale.cash_price) for sale in saved]
                    receipt_number = saved[0].id
                    total = sum(sale.cash_price for sale in saved)
                    amount_paid = sum(
                        sale.down_payment if sale.sale_type == "credit" else sale.cash_price for sale in saved
                    )
                    remaining = sum(
                        max(0, sale.financed - sum(payment.amount for payment in sale.credit_payments))
                        for sale in saved if sale.sale_type == "credit"
                    )
                    sale_type = "credit" if remaining > 0 else "cash"
                    purchase_date = saved[-1].purchase_date
                    expected_pay_date = next(
                        (sale.expected_pay_date for sale in saved if sale.expected_pay_date), None
                    )
                    if saved[-1].customer:
                        customer_name = saved[-1].customer.full_name
                        customer_phone = saved[-1].customer.phone
        except Exception:
            pass

        if total <= 0:
            self._show_error("يرجى إدخال مبالغ العملية أو حفظ البيع أولاً لتصدير الوصل.")
            return

        if not self.cash_walkin_check.isChecked() and self.cash_customer_combo.currentText():
            customer_name = self.cash_customer_combo.currentText().split("·")[0].strip()

        receipts_dir = data_dir() / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        default_file = str(receipts_dir / f"recu_{date.today().isoformat()}.pdf")

        path, _filter = QFileDialog.getSaveFileName(
            self,
            ar.CASH_RECEIPT_TITLE,
            default_file,
            "PDF Files (*.pdf)",
        )
        if not path:
            return

        try:
            out = generate_cash_sale_receipt_pdf(
                path=path,
                customer_name=customer_name,
                customer_phone=customer_phone,
                product=product_name,
                sale_type=sale_type,
                total=total,
                amount_paid=amount_paid,
                remaining_balance=remaining,
                purchase_date=purchase_date,
                expected_pay_date=expected_pay_date,
                issued_by=self.current_user.username,
                items=items,
                receipt_number=receipt_number,
                templates=templates,
            )
            events.notify.emit("success", "تم تصدير الوصل بنجاح 🧾", f"تم حفظ الوصل في: {out.name}", 4000)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))
        except Exception as error:
            self._show_error(f"تعذر إنشاء ملف الوصل: {error}")

    # =========================================================================
    # 2. INSTALLMENT MODULE (Facilité Contracts & Payment Schedules)
    # =========================================================================
    def _build_installment_module(self) -> QWidget:
        """Build the dedicated Installment contract interface."""
        page = QWidget(self)
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # Interactive Step Indicator
        stepper_container = QWidget(page)
        stepper_layout = QHBoxLayout(stepper_container)
        stepper_layout.setContentsMargins(0, 0, 0, 4)
        stepper_layout.setSpacing(12)

        self.step1_pill = QLabel("1. اختيار وبيانات الزبون", stepper_container)
        self.step_divider = QLabel("───", stepper_container)
        self.step_divider.setStyleSheet("color: #2E4060; font-weight: 700;")
        self.step2_pill = QLabel("2. تفاصيل البيع والجدول المالي", stepper_container)

        stepper_layout.addWidget(self.step1_pill)
        stepper_layout.addWidget(self.step_divider)
        stepper_layout.addWidget(self.step2_pill)
        stepper_layout.addStretch(1)
        root.addWidget(stepper_container)

        self.steps = QStackedWidget(page)
        root.addWidget(self.steps, 1)
        self.steps.addWidget(self._build_customer_step())
        self.steps.addWidget(self._build_sale_step())
        self.steps.currentChanged.connect(self._update_stepper_style)
        self._update_stepper_style(0)
        return page

    def _update_stepper_style(self, active_index: int) -> None:
        """Update stepper active/inactive styling."""
        active_style = (
            "background-color: #0758CD; color: #FFFFFF; border: 1px solid #3B92D9; "
            "border-radius: 8px; padding: 6px 14px; font-weight: 700; font-size: 13px;"
        )
        inactive_style = (
            "background-color: #0E141D; color: #8A94A6; border: 1px solid #24334C; "
            "border-radius: 8px; padding: 6px 14px; font-weight: 600; font-size: 13px;"
        )
        completed_style = (
            "background-color: #12284C; color: #9DBEFF; border: 1px solid #285494; "
            "border-radius: 8px; padding: 6px 14px; font-weight: 700; font-size: 13px;"
        )
        if active_index == 0:
            self.step1_pill.setStyleSheet(active_style)
            self.step2_pill.setStyleSheet(inactive_style)
        else:
            self.step1_pill.setStyleSheet(completed_style)
            self.step2_pill.setStyleSheet(active_style)

    def _build_customer_step(self) -> QWidget:
        """Build customer lookup and new-customer form."""
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(14)
        heading = QLabel(ar.STEP_CUSTOMER, page)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)

        form_card = QFrame(page)
        form_card.setObjectName("summaryTile")
        form_card.setStyleSheet(
            "QFrame#summaryTile { background-color: #0E141D; border: 1px solid #1F2A3D; border-radius: 12px; padding: 18px; }"
        )
        form_layout = QVBoxLayout(form_card)
        form_layout.setSpacing(12)

        search_row = QHBoxLayout()
        search_icon = QLabel("🔍", form_card)
        search_icon.setStyleSheet("color: #3B92D9; font-size: 14px;")
        self.customer_search = QLineEdit(form_card)
        self.customer_search.setPlaceholderText(f"{ar.CUST_SEARCH}...")
        self.add_customer_button = QPushButton(f"➕  {ar.ADD_CUSTOMER}", form_card)
        self.add_customer_button.setProperty("variant", "secondary")
        self.add_customer_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_customer_button.clicked.connect(self.add_new_customer)
        search_row.addWidget(search_icon)
        search_row.addWidget(self.customer_search, 1)
        search_row.addWidget(self.add_customer_button)
        form_layout.addLayout(search_row)

        self.customer_combo = QComboBox(form_card)
        self.customer_combo.currentIndexChanged.connect(
            lambda _index: self._sync_customer("customer_combo", "cash_customer_combo")
        )
        form_layout.addWidget(self.customer_combo)

        layout.addWidget(form_card)
        layout.addStretch(1)

        self.next_button = QPushButton(f"{ar.NEXT}  ⬅️", page)
        self.next_button.setProperty("variant", "primary")
        self.next_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.next_button.clicked.connect(self._continue_to_sale)
        layout.addWidget(self.next_button, alignment=Qt.AlignmentFlag.AlignLeft)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._refresh_customers)
        self.customer_search.textChanged.connect(lambda _text: self._search_timer.start())
        return page

    def _build_sale_step(self) -> QWidget:
        """Build the installment contract form and live calculation preview."""
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(14)

        content = QHBoxLayout()
        content.setSpacing(18)

        # Left Column: Inputs Form
        left_container = QFrame(page)
        left_container.setObjectName("summaryTile")
        left_container.setStyleSheet(
            "QFrame#summaryTile { background-color: #0E141D; border: 1px solid #1F2A3D; border-radius: 12px; padding: 18px; }"
        )
        left_layout = QVBoxLayout(left_container)
        heading = QLabel(ar.STEP_SALE, left_container)
        heading.setObjectName("sectionTitle")
        left_layout.addWidget(heading)

        sale_form = QFormLayout()
        sale_form.setSpacing(10)
        self.product = QComboBox(page)
        self.product.setEditable(self.current_user.role == "owner")
        self.product.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.sale_type = QComboBox(page)
        for label, value in (
            (ar.SALE_INSTALLMENT, "installment"),
            (ar.SALE_CASH, "cash"),
            (ar.SALE_CREDIT, "credit"),
        ):
            self.sale_type.addItem(label, value)
        self.wholesale_price = self._money_input(page)
        self.cash_price = self._money_input(page)
        if self.current_user.role != "owner":
            # A seller may lower the price up to the owner's limit; more needs the owner's password.
            self.cash_price.setToolTip(ar.DISCOUNT_SELLER_HINT)
        self.rate = QSpinBox(page)
        self.rate.setRange(0, 100)
        self.down_payment = self._money_input(page)
        self.months = QSpinBox(page)
        self.months.setRange(1, calc.MAX_PLAN_MONTHS)
        self.months.setValue(product_service.DEFAULT_PLAN_MONTHS)
        self.interval = QSpinBox(page)
        self.interval.setRange(1, calc.MAX_PLAN_MONTHS)
        self.interval.setSuffix(f" {ar.PURCHASE_MONTHS_SUFFIX}")
        self.interval.setSpecialValueText(ar.PLAN_EVERY_1)
        self.plan_hint = QLabel(page)
        self.plan_hint.setObjectName("sectionHint")
        self.plan_hint.setWordWrap(True)
        self.purchase_date = QDateEdit(QDate.currentDate(), page)
        self.purchase_date.setDisplayFormat("dd/MM/yyyy")
        self.purchase_date.setCalendarPopup(True)
        self.expected_date_enabled = QCheckBox(ar.EXPECTED_PAY_DATE, page)
        self.expected_date = QDateEdit(QDate.currentDate(), page)
        self.expected_date.setDisplayFormat("dd/MM/yyyy")
        self.expected_date.setCalendarPopup(True)
        self.expected_date.setEnabled(False)
        self.expected_date_enabled.toggled.connect(self.expected_date.setEnabled)

        sale_form.addRow(ar.PRODUCT, self.product)
        sale_form.addRow(ar.SALE_TYPE, self.sale_type)
        if self.current_user.role == "owner":
            sale_form.addRow(ar.WHOLESALE_PRICE, self.wholesale_price)
        sale_form.addRow(ar.CASH_PRICE, self.cash_price)
        sale_form.addRow(ar.RATE, self.rate)
        sale_form.addRow(ar.DOWN_PAYMENT, self.down_payment)
        sale_form.addRow(ar.MONTHS, self.months)
        sale_form.addRow(ar.PLAN_INTERVAL, self.interval)
        sale_form.addRow("", self.plan_hint)
        sale_form.addRow(ar.PURCHASE_DATE, self.purchase_date)
        sale_form.addRow("", self.expected_date_enabled)
        sale_form.addRow(ar.EXPECTED_PAY_DATE, self.expected_date)
        self._sale_field_labels = {
            "rate": sale_form.labelForField(self.rate),
            "down_payment": sale_form.labelForField(self.down_payment),
            "months": sale_form.labelForField(self.months),
            "interval": sale_form.labelForField(self.interval),
            "expected_date": sale_form.labelForField(self.expected_date),
        }
        left_layout.addLayout(sale_form)
        details_title = QLabel(ar.SALE_DETAILS_SECTION, left_container)
        details_title.setObjectName("sectionTitle")
        left_layout.addWidget(details_title)
        self.sale_details = PhoneDetailsSection(parent=left_container)
        self.sale_details.unit_selected.connect(self._on_sale_unit_selected)
        self.sale_details.quantity_changed.connect(self._on_sale_quantity_changed)
        self.sale_details.scanned.connect(
            lambda name, unit_id: self._show_scanned(self.product, self.sale_details, name, unit_id)
        )
        left_layout.addWidget(self.sale_details)
        left_layout.addStretch(1)
        content.addWidget(_scrolling(left_container), 2)

        # Right Column: Financial Preview Card. It scrolls as a whole, so the
        # full payment schedule is readable instead of a squeezed table.
        preview = QFrame(page)
        preview.setObjectName("salePreview")
        preview.setStyleSheet(
            "QFrame#salePreview { background-color: #0A0E17; border: 1px solid #1F2A3D; border-radius: 12px; }"
            "QFrame#previewTile { background-color: #0E141D; border: 1px solid #1F2A3D; border-radius: 10px; }"
        )
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(18, 16, 18, 16)
        preview_layout.setSpacing(12)

        # Several products for the same client: kept here until the sale is saved.
        self.sale_basket = SaleBasket(preview)
        self.sale_basket.changed.connect(self._sale_basket_changed)
        preview_layout.addWidget(self.sale_basket)

        summary_title = QLabel(f"📊  {ar.SALE_PREVIEW_TITLE}", preview)
        summary_title.setObjectName("sectionTitle")
        preview_layout.addWidget(summary_title)

        preview_grid = QGridLayout()
        preview_grid.setSpacing(10)
        _tile, _caption, self.total_value = self._preview_tile(preview, ar.TOTAL_PRICE, None)
        preview_grid.addWidget(_tile, 0, 0)
        _tile, _caption, self.financed_value = self._preview_tile(preview, ar.FINANCED_AMOUNT, "#3B92D9")
        preview_grid.addWidget(_tile, 0, 1)
        _tile, self.monthly_caption, self.monthly_value = self._preview_tile(preview, ar.MONTHLY_AMOUNT, "#22C55E")
        preview_grid.addWidget(_tile, 1, 0)
        _tile, _caption, self.profit_value = self._preview_tile(preview, ar.PROFIT, "#9DBEFF")
        preview_grid.addWidget(_tile, 1, 1)
        preview_layout.addLayout(preview_grid)

        end_date_row = QHBoxLayout()
        end_date_row.setContentsMargins(4, 0, 4, 0)
        end_lbl = QLabel(f"📅  {ar.END_DATE}:", preview)
        end_lbl.setStyleSheet("color: #8A94A6; font-size: 13px; font-weight: 600;")
        self.end_date_value = QLabel("—", preview)
        self.end_date_value.setStyleSheet("color: #EBF0FF; font-weight: 700; font-size: 13px;")
        end_date_row.addWidget(end_lbl)
        end_date_row.addWidget(self.end_date_value)
        end_date_row.addStretch(1)
        preview_layout.addLayout(end_date_row)

        self.schedule_label = QLabel(f"📋  {ar.SCHEDULE}", preview)
        self.schedule_label.setStyleSheet("color: #9DBEFF; font-weight: 700; font-size: 13px; margin-top: 4px;")
        preview_layout.addWidget(self.schedule_label)

        self.schedule_table = QTableWidget(0, 3, preview)
        self.schedule_table.setHorizontalHeaderLabels(("#", ar.DUE_DATE, ar.MONTHLY_AMOUNT))
        schedule_header = self.schedule_table.horizontalHeader()
        schedule_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        schedule_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.schedule_table.setColumnWidth(0, 56)
        self.schedule_table.verticalHeader().setVisible(False)
        self.schedule_table.verticalHeader().setDefaultSectionSize(38)
        self.schedule_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.schedule_table.setAlternatingRowColors(True)
        self.schedule_table.setShowGrid(False)
        self.schedule_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.schedule_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        preview_layout.addWidget(self.schedule_table)
        preview_layout.addStretch(1)

        content.addWidget(_scrolling(preview), 3)
        layout.addLayout(content, 1)

        buttons = QHBoxLayout()
        self.back_button = QPushButton(f"➡️  {ar.BACK}", page)
        self.back_button.setProperty("variant", "secondary")
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.clicked.connect(lambda: self.steps.setCurrentIndex(0))
        self.save_button = QPushButton(f"💾  {ar.SAVE_SALE}", page)
        self.save_button.setProperty("variant", "primary")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.clicked.connect(self._save_sale)
        self.add_to_basket_button = QPushButton(f"➕  {ar.BASKET_ADD}", page)
        self.add_to_basket_button.setProperty("variant", "secondary")
        self.add_to_basket_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_to_basket_button.setToolTip(ar.BASKET_ADD_TOOLTIP)
        self.add_to_basket_button.clicked.connect(self._add_sale_to_basket)
        buttons.addWidget(self.back_button)
        buttons.addStretch(1)
        buttons.addWidget(self.add_to_basket_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

        self.sale_type.currentIndexChanged.connect(self._update_sale_type)
        self.months.valueChanged.connect(self._month_changed)
        self.months.valueChanged.connect(self.interval.setMaximum)
        self.interval.valueChanged.connect(self._update_preview)
        self.product.currentTextChanged.connect(self._product_changed)
        for control in (self.wholesale_price, self.cash_price, self.rate, self.down_payment):
            control.valueChanged.connect(self._update_preview)
        self.months.valueChanged.connect(self._update_preview)
        self.purchase_date.dateChanged.connect(self._update_preview)
        return page

    @staticmethod
    def _preview_tile(parent: QWidget, title: str, color: str | None) -> tuple[QFrame, QLabel, QLabel]:
        """One compact figure (caption over value) of the sale preview."""
        tile = QFrame(parent)
        tile.setObjectName("previewTile")
        tile_layout = QVBoxLayout(tile)
        tile_layout.setContentsMargins(14, 10, 14, 10)
        tile_layout.setSpacing(4)
        caption = QLabel(title, tile)
        caption.setObjectName("summaryTileTitle")
        value = QLabel("—", tile)
        value.setObjectName("summaryTileValue")
        if color:
            value.setStyleSheet(f"color: {color};")
        tile_layout.addWidget(caption)
        tile_layout.addWidget(value)
        return tile, caption, value

    @staticmethod
    def _money_input(parent: QWidget) -> QSpinBox:
        """Create an integer-only DZD editor."""
        control = QSpinBox(parent)
        control.setRange(0, 2_000_000_000)
        control.setGroupSeparatorShown(True)
        return control

    def _refresh_customers(self) -> None:
        """Refresh the existing-customer picker with the current search."""
        selected = self.customer_combo.currentData() if self.customer_combo.count() else None
        query = self.customer_search.text().strip() or None
        with session_scope() as session:
            rows = customers.list_customers(
                session,
                year=date.today().year,
                month=date.today().month,
                today=date.today(),
                search=query,
            )
        self.customer_combo.blockSignals(True)
        self.customer_combo.clear()
        for row in rows:
            phone = row.phone or "—"
            self.customer_combo.addItem(f"{row.full_name}  ·  {phone}", row.id)
        index = self.customer_combo.findData(selected) if selected is not None else -1
        if index >= 0:
            self.customer_combo.setCurrentIndex(index)
        self.customer_combo.blockSignals(False)
        self._refresh_cash_customers()

    def _refresh_products(self, *_args: object) -> None:
        """Load the active catalog products with a phone in stock for both Cash and Installment.

        Runs when the page opens and after any change elsewhere (a product
        edited on the Products page, a phone added to stock...): both forms
        keep the product shown and pick up its new price and plan, unless a
        price was typed by hand.
        """
        self._refreshing_products = True
        try:
            self._reload_products()
        finally:
            self._refreshing_products = False

    def _reload_products(self) -> None:
        current_text = self.product.currentText()
        cash_text = self.cash_product.currentText() if hasattr(self, "cash_product") else ""
        self.product.blockSignals(True)
        self.product.clear()
        if hasattr(self, "cash_product"):
            self.cash_product.blockSignals(True)
            self.cash_product.clear()

        with session_scope() as session:
            available = {product_id: counts[0] for product_id, counts in stock.stock_counts(session).items()}
            catalog = [entry for entry in product_service.list_products(session) if available.get(entry.id, 0) > 0]

        for entry in catalog:
            self.product.addItem(entry.name, entry.id)
            idx = self.product.count() - 1
            self.product.setItemData(idx, entry.wholesale_price, Qt.ItemDataRole.UserRole + 1)
            self.product.setItemData(idx, entry.cash_price, Qt.ItemDataRole.UserRole + 2)
            plan = product_service.plan_of(entry)
            self.product.setItemData(idx, (plan.months, plan.interval, plan.rate), _PLAN_ROLE)

            if hasattr(self, "cash_product"):
                self.cash_product.addItem(entry.name, entry.id)
                self.cash_product.setItemData(idx, entry.wholesale_price, Qt.ItemDataRole.UserRole + 1)
                self.cash_product.setItemData(idx, entry.cash_price, Qt.ItemDataRole.UserRole + 2)

        self._restore_text(self.product, current_text)
        self.product.blockSignals(False)
        if hasattr(self, "cash_product"):
            self._restore_text(self.cash_product, cash_text)
            self.cash_product.blockSignals(False)
            if hasattr(self, "cash_details"):
                self._on_cash_product_changed(self.cash_product.currentText(), keep_typed=True)

        self._product_changed(self.product.currentText(), keep_typed=True)

    @staticmethod
    def _restore_text(combo: QComboBox, text: str) -> None:
        """Show ``text`` again after the list was reloaded."""
        if combo.isEditable():
            combo.setCurrentText(text)
            return
        index = combo.findText(text, Qt.MatchFlag.MatchExactly)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _product_prices(self, name: str) -> tuple[int, int] | None:
        """Return prices only when the product text exactly matches catalog data."""
        index = self.product.findText(name, Qt.MatchFlag.MatchExactly)
        if index < 0:
            return None
        wholesale = self.product.itemData(index, Qt.ItemDataRole.UserRole + 1)
        cash = self.product.itemData(index, Qt.ItemDataRole.UserRole + 2)
        if wholesale is None or cash is None:
            return None
        return int(wholesale), int(cash)

    def _product_plan(self, name: str) -> tuple[int, int, int | None] | None:
        """The catalog product's default (months, interval, rate), if it is in the catalog."""
        index = self.product.findText(name, Qt.MatchFlag.MatchExactly)
        plan = self.product.itemData(index, _PLAN_ROLE) if index >= 0 else None
        return tuple(plan) if plan else None

    def _product_changed(self, name: str, *, keep_typed: bool = False) -> None:
        """Fill prices and the product's default plan when a catalog product is selected.

        ``keep_typed`` (a refresh) leaves a hand-typed price alone and only
        applies the plan when the product's plan itself changed.
        """
        plan = self._product_plan(name)
        if plan is not None:
            months, interval, _rate = plan
            if not keep_typed or self._auto_plan != (months, interval):
                self.months.setValue(months)
                self.interval.setValue(min(interval, months))
                self._month_changed(months)
            self._auto_plan = (months, interval)
            self.plan_hint.setText(ar.PLAN_FROM_PRODUCT.format(plan=plan_description(months, interval)))
        else:
            self._auto_plan = None
            self.plan_hint.setText("")
        self.plan_hint.setVisible(bool(self.plan_hint.text()) and self.sale_type.currentData() == "installment")
        if hasattr(self, "sale_details"):
            self.sale_details.set_product(name)
            prices = self.sale_details.unit_prices()
            quantity = self.sale_details.quantity.value()
        else:
            prices, quantity = self._product_prices(name), 1
        if prices is not None:
            self._apply_prices("sale", (prices[0] * quantity, prices[1] * quantity), keep_typed=keep_typed)
        elif self.product.isEditable() and self.product.currentIndex() < 0:
            self._apply_prices("sale", (0, 0), keep_typed=keep_typed)
        self._update_preview()

    def _on_sale_unit_selected(self, unit: dict | None) -> None:
        """A phone picked from stock is sold at its own price."""
        if unit is None or self._refreshing_products:
            return  # a refresh fills prices itself, keeping hand-typed ones
        self._on_sale_quantity_changed(self.sale_details.quantity.value())

    def _on_sale_quantity_changed(self, quantity: int) -> None:
        """Prices follow the quantity: one phone's price times the number sold."""
        prices = self.sale_details.unit_prices()
        if prices is None:
            return
        self._apply_prices("sale", (prices[0] * quantity, prices[1] * quantity))
        self._update_preview()

    def add_new_customer(self) -> int | None:
        """Open the full new-client form; once saved, select that client for this sale.

        The form opens again with what was typed when saving fails, so nothing
        has to be re-entered.
        """
        with session_scope() as session:
            types = categories.list_client_types(session)
        if not types:
            self._show_error(ar.FORM_CATEGORY_REQUIRED)
            return None
        dialog = CustomerFormDialog([(item, item.customer_count) for item in types], None, self)
        while dialog.exec() == QDialog.DialogCode.Accepted:
            customer_id = self.save_new_customer(dialog.values())
            if customer_id is not None:
                return customer_id
        return None

    def save_new_customer(self, values: dict[str, object]) -> int | None:
        """Create a client from the form values and select them; ``None`` when not saved."""
        if not str(values["full_name"]).strip():
            self._show_error(ar.FORM_NAME_REQUIRED)
            return None
        try:
            customers.normalize_phone(str(values["phone"]))
        except ValueError:
            self._show_error(ar.FORM_PHONE_INVALID)
            return None
        try:
            customer_id = self._insert_customer(values, allow_duplicate_phone=False)
        except DuplicateCustomerPhoneError as error:
            names = "، ".join(customer.full_name for customer in error.matches)
            answer = QMessageBox.question(
                self,
                ar.ADD_CUSTOMER,
                f"{ar.FORM_DUP_PHONE_CONFIRM}\n{names}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return None
            try:
                customer_id = self._insert_customer(values, allow_duplicate_phone=True)
            except (ValueError, RuntimeError, SQLAlchemyError):
                self._show_error(ar.CUST_SAVE_ERROR)
                return None
        except (ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.CUST_SAVE_ERROR)
            return None
        events.data_changed.emit()
        events.customer_changed.emit(customer_id)
        events.notify.emit("success", ar.ADD_CUSTOMER, ar.CUST_SAVED_TOAST, 3500)
        self._select_customer(customer_id)
        return customer_id

    @staticmethod
    def _insert_customer(values: dict[str, object], *, allow_duplicate_phone: bool) -> int:
        with session_scope() as session:
            customer = customers.create_customer(
                session,
                allow_duplicate_phone=allow_duplicate_phone,
                **{**values, "full_name": str(values["full_name"]), "category_id": int(values["category_id"])},
            )
            return customer.id

    def _select_customer(self, customer_id: int) -> None:
        """Show every client again and pick ``customer_id`` in both pickers (cash and installment)."""
        self._search_timer.stop()
        self._cash_search_timer.stop()
        for search in (self.customer_search, self.cash_customer_search):
            search.blockSignals(True)
            search.clear()
            search.blockSignals(False)
        self._refresh_customers()
        for combo in (self.customer_combo, self.cash_customer_combo):
            index = combo.findData(customer_id)
            if index >= 0:
                combo.setCurrentIndex(index)
        # A registered client is not a walk-in: the cash sale goes on their account.
        self.cash_walkin_check.setChecked(False)

    def _sync_customer(self, source_name: str, target_name: str) -> None:
        """The client picked in one module is picked in the other one too."""
        source, target = getattr(self, source_name, None), getattr(self, target_name, None)
        if source is None or target is None:
            return
        customer_id = source.currentData()
        if customer_id is None or target.currentData() == customer_id:
            return
        index = target.findData(customer_id)
        if index >= 0:
            target.blockSignals(True)
            target.setCurrentIndex(index)
            target.blockSignals(False)

    def _continue_to_sale(self) -> None:
        """Validate customer selection before stepping to installment sale form."""
        self._customer_id = self.customer_combo.currentData()
        if self._customer_id is None:
            self._show_error(ar.FORM_SELECT_CUSTOMER)
            return
        try:
            with session_scope() as session:
                overdue_count = customers.count_overdue_installments(
                    session,
                    int(self._customer_id),
                    today=date.today(),
                )
        except (ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.FORM_SAVE_ERROR)
            return
        if overdue_count >= 2:
            answer = QMessageBox.question(
                self,
                ar.SALE_RISK_TITLE,
                ar.SALE_RISK_WARNING.format(months=overdue_count),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.steps.setCurrentIndex(1)
        self._update_preview()

    def _month_changed(self, months: int) -> None:
        with session_scope() as session:
            presets = get_rate_presets(session)
        self._rate_presets = presets
        self.rate.setValue(self._rate_for(months))

    def _rate_for(self, months: int) -> int:
        """The product's own rate for its default duration, else the preset for ``months``."""
        plan = self._product_plan(self.product.currentText()) if hasattr(self, "product") else None
        if plan is not None and plan[2] is not None and plan[0] == months:
            return int(plan[2])
        return self._rate_presets.get(months, 0)

    def _update_sale_type(self, _index: int | None = None) -> None:
        sale_type = self.sale_type.currentData()
        installment = sale_type == "installment"
        credit = sale_type == "credit"
        self.rate.setVisible(installment)
        self.months.setVisible(installment)
        self.interval.setVisible(installment)
        self.plan_hint.setVisible(installment and bool(self.plan_hint.text()))
        self.down_payment.setVisible(installment or credit)
        self.expected_date_enabled.setVisible(credit)
        self.expected_date.setVisible(credit)
        self._set_field_label_visible("rate", installment)
        self._set_field_label_visible("months", installment)
        self._set_field_label_visible("interval", installment)
        self._set_field_label_visible("down_payment", installment or credit)
        self._set_field_label_visible("expected_date", credit)
        if sale_type == "cash":
            self.rate.setValue(0)
            self.down_payment.setValue(0)
        elif credit:
            self.rate.setValue(0)
        elif installment:
            self.rate.setValue(self._rate_for(self.months.value()))
        self._update_preview()

    def _set_field_label_visible(self, name: str, visible: bool) -> None:
        label = self._sale_field_labels.get(name)
        if label is not None:
            label.setVisible(visible)

    def _update_preview(self, *_args: object) -> None:
        """Recalculate installment schedule and update financial preview."""
        sale_type = self.sale_type.currentData()
        cash_price = self.cash_price.value()
        rate = self.rate.value() if sale_type == "installment" else 0
        down_payment = self.down_payment.value() if sale_type != "cash" else 0
        months = self.months.value() if sale_type == "installment" else None
        interval = self._interval() if sale_type == "installment" else 1
        product_prices = self._product_prices(self.product.currentText())
        wholesale = (
            self.wholesale_price.value()
            if self.current_user.role == "owner"
            else product_prices[0] if product_prices is not None else 0
        )
        try:
            result = calc.compute_sale(
                cash_price=cash_price,
                rate=rate,
                down_payment=down_payment,
                months=months,
                wholesale=wholesale,
                sale_type=sale_type,
                purchase_date=self.purchase_date.date().toPython(),
                interval=interval,
            )
        except (TypeError, ValueError):
            self._clear_preview()
            return
        self.monthly_caption.setText(amount_label(interval))
        self.schedule_table.horizontalHeaderItem(2).setText(amount_label(interval))

        self.total_value.setText(f"{result.total:,} {ar.CURRENCY_SUFFIX}")
        self.financed_value.setText(f"{result.financed:,} {ar.CURRENCY_SUFFIX}")
        self.monthly_value.setText(
            f"{result.monthly_list[0]:,} {ar.CURRENCY_SUFFIX}" if result.monthly_list else "—"
        )
        if self.current_user.role == "owner":
            self.profit_value.setText(f"{result.profit:,} {ar.CURRENCY_SUFFIX}")
        else:
            self.profit_value.setText("••••")
        self.end_date_value.setText(
            result.end_date.strftime("%d/%m/%Y") if result.end_date else "—"
        )

        self.schedule_table.setRowCount(0)
        if sale_type == "installment" and months is not None:
            with session_scope() as session:
                due_mode = get_value(session, "due_mode", schedule.DEFAULT_DUE_MODE)
            schedule_rows = schedule.build(
                SimpleNamespace(
                    sale_type=sale_type,
                    months=months,
                    payment_interval=interval,
                    financed=result.financed,
                    monthly_list=result.monthly_list,
                    purchase_date=self.purchase_date.date().toPython(),
                ),
                {"due_mode": due_mode},
            )
            self.schedule_table.setRowCount(len(schedule_rows))
            for row_index, row in enumerate(schedule_rows):
                values = (
                    str(row.installment_index),
                    row.due_date.strftime("%d/%m/%Y"),
                    f"{row.amount_due:,} {ar.CURRENCY_SUFFIX}",
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.schedule_table.setItem(row_index, column, item)
        self._show_schedule(self.schedule_table.rowCount() > 0)

    def _show_schedule(self, visible: bool) -> None:
        """Show the payment schedule at full height, or hide it when there is none."""
        self.schedule_label.setVisible(visible)
        self.schedule_table.setVisible(visible)
        if visible:
            fit_table_to_rows(self.schedule_table)

    def _clear_preview(self) -> None:
        for label in (self.total_value, self.financed_value, self.monthly_value,
                      self.profit_value, self.end_date_value):
            label.setText("—")
        self.schedule_table.setRowCount(0)
        self._show_schedule(False)

    def _sale_line_values(self) -> dict[str, object] | None:
        """The product in the installment form as sale values; None (after a message) when incomplete."""
        if not self.product.currentText().strip():
            self._show_error(ar.BASKET_PRODUCT_REQUIRED)
            return None
        if (
            self.current_user.role != "owner"
            and self._product_prices(self.product.currentText()) is None
        ):
            self._show_error(ar.SELLER_SELECT_CATALOG_PRODUCT)
            return None
        if not self.sale_details.in_stock():
            self._show_error(ar.SALE_OUT_OF_STOCK)
            return None
        details_error = self.sale_details.error_message()
        if details_error is not None:
            self._show_error(details_error)
            return None
        sale_type = self.sale_type.currentData()
        return {
            "product": self.product.currentText().strip(),
            "sale_type": sale_type,
            # A seller's prices come from the catalog (or the phone) in the sales service.
            "wholesale_price": self.wholesale_price.value() if self.current_user.role == "owner" else 0,
            "cash_price": self.cash_price.value(),
            "rate": self.rate.value() if sale_type == "installment" else 0,
            "down_payment": self.down_payment.value() if sale_type != "cash" else 0,
            "months": self.months.value() if sale_type == "installment" else None,
            "payment_interval": self._interval() if sale_type == "installment" else 1,
            **self.sale_details.values(),
            "purchase_date": self.purchase_date.date().toPython(),
            "expected_pay_date": (
                self.expected_date.date().toPython()
                if sale_type == "credit" and self.expected_date_enabled.isChecked()
                else None
            ),
        }

    def _sale_form_has_product(self) -> bool:
        return bool(self.product.currentText().strip())

    def _add_sale_to_basket(self) -> None:
        """Keep the product in the form (its plan too) and empty the form for the next product."""
        values = self._sale_line_values()
        if values is None:
            return
        self._clear_sale_product()
        self.sale_basket.add(values)

    def _sale_basket_changed(self) -> None:
        self.sale_details.set_reserved(self.sale_basket.reserved_units(), self.sale_basket.reserved_counts())
        count = self.sale_basket.count()
        self.save_button.setText(ar.BASKET_SAVE_MANY.format(count=count) if count else f"💾  {ar.SAVE_SALE}")

    def _clear_sale_product(self) -> None:
        """Empty the product part of the form; type, plan and dates stay for the next product.

        No product stays selected (a seller's list too), so saving the basket
        does not add the first catalog product as one more line.
        """
        if self.product.isEditable():
            self.product.setCurrentText("")
        else:
            self.product.setCurrentIndex(-1)
        self.sale_details.clear()
        self.sale_details.set_product(self.product.currentText())
        self._apply_prices("sale", (0, 0))
        self.down_payment.setValue(0)
        self._update_preview()

    def _save_sale(self) -> None:
        """Save the basket and the product in the form: one sale per product, all or none."""
        lines = [line.values for line in self.sale_basket.lines()]
        if self._sale_form_has_product() or not lines:
            current = self._sale_line_values()
            if current is None:
                return
            lines.append(current)
        if len(lines) > 1 and not self._confirm_basket(lines):
            return
        customer_id = self._customer_id
        if customer_id is None:
            self._show_error(ar.FORM_SELECT_CUSTOMER)
            return
        try:
            saved_batch = self._create_batch(lambda _session: int(customer_id), lines)
        except stock.StockError as error:
            self._show_error(self._stock_error_text(error))
            return
        except (PermissionError, ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.FORM_SAVE_ERROR)
            return
        if saved_batch is None:
            return
        _customer, sale_ids = saved_batch
        for sale_id in sale_ids[:-1]:
            events.sale_changed.emit(sale_id)
        self._sale_saved(sale_ids[-1], int(customer_id))

    def _interval(self) -> int:
        """Months between payments, never longer than the plan."""
        return min(self.interval.value(), self.months.value())

    def _sale_saved(self, sale_id: int, customer_id: int) -> None:
        events.data_changed.emit()
        events.sale_changed.emit(sale_id)
        events.notify.emit("success", ar.SAVE_SALE, ar.SALE_SAVED, 4500)
        QMessageBox.information(self, ar.SAVE_SALE, ar.SALE_SAVED)
        self._reset_form()
        self.customer_details_requested.emit(customer_id)

    def _reset_form(self) -> None:
        self.customer_search.clear()
        self._refresh_customers()
        if self.product.isEditable():
            self.product.setCurrentText("")
        elif self.product.count():
            self.product.setCurrentIndex(0)
        self._apply_prices("sale", (0, 0))
        self.rate.setValue(0)
        self.down_payment.setValue(0)
        self.months.setValue(product_service.DEFAULT_PLAN_MONTHS)
        self.interval.setValue(1)
        self._month_changed(product_service.DEFAULT_PLAN_MONTHS)
        self.sale_basket.clear()
        self.sale_details.clear()
        self.sale_details.set_product(self.product.currentText())
        self.purchase_date.setDate(QDate.currentDate())
        self.expected_date_enabled.setChecked(False)
        self.expected_date.setDate(QDate.currentDate())
        self.sale_type.setCurrentIndex(0)
        self.steps.setCurrentIndex(0)
        self._update_preview()

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self, ar.SIDEBAR_ITEMS[2], message)
