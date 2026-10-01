"""Today's register: money collected, sales made and (for the owner) profit today.

Every figure comes from the dashboard service; nothing here is illustrative.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.i18n import ar
from app.ui import icons


class DailyRegisterCard(QFrame):
    """Compact card with today's three headline numbers."""

    def __init__(self, is_owner: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.is_owner = is_owner
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        icon = QLabel(self)
        icon.setPixmap(icons.pixmap("cash", "primary-glow", 20))
        title = QLabel(ar.DASH_TODAY_TITLE, self)
        title.setObjectName("sectionTitle")
        header.addWidget(icon)
        header.addWidget(title, 1)
        layout.addLayout(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(2)
        self.collected_tile = self._tile(grid, 0, ar.DASH_TODAY_COLLECTED, "paid")
        self.count_tile = self._tile(grid, 1, ar.DASH_TODAY_SALES, "text")
        if is_owner:
            self.profit_tile = self._tile(grid, 2, ar.DASH_TODAY_PROFIT, "highlight")
        layout.addLayout(grid)
        self.update_metrics(collected=0, sales_count=0, profit=0)

    def _tile(self, grid: QGridLayout, column: int, caption: str, tone: str) -> tuple[QLabel, QLabel]:
        caption_label = QLabel(caption, self)
        caption_label.setObjectName("kpiCaption")
        value_label = QLabel(self)
        value_label.setObjectName("kpiValue")
        value_label.setProperty("tone", tone)
        value_label.setStyleSheet("font-size: 22px;")
        grid.addWidget(caption_label, 0, column, Qt.AlignmentFlag.AlignLeading)
        grid.addWidget(value_label, 1, column, Qt.AlignmentFlag.AlignLeading)
        grid.setColumnStretch(column, 1)
        return caption_label, value_label

    def update_metrics(self, collected: int, sales_count: int, profit: int = 0) -> None:
        """Show today's totals."""
        self.collected_tile[1].setText(f"{collected:,} {ar.CURRENCY_SUFFIX}")
        self.count_tile[1].setText(ar.DASH_OPERATIONS_COUNT.format(count=sales_count))
        if self.is_owner and hasattr(self, "profit_tile"):
            self.profit_tile[1].setText(f"{profit:,} {ar.CURRENCY_SUFFIX}")
