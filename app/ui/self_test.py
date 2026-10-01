"""Start-up self-test: can a user actually click the app?

``AkremMobile.exe --self-test [report.json]`` runs this against the packaged
build, on a throw-away data folder, so a broken installer is caught on the
build PC instead of at the shop. It checks that

* migrations, seed data and an owner account work from the frozen bundle;
* the main window opens without any modal dialog or popup grabbing input;
* every sidebar button really receives a mouse click at its centre (no
  invisible layer on top of it) and switches to its page;
* the top-bar buttons are reachable by the mouse as well.

The checks use real mouse events (``QTest.mouseClick`` on the widget the
window reports at that point), not ``button.click()``, which would bypass
hit-testing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QAbstractButton, QApplication, QWidget

_LOG = logging.getLogger(__name__)
_SETTLE_ROUNDS = 25


@dataclass
class SelfTestReport:
    """What the self-test found; ``ok`` is False when anything failed."""

    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def passed(self, message: str) -> None:
        self.checks.append(message)

    def failed(self, message: str) -> None:
        _LOG.error("Self-test: %s", message)
        self.failures.append(message)

    def to_json(self) -> str:
        return json.dumps({"ok": self.ok, **asdict(self)}, ensure_ascii=False, indent=2)


def settle(app: QApplication, rounds: int = _SETTLE_ROUNDS) -> None:
    """Let pending paints, layouts and queued signals run."""
    for _ in range(rounds):
        app.processEvents()


def click_target(window: QWidget, widget: QWidget) -> QWidget | None:
    """The widget that would receive a mouse press at ``widget``'s centre."""
    point = widget.mapTo(window, widget.rect().center())
    return window.childAt(point)


def reaches(window: QWidget, widget: QWidget) -> bool:
    """True when a click at ``widget``'s centre lands on it (or one of its children)."""
    target = click_target(window, widget)
    return target is widget or (target is not None and widget.isAncestorOf(target))


def check_window(app: QApplication, window, report: SelfTestReport) -> None:
    """Run the interaction checks against an open main window."""
    from PySide6.QtTest import QTest

    settle(app)
    modal = QApplication.activeModalWidget()
    popup = QApplication.activePopupWidget()
    if modal is not None:
        report.failed(f"a modal dialog blocks the window: {type(modal).__name__} {modal.windowTitle()!r}")
    elif popup is not None:
        report.failed(f"a popup grabs the mouse: {type(popup).__name__}")
    else:
        report.passed("no modal dialog or popup is blocking input")

    for index, button in enumerate(window.sidebar.buttons):
        if button is None or not button.isVisible():
            continue
        if not reaches(window, button):
            target = click_target(window, button)
            report.failed(
                f"sidebar button {index} is covered by {type(target).__name__ if target else 'nothing'}"
            )
            continue
        QTest.mouseClick(button, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         button.rect().center())
        settle(app, 5)
        if window.pages.currentIndex() != index:
            report.failed(f"clicking sidebar button {index} did not open its page")
        else:
            report.passed(f"sidebar button {index} opens its page")

    blocked = [
        button for button in window.topbar.findChildren(QAbstractButton)
        if button.isVisible() and button.isEnabled() and not reaches(window, button)
    ]
    if blocked:
        report.failed(f"{len(blocked)} top-bar button(s) cannot be reached by the mouse")
    else:
        report.passed("top-bar buttons are reachable")


def run_self_test(report_path: str | None = None) -> int:
    """Run the full start-up self-test in a temporary data folder; returns an exit code."""
    import os
    import tempfile

    from PySide6.QtCore import QSettings

    report = SelfTestReport()
    with tempfile.TemporaryDirectory(prefix="akremmobile-selftest-") as folder:
        os.environ["AKREMMOBILE_DATA_DIR"] = str(Path(folder) / "data")
        os.environ.pop("AKREMMOBILE_DATABASE_URL", None)
        QSettings.setPath(QSettings.Format.NativeFormat, QSettings.Scope.UserScope, str(Path(folder) / "settings"))
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(Path(folder) / "settings"))
        app = QApplication.instance() or QApplication([])
        window = None
        try:
            from app.db.migrate import upgrade_database
            from app.db.models import User
            from app.db.session import dispose_database_engines, session_scope
            from app.services import auth
            from app.services.seed import seed_defaults
            from app.ui.locale import apply_language
            from app.ui.main_window import MainWindow

            upgrade_database()
            seed_defaults()
            with session_scope() as session:
                owner = auth.create_first_owner(
                    session, username="selftest", password="SelfTest123", password_confirmation="SelfTest123"
                )
                owner_id = owner.id
            report.passed("database migrations and owner account work")
            for language in ("ar", "en"):
                apply_language(language)
                with session_scope() as session:
                    user = session.get(User, owner_id)
                window = MainWindow(current_user=user)
                window.resize(1366, 768)
                window.show()
                check_window(app, window, report)
                window._quitting = True
                window.monitor.stop()
                window.hide()  # never close(): that would save geometry to the real settings
                settle(app)
                window = None
            report.passed("main window opens in Arabic (RTL) and English (LTR)")
            dispose_database_engines()
        except Exception as error:  # noqa: BLE001 - every failure is reported, never raised
            _LOG.exception("Self-test crashed")
            report.failed(f"{type(error).__name__}: {error}")
        finally:
            if window is not None:
                window.hide()
    text = report.to_json()
    if report_path:
        Path(report_path).write_text(text, encoding="utf-8")
    _LOG.info("Self-test result: %s", text)
    return 0 if report.ok else 1
