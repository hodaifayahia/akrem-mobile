"""Two-module sales page: Dedicated Cash Checkout vs. Installment Contracts."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QDate, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
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
from app.services import calc, customers, sales
from app.services.categories import list_categories
from app.services.customers import DuplicateCustomerPhoneError
from app.services.pdf_forms import generate_cash_sale_receipt_pdf
from app.services import products as product_service
from app.services import schedule
from app.services.settings import get_rate_presets, get_value
from app.ui.events import events


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
        self._creating_customer = False
        self._customer_id: int | None = None
        self._rate_presets: dict[int, int] = {}
        self._cash_status = "paid"  # 'paid' or 'credit'
        self._last_saved_cash_sale_id: int | None = None

        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._build_ui()
        self._refresh_categories()
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
        search_row.addWidget(search_lbl)
        search_row.addWidget(self.cash_customer_search, 1)
        cust_sel_layout.addLayout(search_row)

        self.cash_customer_combo = QComboBox(self.cash_cust_select_container)
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
        form_layout.addStretch(1)

        layout.addWidget(form_card, 3)

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

        summary_layout.addStretch(1)

        # Action Buttons
        btn_col = QVBoxLayout()
        btn_col.setSpacing(8)

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

        self.c_tile_total[1].setText(f"{total:,} {ar.CURRENCY_SUFFIX}")
        self.c_tile_collected[1].setText(f"{amount_paid:,} {ar.CURRENCY_SUFFIX}")
        self.c_tile_debt[1].setText(f"{remaining_balance:,} {ar.CURRENCY_SUFFIX}")
        self.c_tile_debt[1].setStyleSheet(
            "color: #F59E0B; font-size: 16px; font-weight: 700; font-family: 'Rajdhani', sans-serif;"
            if remaining_balance > 0
            else "color: #64748B; font-size: 16px; font-weight: 700; font-family: 'Rajdhani', sans-serif;"
        )
        self.c_tile_profit[1].setText(f"{profit:,} {ar.CURRENCY_SUFFIX}")

    def _on_cash_product_changed(self, name: str) -> None:
        prices = self._product_prices(name)
        if prices is not None:
            self.cash_wholesale.setValue(prices[0])
            self.cash_total.setValue(prices[1])
        elif self.cash_product.isEditable() and self.cash_product.currentIndex() < 0:
            self.cash_wholesale.setValue(0)
            self.cash_total.setValue(0)
        self._update_cash_calculations()

    def _refresh_cash_customers(self) -> None:
        """Refresh customer list for cash view search."""
        query = self.cash_customer_search.text().strip() or None
        with session_scope() as session:
            rows = customers.list_customers(
                session,
                year=date.today().year,
                month=date.today().month,
                today=date.today(),
                search=query,
            )
        self.cash_customer_combo.clear()
        for row in rows:
            phone = row.phone or "—"
            self.cash_customer_combo.addItem(f"{row.full_name}  ·  {phone}", row.id)

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

    def _save_cash_sale(self) -> None:
        """Persist a spot cash or partial-payment credit sale."""
        product_name = self.cash_product.currentText().strip()
        if not product_name:
            self._show_error("يرجى إدخال أو تحديد اسم المنتج.")
            return

        total = self.cash_total.value()
        if total <= 0:
            self._show_error("يرجى إدخال مبلغ صحيح للبيع.")
            return

        wholesale = (
            self.cash_wholesale.value()
            if self.current_user.role == "owner"
            else (self._product_prices(product_name) or (0, 0))[0]
        )

        sale_type = "credit" if self._cash_status == "credit" else "cash"
        amount_paid = total if sale_type == "cash" else self.cash_amount_paid.value()
        expected_date = (
            self.cash_expected_date.date().toPython()
            if sale_type == "credit"
            else None
        )

        try:
            with session_scope() as session:
                if sale_type == "cash" and self.cash_walkin_check.isChecked():
                    customer = self._get_or_create_walkin_customer(session)
                    customer_id = customer.id
                else:
                    customer_id = self.cash_customer_combo.currentData()
                    if customer_id is None:
                        self._show_error(ar.CASH_CREDIT_CUSTOMER_REQUIRED)
                        return

                saved_sale = sales.create_sale_for_user(
                    session,
                    int(self.current_user.id),
                    customer_id=customer_id,
                    product=product_name,
                    sale_type=sale_type,
                    wholesale_price=wholesale,
                    cash_price=total,
                    rate=0,
                    down_payment=amount_paid if sale_type == "credit" else 0,
                    months=None,
                    purchase_date=self.cash_date.date().toPython(),
                    expected_pay_date=expected_date,
                )
                self._last_saved_cash_sale_id = saved_sale.id
                sale_id = saved_sale.id

        except Exception as error:
            self._show_error(f"{ar.FORM_SAVE_ERROR}\n{error}")
            return

        events.data_changed.emit()
        events.sale_changed.emit(sale_id)
        events.customer_changed.emit(customer_id)

        if sale_type == "cash":
            events.notify.emit("success", "تم تسجيل البيع بنجاح 🎉", f"تم قبض {total:,} دج نقداً في الخزينة", 4500)
            QMessageBox.information(self, "تسجيل بيع نقدي", f"تم تسجيل البيع وقبض {total:,} دج بنجاح.")
        else:
            remaining = total - amount_paid
            events.notify.emit("warning", "تم تسجيل بيع بالدين (كريدي) ⚠️", f"تم تقييد دين بقيمة {remaining:,} دج في كشف حساب الزبون", 5000)
            QMessageBox.information(
                self,
                "تسجيل بيع بالدين",
                f"تم قبض تسبيق بقيمة {amount_paid:,} دج، وتقييد دين متبقي قدره {remaining:,} دج على حساب الزبون.",
            )

        # Offer receipt print
        answer = QMessageBox.question(
            self,
            "طباعة الوصل",
            "هل ترغب في تصدير وطباعة وصل المعاملة الآن؟",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._export_cash_receipt()

        # Reset cash form
        self.cash_total.setValue(0)
        self.cash_amount_paid.setValue(0)
        self.cash_wholesale.setValue(0)
        self._update_cash_calculations()

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

        if total <= 0 and self._last_saved_cash_sale_id:
            try:
                from app.db.models import Sale
                with session_scope() as session:
                    sale = session.get(Sale, self._last_saved_cash_sale_id)
                    if sale:
                        product_name = sale.product
                        total = sale.cash_price
                        sale_type = sale.sale_type
                        amount_paid = sale.down_payment if sale_type == "credit" else total
                        remaining = sale.remaining
                        purchase_date = sale.purchase_date
                        expected_pay_date = sale.expected_pay_date
                        if sale.customer:
                            customer_name = sale.customer.full_name
                            customer_phone = sale.customer.phone
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

        self.new_customer_check = QCheckBox(f"➕  {ar.NEW_CUSTOMER_CHECK}", form_card)
        self.new_customer_check.setStyleSheet("color: #9DBEFF; font-weight: 600; font-size: 13px;")
        self.new_customer_check.toggled.connect(self._toggle_new_customer)
        form_layout.addWidget(self.new_customer_check)

        search_row = QHBoxLayout()
        search_icon = QLabel("🔍", form_card)
        search_icon.setStyleSheet("color: #3B92D9; font-size: 14px;")
        self.customer_search = QLineEdit(form_card)
        self.customer_search.setPlaceholderText(f"{ar.CUST_SEARCH}...")
        search_row.addWidget(search_icon)
        search_row.addWidget(self.customer_search, 1)
        form_layout.addLayout(search_row)

        self.customer_combo = QComboBox(form_card)
        form_layout.addWidget(self.customer_combo)

        # Inline New Customer Form Panel
        self.new_customer_panel = QFrame(page)
        self.new_customer_panel.setObjectName("summaryTile")
        self.new_customer_panel.setStyleSheet(
            "QFrame#summaryTile { background-color: #0B1019; border: 1px solid #23354E; border-radius: 10px; padding: 14px; margin-top: 8px; }"
        )
        customer_form = QFormLayout(self.new_customer_panel)
        panel_title = QLabel("➕  بيانات الزبون الجديد", self.new_customer_panel)
        panel_title.setStyleSheet("color: #9DBEFF; font-weight: 700; font-size: 13px;")
        customer_form.addRow(panel_title)
        self.new_customer_name = QLineEdit(self.new_customer_panel)
        self.new_customer_phone = QLineEdit(self.new_customer_panel)
        self.new_customer_phone.setMaxLength(10)
        self.new_customer_phone.setPlaceholderText("05xxxxxxxx")
        self.new_customer_category = QComboBox(self.new_customer_panel)
        customer_form.addRow(ar.CUSTOMER_NAME, self.new_customer_name)
        customer_form.addRow(ar.PHONE, self.new_customer_phone)
        customer_form.addRow(ar.CATEGORY, self.new_customer_category)
        self.new_customer_panel.setVisible(False)
        form_layout.addWidget(self.new_customer_panel)

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
            self.cash_price.setEnabled(False)
        self.rate = QSpinBox(page)
        self.rate.setRange(0, 50)
        self.down_payment = self._money_input(page)
        self.months = QSpinBox(page)
        self.months.setRange(2, 24)
        self.months.setValue(6)
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
        sale_form.addRow(ar.PURCHASE_DATE, self.purchase_date)
        sale_form.addRow("", self.expected_date_enabled)
        sale_form.addRow(ar.EXPECTED_PAY_DATE, self.expected_date)
        self._sale_field_labels = {
            "rate": sale_form.labelForField(self.rate),
            "down_payment": sale_form.labelForField(self.down_payment),
            "months": sale_form.labelForField(self.months),
            "expected_date": sale_form.labelForField(self.expected_date),
        }
        left_layout.addLayout(sale_form)
        left_layout.addStretch(1)
        content.addWidget(left_container, 2)

        # Right Column: Financial Preview Card
        preview = QFrame(page)
        preview.setObjectName("summaryTile")
        preview.setStyleSheet(
            "QFrame#summaryTile { background-color: #0A0E17; border: 1px solid #1F2A3D; border-radius: 12px; padding: 18px; }"
        )
        preview_layout = QVBoxLayout(preview)
        preview_layout.setSpacing(12)

        summary_title = QLabel("📊  الملخص المالي وجدول الأقساط", preview)
        summary_title.setObjectName("sectionTitle")
        preview_layout.addWidget(summary_title)

        preview_grid = QGridLayout()
        preview_grid.setSpacing(10)

        # Tile 1: Total Price
        tile1 = QFrame(preview)
        tile1.setObjectName("summaryTile")
        tile1_layout = QVBoxLayout(tile1)
        tile1_layout.setContentsMargins(10, 8, 10, 8)
        tile1_lbl = QLabel(ar.TOTAL_PRICE, tile1)
        tile1_lbl.setObjectName("summaryTileTitle")
        self.total_value = QLabel("—", tile1)
        self.total_value.setObjectName("summaryTileValue")
        tile1_layout.addWidget(tile1_lbl)
        tile1_layout.addWidget(self.total_value)
        preview_grid.addWidget(tile1, 0, 0)

        # Tile 2: Financed Amount
        tile2 = QFrame(preview)
        tile2.setObjectName("summaryTile")
        tile2_layout = QVBoxLayout(tile2)
        tile2_layout.setContentsMargins(10, 8, 10, 8)
        tile2_lbl = QLabel(ar.FINANCED_AMOUNT, tile2)
        tile2_lbl.setObjectName("summaryTileTitle")
        self.financed_value = QLabel("—", tile2)
        self.financed_value.setObjectName("summaryTileValue")
        self.financed_value.setStyleSheet("color: #3B92D9;")
        tile2_layout.addWidget(tile2_lbl)
        tile2_layout.addWidget(self.financed_value)
        preview_grid.addWidget(tile2, 0, 1)

        # Tile 3: Monthly Amount
        tile3 = QFrame(preview)
        tile3.setObjectName("summaryTile")
        tile3_layout = QVBoxLayout(tile3)
        tile3_layout.setContentsMargins(10, 8, 10, 8)
        tile3_lbl = QLabel(ar.MONTHLY_AMOUNT, tile3)
        tile3_lbl.setObjectName("summaryTileTitle")
        self.monthly_value = QLabel("—", tile3)
        self.monthly_value.setObjectName("summaryTileValue")
        self.monthly_value.setStyleSheet("color: #22C55E;")
        tile3_layout.addWidget(tile3_lbl)
        tile3_layout.addWidget(self.monthly_value)
        preview_grid.addWidget(tile3, 1, 0)

        # Tile 4: Profit
        tile4 = QFrame(preview)
        tile4.setObjectName("summaryTile")
        tile4_layout = QVBoxLayout(tile4)
        tile4_layout.setContentsMargins(10, 8, 10, 8)
        tile4_lbl = QLabel(ar.PROFIT, tile4)
        tile4_lbl.setObjectName("summaryTileTitle")
        self.profit_value = QLabel("—", tile4)
        self.profit_value.setObjectName("summaryTileValue")
        self.profit_value.setStyleSheet("color: #9DBEFF;")
        tile4_layout.addWidget(tile4_lbl)
        tile4_layout.addWidget(self.profit_value)
        preview_grid.addWidget(tile4, 1, 1)

        preview_layout.addLayout(preview_grid)

        # End date row
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

        sched_lbl = QLabel(f"📋  {ar.SCHEDULE} (Échéancier)", preview)
        sched_lbl.setStyleSheet("color: #9DBEFF; font-weight: 700; font-size: 13px; margin-top: 4px;")
        preview_layout.addWidget(sched_lbl)

        self.schedule_table = QTableWidget(0, 3, preview)
        self.schedule_table.setHorizontalHeaderLabels(("#", ar.DUE_DATE, ar.MONTHLY_AMOUNT))
        self.schedule_table.horizontalHeader().setStretchLastSection(True)
        self.schedule_table.verticalHeader().setVisible(False)
        self.schedule_table.setAlternatingRowColors(True)
        self.schedule_table.setShowGrid(False)
        self.schedule_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        preview_layout.addWidget(self.schedule_table, 1)

        content.addWidget(preview, 3)
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
        buttons.addWidget(self.back_button)
        buttons.addStretch(1)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

        self.sale_type.currentIndexChanged.connect(self._update_sale_type)
        self.months.valueChanged.connect(self._month_changed)
        self.product.currentTextChanged.connect(self._product_changed)
        for control in (self.wholesale_price, self.cash_price, self.rate, self.down_payment):
            control.valueChanged.connect(self._update_preview)
        self.months.valueChanged.connect(self._update_preview)
        self.purchase_date.dateChanged.connect(self._update_preview)
        return page

    @staticmethod
    def _money_input(parent: QWidget) -> QSpinBox:
        """Create an integer-only DZD editor."""
        control = QSpinBox(parent)
        control.setRange(0, 2_000_000_000)
        control.setGroupSeparatorShown(True)
        return control

    def _refresh_categories(self) -> None:
        """Load customer categories through the service layer."""
        with session_scope() as session:
            cats = list_categories(session)
        self.new_customer_category.clear()
        for category, _count in cats:
            self.new_customer_category.addItem(category.name, category.id)

    def _refresh_customers(self) -> None:
        """Refresh the existing-customer picker with the current search."""
        query = self.customer_search.text().strip() or None
        with session_scope() as session:
            rows = customers.list_customers(
                session,
                year=date.today().year,
                month=date.today().month,
                today=date.today(),
                search=query,
            )
        self.customer_combo.clear()
        for row in rows:
            phone = row.phone or "—"
            self.customer_combo.addItem(f"{row.full_name}  ·  {phone}", row.id)
        self._refresh_cash_customers()

    def _refresh_products(self, *_args: object) -> None:
        """Load active price-book entries for both Cash and Installment products."""
        current_text = self.product.currentText()
        self.product.blockSignals(True)
        self.product.clear()
        if hasattr(self, "cash_product"):
            self.cash_product.blockSignals(True)
            self.cash_product.clear()

        with session_scope() as session:
            catalog = product_service.list_products(session)

        for entry in catalog:
            self.product.addItem(entry.name, entry.id)
            idx = self.product.count() - 1
            self.product.setItemData(idx, entry.wholesale_price, Qt.ItemDataRole.UserRole + 1)
            self.product.setItemData(idx, entry.cash_price, Qt.ItemDataRole.UserRole + 2)

            if hasattr(self, "cash_product"):
                self.cash_product.addItem(entry.name, entry.id)
                self.cash_product.setItemData(idx, entry.wholesale_price, Qt.ItemDataRole.UserRole + 1)
                self.cash_product.setItemData(idx, entry.cash_price, Qt.ItemDataRole.UserRole + 2)

        if self.product.isEditable():
            self.product.setCurrentText(current_text)
        else:
            idx = self.product.findText(current_text, Qt.MatchFlag.MatchExactly)
            if idx >= 0:
                self.product.setCurrentIndex(idx)

        self.product.blockSignals(False)
        if hasattr(self, "cash_product"):
            self.cash_product.blockSignals(False)

        if self._product_prices(self.product.currentText()) is not None:
            self._product_changed(self.product.currentText())
        else:
            self._update_preview()

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

    def _product_changed(self, name: str) -> None:
        """Fill prices when a catalog product is selected."""
        prices = self._product_prices(name)
        if prices is not None:
            self.wholesale_price.setValue(prices[0])
            self.cash_price.setValue(prices[1])
        elif self.product.isEditable() and self.product.currentIndex() < 0:
            self.wholesale_price.setValue(0)
            self.cash_price.setValue(0)
        self._update_preview()

    def _toggle_new_customer(self, enabled: bool) -> None:
        self._creating_customer = enabled
        self.new_customer_panel.setVisible(enabled)
        self.customer_search.setVisible(not enabled)
        self.customer_combo.setVisible(not enabled)
        self._update_preview()

    def _continue_to_sale(self) -> None:
        """Validate customer selection before stepping to installment sale form."""
        if self._creating_customer:
            if not self.new_customer_name.text().strip():
                self._show_error(ar.FORM_NAME_REQUIRED)
                return
            if not self.new_customer_phone.text().strip():
                self._show_error(ar.FORM_PHONE_REQUIRED)
                return
            try:
                customers.normalize_phone(self.new_customer_phone.text())
            except ValueError:
                self._show_error(ar.FORM_PHONE_INVALID)
                return
            if self.new_customer_category.currentData() is None:
                self._show_error(ar.FORM_CATEGORY_REQUIRED)
                return
            self._customer_id = None
        else:
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
        self.rate.setValue(presets.get(months, 0))

    def _update_sale_type(self, _index: int | None = None) -> None:
        sale_type = self.sale_type.currentData()
        installment = sale_type == "installment"
        credit = sale_type == "credit"
        self.rate.setVisible(installment)
        self.months.setVisible(installment)
        self.down_payment.setVisible(installment or credit)
        self.expected_date_enabled.setVisible(credit)
        self.expected_date.setVisible(credit)
        self._set_field_label_visible("rate", installment)
        self._set_field_label_visible("months", installment)
        self._set_field_label_visible("down_payment", installment or credit)
        self._set_field_label_visible("expected_date", credit)
        if sale_type == "cash":
            self.rate.setValue(0)
            self.down_payment.setValue(0)
        elif credit:
            self.rate.setValue(0)
        elif installment:
            self.rate.setValue(self._rate_presets.get(self.months.value(), 0))
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
            )
        except (TypeError, ValueError):
            self._clear_preview()
            return

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
                due_mode = get_value(session, "due_mode", "first_of_month")
            schedule_rows = schedule.build(
                SimpleNamespace(
                    sale_type=sale_type,
                    months=months,
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

    def _clear_preview(self) -> None:
        for label in (self.total_value, self.financed_value, self.monthly_value,
                      self.profit_value, self.end_date_value):
            label.setText("—")
        self.schedule_table.setRowCount(0)

    def _save_sale(self) -> None:
        """Save customer and installment sale within one transaction."""
        if (
            self.current_user.role != "owner"
            and self._product_prices(self.product.currentText()) is None
        ):
            self._show_error(ar.SELLER_SELECT_CATALOG_PRODUCT)
            return
        sale_type = self.sale_type.currentData()
        try:
            with session_scope() as session:
                customer_id = self._customer_id
                if self._creating_customer:
                    customer_id = self._save_new_customer(session)
                if customer_id is None:
                    raise ValueError("Customer selection is missing")
                wholesale_price = self._sale_wholesale_price(session)
                saved_sale = sales.create_sale_for_user(
                    session,
                    int(self.current_user.id),
                    customer_id=customer_id,
                    product=self.product.currentText(),
                    sale_type=sale_type,
                    wholesale_price=wholesale_price,
                    cash_price=self.cash_price.value(),
                    rate=self.rate.value() if sale_type == "installment" else 0,
                    down_payment=self.down_payment.value() if sale_type != "cash" else 0,
                    months=self.months.value() if sale_type == "installment" else None,
                    purchase_date=self.purchase_date.date().toPython(),
                    expected_pay_date=(
                        self.expected_date.date().toPython()
                        if sale_type == "credit" and self.expected_date_enabled.isChecked()
                        else None
                    ),
                )
                sale_id = saved_sale.id
        except DuplicateCustomerPhoneError as error:
            names = "، ".join(customer.full_name for customer in error.matches)
            answer = QMessageBox.question(
                self,
                ar.ADD_CUSTOMER,
                f"{ar.FORM_DUP_PHONE_CONFIRM}\n{names}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._save_sale_allowing_duplicate()
            return
        except (PermissionError, ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.FORM_SAVE_ERROR)
            return
        self._sale_saved(sale_id, int(customer_id))

    def _save_new_customer(self, session: Session, *, allow_duplicate_phone: bool = False) -> int:
        customer = customers.create_customer(
            session,
            full_name=self.new_customer_name.text(),
            phone=self.new_customer_phone.text(),
            category_id=int(self.new_customer_category.currentData()),
            allow_duplicate_phone=allow_duplicate_phone,
        )
        return customer.id

    def _save_sale_allowing_duplicate(self) -> None:
        sale_type = self.sale_type.currentData()
        try:
            with session_scope() as session:
                customer_id = self._customer_id
                if self._creating_customer:
                    customer_id = self._save_new_customer(session, allow_duplicate_phone=True)
                if customer_id is None:
                    raise ValueError("Customer selection is missing")
                wholesale_price = self._sale_wholesale_price(session)
                saved_sale = sales.create_sale_for_user(
                    session,
                    int(self.current_user.id),
                    customer_id=customer_id,
                    product=self.product.currentText(),
                    sale_type=sale_type,
                    wholesale_price=wholesale_price,
                    cash_price=self.cash_price.value(),
                    rate=self.rate.value() if sale_type == "installment" else 0,
                    down_payment=self.down_payment.value() if sale_type != "cash" else 0,
                    months=self.months.value() if sale_type == "installment" else None,
                    purchase_date=self.purchase_date.date().toPython(),
                    expected_pay_date=(
                        self.expected_date.date().toPython()
                        if sale_type == "credit" and self.expected_date_enabled.isChecked()
                        else None
                    ),
                )
                sale_id = saved_sale.id
        except (PermissionError, ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.FORM_SAVE_ERROR)
            return
        self._sale_saved(sale_id, int(customer_id))

    def _sale_wholesale_price(self, session: Session) -> int:
        if self.current_user.role == "owner":
            return self.wholesale_price.value()
        product = product_service.find_active_product(session, self.product.currentText())
        if product is None:
            raise ValueError(ar.SELLER_SELECT_CATALOG_PRODUCT)
        return product.wholesale_price

    def _sale_saved(self, sale_id: int, customer_id: int) -> None:
        events.data_changed.emit()
        events.sale_changed.emit(sale_id)
        events.notify.emit("success", ar.SAVE_SALE, ar.SALE_SAVED, 4500)
        QMessageBox.information(self, ar.SAVE_SALE, ar.SALE_SAVED)
        self._reset_form()
        self.customer_details_requested.emit(customer_id)

    def _reset_form(self) -> None:
        self.new_customer_check.setChecked(False)
        self.new_customer_name.clear()
        self.new_customer_phone.clear()
        self.customer_search.clear()
        self._refresh_customers()
        if self.product.isEditable():
            self.product.setCurrentText("")
        elif self.product.count():
            self.product.setCurrentIndex(0)
        self.wholesale_price.setValue(0)
        self.cash_price.setValue(0)
        self.rate.setValue(0)
        self.down_payment.setValue(0)
        self.months.setValue(6)
        self._month_changed(6)
        self.purchase_date.setDate(QDate.currentDate())
        self.expected_date_enabled.setChecked(False)
        self.expected_date.setDate(QDate.currentDate())
        self.sale_type.setCurrentIndex(0)
        self.steps.setCurrentIndex(0)
        self._update_preview()

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self, ar.SIDEBAR_ITEMS[2], message)
