"""Clickable summary card for dashboard counts and totals."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout


class StatCard(QFrame):
    """A themed card with an accent stripe, an Arabic title, a large formatted value, and subtitle."""

    clicked = Signal()

    def __init__(self, label: str, *, color: str = "#38BDF8", subtitle: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("statCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("accentColor", color)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setMinimumHeight(115)
        self.setMinimumWidth(160)
        self.setStyleSheet(
            f"QFrame#statCard {{"
            f"  background: rgba(15, 23, 42, 0.72);"
            f"  border: 1px solid rgba(51, 65, 85, 0.55);"
            f"  border-top: 3px solid {color};"
            f"  border-radius: 14px;"
            f"}}"
            f"QFrame#statCard:hover {{"
            f"  background: rgba(21, 31, 56, 0.85);"
            f"  border-color: {color};"
            f"}}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)

        # Top row: Title right-aligned + colored dot on left
        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)

        self.title_label = QLabel(label, self)
        self.title_label.setObjectName("statCardTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.title_label.setStyleSheet("color: #E2E8F0; font-size: 12px; font-weight: 700;")
        title_row.addWidget(self.title_label, 1)

        self.status_dot = QLabel("●", self)
        self.status_dot.setObjectName("statCardDot")
        self.status_dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 10px;")
        title_row.addWidget(self.status_dot)
        layout.addLayout(title_row)

        # Main large value (Right-aligned)
        self.value_label = QLabel("0", self)
        self.value_label.setObjectName("statCardValue")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.value_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.value_label.setStyleSheet(
            f"color: {color}; font-size: 24px; font-weight: 900; "
            "font-family: 'Cairo', 'Rajdhani', sans-serif; letter-spacing: -0.5px; margin: 2px 0;"
        )
        layout.addWidget(self.value_label)

        # Subtitle (Right-aligned)
        self.subtitle_label = QLabel(subtitle, self)
        self.subtitle_label.setObjectName("statCardSubtitle")
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.subtitle_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.subtitle_label.setStyleSheet("color: #64748B; font-size: 10px; font-weight: 500;")
        layout.addWidget(self.subtitle_label)

        self.setAccessibleName(label)

    def set_subtitle(self, subtitle: str) -> None:
        """Update subtitle description text."""
        self.subtitle_label.setText(subtitle)

    def set_value(self, value: int | str, *, currency: bool = False, unit: str = "") -> None:
        """Set the displayed value, using Western digits and grouped numbers."""
        if currency and isinstance(value, int):
            text = f"{value:,} دج"
        elif isinstance(value, int):
            text = f"{value:,} {unit}".strip()
        else:
            text = f"{value} {unit}".strip()
        self.value_label.setText(text)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt callback name
        """Emit a click signal when the card is pressed."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)
