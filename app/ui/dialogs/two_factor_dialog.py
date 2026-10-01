"""Turn Google Authenticator two-step verification on or off for one account.

Setup shows a QR code (and the same key as text for manual entry); the
account is only protected once the user types a valid 6-digit code, so a
failed scan can never lock anyone out.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import User
from app.db.session import session_scope
from app.i18n import ar
from app.services import auth, qr, two_factor
from app.ui import icons
from app.ui.dialogs.auth_dialogs import InlineError, PasswordField, code_input, normalized_code
from app.ui.widgets.qr_view import QrCodeLabel


class TwoFactorSetupDialog(QDialog):
    """Scan the QR code, then confirm with a first code."""

    def __init__(self, user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.user_id = user.id
        self.setWindowTitle(ar.TFA_SETUP_TITLE)
        self.setMinimumWidth(520)
        with session_scope() as session:
            secret = auth.start_two_factor_setup(session, user.id)
        self.secret = secret
        uri = two_factor.provisioning_uri(secret, user.username)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(10)
        title = QLabel(ar.TFA_SETUP_TITLE, self)
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        for step in (ar.TFA_STEP_1, ar.TFA_STEP_2, ar.TFA_STEP_3):
            label = QLabel(step, self)
            label.setObjectName("notificationBody")
            label.setWordWrap(True)
            layout.addWidget(label)

        self.qr = QrCodeLabel(self)
        self.qr.set_matrix(qr.qr_matrix(uri), 6)
        layout.addWidget(self.qr, 0, Qt.AlignmentFlag.AlignHCenter)

        key_row = QHBoxLayout()
        key_caption = QLabel(ar.TFA_MANUAL_KEY, self)
        key_caption.setObjectName("sectionHint")
        self.key_label = QLabel(two_factor.format_secret(secret), self)
        self.key_label.setObjectName("moneyValue")
        self.key_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        copy = QPushButton(ar.TFA_COPY, self)
        copy.setProperty("variant", "ghost")
        copy.setProperty("compact", True)
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(secret))
        key_row.addWidget(key_caption)
        key_row.addWidget(self.key_label, 1)
        key_row.addWidget(copy)
        layout.addLayout(key_row)

        self.code = code_input(self)
        self.code.returnPressed.connect(self.confirm)
        layout.addWidget(self.code)
        self.error = InlineError(self)
        layout.addWidget(self.error)
        self.code.textEdited.connect(self.error.clear)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(ar.CT_CANCEL, self)
        cancel.setProperty("variant", "secondary")
        cancel.clicked.connect(self.reject)
        self.confirm_button = QPushButton(ar.TFA_ENABLE_CONFIRM, self)
        self.confirm_button.setIcon(icons.icon("check", "#FFFFFF", 16))
        self.confirm_button.clicked.connect(self.confirm)
        buttons.addWidget(cancel)
        buttons.addWidget(self.confirm_button)
        layout.addLayout(buttons)
        self.code.setFocus()

    def confirm(self) -> bool:
        """Verify the typed code; on success two-step verification is on."""
        try:
            with session_scope() as session:
                auth.confirm_two_factor_setup(session, self.user_id, normalized_code(self.code.text()))
        except ValueError:
            self.error.show_message(ar.TFA_INVALID_CODE)
            self.code.selectAll()
            return False
        except (auth.AuthorizationError, SQLAlchemyError):
            self.error.show_message(ar.ERROR_BODY)
            return False
        self.accept()
        return True


class TwoFactorDisableDialog(QDialog):
    """Turn two-step verification off after re-entering the password."""

    def __init__(self, user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.user_id = user.id
        self.setWindowTitle(ar.TFA_DISABLE_TITLE)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        message = QLabel(ar.TFA_DISABLE_PROMPT, self)
        message.setWordWrap(True)
        layout.addWidget(message)
        self.password = PasswordField(self)
        self.password.setPlaceholderText(ar.AUTH_PASSWORD_PLACEHOLDER)
        self.password.returnPressed.connect(self.confirm)
        layout.addWidget(self.password)
        self.error = InlineError(self)
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(ar.CT_CANCEL, self)
        cancel.setProperty("variant", "secondary")
        cancel.clicked.connect(self.reject)
        confirm = QPushButton(ar.TFA_DISABLE, self)
        confirm.setProperty("variant", "danger")
        confirm.clicked.connect(self.confirm)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)

    def confirm(self) -> bool:
        """Disable two-step verification when the password is right."""
        try:
            with session_scope() as session:
                auth.disable_two_factor(session, self.user_id, password=self.password.text())
        except ValueError:
            self.error.show_message(ar.AUTH_REAUTH_FAILED)
            return False
        except (auth.AuthorizationError, SQLAlchemyError):
            self.error.show_message(ar.ERROR_BODY)
            return False
        self.accept()
        return True
