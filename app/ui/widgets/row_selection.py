"""Row checkboxes for list tables: tick rows, select all, and delete the ticked rows.

The checkboxes are drawn in the table's row header (the vertical header), so
no page has to add a column to its model. Ticks are kept by a stable key per
row (a customer id, a debt id...), so they survive sorting and re-renders.
Actions only ever use ticked rows that are on screen, and when a page reloads
or filters its list it calls :meth:`SelectionBar.refresh`, which unticks the
rows that left it.

When nothing is ticked, the actions use the rows highlighted in the table
(click, Ctrl/Shift+click or the keyboard), so single-row use is unchanged.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable, Hashable, Sequence

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QMouseEvent, QPainter, QPainterPath, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QWidget,
)
from shiboken6 import isValid

from app.i18n import ar
from app.ui import icons
from app.ui.theme import qcolor

HEADER_WIDTH = 38
_BOX = 16

KeyAt = Callable[[int], Hashable | None]


class CheckHeader(QHeaderView):
    """Vertical header drawing one checkbox per row; a click asks to toggle that row."""

    toggled = Signal(int)

    def __init__(self, is_checked: Callable[[int], bool], parent: QTableView) -> None:
        super().__init__(Qt.Orientation.Vertical, parent)
        self._is_checked = is_checked
        self.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.setMinimumWidth(HEADER_WIDTH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(ar.SEL_ROW_TIP)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return QSize(HEADER_WIDTH, super().sizeHint().height())

    def paintSection(self, painter: QPainter, rect: QRect, logical_index: int) -> None:  # noqa: N802 - Qt API
        if not rect.isValid():
            return
        checked = self._is_checked(logical_index)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(rect, qcolor("bg-raised"))
        painter.setPen(qcolor("border"))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        box = QRectF(rect.center().x() - _BOX / 2 + 0.5, rect.center().y() - _BOX / 2 + 0.5, _BOX, _BOX)
        painter.setPen(QPen(qcolor("primary-glow" if checked else "border-strong"), 1))
        painter.setBrush(qcolor("primary" if checked else "surface"))
        painter.drawRoundedRect(box, 4, 4)
        if checked:
            tick = QPen(QColor("#FFFFFF"), 2)
            tick.setCapStyle(Qt.PenCapStyle.RoundCap)
            tick.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(tick)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            path = QPainterPath()
            path.moveTo(box.left() + 3.8, box.center().y() + 0.2)
            path.lineTo(box.left() + 6.8, box.bottom() - 4.2)
            path.lineTo(box.right() - 3.6, box.top() + 4.6)
            painter.drawPath(path)
        painter.restore()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            row = self.logicalIndexAt(event.position().toPoint())
            if row >= 0:
                self.toggled.emit(row)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        event.accept()  # no drag-selection or resizing from the checkbox column

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        self.mousePressEvent(event)  # a fast second click toggles again

    def row_rect(self, row: int) -> QRect:
        """The on-screen rectangle of a row's checkbox cell (used by tests)."""
        return QRect(QPoint(0, self.sectionViewportPosition(row)), QSize(self.width(), self.sectionSize(row)))


class SelectionBar(QWidget):
    """"Select all", the ticked count, "clear" and a delete button for one table.

    ``key_at(row)`` returns a stable key for a table row (the row as shown,
    after sorting), or ``None`` for a row that cannot be ticked.
    """

    changed = Signal()
    delete_requested = Signal()

    def __init__(
        self,
        table: QTableView,
        key_at: KeyAt,
        parent: QWidget | None = None,
        *,
        delete_text: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.table = table
        self._key_at = key_at
        self._checked: set[Hashable] = set()
        self._delete_text = delete_text or ar.SEL_DELETE

        old_header = table.verticalHeader()
        self.header = CheckHeader(self._row_checked, table)
        self.header.setDefaultSectionSize(old_header.defaultSectionSize())
        table.setVerticalHeader(self.header)
        self.header.setVisible(True)
        self.header.toggled.connect(self.toggle_row)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        model = table.model()
        rows_changed = _weak_slot(self, SelectionBar._changed)
        for signal in (model.modelReset, model.layoutChanged, model.rowsInserted, model.rowsRemoved):
            signal.connect(rows_changed)
        table.selectionModel().selectionChanged.connect(_weak_slot(self, SelectionBar._update))
        shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), table)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(self._delete_clicked)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.select_all_box = QCheckBox(ar.SEL_ALL, self)
        self.select_all_box.setTristate(True)
        self.select_all_box.setCursor(Qt.CursorShape.PointingHandCursor)
        self.select_all_box.clicked.connect(self._select_all_clicked)
        self.count_label = QLabel(self)
        self.count_label.setObjectName("sectionHint")
        self.clear_button = QPushButton(ar.SEL_CLEAR, self)
        self.clear_button.setProperty("variant", "ghost")
        self.clear_button.setProperty("compact", True)
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.clicked.connect(self.clear)
        self.delete_button = QPushButton(self._delete_text, self)
        self.delete_button.setProperty("variant", "secondary")
        self.delete_button.setIcon(icons.icon("trash", "failed", 16))
        self.delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_button.clicked.connect(self._delete_clicked)
        for widget in (self.select_all_box, self.count_label, self.clear_button, self.delete_button):
            layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        self._update()

    # ---------------------------------------------------------------- keys
    def visible_keys(self) -> list[Hashable]:
        """Keys of every row in the table, in on-screen order."""
        keys = []
        for row in range(self.table.model().rowCount()):
            key = self._key_at(row)
            if key is not None:
                keys.append(key)
        return keys

    def checked_keys(self) -> list[Hashable]:
        """Keys of the ticked rows, in on-screen order."""
        return [key for key in self.visible_keys() if key in self._checked]

    def highlighted_keys(self) -> list[Hashable]:
        """Keys of the rows highlighted in the table (click / Ctrl / Shift / keyboard)."""
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows:
            current = self.table.currentIndex()
            selected = self.table.selectionModel().isSelected(current) if current.isValid() else False
            rows = [current.row()] if selected else []
        return [key for row in rows if (key := self._key_at(row)) is not None]

    def target_keys(self) -> list[Hashable]:
        """What an action applies to: the ticked rows, or else the highlighted ones."""
        return self.checked_keys() or self.highlighted_keys()

    # ------------------------------------------------------------- changes
    def toggle_row(self, row: int) -> None:
        """Tick or untick one table row."""
        key = self._key_at(row)
        if key is None:
            return
        self.set_checked(key, key not in self._checked)

    def set_checked(self, key: Hashable, checked: bool) -> None:
        """Tick or untick the row with ``key``."""
        if checked:
            self._checked.add(key)
        else:
            self._checked.discard(key)
        self._changed()

    def check_rows(self, rows: Sequence[int]) -> None:
        """Tick several table rows."""
        for row in rows:
            key = self._key_at(row)
            if key is not None:
                self._checked.add(key)
        self._changed()

    def select_all(self, checked: bool = True) -> None:
        """Tick every row in the table (or untick all)."""
        self._checked = set(self.visible_keys()) if checked else set()
        self._changed()

    def clear(self) -> None:
        """Untick every row."""
        self.select_all(False)

    def refresh(self) -> None:
        """Forget ticks on rows that are no longer listed, then redraw.

        Pages call this once their table shows freshly loaded or filtered rows.
        """
        if self._checked:
            self._checked &= set(self.visible_keys())
        self._changed()

    # ---------------------------------------------------------- internals
    def _row_checked(self, row: int) -> bool:
        key = self._key_at(row)
        return key is not None and key in self._checked

    def _select_all_clicked(self, *_args: object) -> None:
        visible = self.visible_keys()
        everything = bool(visible) and self._checked.issuperset(visible)
        self.select_all(not everything)

    def _delete_clicked(self) -> None:
        if self.delete_button.isEnabled():
            self.delete_requested.emit()

    def _changed(self) -> None:
        self.header.viewport().update()
        self._update()

    def _update(self) -> None:
        visible = self.visible_keys()
        checked = sum(1 for key in visible if key in self._checked)
        state = Qt.CheckState.Unchecked
        if checked and checked == len(visible):
            state = Qt.CheckState.Checked
        elif checked:
            state = Qt.CheckState.PartiallyChecked
        self.select_all_box.blockSignals(True)
        self.select_all_box.setCheckState(state)
        self.select_all_box.blockSignals(False)
        self.select_all_box.setEnabled(bool(visible))
        self.count_label.setText(ar.SEL_COUNT.format(count=checked) if checked else "")
        self.count_label.setVisible(bool(checked))
        self.clear_button.setVisible(bool(checked))
        targets = len(self.target_keys())
        self.delete_button.setEnabled(targets > 0)
        self.delete_button.setText(f"{self._delete_text} ({targets})" if targets > 1 else self._delete_text)
        self.changed.emit()


def _weak_slot(bar: SelectionBar, method: Callable[[SelectionBar], None]) -> Callable[..., None]:
    """A slot for the table's signals that calls ``method`` only while the bar and table exist.

    The bar can go away before its table (both belong to the page), and a
    table being torn down still reports its rows as removed.
    """
    ref = weakref.ref(bar)

    def call(*_args: object) -> None:
        target = ref()
        if target is not None and isValid(target) and isValid(target.table):
            method(target)

    return call


# --------------------------------------------------------------- dialogs
def ask_delete(
    parent: QWidget,
    title: str,
    message: str,
    choices: Sequence[tuple[str, str]] | None = None,
    *,
    details: str | None = None,
) -> str | None:
    """Ask before deleting; returns the chosen choice key, or ``None`` when cancelled.

    Without ``choices`` there is a single "Delete" button, whose key is ``"delete"``.
    Cancel is the default button, so Enter or Escape never deletes anything.
    """
    return _ask(
        parent, title, message, choices or (("delete", ar.SEL_DELETE_BUTTON),), details,
        QMessageBox.Icon.Warning, QMessageBox.ButtonRole.DestructiveRole,
    )


def ask_choice(
    parent: QWidget,
    title: str,
    message: str,
    choices: Sequence[tuple[str, str]],
    *,
    details: str | None = None,
) -> str | None:
    """Offer a follow-up action (not a deletion); returns the chosen key or ``None``."""
    return _ask(
        parent, title, message, choices, details, QMessageBox.Icon.Question, QMessageBox.ButtonRole.AcceptRole
    )


def _ask(
    parent: QWidget,
    title: str,
    message: str,
    choices: Sequence[tuple[str, str]],
    details: str | None,
    icon: QMessageBox.Icon,
    role: QMessageBox.ButtonRole,
) -> str | None:
    box = QMessageBox(icon, title, message, parent=parent)
    if details:
        box.setInformativeText(details)
    keys = {box.addButton(text, role): key for key, text in choices}
    cancel = box.addButton(ar.USER_CANCEL, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    return keys.get(box.clickedButton())


def names_preview(names: Sequence[str], limit: int = 6) -> str:
    """A short bulleted list of names, ending with "and N more" when it is long."""
    lines = [f"• {name}" for name in names[:limit]]
    if len(names) > limit:
        lines.append(ar.SEL_MORE.format(count=len(names) - limit))
    return "\n".join(lines)


__all__ = ["CheckHeader", "SelectionBar", "ask_choice", "ask_delete", "names_preview", "HEADER_WIDTH"]
