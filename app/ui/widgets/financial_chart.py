"""Six-month trend of expected versus collected money.

The header and legend are ordinary widgets, so Qt mirrors them. The plot is
custom-painted and lays time out from the reading-start edge: oldest month
on the right in Arabic, on the left in English/French. Y-axis labels sit on
the start side as well.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from app.i18n import ar
from app.ui.theme import qcolor


def compact_amount(value: int) -> str:
    """Shorten large amounts for axis labels (1.2M, 450K)."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value // 1_000}K"
    return str(value)


def nice_ceiling(value: int) -> int:
    """Round an axis maximum up to a readable number."""
    if value <= 10:
        return 10
    magnitude = 10 ** (len(str(value)) - 1)
    return ((value // magnitude) + 1) * magnitude


class _Plot(QWidget):
    """Painted plot area; data is a list of (label, expected, collected)."""

    MARGIN_AXIS = 52
    MARGIN_END = 18
    MARGIN_TOP = 12
    MARGIN_BOTTOM = 30

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.data: list[tuple[str, int, int]] = []
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def _x(self, fraction: float, width: float) -> float:
        """Map 0..1 along the time axis to a pixel x from the start edge."""
        plot_w = max(10.0, width - self.MARGIN_AXIS - self.MARGIN_END)
        offset = self.MARGIN_AXIS + fraction * plot_w
        return width - offset if self.isRightToLeft() else offset

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt callback name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = float(self.width()), float(self.height())
        top = self.MARGIN_TOP
        base = height - self.MARGIN_BOTTOM
        plot_h = max(10.0, base - top)
        rtl = self.isRightToLeft()
        scale = nice_ceiling(max([1, *(max(e, c) for _l, e, c in self.data)]))

        # Grid and Y-axis labels (on the reading-start side).
        painter.setFont(QFont("Rajdhani", 9, QFont.Weight.DemiBold))
        for step in range(5):
            y = base - plot_h * step / 4
            painter.setPen(QPen(qcolor("border"), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(self._x(0, width), y), QPointF(self._x(1, width), y))
            painter.setPen(qcolor("text-muted"))
            label_rect = (
                QRectF(width - self.MARGIN_AXIS + 6, y - 9, self.MARGIN_AXIS - 10, 18)
                if rtl else QRectF(0, y - 9, self.MARGIN_AXIS - 10, 18)
            )
            align = Qt.AlignmentFlag.AlignAbsolute | (
                Qt.AlignmentFlag.AlignLeft if rtl else Qt.AlignmentFlag.AlignRight
            )
            painter.drawText(label_rect, align | Qt.AlignmentFlag.AlignVCenter,
                             compact_amount(scale * step // 4))

        count = len(self.data)
        if count < 2:
            painter.end()
            return
        expected = [QPointF(self._x(i / (count - 1), width), base - min(e, scale) / scale * plot_h)
                    for i, (_l, e, _c) in enumerate(self.data)]
        collected = [QPointF(self._x(i / (count - 1), width), base - min(c, scale) / scale * plot_h)
                     for i, (_l, _e, c) in enumerate(self.data)]

        glow = qcolor("primary-glow")
        area = QPainterPath(QPointF(collected[0].x(), base))
        for point in collected:
            area.lineTo(point)
        area.lineTo(collected[-1].x(), base)
        area.closeSubpath()
        gradient = QLinearGradient(0, top, 0, base)
        gradient.setColorAt(0.0, QColor(glow.red(), glow.green(), glow.blue(), 80))
        gradient.setColorAt(1.0, QColor(glow.red(), glow.green(), glow.blue(), 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawPath(area)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(qcolor("silver"), 2, Qt.PenStyle.DashLine))
        painter.drawPolyline(expected)
        painter.setPen(QPen(glow, 2.5))
        painter.drawPolyline(collected)
        painter.setPen(QPen(qcolor("surface"), 2))
        painter.setBrush(glow)
        for point in collected:
            painter.drawEllipse(point, 4, 4)

        painter.setFont(QFont("Cairo", 9))
        for index, (label, _e, _c) in enumerate(self.data):
            x = collected[index].x()
            latest = index == count - 1
            painter.setPen(qcolor("highlight") if latest else qcolor("text-muted"))
            left = min(max(0.0, x - 40), width - 80)
            painter.drawText(QRectF(left, base + 6, 80, 20), Qt.AlignmentFlag.AlignCenter, label)
        painter.end()


class _LegendDot(QLabel):
    def __init__(self, color: QColor, dashed: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(18, 10)
        self._color = color
        self._dashed = dashed

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt callback name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        style = Qt.PenStyle.DashLine if self._dashed else Qt.PenStyle.SolidLine
        painter.setPen(QPen(self._color, 2.5, style))
        painter.drawLine(1, 5, 17, 5)
        painter.end()


class FinancialTrendChart(QFrame):
    """Card with a title, legend and the expected/collected trend plot."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setMinimumHeight(290)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title = QLabel(ar.DASH_TREND_TITLE, self)
        self.title.setObjectName("sectionTitle")
        self.subtitle = QLabel(ar.DASH_TREND_SUBTITLE, self)
        self.subtitle.setObjectName("sectionHint")
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles, 1)
        for color, dashed, text in (
            (qcolor("primary-glow"), False, ar.DASH_LEGEND_COLLECTED),
            (qcolor("silver"), True, ar.DASH_LEGEND_EXPECTED),
        ):
            header.addWidget(_LegendDot(color, dashed, self))
            legend = QLabel(text, self)
            legend.setObjectName("sectionHint")
            header.addWidget(legend)
            header.addSpacing(8)
        layout.addLayout(header)

        self.plot = _Plot(self)
        layout.addWidget(self.plot, 1)

    @property
    def _data(self) -> list[tuple[str, int, int]]:
        return self.plot.data

    def set_data(self, data: list[tuple[str, int, int]] | tuple[tuple[str, int, int], ...]) -> None:
        """Replace the (label, expected, collected) points, oldest first."""
        self.plot.data = list(data)
        self.plot.update()
