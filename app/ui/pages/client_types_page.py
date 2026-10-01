"""Client types page (owner): the types list with counts and management actions."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from app.db.models import User
from app.ui.dialogs.client_types_dialog import ClientTypesPanel


class ClientTypesPage(QWidget):
    """Sidebar page wrapping :class:`ClientTypesPanel`."""

    view_customers_requested = Signal(int)

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel = ClientTypesPanel(current_user.id, self)
        self.panel.view_customers_requested.connect(self.view_customers_requested)
        layout.addWidget(self.panel)
