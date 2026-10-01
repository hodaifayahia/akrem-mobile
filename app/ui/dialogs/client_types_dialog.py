"""Owner dialogs to create, edit, reorder, delete and assign client types.

All writes go through :mod:`app.services.categories`, which re-checks that
the acting user is an owner. Validation errors arrive as
``ClientTypeError`` codes and are shown inline in the active language.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import session_scope
from app.i18n import ar
from app.services import auth, categories
from app.services.categories import TYPE_COLORS, ClientType, ClientTypeError
from app.ui import icons
from app.ui.dialogs.auth_dialogs import InlineError
from app.ui.events import events
from app.ui.theme import TYPE_COLOR_HEX, token
from app.ui.widgets.client_types import color_dot

_ERROR_KEYS = {
    "not_found": "CT_ERR_NOT_FOUND",
    "empty_name": "CT_ERR_EMPTY_NAME",
    "name_too_long": "CT_ERR_NAME_TOO_LONG",
    "duplicate_name": "CT_ERR_DUPLICATE",
    "invalid_color": "CT_ERR_INVALID_COLOR",
    "system_type": "CT_ERR_SYSTEM",
    "in_use": "CT_ERR_IN_USE",
    "invalid_reassign": "CT_ERR_REASSIGN",
    "no_customers": "CT_ERR_NO_CUSTOMERS",
    "invalid_order": "CT_ERR_INVALID_ORDER",
}


def error_text(error: Exception) -> str:
    """Translate a service error into a message for the owner."""
    if isinstance(error, ClientTypeError):
        return getattr(ar, _ERROR_KEYS.get(error.code, "ERROR_BODY"))
    if isinstance(error, auth.AuthorizationError):
        return ar.CT_ERR_PERMISSION
    return ar.ERROR_BODY


def load_client_types() -> list[ClientType]:
    """Read client types with their customer counts."""
    with session_scope() as session:
        return categories.list_client_types(session)


class ClientTypeEditor(QDialog):
    """Name and colour form for a new or existing client type."""

    def __init__(self, parent: QWidget, client_type: ClientType | None = None) -> None:
        super().__init__(parent)
        self.client_type = client_type
        self.setWindowTitle(ar.CT_EDIT_TITLE if client_type else ar.CT_NEW_TITLE)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(8)
        title = QLabel(self.windowTitle(), self)
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        layout.addSpacing(6)

        name_label = QLabel(ar.CT_NAME, self)
        name_label.setObjectName("fieldLabel")
        self.name = QLineEdit(self)
        self.name.setMaxLength(categories.MAX_TYPE_NAME_LENGTH)
        self.name.setPlaceholderText(ar.CT_NAME_PLACEHOLDER)
        if client_type is not None:
            self.name.setText(client_type.name)
            self.name.setReadOnly(client_type.is_system)
        name_label.setBuddy(self.name)
        layout.addWidget(name_label)
        layout.addWidget(self.name)
        if client_type is not None and client_type.is_system:
            hint = QLabel(ar.CT_SYSTEM_HINT, self)
            hint.setObjectName("sectionHint")
            hint.setWordWrap(True)
            layout.addWidget(hint)
        layout.addSpacing(8)

        color_label = QLabel(ar.CT_COLOR, self)
        color_label.setObjectName("fieldLabel")
        layout.addWidget(color_label)
        swatches = QHBoxLayout()
        swatches.setSpacing(8)
        self.color_group = QButtonGroup(self)
        self.color_group.setExclusive(True)
        current = client_type.color if client_type else "blue"
        for index, key in enumerate(TYPE_COLORS):
            swatch = QPushButton(self)
            swatch.setProperty("swatch", True)
            swatch.setCheckable(True)
            swatch.setCursor(Qt.CursorShape.PointingHandCursor)
            swatch.setStyleSheet(
                f"QPushButton {{ background-color: {TYPE_COLOR_HEX[key]}; border-radius: 15px; "
                "padding: 0; min-height: 30px; max-height: 30px; min-width: 30px; max-width: 30px; "
                "border: 2px solid transparent; }"
                f"QPushButton:hover {{ background-color: {TYPE_COLOR_HEX[key]}; border-color: {token('border-strong')}; }}"
                f"QPushButton:checked {{ border: 2px solid {token('text')}; }}"
            )
            swatch.setFixedSize(30, 30)
            label = ar.CT_COLOR_LABELS[index] if index < len(ar.CT_COLOR_LABELS) else key
            swatch.setToolTip(label)
            swatch.setAccessibleName(label)
            swatch.setProperty("colorKey", key)
            swatch.setChecked(key == current)
            self.color_group.addButton(swatch)
            swatches.addWidget(swatch)
        swatches.addStretch(1)
        layout.addLayout(swatches)
        layout.addSpacing(8)

        self.error = InlineError(self)
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(ar.CT_CANCEL, self)
        cancel.setProperty("variant", "secondary")
        cancel.clicked.connect(self.reject)
        self.save_button = QPushButton(ar.CT_SAVE, self)
        self.save_button.setDefault(True)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.name.textEdited.connect(self.error.clear)
        self.name.setFocus()

    def color(self) -> str:
        """Return the selected colour key."""
        checked = self.color_group.checkedButton()
        return str(checked.property("colorKey")) if checked is not None else "blue"


class ClientTypesDialog(QDialog):
    """Owner panel listing every client type with edit/reorder/delete actions."""

    def __init__(self, owner_user_id: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.owner_user_id = owner_user_id
        self.setWindowTitle(ar.CT_TITLE)
        self.setMinimumSize(560, 560)
        self._types: list[ClientType] = []
        self.changed = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(10)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel(ar.CT_TITLE, self)
        title.setObjectName("pageTitle")
        subtitle = QLabel(ar.CT_SUBTITLE, self)
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        self.add_button = QPushButton(ar.CT_ADD, self)
        self.add_button.setIcon(icons.icon("plus", "#FFFFFF", 16))
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.clicked.connect(self._create)
        header.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        layout.addSpacing(6)

        self.error = InlineError(self)
        layout.addWidget(self.error)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.list_widget = QWidget(scroll)
        self.rows = QVBoxLayout(self.list_widget)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(8)
        scroll.setWidget(self.list_widget)
        layout.addWidget(scroll, 1)

        close = QPushButton(ar.CUST_CLOSE, self)
        close.setProperty("variant", "secondary")
        close.clicked.connect(self.accept)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(close)
        layout.addLayout(footer)
        self.reload()

    # ------------------------------------------------------------------ list
    def reload(self) -> None:
        """Re-read the types and rebuild the rows."""
        try:
            self._types = load_client_types()
        except SQLAlchemyError:
            self.error.show_message(ar.ERROR_BODY)
            return
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        movable = [item for item in self._types if not item.is_system]
        for item in self._types:
            self.rows.addWidget(self._row(item, movable))
        self.rows.addStretch(1)

    def _row(self, item: ClientType, movable: list[ClientType]) -> QFrame:
        row = QFrame(self.list_widget)
        row.setObjectName("typeRow")
        line = QHBoxLayout(row)
        line.setContentsMargins(14, 10, 10, 10)
        line.setSpacing(10)
        dot = QLabel(row)
        dot.setPixmap(color_dot(item.color, 14).pixmap(14, 14))
        line.addWidget(dot)
        text = QVBoxLayout()
        text.setSpacing(0)
        name = QLabel(item.name, row)
        name.setObjectName("attentionName")
        meta = QLabel(ar.CT_COUNT.format(count=item.customer_count), row)
        meta.setObjectName("attentionMeta")
        text.addWidget(name)
        text.addWidget(meta)
        line.addLayout(text, 1)
        if item.is_system:
            lock = QLabel(row)
            lock.setPixmap(icons.pixmap("lock", "text-muted", 15))
            lock.setToolTip(ar.CT_SYSTEM_HINT)
            line.addWidget(lock)
        else:
            position = movable.index(item)
            up = self._tool_button("chevron-up", ar.CT_MOVE_UP, row)
            up.setEnabled(position > 0)
            up.clicked.connect(lambda: self._move(item.id, -1))
            down = self._tool_button("chevron-down", ar.CT_MOVE_DOWN, row)
            down.setEnabled(position < len(movable) - 1)
            down.clicked.connect(lambda: self._move(item.id, 1))
            line.addWidget(up)
            line.addWidget(down)
        edit = self._tool_button("edit", ar.CT_EDIT, row)
        edit.clicked.connect(lambda: self._edit(item))
        line.addWidget(edit)
        if not item.is_system:
            delete = self._tool_button("trash", ar.CT_DELETE, row, color="failed")
            delete.clicked.connect(lambda: self._delete(item))
            line.addWidget(delete)
        return row

    @staticmethod
    def _tool_button(name: str, tip: str, parent: QWidget, *, color: str = "text-muted") -> QToolButton:
        button = QToolButton(parent)
        button.setObjectName("iconButton")
        button.setFixedSize(32, 32)
        button.setIconSize(QSize(16, 16))
        button.setIcon(icons.icon(name, color, 16))
        button.setToolTip(tip)
        button.setAccessibleName(tip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    # --------------------------------------------------------------- actions
    def _create(self) -> None:
        editor = ClientTypeEditor(self)
        editor.save_button.clicked.connect(lambda: self._save_editor(editor))
        editor.exec()

    def _edit(self, item: ClientType) -> None:
        editor = ClientTypeEditor(self, item)
        editor.save_button.clicked.connect(lambda: self._save_editor(editor))
        editor.exec()

    def _save_editor(self, editor: ClientTypeEditor) -> None:
        try:
            with session_scope() as session:
                if editor.client_type is None:
                    categories.create_client_type(
                        session, self.owner_user_id, name=editor.name.text(), color=editor.color()
                    )
                else:
                    categories.update_client_type(
                        session,
                        self.owner_user_id,
                        editor.client_type.id,
                        name=None if editor.client_type.is_system else editor.name.text(),
                        color=editor.color(),
                    )
        except (ClientTypeError, auth.AuthorizationError, SQLAlchemyError) as error:
            editor.error.show_message(error_text(error))
            return
        editor.accept()
        self._after_change(ar.CT_SAVED_TOAST)

    def _move(self, type_id: int, step: int) -> None:
        order = [item.id for item in self._types if not item.is_system]
        index = order.index(type_id)
        target = index + step
        if not 0 <= target < len(order):
            return
        order[index], order[target] = order[target], order[index]
        self._run(lambda session: categories.reorder_client_types(session, self.owner_user_id, order))

    def _delete(self, item: ClientType) -> None:
        reassign_to: int | None = None
        if item.customer_count:
            dialog = _ReassignDialog(item, [t for t in self._types if t.id != item.id], self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            reassign_to = dialog.target_id()
        else:
            box = QMessageBox(QMessageBox.Icon.Question, ar.CT_DELETE,
                              ar.CT_DELETE_CONFIRM.format(name=item.name), parent=self)
            confirm = box.addButton(ar.CT_DELETE, QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(ar.CT_CANCEL, QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is not confirm:
                return
        self._run(
            lambda session: categories.delete_client_type(
                session, self.owner_user_id, item.id, reassign_to_id=reassign_to
            ),
            ar.CT_DELETED_TOAST,
        )

    def _run(self, action, toast: str | None = None) -> None:
        self.error.clear()
        try:
            with session_scope() as session:
                action(session)
        except (ClientTypeError, auth.AuthorizationError, SQLAlchemyError) as error:
            self.error.show_message(error_text(error))
            return
        self._after_change(toast)

    def _after_change(self, toast: str | None) -> None:
        self.changed = True
        self.reload()
        events.data_changed.emit()
        if toast:
            events.notify.emit("success", ar.CT_TITLE, toast, 3000)


class _ReassignDialog(QDialog):
    """Pick where a deleted type's clients go."""

    def __init__(self, item: ClientType, others: list[ClientType], parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle(ar.CT_DELETE_CONFIRM.format(name=item.name))
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        message = QLabel(ar.CT_DELETE_REASSIGN.format(count=item.customer_count), self)
        message.setWordWrap(True)
        layout.addWidget(message)
        self.target = QComboBox(self)
        for other in others:
            self.target.addItem(color_dot(other.color), other.name, other.id)
        fallback = next((index for index, other in enumerate(others) if other.is_system), 0)
        self.target.setCurrentIndex(fallback)
        layout.addWidget(self.target)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(ar.CT_CANCEL, self)
        cancel.setProperty("variant", "secondary")
        cancel.clicked.connect(self.reject)
        delete = QPushButton(ar.CT_DELETE, self)
        delete.setProperty("variant", "danger")
        delete.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(delete)
        layout.addLayout(buttons)

    def target_id(self) -> int | None:
        """Return the chosen destination type id."""
        value = self.target.currentData()
        return int(value) if value is not None else None


class AssignTypeDialog(QDialog):
    """Choose one client type to apply to several selected customers."""

    def __init__(self, types: list[ClientType], count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(ar.CT_ASSIGN_TITLE.format(count=count))
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(8)
        title = QLabel(self.windowTitle(), self)
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for item in types:
            option = QPushButton(item.name, self)
            option.setProperty("chip", True)
            option.setCheckable(True)
            option.setIcon(color_dot(item.color))
            option.setProperty("typeId", item.id)
            option.setProperty("typeName", item.name)
            option.setCursor(Qt.CursorShape.PointingHandCursor)
            self.group.addButton(option)
            layout.addWidget(option)
        if self.group.buttons():
            self.group.buttons()[0].setChecked(True)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton(ar.CT_CANCEL, self)
        cancel.setProperty("variant", "secondary")
        cancel.clicked.connect(self.reject)
        apply = QPushButton(ar.CT_ASSIGN, self)
        apply.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(apply)
        layout.addSpacing(6)
        layout.addLayout(buttons)

    def selected(self) -> tuple[int, str] | None:
        """Return the chosen (type id, name)."""
        button = self.group.checkedButton()
        if button is None:
            return None
        return int(button.property("typeId")), str(button.property("typeName"))
