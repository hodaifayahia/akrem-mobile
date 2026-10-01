"""Client-type UI pieces: colour dots, the filter-chip bar and the table tag."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from app.i18n import ar
from app.services.categories import ClientType
from app.ui.theme import type_color


def color_dot(key: str | None, size: int = 10) -> QIcon:
    """Return a round swatch icon for a client-type colour key."""
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(type_color(key))
    painter.drawEllipse(QRectF(0.5, 0.5, size - 1, size - 1))
    painter.end()
    return QIcon(pixmap)


class TypeFilterBar(QScrollArea):
    """Horizontal row of exclusive chips: "All" followed by each client type.

    Emits ``type_selected`` with the chosen type id, or ``None`` for "All".
    The row scrolls sideways when there are more types than fit.
    """

    type_selected = Signal(object)
    manage_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedHeight(46)
        self._row_widget = QWidget(self)
        self._row = QHBoxLayout(self._row_widget)
        self._row.setContentsMargins(0, 4, 0, 4)
        self._row.setSpacing(8)
        self._row.addStretch(1)
        self.setWidget(self._row_widget)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._chips: dict[object, QPushButton] = {}
        self._selected: int | None = None

    def set_types(self, types: Sequence[ClientType], selected_id: int | None) -> None:
        """Rebuild the chips with counts and restore the selection."""
        for chip in self._chips.values():
            self._group.removeButton(chip)
            chip.deleteLater()
        self._chips.clear()
        total = sum(item.customer_count for item in types)
        self._add_chip(None, f"{ar.CT_ALL}  {total}", None)
        for item in types:
            chip = self._add_chip(item.id, f"{item.name}  {item.customer_count}", item.color)
            chip.setToolTip(ar.CT_SYSTEM_HINT if item.is_system else item.name)
        if selected_id not in self._chips:
            selected_id = None
        self._selected = selected_id
        self._chips[selected_id].setChecked(True)

    def selected_type_id(self) -> int | None:
        """Return the selected type id, or ``None`` for all types."""
        return self._selected

    def _add_chip(self, type_id: int | None, text: str, color: str | None) -> QPushButton:
        chip = QPushButton(text, self._row_widget)
        chip.setProperty("chip", True)
        chip.setCheckable(True)
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        if color is not None:
            chip.setIcon(color_dot(color))
            chip.setIconSize(QSize(10, 10))
        chip.clicked.connect(lambda _checked=False, value=type_id: self._choose(value))
        chip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        chip.customContextMenuRequested.connect(lambda _pos: self.manage_requested.emit())
        self._group.addButton(chip)
        self._row.insertWidget(self._row.count() - 1, chip)
        self._chips[type_id] = chip
        return chip

    def _choose(self, type_id: int | None) -> None:
        if type_id == self._selected:
            return
        self._selected = type_id
        self.type_selected.emit(type_id)


class TypeTagDelegate(QStyledItemDelegate):
    """Paint a client type as a coloured pill aligned to the reading start.

    The model must return the type name for ``DisplayRole`` and the colour
    key for ``color_role``.
    """

    def __init__(self, color_role: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.color_role = color_role

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        base = QStyleOptionViewItem(option)
        self.initStyleOption(base, index)
        text = base.text
        base.text = ""
        style = base.widget.style() if base.widget is not None else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, base, painter, base.widget)
        if not text:
            return
        color = type_color(index.data(self.color_role))
        metrics = option.fontMetrics
        width = min(option.rect.width() - 16, metrics.horizontalAdvance(text) + 34)
        height = 22
        rtl = option.direction == Qt.LayoutDirection.RightToLeft
        x = option.rect.right() - 8 - width if rtl else option.rect.left() + 8
        y = option.rect.center().y() - height / 2
        pill = QRectF(x, y, width, height)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        fill = QColor(color)
        fill.setAlpha(40)
        painter.setBrush(fill)
        painter.drawRoundedRect(pill, height / 2, height / 2)
        painter.setBrush(color)
        dot_x = pill.right() - 13 if rtl else pill.left() + 7
        painter.drawEllipse(QRectF(dot_x, pill.center().y() - 3, 6, 6))
        painter.setPen(color.lighter(130))
        text_rect = pill.adjusted(6, 0, -20, 0) if rtl else pill.adjusted(20, 0, -6, 0)
        elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(text_rect.width()))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, elided)
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        hint = super().sizeHint(option, index)
        return QSize(hint.width() + 30, max(hint.height(), 30))
