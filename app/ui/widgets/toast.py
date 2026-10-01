"""World-class animated toast notification system for PySide6 desktop."""

from __future__ import annotations

from typing import Callable
from PySide6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.events import events


class ToastCard(QFrame):
    """An individual animated notification card with countdown and pause-on-hover."""

    dismissed = Signal(object)

    STYLES = {
        "success": {
            "border_color": "#22C55E",
            "icon_bg": "#0D2E1E",
            "icon_color": "#4ADE80",
            "icon": "✓",
            "bar_color": "#22C55E",
        },
        "warning": {
            "border_color": "#F59E0B",
            "icon_bg": "#332200",
            "icon_color": "#FBBF24",
            "icon": "⚠️",
            "bar_color": "#F59E0B",
        },
        "error": {
            "border_color": "#EF4444",
            "icon_bg": "#361014",
            "icon_color": "#F87171",
            "icon": "✕",
            "bar_color": "#EF4444",
        },
        "info": {
            "border_color": "#3B92D9",
            "icon_bg": "#0F2646",
            "icon_color": "#9DBEFF",
            "icon": "ℹ️",
            "bar_color": "#3B92D9",
        },
    }

    def __init__(
        self,
        level: str,
        title: str,
        message: str,
        duration_ms: int = 4000,
        action_text: str | None = None,
        action_callback: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.level = level if level in self.STYLES else "info"
        self.duration_ms = max(duration_ms, 1500)
        self.remaining_ms = self.duration_ms
        self.action_callback = action_callback

        self.setFixedWidth(380)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setObjectName("toastCard")

        cfg = self.STYLES[self.level]

        # Layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(14, 12, 14, 8)
        main_layout.setSpacing(6)

        # Content row: Icon + Texts + (Action) + Close button
        content_row = QHBoxLayout()
        content_row.setContentsMargins(0, 0, 0, 0)
        content_row.setSpacing(12)

        # Icon badge
        icon_label = QLabel(cfg["icon"], self)
        icon_label.setFixedSize(32, 32)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setStyleSheet(
            f"background-color: {cfg['icon_bg']}; color: {cfg['icon_color']}; "
            f"border: 1px solid {cfg['border_color']}; border-radius: 16px; font-weight: 700; font-size: 14px;"
        )
        content_row.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)

        # Text column
        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(3)

        title_label = QLabel(title, self)
        title_label.setStyleSheet("color: #FFFFFF; font-weight: 700; font-size: 13px;")
        text_layout.addWidget(title_label)

        msg_label = QLabel(message, self)
        msg_label.setWordWrap(True)
        msg_label.setStyleSheet("color: #9DB0C7; font-size: 12px; line-height: 1.3;")
        text_layout.addWidget(msg_label)

        if action_text and action_callback:
            action_btn = QPushButton(action_text, self)
            action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            action_btn.setStyleSheet(
                f"QPushButton {{ background-color: {cfg['icon_bg']}; color: {cfg['icon_color']}; "
                f"border: 1px solid {cfg['border_color']}; border-radius: 5px; padding: 3px 10px; font-size: 11px; font-weight: 600; }} "
                f"QPushButton:hover {{ background-color: {cfg['border_color']}; color: #FFFFFF; }}"
            )
            action_btn.clicked.connect(self._handle_action)
            text_layout.addWidget(action_btn, 0, Qt.AlignmentFlag.AlignRight)

        content_row.addLayout(text_layout, 1)

        # Close button
        close_btn = QPushButton("×", self)
        close_btn.setFixedSize(22, 22)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #64748B; font-size: 16px; font-weight: 700; } "
            "QPushButton:hover { color: #FFFFFF; background: #1E293B; border-radius: 11px; }"
        )
        close_btn.clicked.connect(self.close_toast)
        content_row.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignTop)

        main_layout.addLayout(content_row)

        # Countdown Progress bar at the bottom
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setFixedHeight(3)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, self.duration_ms)
        self.progress_bar.setValue(self.duration_ms)
        self.progress_bar.setStyleSheet(
            f"QProgressBar {{ background-color: rgba(255, 255, 255, 0.08); border: none; border-radius: 1px; }} "
            f"QProgressBar::chunk {{ background-color: {cfg['bar_color']}; border-radius: 1px; }}"
        )
        main_layout.addWidget(self.progress_bar)

        # Style sheet for card body
        self.setStyleSheet(
            f"QFrame#toastCard {{ "
            f"  background-color: #0E1522; "
            f"  border: 1px solid #1F2E47; "
            f"  border-right: 4px solid {cfg['border_color']}; "
            f"  border-radius: 10px; "
            f"}}"
        )

        # Animation effects
        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(0.0)

        # Timers
        self.tick_interval_ms = 40
        self.timer = QTimer(self)
        self.timer.setInterval(self.tick_interval_ms)
        self.timer.timeout.connect(self._on_tick)

        self._is_closing = False

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt event name
        """Pause auto-dismiss when hovering over the notification."""
        self.timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt event name
        """Resume auto-dismiss when mouse leaves the notification."""
        if not self._is_closing and self.remaining_ms > 0:
            self.timer.start()
        super().leaveEvent(event)

    def _handle_action(self) -> None:
        if self.action_callback:
            self.action_callback()
        self.close_toast()

    def _on_tick(self) -> None:
        self.remaining_ms -= self.tick_interval_ms
        self.progress_bar.setValue(max(0, self.remaining_ms))
        if self.remaining_ms <= 0:
            self.timer.stop()
            self.close_toast()

    def animate_show(self, start_pos: QPoint, end_pos: QPoint) -> None:
        """Fade in and smoothly slide up to target position."""
        self.move(start_pos)
        self.show()

        pos_anim = QPropertyAnimation(self, b"pos", self)
        pos_anim.setDuration(260)
        pos_anim.setStartValue(start_pos)
        pos_anim.setEndValue(end_pos)
        pos_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        fade_anim = QPropertyAnimation(self.opacity_effect, b"opacity", self)
        fade_anim.setDuration(220)
        fade_anim.setStartValue(0.0)
        fade_anim.setEndValue(1.0)

        group = QParallelAnimationGroup(self)
        group.addAnimation(pos_anim)
        group.addAnimation(fade_anim)
        group.finished.connect(self.timer.start)
        group.start()

    def close_toast(self) -> None:
        """Fade out and emit dismissed signal."""
        if self._is_closing:
            return
        self._is_closing = True
        self.timer.stop()

        fade_anim = QPropertyAnimation(self.opacity_effect, b"opacity", self)
        fade_anim.setDuration(180)
        fade_anim.setStartValue(self.opacity_effect.opacity())
        fade_anim.setEndValue(0.0)
        fade_anim.finished.connect(self._finish_close)
        fade_anim.start()

    def _finish_close(self) -> None:
        self.dismissed.emit(self)
        self.deleteLater()


class ToastManager(QWidget):
    """Floating overlay manager managing stacking and layout of notifications."""

    def __init__(self, parent_window: QWidget) -> None:
        super().__init__(parent_window)
        self.parent_window = parent_window
        self.toasts: list[ToastCard] = []
        self.max_toasts = 4
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.hide()
        self.setGeometry(0, 0, 0, 0)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        # Position at bottom-left in RTL
        self.margin_x = 24
        self.margin_y = 24
        self.spacing = 10

        events.notify.connect(self._on_notify_signal)
        self.parent_window.installEventFilter(self)

    def eventFilter(self, watched: object, event) -> bool:  # noqa: N802
        if watched == self.parent_window and event.type() == event.Type.Resize:
            self._reposition_toasts()
        return super().eventFilter(watched, event)

    def _on_notify_signal(self, level: str, title: str, message: str, duration_ms: int) -> None:
        self.show_toast(level=level, title=title, message=message, duration_ms=duration_ms)

    def show_toast(
        self,
        level: str,
        title: str,
        message: str,
        duration_ms: int = 4000,
        action_text: str | None = None,
        action_callback: Callable[[], None] | None = None,
    ) -> ToastCard:
        """Create and animate a new toast notification."""
        # If too many, dismiss oldest
        if len(self.toasts) >= self.max_toasts:
            self.toasts[0].close_toast()

        card = ToastCard(
            level=level,
            title=title,
            message=message,
            duration_ms=duration_ms,
            action_text=action_text,
            action_callback=action_callback,
            parent=self.parent_window,
        )
        card.dismissed.connect(self._on_toast_dismissed)
        self.toasts.append(card)

        card.adjustSize()
        card_height = card.sizeHint().height()

        target_y = self._calculate_toast_y(len(self.toasts) - 1, card_height)
        start_y = target_y + 20
        target_x = self.margin_x

        start_pos = QPoint(target_x, start_y)
        end_pos = QPoint(target_x, target_y)

        card.raise_()
        card.animate_show(start_pos, end_pos)
        self._reposition_toasts()
        return card

    def _calculate_toast_y(self, index: int, card_height: int) -> int:
        """Stack toasts from bottom to top."""
        ref_h = self.parent_window.height() if self.parent_window else 600
        bottom_y = ref_h - self.margin_y
        offset = 0
        for i in range(index):
            offset += (self.toasts[i].height() or 80) + self.spacing
        return bottom_y - offset - card_height

    def _reposition_toasts(self) -> None:
        """Smoothly re-arrange remaining toasts when one closes or window resizes."""
        if not self.parent_window:
            return
        bottom_y = self.parent_window.height() - self.margin_y
        accumulated_height = 0

        for card in self.toasts:
            card_height = card.height() or card.sizeHint().height()
            accumulated_height += card_height + self.spacing
            target_y = bottom_y - accumulated_height + self.spacing
            target_x = self.margin_x

            # Animate repositioning
            anim = QPropertyAnimation(card, b"pos", self)
            anim.setDuration(180)
            anim.setStartValue(card.pos())
            anim.setEndValue(QPoint(target_x, target_y))
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.start()

    def _on_toast_dismissed(self, card: ToastCard) -> None:
        if card in self.toasts:
            self.toasts.remove(card)
        self._reposition_toasts()

    def success(self, title: str, message: str, duration_ms: int = 4000) -> ToastCard:
        return self.show_toast("success", title, message, duration_ms)

    def warning(self, title: str, message: str, duration_ms: int = 4000) -> ToastCard:
        return self.show_toast("warning", title, message, duration_ms)

    def error(self, title: str, message: str, duration_ms: int = 4000) -> ToastCard:
        return self.show_toast("error", title, message, duration_ms)

    def info(self, title: str, message: str, duration_ms: int = 4000) -> ToastCard:
        return self.show_toast("info", title, message, duration_ms)
