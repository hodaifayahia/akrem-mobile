"""A customer's history: what they bought, how they pay, and every payment.

The summary strip answers "who is this customer for us?" at a glance:
purchase count, money bought/paid/remaining, and whether they pay cash or
little by little. The purchase tree lists each sale with its type tag and
current status, and under it every transaction made for that product (down
payment, each payment, and what was still owed after it).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.i18n import ar, is_rtl
from app.services.customers import CustomerHistory, PurchaseRecord, TransactionRecord
from app.services.status import Status
from app.i18n.plan_text import plan_summary
from app.i18n.stock_text import battery_text, ltr_text
from app.ui.theme import qcolor

_TYPE_TONES = {"cash": "paid", "installment": "primary-glow", "credit": "pending", "returned": "failed"}
_STATUS_TONES = {Status.PAID: "paid", Status.PENDING: "pending", Status.FAILED: "failed", Status.NONE: "text-muted"}


def type_label(kind: str) -> str:
    """Localized sale type."""
    return {
        "cash": ar.SALE_CASH,
        "installment": ar.SALE_INSTALLMENT,
        "credit": ar.SALE_CREDIT,
        "mixed": ar.SALE_MIXED,
        "returned": ar.SALE_RETURNED,
    }.get(kind, kind)


def status_label(status: Status) -> str:
    """Localized monthly status."""
    return {
        Status.PAID: ar.CUST_STATUS_TIP_PAID,
        Status.PENDING: ar.CUST_STATUS_TIP_PENDING,
        Status.FAILED: ar.CUST_STATUS_TIP_FAILED,
        Status.NONE: ar.HIST_STATUS_NONE,
    }[status]


def method_label(method: str | None) -> str:
    """Localized payment method (CCP and BaridiMob keep their names)."""
    if not method:
        return ""
    return ar.PDF_METHOD_CASH if method.casefold() == "cash" else method


def transaction_label(transaction: TransactionRecord) -> str:
    """What a transaction was: the down payment, a payment for installment N, ..."""
    if transaction.kind == "full_payment":
        return ar.HIST_TX_FULL
    if transaction.kind == "down_payment":
        return ar.HIST_TX_DOWN_PAYMENT
    indexes = transaction.installment_indexes or (
        (transaction.installment_index,) if transaction.installment_index is not None else ()
    )
    numbers = ("، " if is_rtl() else ", ").join(str(number) for number in indexes)
    if transaction.kind == "earlier":
        return ar.HIST_TX_EARLIER.format(n=numbers)
    if len(indexes) > 1:
        return ar.HIST_TX_INSTALLMENTS.format(n=numbers)
    if indexes:
        return ar.HIST_TX_INSTALLMENT.format(n=numbers)
    return ar.HIST_TX_PAYMENT


def _product_text(purchase: PurchaseRecord) -> str:
    """Product name (with the quantity) and the phone's colour, battery and REF when recorded."""
    name = ltr_text(purchase.product)
    if purchase.quantity > 1:
        name += f" ×{purchase.quantity}"
    parts = [name]
    if purchase.color:
        parts.append(ltr_text(purchase.color))
    if purchase.is_new or purchase.battery_health is not None:
        parts.append(battery_text(purchase.is_new, purchase.battery_health))
    if purchase.reference:
        parts.append(ltr_text(purchase.reference))
    return " · ".join(parts)


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
    """Purchases, each with every transaction made for it; double-click a row to open its sale."""

    purchase_activated = Signal(int)

    SALE_ROLE = Qt.ItemDataRole.UserRole

    def __init__(self, *, is_owner: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.is_owner = is_owner
        self._purchases: tuple[PurchaseRecord, ...] = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        purchases_title = QLabel(ar.HIST_PURCHASES_TITLE, self)
        purchases_title.setObjectName("sectionTitle")
        titles.addWidget(purchases_title)
        hint = QLabel(ar.HIST_PURCHASES_HINT, self)
        hint.setObjectName("sectionHint")
        titles.addWidget(hint)
        header.addLayout(titles, 1)
        self.dates_label = QLabel(self)
        self.dates_label.setObjectName("sectionHint")
        header.addWidget(self.dates_label, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(header)

        self.purchases_tree = QTreeWidget(self)
        self.purchases_tree.setObjectName("historyTree")
        self.purchases_tree.setHeaderLabels([
            ar.HIST_COL_DATE, ar.HIST_COL_ITEM, ar.HIST_COL_TYPE, ar.HIST_COL_TOTAL, ar.HIST_COL_PAID,
            ar.HIST_REMAINING, ar.HIST_COL_PLAN, ar.HIST_COL_STATUS,
        ])
        self.purchases_tree.setRootIsDecorated(True)
        self.purchases_tree.setIndentation(18)
        self.purchases_tree.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
        self.purchases_tree.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        tree_header = self.purchases_tree.header()
        tree_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        tree_header.setStretchLastSection(True)
        tree_header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.purchases_tree.itemDoubleClicked.connect(self._activate)
        layout.addWidget(self.purchases_tree, 1)
        self.empty_purchases = QLabel(ar.HIST_PROFILE_NONE, self)
        self.empty_purchases.setObjectName("emptyStateSub")
        self.empty_purchases.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_purchases.setVisible(False)
        layout.addWidget(self.empty_purchases)

    def show_history(self, history: CustomerHistory) -> None:
        """Fill the tree: one row per purchase (newest first), its transactions under it (oldest first)."""
        self._purchases = history.purchases
        self.purchases_tree.clear()
        for purchase in history.purchases:
            parent = self._purchase_item(purchase)
            self.purchases_tree.addTopLevelItem(parent)
            for transaction in purchase.transactions:
                parent.addChild(self._transaction_item(purchase, transaction))
            if not purchase.transactions:
                parent.addChild(self._note_item(purchase.sale_id, ar.HIST_TX_NONE))
            for brought_back in purchase.returns:
                text = ar.HIST_RETURNED.format(
                    date=brought_back.returned_at.strftime("%d/%m/%Y"), refund=money(brought_back.refund)
                )
                if len(purchase.products) > 1:
                    text = f"{ltr_text(brought_back.product)} · {text}"
                if brought_back.reason:
                    text = f"{text} · {brought_back.reason}"
                parent.addChild(self._note_item(purchase.sale_id, text))
        self.purchases_tree.expandAll()
        self.purchases_tree.setVisible(bool(history.purchases))
        self.empty_purchases.setVisible(not history.purchases)
        first = history.first_purchase.strftime("%d/%m/%Y") if history.first_purchase else "—"
        last_payment = history.last_payment.strftime("%d/%m/%Y") if history.last_payment else "—"
        self.dates_label.setText(ar.HIST_DATES.format(first=first, last_payment=last_payment))

    def _purchase_item(self, purchase: PurchaseRecord) -> QTreeWidgetItem:
        plan = (
            plan_summary(purchase.monthly_amount or 0, purchase.payment_interval, purchase.payment_count)
            if purchase.sale_type == "installment" and purchase.months
            else ar.HIST_PLAN_CASH if purchase.sale_type == "cash"
            else ar.HIST_PLAN_CREDIT if purchase.sale_type == "credit"
            else "" if purchase.sale_type == "returned"
            else ar.HIST_PLAN_MIXED
        )
        item = self._item(purchase.sale_id, [
            (purchase.purchase_date.strftime("%d/%m/%Y"), None),
            (_product_text(purchase), None),
            (type_label(purchase.sale_type), _TYPE_TONES.get(purchase.sale_type)),
            (money(purchase.total), None),
            (money(purchase.paid), "paid" if purchase.paid else None),
            (money(purchase.remaining), "failed" if purchase.overdue_installments else None),
            (plan, None),
            (status_label(purchase.status), _STATUS_TONES[purchase.status]),
        ])
        bold = QFont(item.font(0))
        bold.setBold(True)
        for column in range(item.columnCount()):
            item.setFont(column, bold)
        return item

    def _transaction_item(self, purchase: PurchaseRecord, transaction: TransactionRecord) -> QTreeWidgetItem:
        description = transaction_label(transaction)
        # Name the product only when the amount went to some of the basket's products.
        paid_for = set(transaction.product.split(" + ")) if transaction.product else set()
        if len(purchase.products) > 1 and paid_for and paid_for != set(purchase.products):
            description = f"{ltr_text(transaction.product)} · {description}"
        return self._item(purchase.sale_id, [
            (transaction.transaction_date.strftime("%d/%m/%Y"), "text-muted"),
            (description, None),
            (method_label(transaction.method), "text-muted"),
            ("", None),
            (f"+{money(transaction.amount)}", "paid"),
            (money(transaction.remaining_after), "text-muted" if transaction.remaining_after else "paid"),
            (transaction.note or "", "text-muted"),
            ("", None),
        ])

    def _note_item(self, sale_id: int, text: str) -> QTreeWidgetItem:
        return self._item(sale_id, [("", None), (text, "text-muted")] + [("", None)] * 6)

    def _item(self, sale_id: int, cells: list[tuple[str, str | None]]) -> QTreeWidgetItem:
        item = QTreeWidgetItem([text for text, _tone in cells])
        for column, (_text, tone) in enumerate(cells):
            item.setTextAlignment(column, Qt.AlignmentFlag.AlignCenter)
            if tone is not None:
                item.setForeground(column, QColor(qcolor(tone)))
        item.setData(0, self.SALE_ROLE, sale_id)
        return item

    def _activate(self, item: QTreeWidgetItem, _column: int) -> None:
        sale_id = item.data(0, self.SALE_ROLE)
        if sale_id is not None:
            self.purchase_activated.emit(int(sale_id))
