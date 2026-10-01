"""Small Qt signal bus used to refresh dependent screens after edits."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class UiEvents(QObject):
    """Application-wide UI events for customer, sale, and payment changes."""

    data_changed = Signal()
    customer_changed = Signal(int)
    sale_changed = Signal(int)
    payment_changed = Signal(int)
    settings_changed = Signal()
    notify = Signal(str, str, str, int)
    open_customer = Signal(int)
    navigate_to = Signal(int)
    language_changed = Signal(str)


events = UiEvents()
