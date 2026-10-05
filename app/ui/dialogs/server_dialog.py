"""Connect this PC to the shop's server (the main PC running PostgreSQL), or go back to local.

The dialog tests the connection, prepares the server's tables, offers to copy
this PC's records the first time (when the server is still empty), saves the
choice for this PC only and then asks to reopen the app, which only reads the
database choice when it starts.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from app.i18n import ar
from app.services import shared_db
from app.services.shared_db import ConnectionCheck, ServerDetails
from app.ui.dialogs.auth_dialogs import InlineError, PasswordField
from app.ui.theme import repolish

_LOG = logging.getLogger(__name__)


def check_text(check: ConnectionCheck) -> str:
    """One sentence describing a connection test."""
    if check.ok:
        state = ar.SERVER_HAS_DATA if check.has_data else ar.SERVER_EMPTY
        return ar.SERVER_TEST_OK.format(version=check.server_version.split(" ")[0], state=state)
    return {
        "unreachable": ar.SERVER_UNREACHABLE,
        "login_failed": ar.SERVER_LOGIN_FAILED,
        "no_database": ar.SERVER_NO_DATABASE,
    }.get(check.code, ar.SERVER_ERROR)


class ServerConnectionDialog(QDialog):
    """Server address and password, test, then "use this server" or "use this PC only"."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(ar.SERVER_TITLE)
        self.setMinimumWidth(500)
        self.saved = False
        current = shared_db.current_server()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        heading = QLabel(ar.SERVER_TITLE, self)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        hint = QLabel(ar.SERVER_HINT, self)
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.host = QLineEdit(self)
        self.host.setPlaceholderText(ar.SERVER_HOST_PLACEHOLDER)
        self.host.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.password = PasswordField(self)
        self.port = QSpinBox(self)
        self.port.setRange(1, 65535)
        self.port.setValue(shared_db.DEFAULT_PORT)
        self.port.setMaximumWidth(120)
        self.database = QLineEdit(shared_db.DEFAULT_DATABASE, self)
        self.user = QLineEdit(shared_db.DEFAULT_USER, self)
        for editor in (self.database, self.user):
            editor.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        if current is not None:
            self.host.setText(current.host)
            self.password.setText(current.password)
            self.port.setValue(current.port)
            self.database.setText(current.database)
            self.user.setText(current.user)
        form = QFormLayout()
        form.addRow(ar.SERVER_HOST, self.host)
        form.addRow(ar.SERVER_PASSWORD, self.password)
        form.addRow(ar.SERVER_PORT, self.port)
        form.addRow(ar.SERVER_DATABASE, self.database)
        form.addRow(ar.SERVER_USER, self.user)
        layout.addLayout(form)

        self.status = QLabel(ar.SERVER_MODE_SERVER.format(host=current.host) if current else ar.SERVER_MODE_LOCAL, self)
        self.status.setObjectName("sectionHint")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.error = InlineError(self)
        layout.addWidget(self.error)

        buttons = QHBoxLayout()
        self.test_button = QPushButton(ar.SERVER_TEST, self)
        self.test_button.setProperty("variant", "secondary")
        self.test_button.clicked.connect(self.run_test)
        self.local_button = QPushButton(ar.SERVER_USE_LOCAL, self)
        self.local_button.setProperty("variant", "secondary")
        self.local_button.setVisible(current is not None)
        self.local_button.clicked.connect(self.use_local)
        self.save_button = QPushButton(ar.SERVER_SAVE, self)
        self.save_button.setProperty("variant", "primary")
        self.save_button.clicked.connect(self.save)
        cancel = QPushButton(ar.USER_CANCEL, self)
        cancel.setProperty("variant", "secondary")
        cancel.clicked.connect(self.reject)
        for button in (self.test_button, self.local_button, self.save_button, cancel):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            repolish(button)
        buttons.addWidget(self.test_button)
        buttons.addWidget(self.local_button)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.host.setFocus()

    def details(self) -> ServerDetails:
        return ServerDetails(
            host=self.host.text().strip(),
            password=self.password.text(),
            port=self.port.value(),
            database=self.database.text().strip() or shared_db.DEFAULT_DATABASE,
            user=self.user.text().strip() or shared_db.DEFAULT_USER,
        )

    def run_test(self) -> ConnectionCheck | None:
        """Test the typed server and show the outcome under the form."""
        self.error.clear()
        if not self.host.text().strip():
            self.error.show_message(ar.SERVER_HOST_REQUIRED)
            return None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            check = shared_db.test_connection(self.details())
        finally:
            QApplication.restoreOverrideCursor()
        if check.ok:
            self.status.setText(check_text(check))
        else:
            _LOG.warning("Server test failed: %s", check.detail)
            self.error.show_message(check_text(check))
        return check

    def save(self) -> bool:
        """Prepare the server, copy this PC's records once if wanted, then use the server from the next start."""
        check = self.run_test()
        if check is None or not check.ok:
            return False
        details = self.details()
        local_data = shared_db.local_has_data()
        copy = False
        if not check.has_data and local_data:
            answer = QMessageBox.question(
                self, ar.SERVER_TITLE, ar.SERVER_COPY_CONFIRM,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            copy = answer == QMessageBox.StandardButton.Yes
        elif check.has_data and local_data:
            answer = QMessageBox.question(
                self, ar.SERVER_TITLE, ar.SERVER_LOCAL_KEPT,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            if copy:
                counts = shared_db.copy_local_data(details)
                _LOG.info("Copied %s rows to the shop server", sum(counts.values()))
            else:
                shared_db.prepare_server(details)
            shared_db.use_server(details)
        except (ValueError, RuntimeError, OSError, SQLAlchemyError):
            _LOG.exception("Connecting to the shop server failed")
            self.error.show_message(ar.SERVER_SAVE_ERROR)
            return False
        finally:
            QApplication.restoreOverrideCursor()
        self.saved = True
        QMessageBox.information(self, ar.SERVER_TITLE, ar.SERVER_SAVED_REOPEN)
        self.accept()
        return True

    def use_local(self) -> bool:
        """Stop using the server on this PC (its own local records come back)."""
        answer = QMessageBox.question(
            self, ar.SERVER_TITLE, ar.SERVER_USE_LOCAL_CONFIRM,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        shared_db.use_this_pc()
        self.saved = True
        QMessageBox.information(self, ar.SERVER_TITLE, ar.SERVER_SAVED_REOPEN)
        self.accept()
        return True


def configure_server_and_quit(parent: QWidget | None = None) -> bool:
    """Open the dialog; when the choice changed, close the app so it reopens on the new database."""
    dialog = ServerConnectionDialog(parent)
    dialog.exec()
    if dialog.saved:
        application = QApplication.instance()
        if application is not None:
            for widget in QApplication.topLevelWidgets():
                quit_window = getattr(widget, "quit_application", None)
                if callable(quit_window):
                    quit_window()  # the main window: exit instead of hiding in the tray
                else:
                    widget.close()
            application.quit()
    return dialog.saved
