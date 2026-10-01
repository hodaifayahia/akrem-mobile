"""Clickable KPI card used on the dashboard."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from app.i18n import ar
from app.ui import icons
from app.ui.theme import TOKENS


class StatCard(QFrame):
    """A KPI tile: icon chip, title, large value and a one-line caption.

    ``color`` is a theme token name (``"paid"``, ``"failed"``…) or a hex
    colour. It tints the icon chip and a slim accent bar painted on the
    card's reading-start edge.
    """

    clicked = Signal()

    def __init__(
        self,
        label: str,
        *,
        color: str = "primary-glow",
        subtitle: str = "",
        icon: str = "dashboard",
        clickable: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("kpiCard")
        self._accent = QColor(TOKENS.get(color, color))
        self._clickable = clickable
        if clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(118)
        self.setMinimumWidth(180)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(32, 32)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setPixmap(icons.pixmap(icon, self._accent.name(), 18))
        red, green, blue = self._accent.red(), self._accent.green(), self._accent.blue()
        self.icon_label.setStyleSheet(
            f"background-color: rgba({red}, {green}, {blue}, 0.14); border-radius: 9px;"
        )
        self.title_label = QLabel(label, self)
        self.title_label.setObjectName("kpiTitle")
        self.title_label.setWordWrap(True)
        top.addWidget(self.icon_label)
        top.addWidget(self.title_label, 1)
        layout.addLayout(top)

        self.value_label = QLabel("0", self)
        self.value_label.setObjectName("kpiValue")
        layout.addWidget(self.value_label)

        self.subtitle_label = QLabel(subtitle, self)
        self.subtitle_label.setObjectName("kpiCaption")
        self.subtitle_label.setWordWrap(True)
        layout.addWidget(self.subtitle_label)
        layout.addStretch(1)

        for child in (self.icon_label, self.title_label, self.value_label, self.subtitle_label):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAccessibleName(label)

    def set_subtitle(self, subtitle: str) -> None:
        """Update the caption under the value."""
        self.subtitle_label.setText(subtitle)

    def set_value(self, value: int | str, *, currency: bool = False, unit: str = "") -> None:
        """Show a value with Western digits and thousands separators."""
        if currency and isinstance(value, int):
            text = f"{value:,} {ar.CURRENCY_SUFFIX}"
        elif isinstance(value, int):
            text = f"{value:,} {unit}".strip()
        else:
            text = f"{value} {unit}".strip()
        self.value_label.setText(text)
        self.setAccessibleDescription(text)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt callback name
        """Draw the card, then a short accent bar on the reading-start edge."""
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._accent)
        x = self.width() - 3 if self.isRightToLeft() else 0
        painter.drawRoundedRect(QRectF(x, 18, 3, 28), 1.5, 1.5)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt callback name
        """Emit ``clicked`` on a left-button press."""
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)
