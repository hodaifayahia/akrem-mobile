"""Run hidden in the tray until someone opens the app (start at Windows sign-in).

Started with ``--background``, AkremMobile shows only its tray icon. The
payment checks run straight away and post Windows notifications. Opening the
app (tray icon, a notification, the menu, or launching it again) shows the
login; once signed in, the normal main window takes over the tray.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QDialog, QWidget

from app.ui.background import FIRST_SCAN_DELAY_MS, CollectionMonitor
from app.ui.notifications import NotificationHub
from app.ui.tray import TrayController


class BackgroundMode(QObject):
    """Tray icon and payment alerts before login; then the login and the main window."""

    window_opened = Signal(object)

    def __init__(
        self,
        hub: NotificationHub,
        sign_in: Callable[[], Any | None],
        open_window: Callable[[Any], QWidget],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._sign_in = sign_in
        self._open_window = open_window
        self._signing_in = False
        self.window: QWidget | None = None
        self.tray: TrayController | None = TrayController(hub, self)
        self.monitor: CollectionMonitor | None = CollectionMonitor(hub, self)
        # Nothing is on screen yet, so every alert is also a Windows toast.
        hub.set_foreground_probe(lambda: False)
        for request in (
            self.tray.open_requested,
            self.tray.new_sale_requested,
            self.tray.notifications_requested,
            self.tray.lock_requested,
        ):
            request.connect(self.open)
        self.tray.quit_requested.connect(lambda: QApplication.instance().quit())

    def start(self, first_delay_ms: int = FIRST_SCAN_DELAY_MS) -> None:
        """Begin the periodic payment checks."""
        if self.monitor is not None:
            self.monitor.start(first_delay_ms)

    def open(self) -> None:
        """Show the login (or bring the main window back once signed in)."""
        if self.window is not None:
            restore = getattr(self.window, "restore_from_tray", None)
            if callable(restore):
                restore()
            return
        if self._signing_in:
            _raise_open_dialogs()
            return
        self._signing_in = True
        try:
            user = self._sign_in()
        finally:
            self._signing_in = False
        if user is None:
            return  # cancelled: stay in the tray
        self._hand_over()
        self.window = self._open_window(user)
        self.window_opened.emit(self.window)

    def _hand_over(self) -> None:
        """Remove this tray icon and stop these checks: the main window has its own."""
        if self.monitor is not None:
            self.monitor.stop()
            self.monitor.deleteLater()
            self.monitor = None
        if self.tray is not None:
            self.tray.hide()
            self.tray.deleteLater()
            self.tray = None


def _raise_open_dialogs() -> None:
    """Bring the login (or first-run setup) dialog to the front."""
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, QDialog) and widget.isVisible():
            widget.raise_()
            widget.activateWindow()
