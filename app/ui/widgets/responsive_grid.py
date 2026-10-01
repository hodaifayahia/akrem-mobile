"""A grid that re-flows its cards into as many columns as the width allows."""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QGridLayout, QSizePolicy, QWidget


class ResponsiveGrid(QWidget):
    """Lay out child cards in 1..``max_columns`` columns of at least ``min_width``.

    Four KPI cards sit in one row on a wide window, two rows of two on a
    laptop screen, and one column when the window is very narrow. Column
    order follows the layout direction, so it mirrors in Arabic.
    """

    def __init__(
        self,
        *,
        min_width: int = 220,
        max_columns: int = 4,
        spacing: int = 14,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.min_width = min_width
        self.max_columns = max_columns
        self._items: list[QWidget] = []
        self._columns = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(spacing)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def add(self, widget: QWidget) -> None:
        """Append a card."""
        widget.setParent(self)
        self._items.append(widget)
        self._reflow(force=True)

    def visible_items(self) -> list[QWidget]:
        """Return cards that are not explicitly hidden."""
        return [item for item in self._items if not item.isHidden()]

    def columns(self) -> int:
        """Return the current column count."""
        return self._columns

    def _target_columns(self, width: int) -> int:
        count = max(1, len(self.visible_items()))
        spacing = self._grid.spacing()
        fit = max(1, (width + spacing) // (self.min_width + spacing))
        return max(1, min(self.max_columns, count, fit))

    def _reflow(self, *, force: bool = False) -> None:
        columns = self._target_columns(self.width() or self.min_width * self.max_columns)
        if columns == self._columns and not force:
            return
        self._columns = columns
        for item in self._items:
            self._grid.removeWidget(item)
        for column in range(self.max_columns):
            self._grid.setColumnStretch(column, 0)
        for index, item in enumerate(self.visible_items()):
            self._grid.addWidget(item, index // columns, index % columns)
        for column in range(columns):
            self._grid.setColumnStretch(column, 1)

    def refresh(self) -> None:
        """Re-flow after cards were shown or hidden."""
        self._reflow(force=True)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt callback name
        super().resizeEvent(event)
        self._reflow()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API name
        return QSize(self.min_width, super().minimumSizeHint().height())
