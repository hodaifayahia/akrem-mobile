"""The products a client takes in one visit, before they are saved.

The sale form fills one product at a time; "add to basket" keeps that
product (with its own type, price, plan and phone) here and empties the form
for the next one. Saving the sale saves every line, plus the product still
in the form, as separate sales of the same client that share one row in the
client list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.i18n import ar
from app.i18n.stock_text import ltr_text
from app.services.products import name_key
from app.ui import icons
from app.ui.screen_fit import fit_table_to_rows


@dataclass(frozen=True)
class BasketLine:
    """One product waiting to be saved: ``values`` are ``create_sale_for_user`` keyword arguments."""

    values: dict[str, Any] = field(default_factory=dict)

    @property
    def product(self) -> str:
        return str(self.values.get("product", ""))

    @property
    def quantity(self) -> int:
        return int(self.values.get("quantity") or 1)

    @property
    def price(self) -> int:
        return int(self.values.get("cash_price") or 0)

    @property
    def stock_item_id(self) -> int | None:
        return self.values.get("stock_item_id")


def sale_type_label(kind: str) -> str:
    """كاش / بالتقسيط / كريدي."""
    return {"cash": ar.SALE_CASH, "installment": ar.SALE_INSTALLMENT, "credit": ar.SALE_CREDIT}.get(kind, kind)


class SaleBasket(QFrame):
    """A small table of the products already added, with a remove button per line."""

    changed = Signal()

    COLUMNS = ("product", "type", "quantity", "price", "remove")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("saleBasket")
        self._lines: list[BasketLine] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        header = QHBoxLayout()
        self.title = QLabel(f"🛒  {ar.BASKET_TITLE}", self)
        self.title.setObjectName("sectionTitle")
        header.addWidget(self.title, 1)
        self.total_label = QLabel(self)
        self.total_label.setProperty("pill", "primary-glow")
        header.addWidget(self.total_label)
        layout.addLayout(header)
        self.hint = QLabel(ar.BASKET_EMPTY_HINT, self)
        self.hint.setObjectName("sectionHint")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.table = QTableWidget(0, len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(
            [ar.PRODUCT, ar.SALE_TYPE, ar.SALE_QUANTITY, ar.BASKET_PRICE, ""]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        table_header = self.table.horizontalHeader()
        table_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        self._render()

    # ------------------------------------------------------------------ lines
    def add(self, values: dict[str, Any]) -> None:
        """Keep one product's sale values."""
        self._lines.append(BasketLine(dict(values)))
        self._render()
        self.changed.emit()

    def remove(self, index: int) -> None:
        if 0 <= index < len(self._lines):
            del self._lines[index]
            self._render()
            self.changed.emit()

    def clear(self) -> None:
        self._lines = []
        self._render()
        self.changed.emit()

    def lines(self) -> list[BasketLine]:
        return list(self._lines)

    def count(self) -> int:
        return len(self._lines)

    def total(self) -> int:
        """Selling price of every line (before any installment rate)."""
        return sum(line.price for line in self._lines)

    def reserved_units(self) -> set[int]:
        """Phones already picked by a line: the form must not offer them again."""
        return {line.stock_item_id for line in self._lines if line.stock_item_id is not None}

    def reserved_counts(self) -> dict[str, int]:
        """How many phones of each product (by name key) the lines without a picked phone will take."""
        counts: dict[str, int] = {}
        for line in self._lines:
            taken = line.quantity - (1 if line.stock_item_id is not None else 0)
            if taken > 0:
                key = name_key(line.product)
                counts[key] = counts.get(key, 0) + taken
        return counts

    # ------------------------------------------------------------------ view
    def _render(self) -> None:
        self.table.setRowCount(len(self._lines))
        for row, line in enumerate(self._lines):
            texts = (
                ltr_text(line.product),
                sale_type_label(str(line.values.get("sale_type", ""))),
                str(line.quantity),
                f"{line.price:,} {ar.CURRENCY_SUFFIX}",
            )
            for column, text in enumerate(texts):
                item = QTableWidgetItem(text)
                if column:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, item)
            button = QPushButton(self.table)
            button.setIcon(icons.icon("trash", "failed", 16))
            button.setToolTip(ar.BASKET_REMOVE)
            button.setProperty("variant", "ghost")
            button.setProperty("compact", True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, index=row: self.remove(index))
            self.table.setCellWidget(row, len(self.COLUMNS) - 1, button)
        has_lines = bool(self._lines)
        self.table.setVisible(has_lines)
        self.hint.setVisible(not has_lines)
        self.total_label.setVisible(has_lines)
        self.total_label.setText(
            ar.BASKET_TOTAL.format(count=len(self._lines), total=f"{self.total():,} {ar.CURRENCY_SUFFIX}")
        )
        if has_lines:
            fit_table_to_rows(self.table)
