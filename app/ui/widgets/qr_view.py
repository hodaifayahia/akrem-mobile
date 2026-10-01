"""Render a QR module matrix as a crisp, scannable image.

QR codes must stay dark-on-light with a quiet border, whatever the app theme,
or phone cameras struggle to read them.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QWidget

QUIET_ZONE = 4


def qr_pixmap(matrix: list[list[bool]], module_px: int = 6) -> QPixmap:
    """Draw ``matrix`` (True = dark) with a 4-module white quiet zone."""
    count = len(matrix)
    side = (count + 2 * QUIET_ZONE) * module_px
    image = QImage(side, side, QImage.Format.Format_RGB32)
    image.fill(QColor("#FFFFFF"))
    painter = QPainter(image)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#000000"))
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                painter.drawRect(QRect(
                    (x + QUIET_ZONE) * module_px, (y + QUIET_ZONE) * module_px, module_px, module_px
                ))
    painter.end()
    return QPixmap.fromImage(image)


class QrCodeLabel(QLabel):
    """A label that shows a QR code for some text."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background: #FFFFFF; border-radius: 10px; padding: 6px;")

    def set_matrix(self, matrix: list[list[bool]], module_px: int = 6) -> None:
        """Show a module matrix."""
        self.setPixmap(qr_pixmap(matrix, module_px))
