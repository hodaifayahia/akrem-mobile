"""Radial collection performance gauge widget for dashboard insights."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPaintEvent,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import QFrame, QWidget


class CollectionGaugeWidget(QFrame):
    """Circular donut progress gauge displaying real-time collection performance percentage."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("collectionGauge")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setMinimumSize(260, 270)
        self.setStyleSheet(
            "QFrame#collectionGauge {"
            "  background: rgba(15, 23, 42, 0.72);"
            "  border: 1px solid rgba(51, 65, 85, 0.55);"
            "  border-radius: 16px;"
            "}"
        )
        self._rate: int = 0
        self._collected: int = 0
        self._expected: int = 0

    def set_values(self, rate: int, collected: int, expected: int) -> None:
        """Update gauge values and trigger repaint."""
        self._rate = max(0, min(100, rate))
        self._collected = collected
        self._expected = expected
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt event name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        rect = self.contentsRect()
        w = rect.width()
        h = rect.height()

        # 1. Header Row
        painter.setFont(QFont("Cairo", 11, QFont.Weight.Bold))
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QRectF(w - 180, 16, 160, 22), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "🎯  معدل التحصيل الشهري")

        # Status badge pill
        if self._rate >= 75:
            badge_bg = QColor(16, 185, 129, 38)
            badge_text = "تحصيل ممتاز"
            badge_color = QColor("#10B981")
            stroke_color = QColor("#06B6D4")
        elif self._rate >= 40:
            badge_bg = QColor(245, 158, 11, 38)
            badge_text = "أداء متوسط"
            badge_color = QColor("#F59E0B")
            stroke_color = QColor("#F59E0B")
        else:
            badge_bg = QColor(239, 68, 68, 38)
            badge_text = "تحصيل ضعيف"
            badge_color = QColor("#EF4444")
            stroke_color = QColor("#EF4444")

        painter.setBrush(QBrush(badge_bg))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(18, 16, 76, 20), 4, 4)
        painter.setFont(QFont("Cairo", 8, QFont.Weight.Bold))
        painter.setPen(badge_color)
        painter.drawText(QRectF(18, 16, 76, 20), Qt.AlignmentFlag.AlignCenter, badge_text)

        # 2. Donut Gauge in Center
        gauge_size = min(w - 90, 140)
        center_x = w / 2.0
        center_y = 116.0
        gauge_rect = QRectF(center_x - gauge_size / 2, center_y - gauge_size / 2, gauge_size, gauge_size)

        # Background track circle
        track_pen = QPen(QColor("#1E293B"), 13)
        track_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(gauge_rect, 0, 360 * 16)

        # Active progress arc
        if self._rate > 0:
            active_pen = QPen(stroke_color, 13)
            active_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(active_pen)
            # Start from top (90 degrees in Qt = 90 * 16) and go clockwise (negative degrees)
            span_angle = int(-360 * 16 * (self._rate / 100.0))
            painter.drawArc(gauge_rect, 90 * 16, span_angle)

        # Percentage Text inside Donut
        painter.setFont(QFont("Rajdhani", 30, QFont.Weight.ExtraBold))
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QRectF(center_x - 60, center_y - 24, 120, 34), Qt.AlignmentFlag.AlignCenter, f"{self._rate}%")

        # Subtitle below percentage
        painter.setFont(QFont("Cairo", 9, QFont.Weight.Medium))
        painter.setPen(QColor("#94A3B8"))
        painter.drawText(QRectF(center_x - 60, center_y + 10, 120, 20), Qt.AlignmentFlag.AlignCenter, "نسبة الاسترداد")

        # 3. Bottom Target Performance Summary
        bottom_y = h - 68
        painter.setPen(QColor("#1E293B"))
        painter.drawLine(18, bottom_y - 8, w - 18, bottom_y - 8)

        # Collected row
        painter.setFont(QFont("Cairo", 9, QFont.Weight.Medium))
        painter.setPen(QColor("#94A3B8"))
        painter.drawText(QRectF(w - 140, bottom_y, 122, 18), Qt.AlignmentFlag.AlignRight, "المحصل حتى اللحظة:")

        painter.setFont(QFont("Cairo", 9, QFont.Weight.Bold))
        painter.setPen(QColor("#10B981"))
        painter.drawText(QRectF(18, bottom_y, 100, 18), Qt.AlignmentFlag.AlignLeft, f"{self._collected:,} دج")

        # Expected row
        painter.setFont(QFont("Cairo", 9, QFont.Weight.Medium))
        painter.setPen(QColor("#94A3B8"))
        painter.drawText(QRectF(w - 140, bottom_y + 20, 122, 18), Qt.AlignmentFlag.AlignRight, "الهدف المالي للشهر:")

        painter.setFont(QFont("Cairo", 9, QFont.Weight.Bold))
        painter.setPen(QColor("#E2E8F0"))
        painter.drawText(QRectF(18, bottom_y + 20, 100, 18), Qt.AlignmentFlag.AlignLeft, f"{self._expected:,} دج")

        # Horizontal progress bar
        bar_y = bottom_y + 42
        bar_w = w - 36
        painter.setBrush(QBrush(QColor("#1E293B")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(18, bar_y, bar_w, 6), 3, 3)

        if self._rate > 0:
            fill_w = max(6, int(bar_w * (self._rate / 100.0)))
            bar_grad = QLinearGradient(18, bar_y, 18 + fill_w, bar_y)
            bar_grad.setColorAt(0.0, QColor("#06B6D4"))
            bar_grad.setColorAt(1.0, QColor("#10B981"))
            painter.setBrush(QBrush(bar_grad))
            painter.drawRoundedRect(QRectF(18, bar_y, fill_w, 6), 3, 3)
