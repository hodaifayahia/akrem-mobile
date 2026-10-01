"""Background jobs that keep running while the window is hidden in the tray.

:class:`CollectionMonitor` periodically scans for overdue and upcoming
installments on a worker thread and posts:

* a once-a-day digest ("5 overdue installments totalling 120,000 DZD"); and
* an alert whenever an installment *becomes* overdue while the app runs.

Both go through the notification hub, so they appear in the notification
center and, when the window is not in front, as native Windows toasts.
Database work never runs on the UI thread, except for in-memory SQLite test
databases, which cannot be shared across threads.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QSettings, QThreadPool, QTimer, Signal

from app.config import APP_NAME, database_url
from app.db import session as db_session
from app.i18n import ar
from app.services.alerts import AlertSummary, CollectionAlert, collection_alerts, summarize
from app.ui.notifications import NotificationHub

SCAN_INTERVAL_MS = 10 * 60 * 1000
FIRST_SCAN_DELAY_MS = 4000
DIGEST_SETTING = "notifications/last_digest"

_LOG = logging.getLogger(__name__)
#: Relays waiting for a worker result; holding them keeps the queued signal alive.
_PENDING: set["_Relay"] = set()


class _Relay(QObject):
    """Carries a worker result back to the UI thread (queued signal)."""

    finished = Signal(object)
    failed = Signal(str)


class _Job(QRunnable):
    def __init__(self, work: Callable[[], Any], relay: _Relay) -> None:
        super().__init__()
        self._work = work
        self._relay = relay

    def run(self) -> None:  # noqa: D401 - Qt callback
        try:
            result = self._work()
        except Exception as error:  # noqa: BLE001 - reported to the UI thread
            _LOG.warning("Background job failed", exc_info=True)
            self._relay.failed.emit(str(error))
            return
        self._relay.finished.emit(result)


def run_in_background(
    work: Callable[[], Any],
    on_done: Callable[[Any], None],
    on_error: Callable[[str], None] | None = None,
) -> None:
    """Run ``work`` on the shared thread pool and call ``on_done`` on the UI thread.

    Falls back to running synchronously when the configured database is an
    in-memory SQLite database (each thread would see a different, empty one).
    """
    if not _threads_share_database():
        try:
            result = work()
        except Exception as error:  # noqa: BLE001 - background work must not crash the UI
            _LOG.warning("Background job failed", exc_info=True)
            if on_error is not None:
                on_error(str(error))
            return
        on_done(result)
        return
    relay = _Relay()
    _PENDING.add(relay)
    relay.finished.connect(on_done)
    if on_error is not None:
        relay.failed.connect(on_error)
    relay.finished.connect(lambda _result: _PENDING.discard(relay))
    relay.failed.connect(lambda _message: _PENDING.discard(relay))
    QThreadPool.globalInstance().start(_Job(work, relay))


def _threads_share_database() -> bool:
    try:
        engine = db_session._application_engine(database_url())
    except Exception:  # noqa: BLE001 - be conservative and run inline
        return False
    url = engine.url
    return not (url.get_backend_name() == "sqlite" and url.database in (None, "", ":memory:"))


def _scan(today: date) -> tuple[list[CollectionAlert], AlertSummary]:
    with db_session.session_scope() as session:
        alerts = collection_alerts(session, today=today)
    return alerts, summarize(alerts)


class CollectionMonitor(QObject):
    """Periodically refresh collection alerts and notify about changes."""

    updated = Signal(object, object)  # list[CollectionAlert], AlertSummary

    def __init__(
        self,
        hub: NotificationHub,
        parent: QObject | None = None,
        *,
        interval_ms: int = SCAN_INTERVAL_MS,
        today: Callable[[], date] = date.today,
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self._today = today
        self._known_overdue: set[str] | None = None
        self._busy = False
        self.alerts: list[CollectionAlert] = []
        self.summary = summarize(())
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.refresh)

    def start(self, first_delay_ms: int = FIRST_SCAN_DELAY_MS) -> None:
        """Begin periodic scanning after a short start-up delay."""
        self._timer.start()
        QTimer.singleShot(first_delay_ms, self.refresh)

    def stop(self) -> None:
        """Stop scanning (on application quit)."""
        self._timer.stop()

    def refresh(self) -> None:
        """Scan now unless a scan is already running."""
        if self._busy:
            return
        self._busy = True
        today = self._today()
        run_in_background(lambda: _scan(today), self._on_scanned, self._on_failed)

    def apply(self, alerts: list[CollectionAlert], summary: AlertSummary) -> None:
        """Store a scan result and raise the notifications it calls for."""
        self.alerts = alerts
        self.summary = summary
        overdue_keys = {alert.key for alert in alerts if alert.kind != "due_soon"}
        if self._known_overdue is None:
            self._post_daily_digest(summary)
        else:
            fresh = [a for a in alerts if a.key in overdue_keys - self._known_overdue]
            if fresh:
                self._post_new_overdue(fresh)
        self._known_overdue = overdue_keys
        self.updated.emit(alerts, summary)

    def _on_scanned(self, result: object) -> None:
        self._busy = False
        if result is None:
            return
        alerts, summary = result  # type: ignore[misc]
        self.apply(alerts, summary)

    def _on_failed(self, _message: str) -> None:
        self._busy = False

    def _post_daily_digest(self, summary: AlertSummary) -> None:
        today = self._today().isoformat()
        settings = QSettings(APP_NAME, APP_NAME)
        if settings.value(DIGEST_SETTING, "") == today:
            return
        if summary.attention_count == 0 and summary.due_soon_count == 0:
            return
        settings.setValue(DIGEST_SETTING, today)
        lines = []
        if summary.attention_count:
            lines.append(ar.NOTIF_DIGEST_OVERDUE.format(
                count=summary.attention_count,
                amount=_money(summary.overdue_amount + summary.credit_overdue_amount),
            ))
        if summary.due_soon_count:
            lines.append(ar.NOTIF_DIGEST_DUE.format(count=summary.due_soon_count))
        level = "warning" if summary.attention_count else "info"
        self.hub.post(level, ar.NOTIF_DIGEST_TITLE, "\n".join(lines),
                      duration_ms=7000, native=None, key=f"digest:{today}")

    def _post_new_overdue(self, fresh: list[CollectionAlert]) -> None:
        amount = sum(alert.amount for alert in fresh)
        customer_id = fresh[0].customer_id if len({a.customer_id for a in fresh}) == 1 else None
        self.hub.post(
            "warning",
            ar.NOTIF_NEW_OVERDUE_TITLE,
            ar.NOTIF_NEW_OVERDUE_BODY.format(count=len(fresh), amount=_money(amount)),
            duration_ms=7000,
            native=None,
            customer_id=customer_id,
            key="overdue:" + ",".join(sorted(alert.key for alert in fresh)),
        )


def _money(amount: int) -> str:
    return f"{amount:,} {ar.CURRENCY_SUFFIX}"
