"""Application entry point."""

from __future__ import annotations

import logging
import sys
import atexit
from logging.handlers import RotatingFileHandler
from types import TracebackType

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from app.config import RESOURCE_DIR, data_dir, ensure_data_dirs
from app.i18n import ar
from app.ui.main_window import MainWindow


def configure_logging() -> None:
    """Configure rotating logs in the per-user data directory."""
    ensure_data_dirs()
    log_path = data_dir() / "logs" / "akremmobile.log"
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[handler])


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
    QMessageBox.critical(None, ar.ERROR_TITLE, ar.ERROR_BODY)


def main() -> int:
    """Start the desktop application."""
    configure_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("AkremMobile")
    app.setOrganizationName("AkremMobile")

    from app.i18n import get_layout_direction, set_language
    from PySide6.QtCore import QSettings
    saved_lang = QSettings("AkremMobile", "AkremMobile").value("language", "ar")
    set_language(saved_lang)
    app.setLayoutDirection(get_layout_direction())

    load_arabic_font()
    theme_path = RESOURCE_DIR / "theme.qss"
    app.setStyleSheet(theme_path.read_text(encoding="utf-8"))
    sys.excepthook = show_uncaught_exception

    try:
        from app.db.migrate import upgrade_database
        from app.db.session import session_scope
        from app.services.auth import owner_exists
        from app.services.backup import create_automated_checkpoint_backup
        from app.services.seed import seed_defaults
        from app.services.settings import get_value
        from app.db.session import dispose_database_engines
        from app.ui.dialogs.auth_dialogs import LoginDialog, SetupOwnerDialog

        atexit.register(dispose_database_engines)
        upgrade_database()
        seed_defaults()
        with session_scope() as session:
            has_owner = owner_exists(session)
            db_lang = get_value(session, "language", None)
            if db_lang:
                set_language(db_lang)
                app.setLayoutDirection(get_layout_direction())
        if not has_owner:
            setup_dialog = SetupOwnerDialog()
            if setup_dialog.exec() != QDialog.DialogCode.Accepted:
                return 0
        login_dialog = LoginDialog()
        if login_dialog.exec() != QDialog.DialogCode.Accepted:
            return 0
        if login_dialog.user is None:
            return 0
        try:
            create_automated_checkpoint_backup(max_per_day=3, min_interval_seconds=3600)
        except (OSError, ValueError):
            # Back up only after setup and authentication so the daily
            # snapshot contains the owner account and runs after successful login.
            logging.exception("Automatic checkpoint backup could not be created")
    except Exception:
        logging.exception("Database initialization failed")
        QMessageBox.critical(None, ar.ERROR_TITLE, ar.ERROR_BODY)
        return 1

    window = MainWindow(current_user=login_dialog.user)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
