"""Unit and integration tests for Notifications, Topbar, and Command Palette."""

from __future__ import annotations

import os
from datetime import date
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.db.models import User
from app.ui.events import events
from app.ui.widgets.toast import ToastCard, ToastManager
from app.ui.widgets.topbar import AppTopBar
from app.ui.widgets.notification_center import NotificationCenterPopup, AlertItem
from app.ui.dialogs.command_palette import CommandPaletteDialog
from app.ui.dialogs.shortcuts_dialog import ShortcutsDialog


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def test_toast_card_creation(qapp):
    """Verify ToastCard can be created for all levels with proper properties."""
    for level in ("success", "warning", "error", "info"):
        card = ToastCard(level, "العنوان", "نص الإشعار التجريبي", duration_ms=2000)
        assert card.level == level
        assert card.duration_ms == 2000
        assert card.progress_bar is not None


def test_toast_manager_lifecycle(qapp):
    """Verify ToastManager catches events.notify and stacks cards."""
    from PySide6.QtWidgets import QWidget
    parent = QWidget()
    parent.resize(800, 600)
    manager = ToastManager(parent)

    events.notify.emit("success", "تم بنجاح", "تمت العملية بنجاح", 3000)
    assert len(manager.toasts) == 1
    assert manager.toasts[0].level == "success"

    # Multiple toasts
    manager.warning("تحذير", "يرجى التحقق من الرصيد")
    assert len(manager.toasts) == 2


def test_topbar_breadcrumbs(qapp):
    """Verify AppTopBar updates breadcrumbs on page navigation."""
    topbar = AppTopBar()
    topbar.set_active_page(0)
    assert "لوحة التحكم" in topbar.page_title_label.text()

    topbar.set_active_page(1)
    assert "إدارة الزبائن" in topbar.page_title_label.text()

    topbar.set_active_page(2)
    assert "بيع جديد" in topbar.page_title_label.text()


def test_command_palette_filtering(qapp, memory_engine):
    """Verify CommandPaletteDialog initializes and filters navigation and action items."""
    user = User(id=1, username="test_owner", role="owner", password_hash="dummy")
    palette = CommandPaletteDialog(current_user=user)

    assert len(palette._static_items) >= 7

    # Filter with specific text
    palette._filter_items("الزبائن")
    matching_titles = [item.title for item in palette._filtered_items]
    assert any("الزبائن" in t for t in matching_titles)


def test_shortcuts_dialog(qapp):
    """Verify ShortcutsDialog instantiates with all hotkeys displayed."""
    dialog = ShortcutsDialog()
    assert dialog.windowTitle() == "اختصارات لوحة المفاتيح"
    assert len(dialog.SHORTCUTS) >= 2


def test_notification_popup(qapp, memory_engine):
    """Verify NotificationCenterPopup instantiates and refreshes safely."""
    popup = NotificationCenterPopup()
    popup.refresh_alerts()
    assert popup.get_alert_count() >= 0
