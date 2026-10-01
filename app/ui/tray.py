"""System-tray icon, background mode and native Windows notifications.

When "minimize to tray on close" is on (the default), closing the main
window hides it instead of quitting. Timers keep running, so collection
alerts and backups continue in the background. Notifications raised while
the window is hidden appear as native Windows toasts through the tray icon.

On Windows 10/11, ``QSystemTrayIcon.showMessage`` is delivered as a regular
toast in the Action Center. Setting an explicit AppUserModelID (see
:func:`set_windows_app_id`) makes Windows attribute those toasts to
"AkremMobile" instead of the Python runtime. The installer gives the Start
menu shortcut the same ID.
"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from app.config import APP_NAME, RESOURCE_DIR
from app.i18n import ar
from app.ui import icons
from app.ui.notifications import NotificationHub, native_enabled, set_native_enabled

WINDOWS_APP_ID = "AkremMobile.InstallmentManager"
MINIMIZE_SETTING = "tray/minimize_on_close"
HINT_SETTING = "tray/hint_shown"

_LOG = logging.getLogger(__name__)

_MESSAGE_ICONS = {
    "error": QSystemTrayIcon.MessageIcon.Critical,
    "warning": QSystemTrayIcon.MessageIcon.Warning,
}


def set_windows_app_id(app_id: str = WINDOWS_APP_ID) -> bool:
    """Give this process an explicit AppUserModelID (Windows only).

    Must run before any window is created. Returns ``False`` on other
    platforms or if the call fails; the app works either way, toasts are
    just labelled with the Python executable's name.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except (AttributeError, OSError):
        _LOG.warning("Could not set the Windows AppUserModelID", exc_info=True)
        return False


def minimize_to_tray_enabled() -> bool:
    """Return the per-PC "minimize to tray on close" preference (default on)."""
    value = QSettings(APP_NAME, APP_NAME).value(MINIMIZE_SETTING, True)
    return value not in (False, "false", "0", 0)


def set_minimize_to_tray(enabled: bool) -> None:
    """Persist the per-PC "minimize to tray on close" preference."""
    QSettings(APP_NAME, APP_NAME).setValue(MINIMIZE_SETTING, bool(enabled))


def app_icon() -> QIcon:
    """Return the application icon used for the window and the tray."""
    for name in ("icon.ico", "logo.png"):
        path = RESOURCE_DIR / name
        if path.exists():
            return QIcon(str(path))
    return icons.icon("wallet", "primary-glow", 32)


class TrayController(QObject):
    """Own the tray icon and its menu, and deliver native toasts."""

    open_requested = Signal()
    new_sale_requested = Signal()
    notifications_requested = Signal()
    lock_requested = Signal()
    quit_requested = Signal()

    def __init__(self, hub: NotificationHub, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.hub = hub
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip(ar.TRAY_TOOLTIP)
        self.menu = QMenu()
        self._build_menu()
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.messageClicked.connect(self.notifications_requested.emit)
        hub.native_requested.connect(self.show_native)
        hub.set_native_probe(self.can_show_native)
        if self.available:
            self.tray.show()

    # ------------------------------------------------------------------ menu
    def _build_menu(self) -> None:
        self.open_action = QAction(self.menu)
        self.open_action.triggered.connect(self.open_requested.emit)
        self.new_sale_action = QAction(self.menu)
        self.new_sale_action.triggered.connect(self.new_sale_requested.emit)
        self.notifications_action = QAction(self.menu)
        self.notifications_action.triggered.connect(self.notifications_requested.emit)
        self.lock_action = QAction(self.menu)
        self.lock_action.triggered.connect(self.lock_requested.emit)

        self.minimize_action = QAction(self.menu)
        self.minimize_action.setCheckable(True)
        self.minimize_action.setChecked(minimize_to_tray_enabled())
        self.minimize_action.toggled.connect(set_minimize_to_tray)
        self.native_action = QAction(self.menu)
        self.native_action.setCheckable(True)
        self.native_action.setChecked(native_enabled())
        self.native_action.toggled.connect(set_native_enabled)

        self.quit_action = QAction(self.menu)
        self.quit_action.triggered.connect(self.quit_requested.emit)

        self.menu.addAction(self.open_action)
        self.menu.addAction(self.new_sale_action)
        self.menu.addAction(self.notifications_action)
        self.menu.addAction(self.lock_action)
        self.menu.addSeparator()
        self.menu.addAction(self.minimize_action)
        self.menu.addAction(self.native_action)
        self.menu.addSeparator()
        self.menu.addAction(self.quit_action)
        self.retranslate()

    def retranslate(self) -> None:
        """Refresh menu labels and icons after a language change."""
        self.tray.setToolTip(ar.TRAY_TOOLTIP)
        entries = (
            (self.open_action, ar.TRAY_OPEN, "window"),
            (self.new_sale_action, ar.TRAY_NEW_SALE, "plus"),
            (self.notifications_action, ar.TRAY_NOTIFICATIONS, "bell"),
            (self.lock_action, ar.TRAY_LOCK, "lock"),
            (self.minimize_action, ar.TRAY_MINIMIZE_ON_CLOSE, None),
            (self.native_action, ar.TRAY_NATIVE_NOTIFICATIONS, None),
            (self.quit_action, ar.TRAY_QUIT, "power"),
        )
        for action, text, icon_name in entries:
            action.setText(text)
            if icon_name:
                action.setIcon(icons.icon(icon_name, "text", 16))

    # ------------------------------------------------------------- behaviour
    def should_hide_on_close(self) -> bool:
        """Return whether closing the window should keep the app in the tray."""
        return self.available and self.tray.isVisible() and minimize_to_tray_enabled()

    def can_show_native(self) -> bool:
        """Return whether a native toast can be displayed right now."""
        return self.available and self.tray.isVisible() and QSystemTrayIcon.supportsMessages()

    def show_native(self, level: str, title: str, message: str, timeout_ms: int = 8000) -> None:
        """Show a native operating-system notification (Windows toast)."""
        if not self.can_show_native():
            return
        icon = _MESSAGE_ICONS.get(level, QSystemTrayIcon.MessageIcon.Information)
        self.tray.showMessage(title, message, icon, timeout_ms)

    def show_background_hint_once(self) -> None:
        """Explain, the first time only, that the app keeps running in the tray."""
        settings = QSettings(APP_NAME, APP_NAME)
        if settings.value(HINT_SETTING, False) in (True, "true", "1", 1):
            return
        settings.setValue(HINT_SETTING, True)
        self.show_native("info", ar.TRAY_STILL_RUNNING_TITLE, ar.TRAY_STILL_RUNNING_BODY)

    def hide(self) -> None:
        """Remove the icon from the notification area (on final quit)."""
        self.tray.hide()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.open_requested.emit()
