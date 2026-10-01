"""First-run owner setup and login dialogs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)
from PySide6.QtGui import QCloseEvent
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import User

from app.config import RESOURCE_DIR
from app.db.session import session_scope
from app.i18n import ar
from app.services.auth import authenticate, create_first_owner


class _PasswordField(QLineEdit):
    """Password field with a paired show/hide checkbox."""

    def __init__(self, parent: QDialog) -> None:
        super().__init__(parent)
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self.visibility = QCheckBox(ar.SHOW_PASSWORD, parent)
        self.visibility.toggled.connect(self._toggle_visibility)

    def _toggle_visibility(self, visible: bool) -> None:
        """Switch between masked and plain-text display."""
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.setEchoMode(mode)


class SetupOwnerDialog(QDialog):
    """Collect the first owner's username and password."""

    def __init__(self, parent: QDialog | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(ar.SETUP_OWNER_TITLE)
        self.setMinimumWidth(440)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.username = QLineEdit(self)
        self.password = _PasswordField(self)
        self.password_confirmation = _PasswordField(self)

        layout = QVBoxLayout(self)
        layout.addWidget(self._logo())
        prompt = QLabel(ar.SETUP_OWNER_PROMPT, self)
        prompt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(prompt)

        fields = QFormLayout()
        fields.addRow(ar.USERNAME, self.username)
        password_row = QHBoxLayout()
        password_row.addWidget(self.password, 1)
        password_row.addWidget(self.password.visibility)
        fields.addRow(ar.PASSWORD, password_row)
        confirmation_row = QHBoxLayout()
        confirmation_row.addWidget(self.password_confirmation, 1)
        confirmation_row.addWidget(self.password_confirmation.visibility)
        fields.addRow(ar.PASSWORD_CONFIRMATION, confirmation_row)
        layout.addLayout(fields)

        self.create_button = QPushButton(f"✨  {ar.CREATE_OWNER_BUTTON}", self)
        self.create_button.setProperty("variant", "primary")
        self.create_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.create_button.clicked.connect(self._create_owner)
        layout.addWidget(self.create_button)

    def _create_owner(self) -> None:
        """Create the owner through the auth service."""
        username = self.username.text().strip()
        password = self.password.text()
        confirmation = self.password_confirmation.text()
        if len(password) < 6:
            self._show_error(ar.PASSWORD_TOO_SHORT)
            return
        if password != confirmation:
            self._show_error(ar.PASSWORD_MISMATCH)
            return
        try:
            with session_scope() as session:
                create_first_owner(
                    session,
                    username=username,
                    password=password,
                    password_confirmation=confirmation,
                )
        except (ValueError, RuntimeError, SQLAlchemyError):
            self._show_error(ar.AUTH_SETUP_ERROR)
            return
        self.accept()

    @staticmethod
    def _logo() -> QLabel:
        """Create a centered brand image for the setup screen."""
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(str(RESOURCE_DIR / "logo.png"))
        label.setPixmap(pixmap.scaled(260, 260, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
        return label

    def _show_error(self, message: str) -> None:
        """Display an Arabic validation error."""
        QMessageBox.warning(self, ar.SETUP_OWNER_TITLE, message)


class LoginDialog(QDialog):
    """Authenticate an owner or seller before opening the main window."""

    def __init__(self, parent: QDialog | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(ar.LOGIN_TITLE)
        self.setMinimumWidth(440)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.user: User | None = None
        self.username = QLineEdit(self)
        self.password = _PasswordField(self)

        layout = QVBoxLayout(self)
        layout.addWidget(SetupOwnerDialog._logo())
        fields = QFormLayout()
        fields.addRow(ar.USERNAME, self.username)
        password_row = QHBoxLayout()
        password_row.addWidget(self.password, 1)
        password_row.addWidget(self.password.visibility)
        fields.addRow(ar.PASSWORD, password_row)
        layout.addLayout(fields)

        self.login_button = QPushButton(f"🚀  {ar.LOGIN_BUTTON}", self)
        self.login_button.setProperty("variant", "primary")
        self.login_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.login_button.clicked.connect(self._login)
        self.password.returnPressed.connect(self._login)
        layout.addWidget(self.login_button)

    def _login(self) -> None:
        """Check credentials and preserve lockout updates."""
        try:
            with session_scope() as session:
                result = authenticate(
                    session,
                    username=self.username.text().strip(),
                    password=self.password.text(),
                )
        except SQLAlchemyError:
            QMessageBox.critical(self, ar.ERROR_TITLE, ar.ERROR_BODY)
            return
        status = getattr(result.status, "value", result.status)
        if status == "success":
            self.user = result.user
            self.accept()
            return
        messages = {
            "invalid_credentials": ar.LOGIN_INVALID,
            "locked": ar.LOGIN_LOCKED,
            "disabled": ar.LOGIN_DISABLED,
        }
        QMessageBox.warning(self, ar.LOGIN_TITLE, messages.get(status, ar.LOGIN_INVALID))
        self.password.clear()
        self.password.setFocus()


class ReauthenticationDialog(QDialog):
    """Require the active account's password after an inactivity timeout."""

    def __init__(self, user: User, parent: QDialog | None = None) -> None:
        super().__init__(parent)
        self.user = user
        self.setWindowTitle(ar.AUTH_REAUTH_TITLE)
        self.setMinimumWidth(440)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 24)
        layout.setSpacing(14)
        layout.addWidget(SetupOwnerDialog._logo())
        prompt = QLabel(ar.AUTH_REAUTH_PROMPT, self)
        prompt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(prompt)
        self.password = _PasswordField(self)
        row = QHBoxLayout()
        row.addWidget(self.password, 1)
        row.addWidget(self.password.visibility)
        layout.addLayout(row)
        self.unlock_button = QPushButton(f"🔓  {ar.LOGIN_BUTTON}", self)
        self.unlock_button.setProperty("variant", "primary")
        self.unlock_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.unlock_button.clicked.connect(self._unlock)
        self.password.returnPressed.connect(self._unlock)
        layout.addWidget(self.unlock_button)

    def _unlock(self) -> None:
        """Verify the locked-in account through the regular lockout service."""
        try:
            with session_scope() as session:
                result = authenticate(
                    session,
                    username=self.user.username,
                    password=self.password.text(),
                )
        except SQLAlchemyError:
            QMessageBox.critical(self, ar.ERROR_TITLE, ar.ERROR_BODY)
            return
        status = getattr(result.status, "value", result.status)
        if status == "success" and result.user is not None and result.user.id == self.user.id:
            self.accept()
            return
        if status == "disabled":
            QMessageBox.warning(self, ar.AUTH_REAUTH_TITLE, ar.LOGIN_DISABLED)
            self.accept()
            QApplication.quit()
            return
        message = ar.LOGIN_LOCKED if status == "locked" else ar.AUTH_REAUTH_FAILED
        QMessageBox.warning(self, ar.AUTH_REAUTH_TITLE, message)
        self.password.clear()
        self.password.setFocus()

    def reject(self) -> None:
        """Keep the application locked until the current user reauthenticates."""
        return

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt callback name
        """Prevent closing the locked application from bypassing authentication."""
        event.ignore()
