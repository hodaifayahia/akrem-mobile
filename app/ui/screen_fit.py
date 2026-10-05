"""Keep dialogs inside the screen they open on.

Laptops in the shop run 1366x768 or 1920x1080 at 125% (1536x816 usable), so a
dialog sized for a big monitor would run past the taskbar and hide its
buttons. Dialogs that can grow scroll their content and call
:func:`fit_to_screen` before they appear.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize
from PySide6.QtWidgets import QApplication, QTableView, QWidget

#: Share of the screen's free area a dialog may use.
SCREEN_WIDTH_SHARE = 0.95
SCREEN_HEIGHT_SHARE = 0.9


def fit_to_screen(window: QWidget, wanted: QSize) -> None:
    """Resize ``window`` to ``wanted``, capped to the free screen area, and center it.

    The window is centered over its parent window when it has one, otherwise
    on the screen, and always kept fully inside the screen's free area.
    """
    parent = window.parentWidget()
    anchor = parent.window() if parent is not None else None
    screen = (anchor or window).screen() or QApplication.primaryScreen()
    if screen is None:
        return
    area = screen.availableGeometry()
    max_size = QSize(int(area.width() * SCREEN_WIDTH_SHARE), int(area.height() * SCREEN_HEIGHT_SHARE))
    window.setMinimumSize(window.minimumSize().boundedTo(max_size))
    window.resize(wanted.expandedTo(window.minimumSize()).boundedTo(max_size))
    center = anchor.frameGeometry().center() if anchor is not None else area.center()
    frame = QRect(0, 0, window.frameGeometry().width(), window.frameGeometry().height())
    frame.moveCenter(center)
    frame.moveLeft(max(area.left(), min(frame.left(), area.right() - frame.width() + 1)))
    frame.moveTop(max(area.top(), min(frame.top(), area.bottom() - frame.height() + 1)))
    window.move(frame.topLeft())


def fit_table_to_rows(table: QTableView, *, resize_rows: bool = False) -> None:
    """Make ``table`` exactly tall enough for every row; the page around it scrolls."""
    if resize_rows:
        table.resizeRowsToContents()
    header = table.horizontalHeader()
    height = 2 * table.frameWidth() + 2
    if not header.isHidden():
        height += header.sizeHint().height()
    model = table.model()
    rows = model.rowCount() if model is not None else 0
    height += sum(table.rowHeight(row) for row in range(rows))
    table.setFixedHeight(height)
