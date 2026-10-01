"""Collection-rate ring for the selected month."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFont, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from app.i18n import ar
from app.ui.theme import qcolor


class _Ring(QWidget):
    """Painted progress ring; it fills clockwise in LTR, counter-clockwise in RTL."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rate = 0
        self.setMinimumSize(150, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt callback name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height()) - 16
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)
        painter.setPen(QPen(qcolor("surface-3"), 12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(rect, 0, 360 * 16)
        color = qcolor("paid") if self.rate >= 80 else qcolor("pending") if self.rate >= 50 else qcolor("failed")
        painter.setPen(QPen(color, 12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        span = int(360 * 16 * max(0, min(self.rate, 100)) / 100)
        painter.drawArc(rect, 90 * 16, span if self.isRightToLeft() else -span)
        painter.setPen(qcolor("text"))
        painter.setFont(QFont("Rajdhani", 30, QFont.Weight.Bold))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{self.rate}%")
        painter.end()


class CollectionGaugeWidget(QFrame):
    """Card showing this month's collection rate and its two inputs."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setMinimumWidth(250)
        self._rate = 0
        self._collected = 0
        self._expected = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(8)
        self.title = QLabel(ar.DASH_GAUGE_TITLE, self)
        self.title.setObjectName("sectionTitle")
        layout.addWidget(self.title)
        self.ring = _Ring(self)
        layout.addWidget(self.ring, 1)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        self.collected_caption = QLabel(ar.DASH_GAUGE_COLLECTED, self)
        self.collected_caption.setObjectName("sectionHint")
        self.collected_value = QLabel(self)
        self.collected_value.setObjectName("moneyValue")
        self.expected_caption = QLabel(ar.DASH_GAUGE_TARGET, self)
        self.expected_caption.setObjectName("sectionHint")
        self.expected_value = QLabel(self)
        self.expected_value.setObjectName("moneyValue")
        trailing = Qt.AlignmentFlag.AlignTrailing | Qt.AlignmentFlag.AlignVCenter
        grid.addWidget(self.collected_caption, 0, 0)
        grid.addWidget(self.collected_value, 0, 1, trailing)
        grid.addWidget(self.expected_caption, 1, 0)
        grid.addWidget(self.expected_value, 1, 1, trailing)
        layout.addLayout(grid)
        self.set_values(rate=0, collected=0, expected=0)

    def set_values(self, *, rate: int, collected: int, expected: int) -> None:
        """Update the ring and the collected/expected amounts."""
        self._rate = rate
        self._collected = collected
        self._expected = expected
        self.ring.rate = rate
        self.ring.update()
        self.collected_value.setText(f"{collected:,} {ar.CURRENCY_SUFFIX}")
        self.expected_value.setText(f"{expected:,} {ar.CURRENCY_SUFFIX}")
        self.setAccessibleDescription(f"{rate}%")
