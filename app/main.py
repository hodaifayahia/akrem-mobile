"""Application entry point."""

from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from types import TracebackType

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QMessageBox, QWidget

from app.config import RESOURCE_DIR, data_dir, ensure_data_dirs
from app.i18n import ar
from app.ui.main_window import MainWindow


_crash_log = None  # kept open for faulthandler
_error_notice: QMessageBox | None = None


def configure_logging() -> None:
    """Configure rotating logs in the per-user data directory."""
    global _crash_log
    ensure_data_dirs()
    log_dir = data_dir() / "logs"
    handler = RotatingFileHandler(log_dir / "akremmobile.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[handler], force=True)
    try:
        # Native crashes (a broken Qt plugin, a bad DLL) leave a trace here.
        _crash_log = open(log_dir / "crash.log", "a", encoding="utf-8")  # noqa: SIM115 - must stay open
        faulthandler.enable(file=_crash_log)
    except (OSError, RuntimeError):
        logging.warning("Could not enable the crash log", exc_info=True)


def ensure_standard_streams() -> None:
    """Give a windowed (no console) Windows build somewhere to write.

    PyInstaller's windowed executables start with ``sys.stdout`` and
    ``sys.stderr`` set to ``None``; any library that writes to them would
    raise inside a Qt slot and pop an error box over the window.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115 - process-wide


def bring_to_front(widget: QWidget) -> None:
    """Show a window un-minimized, on top and focused."""
    if widget.isMinimized():
        widget.showNormal()
    widget.raise_()
    widget.activateWindow()


def load_arabic_font() -> None:
    """Register bundled Cairo fonts when the font assets are present."""
    font_dir = RESOURCE_DIR / "fonts"
    for font_path in sorted((*font_dir.glob("*.ttf"), *font_dir.glob("*.otf"))):
        QFontDatabase.addApplicationFont(str(font_path))
    QApplication.setFont(QFont("Cairo", 10))


def show_uncaught_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
) -> None:
    """Log an uncaught error and display a short Arabic message."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
    show_error_notice()


def show_error_notice() -> None:
    """Tell the user something failed without ever blocking the window.

    The notice is non-modal, stays on top of the app and is shown once at a
    time. A modal box without a parent can open *behind* the main window on
    Windows; the app then ignores every click and looks frozen.
    """
    global _error_notice
    if QApplication.instance() is None:
        return
    if _error_notice is not None and _error_notice.isVisible():
        return
    parent = QApplication.activeWindow()
    box = QMessageBox(QMessageBox.Icon.Critical, ar.ERROR_TITLE, ar.ERROR_BODY, parent=parent)
    box.setWindowModality(Qt.WindowModality.NonModal)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    box.show()
    bring_to_front(box)
    _error_notice = box


def run_modal(dialog: QDialog) -> int:
    """``exec()`` a start-up dialog, making sure it is in front and focused."""
    QTimer.singleShot(0, lambda: bring_to_front(dialog))
    return dialog.exec()


def raise_visible_windows() -> None:
    """Bring whatever this copy is showing (login, setup, main window) to the front."""
    for widget in QApplication.topLevelWidgets():
        if widget.isVisible() and isinstance(widget, (QDialog, QMainWindow)):
            bring_to_front(widget)


def _rewire(signal, slot) -> None:
    """Point a single-instance request at a new handler (dropping the previous one)."""
    try:
        signal.disconnect()
    except (RuntimeError, TypeError):
        pass  # nothing was connected yet
    signal.connect(slot)


def sign_in():
    """Create the owner on first run, then log in; ``None`` when cancelled.

    A checkpoint backup follows a successful login, so the daily snapshot
    contains the owner account.
    """
    from app.db.session import session_scope
    from app.services.auth import owner_exists
    from app.services.backup import create_automated_checkpoint_backup
    from app.ui.dialogs.auth_dialogs import LoginDialog, SetupOwnerDialog

    with session_scope() as session:
        has_owner = owner_exists(session)
    if not has_owner:
        setup_dialog = SetupOwnerDialog()
        if run_modal(setup_dialog) != QDialog.DialogCode.Accepted:
            return None
    login_dialog = LoginDialog()
    if run_modal(login_dialog) != QDialog.DialogCode.Accepted or login_dialog.user is None:
        return None
    try:
        create_automated_checkpoint_backup(max_per_day=3, min_interval_seconds=3600)
    except (OSError, ValueError):
        logging.exception("Automatic checkpoint backup could not be created")
    return login_dialog.user


def wait_for_shop_server() -> bool:
    """When this PC uses the shop server, make sure it answers before starting; False to quit.

    The main PC may still be starting or switched off: the user can retry,
    change the server settings, or quit. The app never falls back to this
    PC's local database on its own, which would split the shop's records.
    """
    from app.services import shared_db
    from app.ui.dialogs.server_dialog import check_text, configure_server_and_quit

    details = shared_db.current_server()
    if details is None:
        return True  # local database, or a server set by the environment variable
    while True:
        check = shared_db.test_connection(details)
        if check.ok:
            return True
        logging.warning("Shop server not reachable: %s", check.detail)
        box = QMessageBox(QMessageBox.Icon.Warning, ar.SERVER_TITLE,
                          ar.SERVER_START_FAILED.format(host=details.host, reason=check_text(check)))
        retry = box.addButton(ar.SERVER_RETRY, QMessageBox.ButtonRole.AcceptRole)
        settings_button = box.addButton(ar.SERVER_CONFIGURE, QMessageBox.ButtonRole.ActionRole)
        box.addButton(ar.SERVER_QUIT, QMessageBox.ButtonRole.RejectRole)
        run_modal(box)
        clicked = box.clickedButton()
        if clicked == retry:
            continue
        if clicked == settings_button:
            if configure_server_and_quit(None):
                return False
            details = shared_db.current_server()
            if details is None:
                return True
            continue
        return False


def open_main_window(app: QApplication, instance, user) -> MainWindow:
    """Show the main window for ``user`` and route later launches to it."""
    # Closing the window hides it in the tray; MainWindow quits explicitly.
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow(current_user=user)
    _rewire(instance.activation_requested, window.restore_from_tray)
    _rewire(instance.quit_requested, window.quit_application)
    window.show()
    bring_to_front(window)
    window.start_background_services()
    logging.info("Main window ready")
    return window


def run_in_background(app: QApplication, instance, *, needs_sign_up: bool = False) -> int:
    """Start hidden in the tray (Windows sign-in); log in when the app is opened.

    On a fresh install there is no account yet, so the sign-up opens at once
    instead of waiting in the tray.
    """
    from app.ui.background_mode import BackgroundMode
    from app.ui.notifications import hub

    app.setQuitOnLastWindowClosed(False)
    mode = BackgroundMode(hub, sign_in, lambda user: open_main_window(app, instance, user))
    _rewire(instance.activation_requested, mode.open)
    _rewire(instance.quit_requested, app.quit)
    mode.start()
    if needs_sign_up:
        QTimer.singleShot(0, mode.open)
    logging.info("Running in the background (tray)")
    exit_code = app.exec()
    instance.close()
    return exit_code


def main() -> int:
    """Start the desktop application.

    ``--quit`` asks a running copy to exit (used by the installer);
    ``--self-test [report.json]`` checks that the build opens and is clickable;
    ``--background`` starts hidden in the tray (Windows sign-in), showing the
    login only when the app is opened.
    """
    ensure_standard_streams()
    configure_logging()
    from app.ui.tray import set_windows_app_id

    # Must happen before the first window so Windows attributes toasts and
    # the taskbar button to AkremMobile rather than to python.exe.
    set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("AkremMobile")
    app.setOrganizationName("AkremMobile")
    logging.info("Starting AkremMobile (%s)", " ".join(sys.argv[1:]) or "normal launch")

    from app.ui.single_instance import SingleInstance

    instance = SingleInstance()
    if "--quit" in sys.argv:
        instance.request_quit()
        return 0
    if "--self-test" in sys.argv:
        from app.ui.self_test import run_self_test

        position = sys.argv.index("--self-test")
        report = sys.argv[position + 1] if len(sys.argv) > position + 1 else None
        load_arabic_font()
        return run_self_test(report)
    background = "--background" in sys.argv
    if background and instance.is_running():
        # Started at sign-in while a copy already runs: leave it alone.
        return 0
    if not background and instance.notify_existing():
        # Already running (possibly hidden in the tray): it shows itself.
        return 0
    instance.listen()
    # Before login there is no main window yet: a second launch raises the
    # login/setup dialog, and the installer can still close this copy.
    instance.activation_requested.connect(raise_visible_windows)
    instance.quit_requested.connect(app.quit)

    from app.ui.locale import apply_language, saved_language

    load_arabic_font()
    apply_language(saved_language() or "ar")
    sys.excepthook = show_uncaught_exception

    try:
        from app.db.migrate import upgrade_database
        from app.db.session import dispose_database_engines, session_scope
        from app.services.auth import owner_exists
        from app.services.seed import seed_defaults
        from app.services.settings import get_value

        atexit.register(dispose_database_engines)
        if not wait_for_shop_server():
            return 0
        upgrade_database()
        seed_defaults()
        with session_scope() as session:
            shop_language = get_value(session, "language", None)
            has_owner = owner_exists(session)
        if saved_language() is None and shop_language:
            # First run on this PC: start in the shop's default language.
            apply_language(shop_language)
        user = None if background else sign_in()
    except Exception:
        logging.exception("Database initialization failed")
        QMessageBox.critical(None, ar.ERROR_TITLE, ar.ERROR_BODY)
        return 1

    if background:
        return run_in_background(app, instance, needs_sign_up=not has_owner)
    if user is None:
        return 0
    open_main_window(app, instance, user)
    exit_code = app.exec()
    instance.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
