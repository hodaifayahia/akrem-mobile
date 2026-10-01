"""Sign-in, first-run owner setup and inactivity lock screens.

Sign-in and setup share a two-panel layout: a brand panel on the
reading-start side and a form card on the other. Qt mirrors the layout in
Arabic. Errors are shown inline under the form (no pop-up boxes), the
password field has a show/hide toggle and a Caps Lock hint, and the language
can be switched before signing in.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRegularExpression,
    QSequentialAnimationGroup,
    QSize,
    Qt,
)
from PySide6.QtGui import QAction, QCloseEvent, QKeyEvent, QPixmap, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from app.config import RESOURCE_DIR
from app.db.models import User
from app.db.session import session_scope
from app.i18n import SUPPORTED_LANGUAGES, ar, get_language
from app.services import auth as auth_service
from app.services.auth import authenticate, create_first_owner
from app.ui import icons
from app.ui.events import events
from app.ui.theme import add_shadow, repolish


def caps_lock_on() -> bool | None:
    """Return the Caps Lock state on Windows, or ``None`` where it is unknown."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetKeyState(0x14) & 1)
    except (AttributeError, OSError):
        return None


def code_input(parent: QWidget) -> QLineEdit:
    """A large, centred, digits-only field for a 6-digit authenticator code."""
    field = QLineEdit(parent)
    field.setObjectName("codeInput")
    field.setMaxLength(7)
    field.setAlignment(Qt.AlignmentFlag.AlignCenter)
    field.setPlaceholderText("123 456")
    field.setValidator(
        QRegularExpressionValidator(QRegularExpression("[0-9\u0660-\u0669\u06F0-\u06F9 ]{0,7}"), field)
    )
    field.setInputMethodHints(Qt.InputMethodHint.ImhDigitsOnly)
    return field


def normalized_code(text: str) -> str:
    """Western digits only: Arabic-Indic digits typed on an Arabic keyboard are converted."""
    return "".join(str(int(char)) for char in text if char.isdecimal())


class PasswordField(QLineEdit):
    """Password input with a trailing show/hide action and Caps Lock tracking."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("authInput")
        self.setEchoMode(QLineEdit.EchoMode.Password)
        self.toggle_action = QAction(self)
        self.toggle_action.triggered.connect(self._toggle)
        self.addAction(self.toggle_action, QLineEdit.ActionPosition.TrailingPosition)
        self.caps_hint: QLabel | None = None
        self._refresh_toggle()

    def _toggle(self) -> None:
        visible = self.echoMode() == QLineEdit.EchoMode.Password
        self.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self._refresh_toggle()

    def _refresh_toggle(self) -> None:
        hidden = self.echoMode() == QLineEdit.EchoMode.Password
        self.toggle_action.setIcon(icons.icon("eye" if hidden else "eye-off", "text-muted", 18))
        self.toggle_action.setToolTip(ar.AUTH_SHOW_PASSWORD_TIP if hidden else ar.AUTH_HIDE_PASSWORD_TIP)

    def retranslate(self) -> None:
        """Refresh the toggle tooltip and placeholder."""
        self._refresh_toggle()

    def _update_caps_hint(self) -> None:
        if self.caps_hint is not None:
            self.caps_hint.setVisible(bool(caps_lock_on()) and self.hasFocus())

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt callback name
        super().keyReleaseEvent(event)
        self._update_caps_hint()

    def focusInEvent(self, event) -> None:  # noqa: N802 - Qt callback name
        super().focusInEvent(event)
        self._update_caps_hint()

    def focusOutEvent(self, event) -> None:  # noqa: N802 - Qt callback name
        super().focusOutEvent(event)
        self._update_caps_hint()


class InlineError(QFrame):
    """A soft red banner that explains what went wrong, in place."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("authError")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(8)
        icon = QLabel(self)
        icon.setPixmap(icons.pixmap("alert", "failed", 16))
        self.text = QLabel(self)
        self.text.setObjectName("authErrorText")
        self.text.setWordWrap(True)
        row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self.text, 1)
        self.hide()

    def show_message(self, message: str) -> None:
        """Display a message (and make screen readers announce it)."""
        self.text.setText(message)
        self.setAccessibleName(message)
        self.show()

    def clear(self) -> None:
        """Hide the banner."""
        self.text.clear()
        self.hide()


def _logo(size: int) -> QLabel:
    label = QLabel()
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    pixmap = QPixmap(str(RESOURCE_DIR / "logo.png"))
    if not pixmap.isNull():
        label.setPixmap(pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                                      Qt.TransformationMode.SmoothTransformation))
    return label


class _AuthShell(QDialog):
    """Two-panel window shared by sign-in and first-run setup."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("authDialog")
        self.setMinimumSize(900, 580)
        self._feature_labels: list[tuple[QLabel, str]] = []
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_brand_panel(), 5)

        form_side = QWidget(self)
        side = QVBoxLayout(form_side)
        side.setContentsMargins(40, 20, 40, 32)
        side.setSpacing(0)
        top = QHBoxLayout()
        top.addStretch(1)
        self.language_selector = QComboBox(form_side)
        self.language_selector.setObjectName("topBarLangSelector")
        for code, native_name in SUPPORTED_LANGUAGES.items():
            self.language_selector.addItem(icons.icon("globe", "text-muted", 14), native_name, code)
        self.language_selector.setCurrentIndex(max(0, self.language_selector.findData(get_language())))
        self.language_selector.currentIndexChanged.connect(self._on_language_selected)
        top.addWidget(self.language_selector)
        side.addLayout(top)
        side.addStretch(1)

        self.card = QFrame(form_side)
        self.card.setObjectName("authCard")
        self.card.setFixedWidth(400)
        add_shadow(self.card, blur=40, y_offset=12, alpha=170)
        self.form = QVBoxLayout(self.card)
        self.form.setContentsMargins(32, 30, 32, 30)
        self.form.setSpacing(6)
        side.addWidget(self.card, 0, Qt.AlignmentFlag.AlignHCenter)
        side.addStretch(2)
        root.addWidget(form_side, 6)
        events.language_changed.connect(self._on_language_changed)

    def _build_brand_panel(self) -> QFrame:
        panel = QFrame(self)
        panel.setObjectName("brandPanel")
        column = QVBoxLayout(panel)
        column.setContentsMargins(44, 44, 44, 36)
        column.setSpacing(10)
        column.addWidget(_logo(120), 0, Qt.AlignmentFlag.AlignLeading)
        column.addSpacing(14)
        self.brand_headline = QLabel(panel)
        self.brand_headline.setObjectName("brandHeadline")
        self.brand_headline.setWordWrap(True)
        column.addWidget(self.brand_headline)
        self.brand_tagline = QLabel(panel)
        self.brand_tagline.setObjectName("brandTagline")
        column.addWidget(self.brand_tagline)
        column.addSpacing(18)
        for key in ("AUTH_BRAND_FEATURE_1", "AUTH_BRAND_FEATURE_2", "AUTH_BRAND_FEATURE_3"):
            row = QHBoxLayout()
            row.setSpacing(10)
            check = QLabel(panel)
            check.setPixmap(icons.pixmap("check-circle", "primary-glow", 18))
            text = QLabel(panel)
            text.setObjectName("brandFeature")
            text.setWordWrap(True)
            self._feature_labels.append((text, key))
            row.addWidget(check, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(text, 1)
            column.addLayout(row)
        column.addStretch(1)
        wordmark = QLabel("AkremMobile", panel)
        wordmark.setObjectName("wordmark")
        column.addWidget(wordmark)
        return panel

    # ----------------------------------------------------------- form helpers
    def _title(self) -> tuple[QLabel, QLabel]:
        title = QLabel(self.card)
        title.setObjectName("authTitle")
        subtitle = QLabel(self.card)
        subtitle.setObjectName("authSubtitle")
        subtitle.setWordWrap(True)
        self.form.addWidget(title)
        self.form.addWidget(subtitle)
        self.form.addSpacing(18)
        return title, subtitle

    def _field(self, widget: QLineEdit) -> QLabel:
        label = QLabel(self.card)
        label.setObjectName("fieldLabel")
        label.setBuddy(widget)
        self.form.addWidget(label)
        self.form.addWidget(widget)
        self.form.addSpacing(10)
        return label

    def _submit_button(self) -> QPushButton:
        button = QPushButton(self.card)
        button.setObjectName("authSubmit")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setDefault(True)
        button.setIconSize(QSize(18, 18))
        return button

    def _set_busy(self, button: QPushButton, busy: bool, idle_text: str) -> None:
        button.setEnabled(not busy)
        button.setText(ar.AUTH_SIGNING_IN if busy else idle_text)
        if busy:
            QApplication.processEvents()

    def _shake(self) -> None:
        """Nudge the card sideways to signal a rejected attempt."""
        start = self.card.pos()
        group = QSequentialAnimationGroup(self)
        for offset in (10, -8, 6, -3, 0):
            step = QPropertyAnimation(self.card, b"pos", group)
            step.setDuration(45)
            step.setEndValue(start + QPoint(offset, 0))
            step.setEasingCurve(QEasingCurve.Type.OutQuad)
            group.addAnimation(step)
        group.start(QSequentialAnimationGroup.DeletionPolicy.DeleteWhenStopped)

    @staticmethod
    def _mark_invalid(widget: QLineEdit, invalid: bool) -> None:
        widget.setProperty("invalid", invalid)
        repolish(widget)

    # ---------------------------------------------------------------- language
    def _on_language_selected(self, index: int) -> None:
        code = self.language_selector.itemData(index)
        if code and code != get_language():
            from app.ui.locale import change_language

            change_language(code)

    def _on_language_changed(self, code: str) -> None:
        index = self.language_selector.findData(code)
        if index >= 0 and index != self.language_selector.currentIndex():
            self.language_selector.blockSignals(True)
            self.language_selector.setCurrentIndex(index)
            self.language_selector.blockSignals(False)
        self.retranslate()

    def retranslate(self) -> None:
        """Refresh brand-panel text; subclasses refresh their form."""
        self.brand_headline.setText(ar.AUTH_BRAND_HEADLINE)
        self.brand_tagline.setText(ar.BRAND_TAGLINE)
        for label, key in self._feature_labels:
            label.setText(getattr(ar, key))


class SetupOwnerDialog(_AuthShell):
    """Collect the first owner's username and password."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title_label, self.subtitle_label = self._title()
        self.username = QLineEdit(self.card)
        self.username.setObjectName("authInput")
        self.password = PasswordField(self.card)
        self.password_confirmation = PasswordField(self.card)
        self.username_label = self._field(self.username)
        self.password_label = self._field(self.password)
        self.confirmation_label = self._field(self.password_confirmation)
        self.error = InlineError(self.card)
        self.form.addWidget(self.error)
        self.form.addSpacing(6)
        self.create_button = self._submit_button()
        self.create_button.clicked.connect(self._create_owner)
        self.password_confirmation.returnPressed.connect(self._create_owner)
        self.form.addWidget(self.create_button)
        self.retranslate()
        self.username.setFocus()

    def retranslate(self) -> None:
        super().retranslate()
        self.setWindowTitle(ar.SETUP_OWNER_TITLE)
        self.title_label.setText(ar.SETUP_OWNER_TITLE)
        self.subtitle_label.setText(ar.SETUP_OWNER_PROMPT)
        self.username_label.setText(ar.USERNAME)
        self.password_label.setText(ar.PASSWORD)
        self.confirmation_label.setText(ar.PASSWORD_CONFIRMATION)
        self.username.setPlaceholderText(ar.AUTH_USERNAME_PLACEHOLDER)
        self.password.setPlaceholderText(ar.AUTH_PASSWORD_PLACEHOLDER)
        self.password_confirmation.setPlaceholderText(ar.AUTH_PASSWORD_CONFIRM_PLACEHOLDER)
        self.password.retranslate()
        self.password_confirmation.retranslate()
        self.create_button.setText(ar.CREATE_OWNER_BUTTON)

    def _create_owner(self) -> None:
        """Create the owner through the auth service."""
        password = self.password.text()
        confirmation = self.password_confirmation.text()
        self._mark_invalid(self.password, False)
        self._mark_invalid(self.password_confirmation, False)
        if len(password) < 6:
            self._mark_invalid(self.password, True)
            self._fail(ar.PASSWORD_TOO_SHORT)
            return
        if password != confirmation:
            self._mark_invalid(self.password_confirmation, True)
            self._fail(ar.PASSWORD_MISMATCH)
            return
        self._set_busy(self.create_button, True, ar.CREATE_OWNER_BUTTON)
        try:
            with session_scope() as session:
                create_first_owner(
                    session,
                    username=self.username.text().strip(),
                    password=password,
                    password_confirmation=confirmation,
                )
        except (ValueError, RuntimeError, SQLAlchemyError):
            self._set_busy(self.create_button, False, ar.CREATE_OWNER_BUTTON)
            self._fail(ar.AUTH_SETUP_ERROR)
            return
        self._set_busy(self.create_button, False, ar.CREATE_OWNER_BUTTON)
        self.accept()

    def _fail(self, message: str) -> None:
        self.error.show_message(message)
        self._shake()


class LoginDialog(_AuthShell):
    """Authenticate an owner or seller before opening the main window."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.user: User | None = None
        self._pending_user: User | None = None
        card_layout = self.form
        self.password_page = QWidget(self.card)
        self.form = QVBoxLayout(self.password_page)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setSpacing(6)
        self.title_label, self.subtitle_label = self._title()
        self.username = QLineEdit(self.card)
        self.username.setObjectName("authInput")
        self.password = PasswordField(self.card)
        self.username_label = self._field(self.username)
        self.password_label = self._field(self.password)
        self.caps_hint = QLabel(self.card)
        self.caps_hint.setObjectName("capsLockHint")
        self.caps_hint.hide()
        self.password.caps_hint = self.caps_hint
        self.form.addWidget(self.caps_hint)
        self.error = InlineError(self.card)
        self.form.addWidget(self.error)
        self.form.addSpacing(6)
        self.login_button = self._submit_button()
        self.login_button.clicked.connect(self._login)
        self.username.returnPressed.connect(self.password.setFocus)
        self.password.returnPressed.connect(self._login)
        self.username.textEdited.connect(self.error.clear)
        self.password.textEdited.connect(self.error.clear)
        self.form.addWidget(self.login_button)
        card_layout.addWidget(self.password_page)
        card_layout.addWidget(self._build_code_page())
        self.retranslate()
        self.username.setFocus()

    def _build_code_page(self) -> QWidget:
        """Second step for accounts protected by Google Authenticator."""
        self.code_page = QWidget(self.card)
        layout = QVBoxLayout(self.code_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        shield = QLabel(self.code_page)
        shield.setPixmap(icons.pixmap("lock", "primary-glow", 34, stroke=1.6))
        layout.addWidget(shield, 0, Qt.AlignmentFlag.AlignLeading)
        self.code_title = QLabel(self.code_page)
        self.code_title.setObjectName("authTitle")
        self.code_prompt = QLabel(self.code_page)
        self.code_prompt.setObjectName("authSubtitle")
        self.code_prompt.setWordWrap(True)
        layout.addWidget(self.code_title)
        layout.addWidget(self.code_prompt)
        layout.addSpacing(10)
        self.code = code_input(self.code_page)
        self.code.textEdited.connect(self._code_edited)
        self.code.returnPressed.connect(self._verify_code)
        layout.addWidget(self.code)
        self.code_error = InlineError(self.code_page)
        layout.addWidget(self.code_error)
        layout.addSpacing(6)
        self.verify_button = self._submit_button()
        self.verify_button.clicked.connect(self._verify_code)
        layout.addWidget(self.verify_button)
        self.back_button = QPushButton(self.code_page)
        self.back_button.setProperty("variant", "ghost")
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.clicked.connect(self._back_to_password)
        layout.addWidget(self.back_button)
        self.code_page.hide()
        return self.code_page

    def retranslate(self) -> None:
        super().retranslate()
        self.setWindowTitle(ar.LOGIN_TITLE)
        self.title_label.setText(ar.AUTH_WELCOME)
        self.subtitle_label.setText(ar.AUTH_SIGNIN_SUBTITLE)
        self.username_label.setText(ar.USERNAME)
        self.password_label.setText(ar.PASSWORD)
        self.username.setPlaceholderText(ar.AUTH_USERNAME_PLACEHOLDER)
        self.password.setPlaceholderText(ar.AUTH_PASSWORD_PLACEHOLDER)
        self.password.retranslate()
        self.caps_hint.setText(ar.AUTH_CAPS_LOCK)
        self.login_button.setText(ar.LOGIN_BUTTON)
        self.login_button.setIcon(icons.icon("chevron-forward", "#FFFFFF", 18))
        self.login_button.setLayoutDirection(
            Qt.LayoutDirection.LeftToRight if self.isRightToLeft() else Qt.LayoutDirection.RightToLeft
        )
        if hasattr(self, "code_page"):
            self.code_title.setText(ar.TFA_LOGIN_TITLE)
            name = self._pending_user.username if self._pending_user is not None else ""
            self.code_prompt.setText(ar.TFA_LOGIN_PROMPT.format(username=name))
            self.verify_button.setText(ar.TFA_VERIFY)
            self.back_button.setText(ar.TFA_BACK)

    def _login(self) -> None:
        """Check credentials; lockout counters are committed either way."""
        username = self.username.text().strip()
        password = self.password.text()
        self._mark_invalid(self.username, not username)
        self._mark_invalid(self.password, not password)
        if not username or not password:
            self.error.show_message(ar.AUTH_REQUIRED_FIELDS)
            self._shake()
            return
        self._set_busy(self.login_button, True, ar.LOGIN_BUTTON)
        try:
            with session_scope() as session:
                result = authenticate(session, username=username, password=password)
        except SQLAlchemyError:
            self._set_busy(self.login_button, False, ar.LOGIN_BUTTON)
            self.error.show_message(ar.ERROR_BODY)
            return
        self._set_busy(self.login_button, False, ar.LOGIN_BUTTON)
        status = getattr(result.status, "value", result.status)
        if status == "success" and result.user is not None and auth_service.requires_second_factor(result.user):
            self._show_code_page(result.user)
            return
        if status == "success":
            self.user = result.user
            self.accept()
            return
        messages = {
            "invalid_credentials": ar.LOGIN_INVALID,
            "locked": ar.LOGIN_LOCKED,
            "disabled": ar.LOGIN_DISABLED,
        }
        self.error.show_message(messages.get(status, ar.LOGIN_INVALID))
        self._mark_invalid(self.password, True)
        self._shake()
        self.password.clear()
        self.password.setFocus()


    def _show_code_page(self, user: User) -> None:
        self._pending_user = user
        self.password_page.hide()
        self.code_page.show()
        self.code.clear()
        self.code_error.clear()
        self.retranslate()
        self.code.setFocus()

    def _back_to_password(self) -> None:
        self._pending_user = None
        self.code_page.hide()
        self.password_page.show()
        self.password.clear()
        self.password.setFocus()

    def _code_edited(self, text: str) -> None:
        self.code_error.clear()
        if len(normalized_code(text)) == 6:
            self._verify_code()

    def _verify_code(self) -> None:
        """Check the authenticator code; wrong codes count toward the lockout."""
        if self._pending_user is None:
            return
        self._set_busy(self.verify_button, True, ar.TFA_VERIFY)
        try:
            with session_scope() as session:
                result = auth_service.verify_second_factor(
                    session, self._pending_user.id, normalized_code(self.code.text())
                )
        except SQLAlchemyError:
            self._set_busy(self.verify_button, False, ar.TFA_VERIFY)
            self.code_error.show_message(ar.ERROR_BODY)
            return
        self._set_busy(self.verify_button, False, ar.TFA_VERIFY)
        status = getattr(result.status, "value", result.status)
        if status == "success":
            self.user = result.user
            self.accept()
            return
        if status in ("locked", "disabled"):
            self._back_to_password()
            self.error.show_message(ar.LOGIN_LOCKED if status == "locked" else ar.LOGIN_DISABLED)
            return
        self.code_error.show_message(ar.TFA_INVALID_CODE)
        self._shake()
        self.code.selectAll()


class ReauthenticationDialog(QDialog):
    """Require the active account's password after inactivity.

    The dialog cannot be dismissed with Esc or the close button; the only
    ways out are the correct password or quitting the application.
    """

    def __init__(self, user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.user = user
        self.setObjectName("authDialog")
        self.setWindowTitle(ar.AUTH_REAUTH_TITLE)
        self.setMinimumWidth(440)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        card = QFrame(self)
        card.setObjectName("authCard")
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(10)

        avatar = QLabel(user.username[:2].upper(), card)
        avatar.setObjectName("avatar")
        avatar.setFixedSize(52, 52)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(avatar, 0, Qt.AlignmentFlag.AlignHCenter)
        title = QLabel(ar.AUTH_LOCKED_AS.format(username=user.username), card)
        title.setObjectName("authTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        prompt = QLabel(ar.AUTH_REAUTH_PROMPT, card)
        prompt.setObjectName("authSubtitle")
        prompt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        layout.addSpacing(6)

        self.password = PasswordField(card)
        self.password.setPlaceholderText(ar.AUTH_PASSWORD_PLACEHOLDER)
        layout.addWidget(self.password)
        self.caps_hint = QLabel(ar.AUTH_CAPS_LOCK, card)
        self.caps_hint.setObjectName("capsLockHint")
        self.caps_hint.hide()
        self.password.caps_hint = self.caps_hint
        layout.addWidget(self.caps_hint)
        self.error = InlineError(card)
        layout.addWidget(self.error)

        self.unlock_button = QPushButton(ar.LOGIN_BUTTON, card)
        self.unlock_button.setObjectName("authSubmit")
        self.unlock_button.setIcon(icons.icon("unlock", "#FFFFFF", 18))
        self.unlock_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.unlock_button.clicked.connect(self._unlock)
        self.password.returnPressed.connect(self._unlock)
        self.password.textEdited.connect(self.error.clear)
        layout.addWidget(self.unlock_button)

        self.quit_button = QPushButton(ar.AUTH_LOCK_QUIT, card)
        self.quit_button.setProperty("variant", "ghost")
        self.quit_button.setIcon(icons.icon("power", "text-muted", 16))
        self.quit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.quit_button.clicked.connect(self._quit_application)
        layout.addWidget(self.quit_button)
        self.password.setFocus()

    def _unlock(self) -> None:
        """Verify the locked-in account through the regular lockout service."""
        self.unlock_button.setEnabled(False)
        try:
            with session_scope() as session:
                result = authenticate(session, username=self.user.username, password=self.password.text())
        except SQLAlchemyError:
            self.unlock_button.setEnabled(True)
            self.error.show_message(ar.ERROR_BODY)
            return
        self.unlock_button.setEnabled(True)
        status = getattr(result.status, "value", result.status)
        if status == "success" and result.user is not None and result.user.id == self.user.id:
            self.accept()
            return
        if status == "disabled":
            self.error.show_message(ar.LOGIN_DISABLED)
            self._quit_application()
            return
        self.error.show_message(ar.AUTH_LOCKED_RETRY if status == "locked" else ar.AUTH_REAUTH_FAILED)
        self.password.clear()
        self.password.setFocus()

    def _quit_application(self) -> None:
        """Leave the app entirely (never back into the unlocked window)."""
        parent = self.parent()
        if parent is not None and hasattr(parent, "quit_application"):
            QDialog.done(self, QDialog.DialogCode.Rejected)
            parent.quit_application()
            return
        QDialog.done(self, QDialog.DialogCode.Rejected)
        QApplication.quit()

    def reject(self) -> None:
        """Ignore Esc so the lock can't be dismissed without a password."""
        return

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt callback name
        """Ignore the window close button while locked."""
        event.ignore()
