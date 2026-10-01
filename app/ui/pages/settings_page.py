"""Owner settings and password management page."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.config import database_url
from app.db.models import User
from app.db.session import session_scope
from app.i18n import ar
from app.services import auth, backup, calc, settings
from app.ui.events import events


class _UserCredentialsDialog(QDialog):
    """Collect credentials for seller creation or owner password reset."""

    def __init__(
        self,
        *,
        title: str,
        username: str | None = None,
        with_role: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(410)
        self.role = QComboBox(self)
        self.role.addItem(ar.ROLE_SELLER, "seller")
        self.role.addItem(ar.ROLE_OWNER, "owner")
        self.role.setVisible(with_role)
        self.username = QLineEdit(self)
        self.username.setText(username or "")
        self.username.setVisible(username is None)
        self.password = self._password_field(self)
        self.confirmation = self._password_field(self)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        if username is None:
            form.addRow(ar.USER_USERNAME, self.username)
        else:
            identity = QLabel(username, self)
            form.addRow(ar.USER_USERNAME, identity)
        if with_role:
            form.addRow(ar.USER_ROLE, self.role)
        form.addRow(ar.USER_NEW_PASSWORD, self.password)
        form.addRow(ar.USER_PASSWORD_CONFIRMATION, self.confirmation)
        layout.addLayout(form)
        buttons = QDialogButtonBox(self)
        self.save_button = buttons.addButton(ar.USER_SAVE, QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setProperty("variant", "primary")
        self.cancel_button = buttons.addButton(ar.USER_CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        self.cancel_button.setProperty("variant", "secondary")
        self.save_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _password_field(parent: QWidget) -> QLineEdit:
        """Return a masked password entry."""
        field = QLineEdit(parent)
        field.setEchoMode(QLineEdit.EchoMode.Password)
        return field


class SettingsPage(QWidget):
    """Configure installment rules and accounts, with service-side role checks."""

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self._is_owner = current_user.role == "owner"
        self._build_ui()
        if self._is_owner:
            try:
                self._load_owner_settings()
                self._refresh_backups()
                self._refresh_sellers()
            except (auth.AuthorizationError, SQLAlchemyError):
                self._set_owner_access(False)
        else:
            self.owner_settings_group.hide()
            self.backup_group.hide()
            self.user_group.hide()
            self.owner_access_label.hide()
        self._update_seller_actions()

    def _build_ui(self) -> None:
        """Build the page sections and scrollable settings form."""
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(14)
        title = QLabel(
            ar.SET_TITLE if self._is_owner else ar.SET_PASSWORD_SECTION,
            self,
        )
        title.setObjectName("pageTitle")
        root.addWidget(title)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        body = QWidget(scroll)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(4, 6, 4, 8)
        body_layout.setSpacing(14)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        self.owner_access_label = QLabel(ar.SET_OWNER_ONLY, body)
        self.owner_access_label.setVisible(False)
        body_layout.addWidget(self.owner_access_label)
        self.owner_settings_group = self._build_owner_settings(body)
        self.backup_group = self._build_backup_section(body)
        self.user_group = self._build_user_management(body)
        if self._is_owner:
            body_layout.addWidget(self.owner_settings_group)
            body_layout.addWidget(self.backup_group)
            body_layout.addWidget(self.user_group)

        self.password_group = self._build_password_section(body)
        body_layout.addWidget(self.password_group)
        self.security_group = self._build_two_factor_section(body)
        body_layout.addWidget(self.security_group)
        body_layout.addStretch(1)

    def _build_owner_settings(self, parent: QWidget) -> QGroupBox:
        """Create owner-only installment and inactivity settings controls."""
        group = QGroupBox(ar.SET_BUSINESS_SECTION, parent)
        layout = QVBoxLayout(group)
        form = QFormLayout()
        self.due_mode = QComboBox(group)
        self.due_mode.addItem(ar.SET_DUE_FIRST_OF_MONTH, "first_of_month")
        self.due_mode.addItem(ar.SET_DUE_PURCHASE_DAY, "purchase_day")
        self.grace_days = QSpinBox(group)
        self.grace_days.setRange(0, 365)
        self.inactivity_minutes = QSpinBox(group)
        self.inactivity_minutes.setRange(1, 180)
        self.language_selector = QComboBox(group)
        self.language_selector.addItem(f"🇩🇿  {ar.LANGUAGE_ARABIC}", "ar")
        self.language_selector.addItem(f"🇬🇧  {ar.LANGUAGE_ENGLISH}", "en")
        self.language_selector.addItem(f"🇫🇷  {ar.LANGUAGE_FRENCH}", "fr")
        form.addRow(ar.LANGUAGE_LABEL, self.language_selector)
        form.addRow(ar.SET_DUE_MODE, self.due_mode)
        form.addRow(ar.SET_GRACE_DAYS, self.grace_days)
        form.addRow(ar.SET_INACTIVITY_MINUTES, self.inactivity_minutes)
        layout.addLayout(form)

        presets_heading = QLabel(ar.SET_RATE_PRESETS, group)
        presets_heading.setObjectName("pageTitle")
        layout.addWidget(presets_heading)
        self.rate_table = QTableWidget(0, 2, group)
        self.rate_table.setHorizontalHeaderLabels([ar.SET_RATE_MONTHS, ar.SET_RATE_PERCENT])
        self.rate_table.verticalHeader().setVisible(False)
        self.rate_table.horizontalHeader().setStretchLastSection(True)
        self.rate_table.setMaximumHeight(250)
        layout.addWidget(self.rate_table)

        rate_actions = QHBoxLayout()
        self.add_rate_button = QPushButton(f"➕  {ar.SET_RATE_ADD}", group)
        self.add_rate_button.setProperty("variant", "secondary")
        self.add_rate_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_rate_button = QPushButton(f"🗑️  {ar.SET_RATE_REMOVE}", group)
        self.remove_rate_button.setProperty("variant", "danger")
        self.remove_rate_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_rate_button.clicked.connect(self._add_rate_row)
        self.remove_rate_button.clicked.connect(self._remove_rate_row)
        rate_actions.addWidget(self.add_rate_button)
        rate_actions.addWidget(self.remove_rate_button)
        rate_actions.addStretch(1)
        layout.addLayout(rate_actions)
        self.save_settings_button = QPushButton(f"💾  {ar.SET_SAVE}", group)
        self.save_settings_button.setProperty("variant", "primary")
        self.save_settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_settings_button.clicked.connect(self._save_owner_settings)
        layout.addWidget(self.save_settings_button, alignment=Qt.AlignmentFlag.AlignLeft)
        return group

    def _build_user_management(self, parent: QWidget) -> QGroupBox:
        """Owner-only list of every account (owners and sellers) and its actions."""
        group = QGroupBox(ar.USER_MANAGEMENT_TITLE, parent)
        layout = QVBoxLayout(group)
        hint = QLabel(ar.USER_MANAGEMENT_HINT, group)
        hint.setObjectName("sectionHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.seller_table = QTableWidget(0, 4, group)
        self.seller_table.setHorizontalHeaderLabels(
            [ar.USER_USERNAME, ar.USER_ROLE, ar.SET_USER_STATUS, ar.TFA_COLUMN]
        )
        self.seller_table.verticalHeader().setVisible(False)
        self.seller_table.horizontalHeader().setStretchLastSection(True)
        self.seller_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.seller_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.seller_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.seller_table.setMaximumHeight(250)
        self.seller_table.itemSelectionChanged.connect(self._update_seller_actions)
        layout.addWidget(self.seller_table)

        actions = QHBoxLayout()
        self.add_seller_button = QPushButton(ar.USER_ADD, group)
        self.add_seller_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_seller_button = QPushButton(ar.USER_DISABLE, group)
        self.toggle_seller_button.setProperty("variant", "warning")
        self.toggle_seller_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_seller_button = QPushButton(ar.USER_RESET_PASSWORD, group)
        self.reset_seller_button.setProperty("variant", "secondary")
        self.reset_seller_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_tfa_button = QPushButton(ar.TFA_RESET, group)
        self.reset_tfa_button.setProperty("variant", "secondary")
        self.reset_tfa_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_seller_button.clicked.connect(self._create_seller)
        self.toggle_seller_button.clicked.connect(self._toggle_seller)
        self.reset_seller_button.clicked.connect(self._reset_seller_password)
        self.reset_tfa_button.clicked.connect(self._reset_user_two_factor)
        for button in (self.add_seller_button, self.toggle_seller_button,
                       self.reset_seller_button, self.reset_tfa_button):
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addLayout(actions)
        return group

    def _build_backup_section(self, parent: QWidget) -> QGroupBox:
        """Create owner-only manual backup and restore controls."""
        group = QGroupBox(f"💾  {ar.SET_BACKUP_SECTION}", parent)
        layout = QVBoxLayout(group)
        self.backup_support_label = QLabel(ar.SET_BACKUP_LOCAL_ONLY, group)
        self.backup_support_label.setWordWrap(True)
        layout.addWidget(self.backup_support_label)

        status_box = QFrame(group)
        status_box.setStyleSheet(
            "QFrame { background-color: #0E1A2E; border: 1px solid #1D3357; border-radius: 8px; }"
        )
        status_layout = QHBoxLayout(status_box)
        status_layout.setContentsMargins(12, 8, 12, 8)
        self.backup_auto_badge = QLabel(status_box)
        self.backup_auto_badge.setStyleSheet(
            "font-weight: 700; color: #60A5FA; font-size: 13px;"
        )
        status_layout.addWidget(self.backup_auto_badge)
        status_layout.addStretch(1)
        layout.addWidget(status_box)

        folder_row = QHBoxLayout()
        folder_caption = QLabel(ar.BACKUP_FOLDER, group)
        folder_caption.setObjectName("fieldLabel")
        self.backup_folder_label = QLabel(group)
        self.backup_folder_label.setObjectName("notificationBody")
        self.backup_folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.backup_folder_label.setWordWrap(True)
        self.change_backup_folder_button = QPushButton(ar.BACKUP_FOLDER_CHANGE, group)
        self.change_backup_folder_button.setProperty("variant", "secondary")
        self.change_backup_folder_button.clicked.connect(self._choose_backup_folder)
        self.default_backup_folder_button = QPushButton(ar.BACKUP_FOLDER_DEFAULT, group)
        self.default_backup_folder_button.setProperty("variant", "ghost")
        self.default_backup_folder_button.clicked.connect(lambda: self.set_backup_folder(None))
        self.open_backup_folder_button = QPushButton(ar.BACKUP_FOLDER_OPEN, group)
        self.open_backup_folder_button.setProperty("variant", "ghost")
        self.open_backup_folder_button.clicked.connect(self._open_backup_folder)
        folder_row.addWidget(folder_caption)
        folder_row.addWidget(self.backup_folder_label, 1)
        folder_row.addWidget(self.open_backup_folder_button)
        folder_row.addWidget(self.default_backup_folder_button)
        folder_row.addWidget(self.change_backup_folder_button)
        layout.addLayout(folder_row)
        folder_hint = QLabel(ar.BACKUP_FOLDER_HINT, group)
        folder_hint.setObjectName("sectionHint")
        folder_hint.setWordWrap(True)
        layout.addWidget(folder_hint)

        self.backup_table = QTableWidget(0, 4, group)
        self.backup_table.setHorizontalHeaderLabels(
            [ar.SET_BACKUP_CREATED_AT, ar.SET_BACKUP_TYPE, ar.SET_BACKUP_FILE, ar.SET_BACKUP_SIZE]
        )
        self.backup_table.verticalHeader().setVisible(False)
        self.backup_table.horizontalHeader().setStretchLastSection(True)
        self.backup_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.backup_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.backup_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.backup_table.setMaximumHeight(220)
        self.backup_table.currentItemChanged.connect(self._update_backup_actions)
        layout.addWidget(self.backup_table)

        self.backup_empty_label = QLabel(ar.SET_BACKUP_LIST_EMPTY, group)
        self.backup_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.backup_empty_label)

        actions = QHBoxLayout()
        self.create_backup_button = QPushButton(f"💾  {ar.SET_BACKUP_CREATE}", group)
        self.create_backup_button.setProperty("variant", "primary")
        self.create_backup_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.upload_backup_button = QPushButton(f"📤  {ar.SET_BACKUP_UPLOAD}", group)
        self.upload_backup_button.setProperty("variant", "secondary")
        self.upload_backup_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_backup_button = QPushButton(f"🔄  {ar.SET_BACKUP_REFRESH}", group)
        self.refresh_backup_button.setProperty("variant", "secondary")
        self.refresh_backup_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.restore_backup_button = QPushButton(f"⚠️  {ar.SET_BACKUP_RESTORE}", group)
        self.restore_backup_button.setProperty("variant", "danger")
        self.restore_backup_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.create_backup_button.clicked.connect(self._create_backup)
        self.upload_backup_button.clicked.connect(self._upload_backup)
        self.refresh_backup_button.clicked.connect(self._refresh_backups)
        self.restore_backup_button.clicked.connect(self._restore_selected_backup)
        actions.addWidget(self.create_backup_button)
        actions.addWidget(self.upload_backup_button)
        actions.addWidget(self.refresh_backup_button)
        actions.addStretch(1)
        actions.addWidget(self.restore_backup_button)
        layout.addLayout(actions)
        return group

    def _build_password_section(self, parent: QWidget) -> QGroupBox:
        """Create the self-service password change form for all active users."""
        group = QGroupBox(f"🔒  {ar.SET_PASSWORD_SECTION}", parent)
        layout = QVBoxLayout(group)
        form = QFormLayout()
        self.current_password = _UserCredentialsDialog._password_field(group)
        self.new_password = _UserCredentialsDialog._password_field(group)
        self.new_password_confirmation = _UserCredentialsDialog._password_field(group)
        form.addRow(ar.SET_CURRENT_PASSWORD, self.current_password)
        form.addRow(ar.SET_NEW_PASSWORD, self.new_password)
        form.addRow(ar.SET_PASSWORD_CONFIRM, self.new_password_confirmation)
        layout.addLayout(form)
        self.change_password_button = QPushButton(f"🔒  {ar.SET_CHANGE_PASSWORD}", group)
        self.change_password_button.setProperty("variant", "primary")
        self.change_password_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.change_password_button.clicked.connect(self._change_own_password)
        layout.addWidget(self.change_password_button, alignment=Qt.AlignmentFlag.AlignLeft)
        return group

    def _build_two_factor_section(self, parent: QWidget) -> QGroupBox:
        """Google Authenticator two-step verification for the signed-in account."""
        group = QGroupBox(ar.TFA_SECTION, parent)
        layout = QVBoxLayout(group)
        explanation = QLabel(ar.TFA_EXPLANATION, group)
        explanation.setObjectName("sectionHint")
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        row = QHBoxLayout()
        self.tfa_status = QLabel(group)
        row.addWidget(self.tfa_status)
        row.addStretch(1)
        self.tfa_enable_button = QPushButton(ar.TFA_ENABLE, group)
        self.tfa_enable_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tfa_enable_button.clicked.connect(self._enable_two_factor)
        self.tfa_disable_button = QPushButton(ar.TFA_DISABLE, group)
        self.tfa_disable_button.setProperty("variant", "secondary")
        self.tfa_disable_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tfa_disable_button.clicked.connect(self._disable_two_factor)
        row.addWidget(self.tfa_enable_button)
        row.addWidget(self.tfa_disable_button)
        layout.addLayout(row)
        self._refresh_two_factor()
        return group

    def _refresh_two_factor(self) -> None:
        with session_scope() as session:
            user = session.get(User, self.current_user.id)
            enabled = user is not None and auth.requires_second_factor(user)
        self.tfa_status.setText(ar.TFA_STATUS_ON if enabled else ar.TFA_STATUS_OFF)
        self.tfa_status.setProperty("pill", "paid" if enabled else "pending")
        self.tfa_status.style().unpolish(self.tfa_status)
        self.tfa_status.style().polish(self.tfa_status)
        self.tfa_enable_button.setVisible(not enabled)
        self.tfa_disable_button.setVisible(enabled)

    def _enable_two_factor(self) -> None:
        from app.ui.dialogs.two_factor_dialog import TwoFactorSetupDialog

        if TwoFactorSetupDialog(self.current_user, self).exec() == QDialog.DialogCode.Accepted:
            events.notify.emit("success", ar.TFA_SECTION, ar.TFA_ENABLED_DONE, 4000)
        self._refresh_two_factor()
        if self._is_owner:
            self._refresh_sellers()

    def _disable_two_factor(self) -> None:
        from app.ui.dialogs.two_factor_dialog import TwoFactorDisableDialog

        if TwoFactorDisableDialog(self.current_user, self).exec() == QDialog.DialogCode.Accepted:
            events.notify.emit("success", ar.TFA_SECTION, ar.TFA_DISABLED_DONE, 4000)
        self._refresh_two_factor()
        if self._is_owner:
            self._refresh_sellers()

    def _load_owner_settings(self) -> None:
        """Load protected settings after verifying the current account in DB."""
        with session_scope() as session:
            auth.require_owner(session, self.current_user.id)
            from app.i18n import get_language
            lang = get_language()
            due_mode = settings.get_value(session, "due_mode", "first_of_month")
            grace_days = settings.get_value(session, "grace_days", 5)
            inactivity_minutes = settings.get_value(session, "inactivity_minutes", 15)
            presets = settings.get_rate_presets(session)
        lang_idx = self.language_selector.findData(lang)
        if lang_idx >= 0:
            self.language_selector.setCurrentIndex(lang_idx)
        self.due_mode.setCurrentIndex(max(0, self.due_mode.findData(due_mode)))
        self.grace_days.setValue(self._bounded_int(grace_days, 5, 0, 365))
        self.inactivity_minutes.setValue(self._bounded_int(inactivity_minutes, 15, 1, 180))
        self.rate_table.setRowCount(0)
        for months, rate in sorted(presets.items()):
            self._append_rate_row(months, rate)

    def _save_owner_settings(self) -> None:
        """Validate and save owner-only business settings transactionally."""
        try:
            presets = self._read_rate_presets()
            new_lang = self.language_selector.currentData()
            with session_scope() as session:
                auth.require_owner(session, self.current_user.id)
                settings.set_value(session, "language", new_lang)
                settings.set_value(session, "due_mode", self.due_mode.currentData())
                settings.set_value(session, "grace_days", self.grace_days.value())
                settings.set_value(session, "inactivity_minutes", self.inactivity_minutes.value())
                settings.set_value(
                    session,
                    "rate_presets",
                    {str(months): rate for months, rate in sorted(presets.items())},
                )
        except ValueError as error:
            self._show_error(self._localized_error(error, settings=True))
            return
        except auth.AuthorizationError:
            self._show_error(ar.SET_OWNER_ONLY)
            return
        except SQLAlchemyError:
            self._show_error(ar.SET_SAVE_ERROR)
            return

        events.data_changed.emit()
        events.settings_changed.emit()
        events.notify.emit("success", ar.SET_TITLE, ar.SET_SAVED, 4000)
        from app.i18n import get_language
        if new_lang and new_lang != get_language():
            # Switching language rebuilds every page, including this one, so
            # it must be the last thing this slot does.
            from app.ui.locale import change_language

            change_language(new_lang)

    def _read_rate_presets(self) -> dict[int, int]:
        """Read and validate editable months-to-rate rows."""
        presets: dict[int, int] = {}
        for row in range(self.rate_table.rowCount()):
            months_item = self.rate_table.item(row, 0)
            rate_item = self.rate_table.item(row, 1)
            try:
                months = int(months_item.text().strip()) if months_item else 0
                rate = int(rate_item.text().strip()) if rate_item else -1
            except ValueError as error:
                raise ValueError(ar.SET_RATE_INVALID) from error
            if not 1 <= months <= calc.MAX_PLAN_MONTHS or not 0 <= rate <= 100:
                raise ValueError(ar.SET_RATE_INVALID)
            if months in presets:
                raise ValueError(ar.SET_RATE_DUPLICATE_MONTH)
            presets[months] = rate
        if not presets:
            raise ValueError(ar.SET_RATE_INVALID)
        return presets

    def _append_rate_row(self, months: int, rate: int) -> None:
        """Append one editable months/rate pair."""
        row = self.rate_table.rowCount()
        self.rate_table.insertRow(row)
        self.rate_table.setItem(row, 0, QTableWidgetItem(str(months)))
        self.rate_table.setItem(row, 1, QTableWidgetItem(str(rate)))

    def _add_rate_row(self) -> None:
        """Add the first unused supported installment duration."""
        existing = {
            self.rate_table.item(row, 0).text().strip()
            for row in range(self.rate_table.rowCount())
            if self.rate_table.item(row, 0) is not None
        }
        months = next(
            (value for value in range(1, calc.MAX_PLAN_MONTHS + 1) if str(value) not in existing), None
        )
        if months is None:
            self._show_error(ar.SET_RATE_INVALID)
            return
        self._append_rate_row(months, 0)
        self.rate_table.selectRow(self.rate_table.rowCount() - 1)

    def _remove_rate_row(self) -> None:
        """Remove the currently selected rate preset."""
        row = self.rate_table.currentRow()
        if row >= 0:
            self.rate_table.removeRow(row)

    def _refresh_sellers(self) -> None:
        """Load every account through the owner-guarded service method."""
        with session_scope() as session:
            auth.require_owner(session, self.current_user.id)
            users = auth.list_users(session, self.current_user.id)
        self.seller_table.setRowCount(len(users))
        for row, user in enumerate(users):
            username = QTableWidgetItem(user.username)
            username.setData(Qt.ItemDataRole.UserRole, user.id)
            username.setData(Qt.ItemDataRole.UserRole + 1, bool(user.disabled))
            self.seller_table.setItem(row, 0, username)
            role = ar.ROLE_OWNER if user.role == "owner" else ar.ROLE_SELLER
            self.seller_table.setItem(row, 1, QTableWidgetItem(role))
            state_text = ar.SET_USER_DISABLED if user.disabled else ar.SET_USER_ACTIVE
            self.seller_table.setItem(row, 2, QTableWidgetItem(state_text))
            tfa = ar.TFA_ON if auth.requires_second_factor(user) else ar.TFA_OFF
            self.seller_table.setItem(row, 3, QTableWidgetItem(tfa))
        self._update_seller_actions()

    def _choose_backup_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, ar.BACKUP_FOLDER_CHANGE, str(backup.backup_directory())
        )
        if chosen:
            self.set_backup_folder(chosen)

    def set_backup_folder(self, folder: str | None) -> bool:
        """Store where backups go on this PC (``None`` = default folder)."""
        try:
            backup.set_backup_directory(folder)
        except backup.BackupLocationError:
            self._show_error(ar.BACKUP_FOLDER_ERROR)
            return False
        self._refresh_backups()
        events.notify.emit(
            "success", ar.SET_BACKUP_SECTION,
            ar.BACKUP_FOLDER_SAVED.format(path=backup.backup_directory()), 4000,
        )
        return True

    def _open_backup_folder(self) -> None:
        folder = backup.backup_directory()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _refresh_backups(self) -> None:
        """List local backup files after checking owner access and backend support."""
        if hasattr(self, "backup_folder_label"):
            folder = backup.backup_directory()
            self.backup_folder_label.setText(str(folder))
            self.default_backup_folder_button.setEnabled(folder != backup.default_backup_directory())
        self._backup_supported = self._supports_local_backups()
        self.create_backup_button.setEnabled(self._backup_supported)
        self.upload_backup_button.setEnabled(self._backup_supported)
        self.refresh_backup_button.setEnabled(self._backup_supported)
        self.restore_backup_button.setEnabled(False)
        self.backup_table.setRowCount(0)
        if not self._backup_supported:
            self.backup_empty_label.hide()
            return
        try:
            with session_scope() as session:
                auth.require_owner(session, self.current_user.id)
            backups = backup.list_backups()
            auto_count = backup.automated_backups_count()
        except auth.AuthorizationError:
            self._show_error(ar.SET_OWNER_ONLY)
            return
        except OSError:
            self._show_backup_error(ar.SET_BACKUP_RESTORE_ERROR)
            return

        if hasattr(self, "backup_auto_badge"):
            if auto_count >= 3:
                self.backup_auto_badge.setText(
                    f"🛡️  {ar.SET_BACKUP_AUTO_BADGE.format(count=3)} - {ar.SET_BACKUP_AUTO_COMPLETE}"
                )
                self.backup_auto_badge.setStyleSheet(
                    "font-weight: 700; color: #34D399; font-size: 13px;"
                )
            else:
                self.backup_auto_badge.setText(
                    f"🛡️  {ar.SET_BACKUP_AUTO_BADGE.format(count=auto_count)} (المستهدف: 3 نسخ يومياً)"
                )
                self.backup_auto_badge.setStyleSheet(
                    "font-weight: 700; color: #60A5FA; font-size: 13px;"
                )

        self.backup_table.setRowCount(len(backups))
        for row, info in enumerate(backups):
            created = QTableWidgetItem(info.created_at.strftime("%d/%m/%Y %H:%M:%S"))
            created.setData(Qt.ItemDataRole.UserRole, str(info.path))
            self.backup_table.setItem(row, 0, created)

            type_label = ar.SET_BACKUP_TYPE_MANUAL
            if info.backup_type == "auto":
                type_label = f"🔄 {ar.SET_BACKUP_TYPE_AUTO}"
            elif info.backup_type == "uploaded":
                type_label = f"📤 {ar.SET_BACKUP_TYPE_UPLOADED}"
            elif info.backup_type == "pre_upgrade":
                type_label = f"🛡️ {ar.SET_BACKUP_TYPE_PRE_UPGRADE}"
            type_item = QTableWidgetItem(type_label)
            self.backup_table.setItem(row, 1, type_item)

            self.backup_table.setItem(row, 2, QTableWidgetItem(info.path.name))
            size_mb = info.size_bytes / (1024 * 1024)
            self.backup_table.setItem(row, 3, QTableWidgetItem(f"{size_mb:.2f} MB"))
        self.backup_empty_label.setVisible(not backups)
        self._update_backup_actions()

    def _upload_backup(self) -> None:
        """Allow the owner to upload/import an external SQLite backup file."""
        if not self._supports_local_backups():
            self._show_backup_error(ar.SET_BACKUP_LOCAL_ONLY)
            return
        try:
            with session_scope() as session:
                auth.require_owner(session, self.current_user.id)
        except auth.AuthorizationError:
            self._show_error(ar.SET_OWNER_ONLY)
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            ar.SET_BACKUP_UPLOAD_TITLE,
            "",
            ar.SET_BACKUP_FILE_FILTER,
        )
        if not file_path:
            return

        try:
            imported_path = backup.import_uploaded_backup(file_path)
        except (ValueError, OSError) as error:
            self._show_backup_error(f"{ar.SET_BACKUP_INVALID_FILE}\n({error})")
            return

        self._refresh_backups()

        reply = QMessageBox.question(
            self,
            ar.SET_BACKUP_SECTION,
            ar.SET_BACKUP_UPLOAD_RESTORE_PROMPT,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                # Pre-restore safety backup of active database
                backup.create_backup()
                backup.restore_backup(imported_path)
            except Exception as error:
                self._show_backup_error(self._backup_error_text(error, restore=True))
                return

            QMessageBox.information(
                self,
                ar.SET_BACKUP_SECTION,
                ar.SET_BACKUP_RESTORE_SUCCESS,
            )
            self.window().close()
            application = QApplication.instance()
            if application is not None:
                application.quit()
        else:
            QMessageBox.information(
                self,
                ar.SET_BACKUP_SECTION,
                ar.SET_BACKUP_UPLOAD_SUCCESS,
            )

    def _create_backup(self) -> None:
        """Create an on-demand local SQLite backup through the backup service."""
        if not self._supports_local_backups():
            self._show_backup_error(ar.SET_BACKUP_LOCAL_ONLY)
            return
        try:
            with session_scope() as session:
                auth.require_owner(session, self.current_user.id)
            backup_path = backup.create_backup()
        except auth.AuthorizationError:
            self._show_error(ar.SET_OWNER_ONLY)
            return
        except (ValueError, OSError, SQLAlchemyError) as error:
            self._show_backup_error(self._backup_error_text(error, restore=False))
            return
        self._refresh_backups()
        QMessageBox.information(
            self,
            ar.SET_BACKUP_SECTION,
            f"{ar.SET_BACKUP_CREATED}\n{backup_path.name}",
        )

    def _restore_selected_backup(self) -> None:
        """Validate, confirm, and restore the selected backup, then exit."""
        if not self._supports_local_backups():
            self._show_backup_error(ar.SET_BACKUP_LOCAL_ONLY)
            return
        backup_path = self._selected_backup_path()
        if backup_path is None:
            self._show_error(ar.SET_BACKUP_SELECT)
            return
        try:
            with session_scope() as session:
                auth.require_owner(session, self.current_user.id)
            backup.validate_backup(backup_path)
        except auth.AuthorizationError:
            self._show_error(ar.SET_OWNER_ONLY)
            return
        except (ValueError, OSError, SQLAlchemyError) as error:
            self._show_backup_error(self._backup_error_text(error, restore=True))
            return

        confirmation = QMessageBox.warning(
            self,
            ar.SET_BACKUP_SECTION,
            ar.SET_BACKUP_RESTORE_CONFIRM,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmation != QMessageBox.StandardButton.Yes:
            return
        try:
            # Close the authorization session before replacing the SQLite file.
            with session_scope() as session:
                auth.require_owner(session, self.current_user.id)
            backup.restore_backup(backup_path)
        except auth.AuthorizationError:
            self._show_error(ar.SET_OWNER_ONLY)
            return
        except (ValueError, OSError, SQLAlchemyError) as error:
            self._show_backup_error(self._backup_error_text(error, restore=True))
            return

        QMessageBox.information(
            self,
            ar.SET_BACKUP_SECTION,
            ar.SET_BACKUP_RESTORE_SUCCESS,
        )
        self.window().close()
        application = QApplication.instance()
        if application is not None:
            application.quit()

    def _selected_backup_path(self) -> str | None:
        """Return the selected backup's full path from its table row."""
        row = self.backup_table.currentRow()
        if row < 0:
            return None
        item = self.backup_table.item(row, 0)
        if item is None:
            return None
        path = item.data(Qt.ItemDataRole.UserRole)
        return str(path) if path else None

    def _update_backup_actions(self, *_args: object) -> None:
        """Enable restore only when a local backup row is selected."""
        if not hasattr(self, "restore_backup_button"):
            return
        self.restore_backup_button.setEnabled(
            getattr(self, "_backup_supported", False)
            and self._selected_backup_path() is not None
        )

    @staticmethod
    def _supports_local_backups() -> bool:
        """Check the same local, file-backed SQLite limits as the backup service."""
        try:
            url = make_url(database_url())
        except Exception:
            return False
        path = url.database
        return bool(
            url.get_backend_name() == "sqlite"
            and path not in (None, "", ":memory:")
            and not path.startswith("file:")
            and url.query.get("mode") != "memory"
        )

    @staticmethod
    def _backup_error_text(error: BaseException, *, restore: bool) -> str:
        """Translate backend limitations and backup validation errors to Arabic."""
        message = str(error).casefold()
        if (
            "shared-server" in message
            or "only for local sqlite databases" in message
            or "require a configured local sqlite database url" in message
            or "require a file-backed local sqlite database" in message
        ):
            return ar.SET_BACKUP_LOCAL_ONLY
        if restore and any(
            phrase in message
            for phrase in (
                "backup file does not exist",
                "integrity_check",
                "required application tables",
                "required application columns",
                "readable sqlite backup",
            )
        ):
            return ar.SET_BACKUP_INVALID
        return ar.SET_BACKUP_RESTORE_ERROR if restore else ar.SET_BACKUP_CREATE_ERROR

    def _show_backup_error(self, message: str) -> None:
        """Display a localized backup operation error."""
        QMessageBox.warning(self, ar.SET_BACKUP_SECTION, message)

    def _selected_seller(self) -> tuple[int, bool] | None:
        """Return the selected account's ID and disabled state."""
        row = self.seller_table.currentRow()
        item = self.seller_table.item(row, 0) if row >= 0 else None
        if item is None:
            return None
        return int(item.data(Qt.ItemDataRole.UserRole)), bool(item.data(Qt.ItemDataRole.UserRole + 1))

    def _update_seller_actions(self) -> None:
        """Set seller action labels and availability for the current selection."""
        if not hasattr(self, "toggle_seller_button"):
            return
        selected = self._selected_seller()
        self.toggle_seller_button.setEnabled(selected is not None)
        self.reset_seller_button.setEnabled(selected is not None)
        self.reset_tfa_button.setEnabled(selected is not None)
        if selected is not None:
            _user_id, disabled = selected
            self.toggle_seller_button.setText(ar.USER_ENABLE if disabled else ar.USER_DISABLE)

    def _create_seller(self) -> None:
        """Collect a seller username and initial password, then create it."""
        dialog = _UserCredentialsDialog(title=ar.USER_ADD, with_role=True, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        username = dialog.username.text().strip()
        password = dialog.password.text()
        confirmation = dialog.confirmation.text()
        if len(password) < 6:
            self._show_error(ar.USER_PASSWORD_TOO_SHORT)
            return
        if password != confirmation:
            self._show_error(ar.USER_PASSWORD_MISMATCH)
            return
        try:
            with session_scope() as session:
                auth.create_user(
                    session,
                    self.current_user.id,
                    username=username,
                    password=password,
                    password_confirmation=confirmation,
                    role=dialog.role.currentData(),
                )
        except (ValueError, auth.AuthorizationError, SQLAlchemyError) as error:
            self._show_error(self._localized_error(error))
            return
        self._refresh_sellers()
        events.data_changed.emit()
        QMessageBox.information(self, ar.USER_MANAGEMENT_TITLE, ar.SET_SELLER_CREATED)

    def _toggle_seller(self) -> None:
        """Enable or disable the selected seller using the guarded auth service."""
        selected = self._selected_seller()
        if selected is None:
            return
        user_id, disabled = selected
        try:
            with session_scope() as session:
                auth.set_user_disabled(session, self.current_user.id, user_id, not disabled)
        except (ValueError, auth.AuthorizationError, SQLAlchemyError) as error:
            self._show_error(self._localized_error(error))
            return
        self._refresh_sellers()
        events.data_changed.emit()
        QMessageBox.information(self, ar.USER_MANAGEMENT_TITLE, ar.SET_USER_UPDATED)

    def _reset_seller_password(self) -> None:
        """Reset the selected seller's password without changing their role."""
        selected = self._selected_seller()
        if selected is None:
            return
        user_id, _disabled = selected
        username_item = self.seller_table.item(self.seller_table.currentRow(), 0)
        if username_item is None:
            return
        dialog = _UserCredentialsDialog(
            title=ar.USER_RESET_PASSWORD,
            username=username_item.text(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        password = dialog.password.text()
        confirmation = dialog.confirmation.text()
        if len(password) < 6:
            self._show_error(ar.USER_PASSWORD_TOO_SHORT)
            return
        if password != confirmation:
            self._show_error(ar.USER_PASSWORD_MISMATCH)
            return
        try:
            with session_scope() as session:
                auth.reset_password(
                    session,
                    self.current_user.id,
                    user_id,
                    new_password=password,
                    password_confirmation=confirmation,
                )
        except (ValueError, auth.AuthorizationError, SQLAlchemyError) as error:
            self._show_error(self._localized_error(error))
            return
        events.data_changed.emit()
        QMessageBox.information(self, ar.USER_MANAGEMENT_TITLE, ar.SET_USER_UPDATED)

    def _reset_user_two_factor(self) -> None:
        """Owner: remove two-step verification from an account (lost phone)."""
        selected = self._selected_seller()
        if selected is None:
            return
        user_id, _disabled = selected
        try:
            with session_scope() as session:
                auth.reset_two_factor(session, self.current_user.id, user_id)
        except (ValueError, auth.AuthorizationError, SQLAlchemyError) as error:
            self._show_error(self._localized_error(error))
            return
        self._refresh_sellers()
        self._refresh_two_factor()
        events.notify.emit("success", ar.USER_MANAGEMENT_TITLE, ar.TFA_RESET_DONE, 3500)

    def _change_own_password(self) -> None:
        """Change the signed-in user's password through the auth service."""
        current = self.current_password.text()
        new = self.new_password.text()
        confirmation = self.new_password_confirmation.text()
        if len(new) < 6:
            self._show_error(ar.USER_PASSWORD_TOO_SHORT)
            return
        if new != confirmation:
            self._show_error(ar.USER_PASSWORD_MISMATCH)
            return
        try:
            with session_scope() as session:
                auth.change_password(
                    session,
                    user_id=self.current_user.id,
                    current_password=current,
                    new_password=new,
                    password_confirmation=confirmation,
                )
        except (ValueError, auth.AuthorizationError, SQLAlchemyError) as error:
            self._show_error(self._localized_error(error, password=True))
            return
        self.current_password.clear()
        self.new_password.clear()
        self.new_password_confirmation.clear()
        events.data_changed.emit()
        QMessageBox.information(self, ar.SET_TITLE, ar.SET_PASSWORD_CHANGED)

    def _set_owner_access(self, allowed: bool) -> None:
        """Hide protected sections when the active account is no longer owner."""
        self.owner_settings_group.setVisible(allowed)
        self.backup_group.setVisible(allowed)
        self.user_group.setVisible(allowed)
        self.owner_access_label.setVisible(not allowed)

    @staticmethod
    def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
        """Clamp a persisted numeric setting to its supported UI range."""
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return min(maximum, max(minimum, value))

    @staticmethod
    def _localized_error(
        error: BaseException,
        *,
        settings: bool = False,
        password: bool = False,
    ) -> str:
        """Map expected service errors to the page's Arabic messages."""
        message = str(error)
        if isinstance(error, auth.AuthorizationError):
            return ar.SET_PASSWORD_ERROR if password else ar.SET_OWNER_ONLY
        if message in {ar.SET_RATE_INVALID, ar.SET_RATE_DUPLICATE_MONTH}:
            return message
        if "last active owner" in message:
            return ar.USER_LAST_ACTIVE_OWNER
        if "already in use" in message:
            return ar.USER_USERNAME_EXISTS
        if "not found" in message.lower():
            return ar.USER_NOT_FOUND
        if "at least 6" in message:
            return ar.USER_PASSWORD_TOO_SHORT
        if "confirmation does not match" in message:
            return ar.USER_PASSWORD_MISMATCH
        if password and "Current password" in message:
            return ar.SET_PASSWORD_ERROR
        return ar.SET_SAVE_ERROR if settings else ar.USER_OPERATION_ERROR

    def _show_error(self, message: str) -> None:
        """Display a concise localized validation or persistence error."""
        QMessageBox.warning(self, ar.SET_TITLE, message)
