"""Pure PySide6 antialiased vector financial trend chart."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPaintEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QFrame, QSizePolicy, QWidget


class FinancialTrendChart(QFrame):
    """High-performance antialiased vector area chart comparing expected vs collected cash."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("trendChart")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setMinimumHeight(270)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMouseTracking(True)
        self.setStyleSheet(
            "QFrame#trendChart {"
            "  background: rgba(15, 23, 42, 0.72);"
            "  border: 1px solid rgba(51, 65, 85, 0.55);"
            "  border-radius: 16px;"
            "}"
        )
        # List of (month_label, expected_amount, collected_amount)
        self._data: list[tuple[str, int, int]] = []

    def set_data(self, data: list[tuple[str, int, int]] | tuple[tuple[str, int, int], ...]) -> None:
        """Update chart data points and trigger repaint."""
        self._data = list(data)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt event name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        rect = self.contentsRect()
        w = rect.width()
        h = rect.height()

        # 1. Header & Legend
        header_y = 24
        painter.setFont(QFont("Cairo", 12, QFont.Weight.Bold))
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QRectF(w - 320, header_y - 8, 300, 22), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "📈  مقارنة التدفق المالي (آخر 6 أشهر)")

        painter.setFont(QFont("Cairo", 9, QFont.Weight.Medium))
        painter.setPen(QColor("#64748B"))
        painter.drawText(QRectF(w - 320, header_y + 14, 300, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "مقارنة بين التحصيل الفعلي والمبالغ المجدولة")

        # Legend (Pills on Left)
        leg_x = 24
        painter.setBrush(QBrush(QColor("#06B6D4")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(leg_x + 6, header_y + 4), 5, 5)
        painter.setFont(QFont("Cairo", 9, QFont.Weight.Bold))
        painter.setPen(QColor("#E2E8F0"))
        painter.drawText(leg_x + 18, header_y + 9, "المحصل الفعلي")

        leg2_x = leg_x + 115
        painter.setBrush(QBrush(QColor("#10B981")))
        painter.drawEllipse(QPointF(leg2_x + 6, header_y + 4), 5, 5)
        painter.setPen(QColor("#E2E8F0"))
        painter.drawText(leg2_x + 18, header_y + 9, "المتوقع تحصيله")

        # 2. Chart Grid Bounds
        margin_left = 65
        margin_right = 35
        margin_top = 65
        margin_bottom = 35

        chart_w = max(50, w - margin_left - margin_right)
        chart_h = max(50, h - margin_top - margin_bottom)

        if not self._data:
            # Fallback 6 default month markers
            self._data = [
                ("ماي", 0, 0), ("جوان", 0, 0), ("جويلية", 0, 0),
                ("أوت", 0, 0), ("سبتمبر", 0, 0), ("أكتوبر", 0, 0)
            ]

        # Calculate max scale ceiling
        max_val = 1
        for _lbl, exp, col in self._data:
            if exp > max_val:
                max_val = exp
            if col > max_val:
                max_val = col

        if max_val <= 10:
            scale_max = 10
        else:
            magnitude = 10 ** (len(str(max_val)) - 1)
            scale_max = ((max_val // magnitude) + 1) * magnitude

        # 3. Horizontal Gridlines & Y-Axis Labels
        painter.setFont(QFont("Rajdhani", 9, QFont.Weight.Medium))
        steps = 4
        for step in range(steps + 1):
            y = margin_top + (chart_h * (steps - step)) / steps
            grid_val = int((scale_max * step) / steps)

            # Dashed gridline
            grid_pen = QPen(QColor("rgba(51, 65, 85, 0.45)"), 1, Qt.PenStyle.DashLine)
            painter.setPen(grid_pen)
            painter.drawLine(margin_left, int(y), int(margin_left + chart_w), int(y))

            # Y label on left
            painter.setPen(QColor("#64748B"))
            if grid_val >= 1_000_000:
                lbl = f"{grid_val / 1_000_000:.1f}M"
            elif grid_val >= 1_000:
                lbl = f"{grid_val // 1_000}K"
            else:
                lbl = str(grid_val)
            painter.drawText(QRectF(10, y - 9, margin_left - 18, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, lbl)

        # 4. Compute Coordinates for Area and Curves
        count = len(self._data)
        if count < 2:
            return

        expected_points: list[QPointF] = []
        collected_points: list[QPointF] = []
        x_step = chart_w / (count - 1)

        for i, (_lbl, exp, col) in enumerate(self._data):
            px = margin_left + i * x_step
            py_exp = margin_top + chart_h - (min(exp, scale_max) / scale_max) * chart_h
            py_col = margin_top + chart_h - (min(col, scale_max) / scale_max) * chart_h
            expected_points.append(QPointF(px, py_exp))
            collected_points.append(QPointF(px, py_col))

        # 5. Draw Shaded Gradient Area for Collected Cash
        area_path = QPainterPath()
        base_y = margin_top + chart_h
        area_path.moveTo(collected_points[0].x(), base_y)
        area_path.lineTo(collected_points[0])

        for pt in collected_points[1:]:
            area_path.lineTo(pt)
        area_path.lineTo(collected_points[-1].x(), base_y)
        area_path.closeSubpath()

        area_grad = QLinearGradient(0, margin_top, 0, base_y)
        area_grad.setColorAt(0.0, QColor(6, 182, 212, 90))
        area_grad.setColorAt(1.0, QColor(6, 182, 212, 0))
        painter.setBrush(QBrush(area_grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(area_path)

        # 6. Draw Expected Line (Dashed Emerald)
        exp_path = QPainterPath()
        exp_path.moveTo(expected_points[0])
        for pt in expected_points[1:]:
            exp_path.lineTo(pt)
        exp_pen = QPen(QColor("#10B981"), 2.5, Qt.PenStyle.DashLine)
        painter.setPen(exp_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(exp_path)

        # 7. Draw Collected Line (Vibrant Solid Cyan)
        col_path = QPainterPath()
        col_path.moveTo(collected_points[0])
        for pt in collected_points[1:]:
            col_path.lineTo(pt)
        col_pen = QPen(QColor("#06B6D4"), 3, Qt.PenStyle.SolidLine)
        painter.setPen(col_pen)
        painter.drawPath(col_path)

        # 8. Draw Data Dots
        for pt in collected_points:
            painter.setBrush(QBrush(QColor("#06B6D4")))
            painter.setPen(QPen(QColor("#FFFFFF"), 2))
            painter.drawEllipse(pt, 5, 5)

        # 9. X-Axis Month Labels
        painter.setFont(QFont("Cairo", 9, QFont.Weight.Medium))
        for i, (lbl, _exp, _col) in enumerate(self._data):
            px = margin_left + i * x_step
            # Highlight current / last month in cyan
            is_latest = (i == count - 1)
            painter.setPen(QColor("#38BDF8") if is_latest else QColor("#94A3B8"))
            painter.drawText(QRectF(px - 35, base_y + 8, 70, 20), Qt.AlignmentFlag.AlignCenter, lbl)
