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
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.ui import icons
from app.ui.notifications import hub
from app.ui.theme import token


class ToastCard(QFrame):
    """An animated notification card with a countdown that pauses on hover."""

    dismissed = Signal(object)

    #: level -> (icon name, accent colour token)
    STYLES = {
        "success": ("check-circle", "paid"),
        "warning": ("alert", "pending"),
        "error": ("alert", "failed"),
        "info": ("info", "primary-glow"),
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
        self.setObjectName("toastCard")
        icon_name, accent_token = self.STYLES[self.level]
        accent = token(accent_token)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(14, 12, 12, 8)
        main_layout.setSpacing(6)
        content_row = QHBoxLayout()
        content_row.setContentsMargins(0, 0, 0, 0)
        content_row.setSpacing(12)

        icon_label = QLabel(self)
        icon_label.setFixedSize(30, 30)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setPixmap(icons.pixmap(icon_name, accent_token, 18))
        content_row.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        title_label = QLabel(title, self)
        title_label.setObjectName("notificationTitle")
        text_layout.addWidget(title_label)
        msg_label = QLabel(message, self)
        msg_label.setObjectName("notificationBody")
        msg_label.setWordWrap(True)
        text_layout.addWidget(msg_label)
        if action_text and action_callback:
            action_btn = QPushButton(action_text, self)
            action_btn.setProperty("variant", "secondary")
            action_btn.setProperty("compact", True)
            action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            action_btn.clicked.connect(self._handle_action)
            text_layout.addWidget(action_btn, 0, Qt.AlignmentFlag.AlignLeading)
        content_row.addLayout(text_layout, 1)

        close_btn = QToolButton(self)
        close_btn.setObjectName("iconButton")
        close_btn.setFixedSize(24, 24)
        close_btn.setIconSize(QSize(12, 12))
        close_btn.setIcon(icons.icon("close", "text-muted", 12))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.close_toast)
        content_row.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignTop)
        main_layout.addLayout(content_row)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setFixedHeight(3)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setRange(0, self.duration_ms)
        self.progress_bar.setValue(self.duration_ms)
        self.progress_bar.setStyleSheet(
            "QProgressBar { background-color: rgba(255, 255, 255, 0.06); border: none; "
            "border-radius: 1px; min-height: 3px; } "
            f"QProgressBar::chunk {{ background-color: {accent}; border-radius: 1px; }}"
        )
        main_layout.addWidget(self.progress_bar)

        # The accent stripe sits on the reading-start edge (Qt doesn't mirror borders).
        start_side = "right" if self.isRightToLeft() else "left"
        self.setStyleSheet(
            f"QFrame#toastCard {{ border-{start_side}: 3px solid {accent}; }}"
        )

        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(0.0)
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
        # Toasts stack in the bottom reading-end corner: bottom-left in Arabic,
        # bottom-right in English/French.
        self.margin_x = 24
        self.margin_y = 24
        self.spacing = 10

        hub.toast_requested.connect(self._on_notify_signal)
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
        target_x = self._toast_x(card)

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
            target_x = self._toast_x(card)

            # Animate repositioning
            anim = QPropertyAnimation(card, b"pos", self)
            anim.setDuration(180)
            anim.setStartValue(card.pos())
            anim.setEndValue(QPoint(target_x, target_y))
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.start()

    def _toast_x(self, card: ToastCard) -> int:
        """Return the x position of the reading-end corner."""
        if self.parent_window.isRightToLeft():
            return self.margin_x
        return self.parent_window.width() - card.width() - self.margin_x

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
