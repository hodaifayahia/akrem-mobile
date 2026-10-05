"""The owner approves, at the counter, a seller's discount above the allowed percentage."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import session_scope
from app.i18n import ar
from app.services import auth
from app.ui.dialogs.auth_dialogs import InlineError, PasswordField
from app.ui.theme import repolish


class OwnerApprovalDialog(QDialog):
    """Owner username and password; ``owner_id`` is set when they are right."""

    def __init__(self, percent: int, limit: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(ar.DISCOUNT_APPROVAL_TITLE)
        self.setMinimumWidth(420)
        self.owner_id: int | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        heading = QLabel(ar.DISCOUNT_APPROVAL_TITLE, self)
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        message = QLabel(ar.DISCOUNT_APPROVAL_MESSAGE.format(percent=percent, limit=limit), self)
        message.setWordWrap(True)
        layout.addWidget(message)
        self.username = QLineEdit(self)
        self.username.setPlaceholderText(ar.AUTH_USERNAME_PLACEHOLDER)
        self.password = PasswordField(self)
        form = QFormLayout()
        form.addRow(ar.DISCOUNT_APPROVAL_OWNER, self.username)
        form.addRow(ar.PASSWORD, self.password)
        layout.addLayout(form)
        self.error = InlineError(self)
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        self.approve_button = QPushButton(ar.DISCOUNT_APPROVE, self)
        self.approve_button.setProperty("variant", "primary")
        self.approve_button.clicked.connect(self.approve)
        cancel = QPushButton(ar.USER_CANCEL, self)
        cancel.setProperty("variant", "secondary")
        cancel.clicked.connect(self.reject)
        for button in (self.approve_button, cancel):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            repolish(button)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.approve_button)
        layout.addLayout(buttons)
        self.password.returnPressed.connect(self.approve)
        self.username.setFocus()

    def approve(self) -> bool:
        """Check the owner's password (wrong tries count toward the sign-in lockout)."""
        try:
            with session_scope() as session:
                owner = auth.approve_as_owner(
                    session, username=self.username.text(), password=self.password.text()
                )
                owner_id = owner.id if owner is not None else None
        except SQLAlchemyError:
            owner_id = None
        if owner_id is None:
            self.password.clear()
            self.error.show_message(ar.DISCOUNT_APPROVAL_WRONG)
            return False
        self.owner_id = owner_id
        self.accept()
        return True


def ask_owner_approval(percent: int, limit: int, parent: QWidget | None = None) -> int | None:
    """Show the approval box; the approving owner's id, or None when cancelled."""
    dialog = OwnerApprovalDialog(percent, limit, parent)
    dialog.exec()
    return dialog.owner_id
