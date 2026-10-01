"""Single entry point for in-app and operating-system notifications.

Everything that wants to tell the user something calls
:meth:`NotificationHub.post`. The hub

* records the notification in an in-memory history shown in the
  notification center (with read/unread state and a badge count);
* shows an in-app toast; and
* asks the system tray to raise a native Windows toast when the window is
  hidden in the tray, minimized, or in the background.

Native toasts can be switched off per PC, and routine confirmations of
something the user just did (``native=False``) never leave the app.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count

from PySide6.QtCore import QObject, QSettings, Signal

from app.config import APP_NAME

HISTORY_LIMIT = 100
NATIVE_SETTING = "notifications/native"


@dataclass
class AppNotification:
    """One entry in the notification center's activity history."""

    id: int
    level: str
    title: str
    message: str
    created_at: datetime = field(default_factory=datetime.now)
    read: bool = False
    customer_id: int | None = None


class NotificationHub(QObject):
    """Route notifications to the history, in-app toasts and native toasts."""

    changed = Signal()
    toast_requested = Signal(str, str, str, int)
    native_requested = Signal(str, str, str)
    activated = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._history: deque[AppNotification] = deque(maxlen=HISTORY_LIMIT)
        self._ids = count(1)
        self._keys: set[str] = set()
        self._foreground: Callable[[], bool] = lambda: True
        self._native_available: Callable[[], bool] = lambda: False

    # ---------------------------------------------------------------- wiring
    def set_foreground_probe(self, probe: Callable[[], bool]) -> None:
        """Tell the hub how to ask whether the main window is in front."""
        self._foreground = probe

    def set_native_probe(self, probe: Callable[[], bool]) -> None:
        """Tell the hub how to ask whether native toasts can be shown."""
        self._native_available = probe

    # --------------------------------------------------------------- posting
    def post(
        self,
        level: str,
        title: str,
        message: str,
        *,
        duration_ms: int = 4000,
        native: bool | None = None,
        customer_id: int | None = None,
        key: str | None = None,
        record: bool = True,
    ) -> AppNotification | None:
        """Publish a notification.

        ``native``: ``None`` sends a Windows toast only while the window is
        not in front; ``True`` always does (when enabled); ``False`` never.
        ``key`` suppresses repeats of the same event for this session.
        ``record=False`` shows a toast without adding history (for routine
        confirmations).
        """
        if key is not None:
            if key in self._keys:
                return None
            self._keys.add(key)

        entry: AppNotification | None = None
        if record:
            entry = AppNotification(
                next(self._ids), level, title, message, customer_id=customer_id
            )
            self._history.appendleft(entry)
            self.changed.emit()

        # In-app toasts are cheap and harmless while hidden (they expire), so
        # they always fire; only the Windows toast depends on the window state.
        self.toast_requested.emit(level, title, message, duration_ms)
        foreground = self._safe(self._foreground)
        wants_native = native is True or (native is None and not foreground)
        if wants_native and native_enabled() and self._safe(self._native_available):
            self.native_requested.emit(level, title, message)
        return entry

    # --------------------------------------------------------------- history
    def history(self) -> list[AppNotification]:
        """Return notifications, newest first."""
        return list(self._history)

    def unread_count(self) -> int:
        """Return how many history entries are unread."""
        return sum(1 for entry in self._history if not entry.read)

    def mark_all_read(self) -> None:
        """Clear the unread state of every entry."""
        if any(not entry.read for entry in self._history):
            for entry in self._history:
                entry.read = True
            self.changed.emit()

    def forget_key(self, key: str) -> None:
        """Allow a de-duplicated event to notify again."""
        self._keys.discard(key)

    @staticmethod
    def _safe(probe: Callable[[], bool]) -> bool:
        try:
            return bool(probe())
        except RuntimeError:
            # The probed window was already destroyed.
            return False


def native_enabled() -> bool:
    """Return the per-PC preference for native Windows toasts (default on)."""
    value = QSettings(APP_NAME, APP_NAME).value(NATIVE_SETTING, True)
    return value not in (False, "false", "0", 0)


def set_native_enabled(enabled: bool) -> None:
    """Persist the per-PC preference for native Windows toasts."""
    QSettings(APP_NAME, APP_NAME).setValue(NATIVE_SETTING, bool(enabled))


def relative_time(moment: datetime, now: datetime | None = None) -> str:
    """Format how long ago a notification arrived, in the active language."""
    from app.i18n import ar

    seconds = max(0, int(((now or datetime.now()) - moment).total_seconds()))
    if seconds < 60:
        return ar.NOTIF_JUST_NOW
    if seconds < 3600:
        return ar.NOTIF_MINUTES_AGO.format(n=seconds // 60)
    if seconds < 86_400:
        return ar.NOTIF_HOURS_AGO.format(n=seconds // 3600)
    return moment.strftime("%d/%m/%Y")


#: Application-wide hub; the main window wires its probes and listeners.
hub = NotificationHub()


def _relay_ui_event(level: str, title: str, message: str, duration_ms: int) -> None:
    """Route ``events.notify`` (in-app confirmations and errors) through the hub.

    These describe something the user just did, so they never become a
    Windows toast. Only warnings and errors are kept in the activity history.
    """
    hub.post(
        level,
        title,
        message,
        duration_ms=duration_ms,
        native=False,
        record=level in ("warning", "error"),
    )


def _connect_ui_events() -> None:
    from app.ui.events import events

    events.notify.connect(_relay_ui_event)


_connect_ui_events()
