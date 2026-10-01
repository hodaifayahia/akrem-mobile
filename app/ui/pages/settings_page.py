"""Owner settings and password management page."""

from __future__ import annotations

from PySide6.QtCore import Qt
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
from app.db.models import Product, User
from app.db.session import session_scope
from app.i18n import ar
from app.services import auth, backup, products, settings
from app.ui.events import events


class _UserCredentialsDialog(QDialog):
    """Collect credentials for seller creation or owner password reset."""

    def __init__(
        self,
        *,
        title: str,
        username: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(410)
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


class _ProductDialog(QDialog):
    """Collect a product's name and owner-only wholesale/cash prices."""

    def __init__(
        self,
        *,
        title: str,
        product: Product | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self.name = QLineEdit(self)
        self.wholesale_price = self._money_input(self)
        self.cash_price = self._money_input(self)
        if product is not None:
            self.name.setText(product.name)
            self.wholesale_price.setValue(product.wholesale_price)
            self.cash_price.setValue(product.cash_price)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow(ar.SET_PRODUCT_NAME, self.name)
        form.addRow(ar.SET_PRODUCT_WHOLESALE, self.wholesale_price)
        form.addRow(ar.SET_PRODUCT_CASH, self.cash_price)
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
    def _money_input(parent: QWidget) -> QSpinBox:
        """Create a whole-dinar price control."""
        editor = QSpinBox(parent)
        editor.setRange(0, 2_000_000_000)
        editor.setGroupSeparatorShown(True)
        return editor


class SettingsPage(QWidget):
    """Configure installment rules and accounts, with service-side role checks."""

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self._is_owner = current_user.role == "owner"
        self._products_by_id: dict[int, Product] = {}
        self._build_ui()
        if self._is_owner:
            try:
                self._load_owner_settings()
                self._refresh_products()
                self._refresh_backups()
                self._refresh_sellers()
            except (auth.AuthorizationError, SQLAlchemyError):
                self._set_owner_access(False)
        else:
            self.owner_settings_group.hide()
            self.product_group.hide()
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
        self.product_group = self._build_product_pricebook(body)
        self.backup_group = self._build_backup_section(body)
        self.user_group = self._build_user_management(body)
        if self._is_owner:
            body_layout.addWidget(self.owner_settings_group)
            body_layout.addWidget(self.product_group)
            body_layout.addWidget(self.backup_group)
            body_layout.addWidget(self.user_group)

        self.password_group = self._build_password_section(body)
        body_layout.addWidget(self.password_group)
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
        """Create the owner-only seller account list and actions."""
        group = QGroupBox(f"👥  {ar.USER_MANAGEMENT_TITLE}", parent)
        layout = QVBoxLayout(group)
        self.seller_table = QTableWidget(0, 2, group)
        self.seller_table.setHorizontalHeaderLabels([ar.USER_USERNAME, ar.SET_USER_STATUS])
        self.seller_table.verticalHeader().setVisible(False)
        self.seller_table.horizontalHeader().setStretchLastSection(True)
        self.seller_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.seller_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.seller_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.seller_table.setMaximumHeight(230)
        self.seller_table.itemSelectionChanged.connect(self._update_seller_actions)
        layout.addWidget(self.seller_table)

        actions = QHBoxLayout()
        self.add_seller_button = QPushButton(f"➕  {ar.USER_ADD_SELLER}", group)
        self.add_seller_button.setProperty("variant", "primary")
        self.add_seller_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_seller_button = QPushButton(f"⚠️  {ar.USER_DISABLE}", group)
        self.toggle_seller_button.setProperty("variant", "warning")
        self.toggle_seller_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_seller_button = QPushButton(f"🔑  {ar.USER_RESET_PASSWORD}", group)
        self.reset_seller_button.setProperty("variant", "secondary")
        self.reset_seller_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_seller_button.clicked.connect(self._create_seller)
        self.toggle_seller_button.clicked.connect(self._toggle_seller)
        self.reset_seller_button.clicked.connect(self._reset_seller_password)
        actions.addWidget(self.add_seller_button)
        actions.addWidget(self.toggle_seller_button)
        actions.addWidget(self.reset_seller_button)
        layout.addLayout(actions)
        return group

    def _build_product_pricebook(self, parent: QWidget) -> QGroupBox:
        """Create the owner-only product catalog with editable price snapshots."""
        group = QGroupBox(f"🏷️  {ar.SET_PRICE_BOOK}", parent)
        layout = QVBoxLayout(group)
        self.product_table = QTableWidget(0, 4, group)
        self.product_table.setHorizontalHeaderLabels(
            [
                ar.SET_PRODUCT_NAME,
                ar.SET_PRODUCT_WHOLESALE,
                ar.SET_PRODUCT_CASH,
                ar.SET_PRODUCT_ACTIVE,
            ]
        )
        self.product_table.verticalHeader().setVisible(False)
        self.product_table.horizontalHeader().setStretchLastSection(True)
        self.product_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.product_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.product_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.product_table.setMaximumHeight(250)
        self.product_table.itemSelectionChanged.connect(self._update_product_actions)
        layout.addWidget(self.product_table)

        actions = QHBoxLayout()
        self.add_product_button = QPushButton(f"➕  {ar.SET_PRODUCT_ADD}", group)
        self.add_product_button.setProperty("variant", "primary")
        self.add_product_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_product_button = QPushButton(f"✏️  {ar.SET_PRODUCT_EDIT}", group)
        self.edit_product_button.setProperty("variant", "secondary")
        self.edit_product_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_product_button = QPushButton(f"📦  {ar.SET_PRODUCT_ARCHIVE}", group)
        self.toggle_product_button.setProperty("variant", "warning")
        self.toggle_product_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_product_button.clicked.connect(self._create_product)
        self.edit_product_button.clicked.connect(self._edit_product)
        self.toggle_product_button.clicked.connect(self._toggle_product)
        actions.addWidget(self.add_product_button)
        actions.addWidget(self.edit_product_button)
        actions.addWidget(self.toggle_product_button)
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
            if not 2 <= months <= 12 or not 0 <= rate <= 50:
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
        months = next((value for value in range(2, 13) if str(value) not in existing), None)
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
        """Load seller accounts through the owner-guarded service method."""
        with session_scope() as session:
            auth.require_owner(session, self.current_user.id)
            users = auth.list_users(session, self.current_user.id)
        sellers = [user for user in users if user.role == "seller"]
        self.seller_table.setRowCount(len(sellers))
        for row, user in enumerate(sellers):
            username = QTableWidgetItem(user.username)
            username.setData(Qt.ItemDataRole.UserRole, user.id)
            self.seller_table.setItem(row, 0, username)
            state_text = ar.SET_USER_DISABLED if user.disabled else ar.SET_USER_ACTIVE
            self.seller_table.setItem(row, 1, QTableWidgetItem(state_text))
        self._update_seller_actions()

    def _refresh_products(self) -> None:
        """Load all catalog entries after checking the active owner role."""
        with session_scope() as session:
            auth.require_owner(session, self.current_user.id)
            catalog = products.list_products(session, include_inactive=True)
        self._products_by_id = {item.id: item for item in catalog}
        self.product_table.setRowCount(len(catalog))
        for row, product in enumerate(catalog):
            name = QTableWidgetItem(product.name)
            name.setData(Qt.ItemDataRole.UserRole, product.id)
            self.product_table.setItem(row, 0, name)
            self.product_table.setItem(row, 1, QTableWidgetItem(self._money(product.wholesale_price)))
            self.product_table.setItem(row, 2, QTableWidgetItem(self._money(product.cash_price)))
            state = ar.SET_USER_ACTIVE if product.active else ar.SET_PRODUCT_ARCHIVED
            self.product_table.setItem(row, 3, QTableWidgetItem(state))
        self._update_product_actions()

    def _refresh_backups(self) -> None:
        """List local backup files after checking owner access and backend support."""
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

    def _selected_product(self) -> Product | None:
        """Return the selected catalog entry."""
        row = self.product_table.currentRow()
        if row < 0:
            return None
        item = self.product_table.item(row, 0)
        if item is None:
            return None
        product_id = item.data(Qt.ItemDataRole.UserRole)
        return self._products_by_id.get(int(product_id))

    def _update_product_actions(self) -> None:
        """Update product action availability and archive button text."""
        if not hasattr(self, "edit_product_button"):
            return
        product = self._selected_product()
        self.edit_product_button.setEnabled(product is not None)
        self.toggle_product_button.setEnabled(product is not None)
        if product is not None:
            self.toggle_product_button.setText(
                ar.SET_PRODUCT_ACTIVATE if not product.active else ar.SET_PRODUCT_ARCHIVE
            )

    def _create_product(self) -> None:
        """Create a product after collecting its owner-entered prices."""
        dialog = _ProductDialog(title=ar.SET_PRODUCT_ADD, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            with session_scope() as session:
                products.create_product(
                    session,
                    self.current_user.id,
                    name=dialog.name.text(),
                    wholesale_price=dialog.wholesale_price.value(),
                    cash_price=dialog.cash_price.value(),
                )
        except (ValueError, auth.AuthorizationError, SQLAlchemyError) as error:
            self._show_error(
                ar.SET_OWNER_ONLY if isinstance(error, auth.AuthorizationError) else ar.SET_PRODUCT_ERROR
            )
            return
        self._refresh_products()
        events.data_changed.emit()
        QMessageBox.information(self, ar.SET_PRICE_BOOK, ar.SET_PRODUCT_SAVED)

    def _edit_product(self) -> None:
        """Edit the selected catalog name and prices."""
        product = self._selected_product()
        if product is None:
            return
        dialog = _ProductDialog(title=ar.SET_PRODUCT_EDIT, product=product, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            with session_scope() as session:
                products.update_product(
                    session,
                    self.current_user.id,
                    product.id,
                    name=dialog.name.text(),
                    wholesale_price=dialog.wholesale_price.value(),
                    cash_price=dialog.cash_price.value(),
                )
        except (ValueError, auth.AuthorizationError, SQLAlchemyError) as error:
            self._show_error(
                ar.SET_OWNER_ONLY if isinstance(error, auth.AuthorizationError) else ar.SET_PRODUCT_ERROR
            )
            return
        self._refresh_products()
        events.data_changed.emit()
        QMessageBox.information(self, ar.SET_PRICE_BOOK, ar.SET_PRODUCT_SAVED)

    def _toggle_product(self) -> None:
        """Archive or reactivate the selected catalog entry."""
        product = self._selected_product()
        if product is None:
            return
        try:
            with session_scope() as session:
                products.set_product_active(
                    session,
                    self.current_user.id,
                    product.id,
                    not product.active,
                )
        except (ValueError, auth.AuthorizationError, SQLAlchemyError) as error:
            self._show_error(
                ar.SET_OWNER_ONLY if isinstance(error, auth.AuthorizationError) else ar.SET_PRODUCT_ERROR
            )
            return
        self._refresh_products()
        events.data_changed.emit()
        QMessageBox.information(self, ar.SET_PRICE_BOOK, ar.SET_USER_UPDATED)

    @staticmethod
    def _money(amount: int) -> str:
        """Format whole dinars for the owner-visible catalog table."""
        return f"{amount:,} {ar.CURRENCY_SUFFIX}"

    def _selected_seller(self) -> tuple[int, bool] | None:
        """Return the selected seller's ID and disabled state."""
        row = self.seller_table.currentRow()
        if row < 0:
            return None
        username = self.seller_table.item(row, 0)
        state = self.seller_table.item(row, 1)
        if username is None or state is None:
            return None
        user_id = username.data(Qt.ItemDataRole.UserRole)
        return int(user_id), state.text() == ar.SET_USER_DISABLED

    def _update_seller_actions(self) -> None:
        """Set seller action labels and availability for the current selection."""
        if not hasattr(self, "toggle_seller_button"):
            return
        selected = self._selected_seller()
        self.toggle_seller_button.setEnabled(selected is not None)
        self.reset_seller_button.setEnabled(selected is not None)
        if selected is not None:
            _user_id, disabled = selected
            self.toggle_seller_button.setText(ar.USER_ENABLE if disabled else ar.USER_DISABLE)

    def _create_seller(self) -> None:
        """Collect a seller username and initial password, then create it."""
        dialog = _UserCredentialsDialog(title=ar.USER_ADD_SELLER, parent=self)
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
                auth.create_seller(
                    session,
                    self.current_user.id,
                    username=username,
                    password=password,
                    password_confirmation=confirmation,
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
        self.product_group.setVisible(allowed)
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
