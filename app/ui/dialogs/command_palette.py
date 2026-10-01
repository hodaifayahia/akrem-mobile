"""Spotlight Command Palette (Ctrl+K) for instant navigation and customer search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from datetime import date
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.db.models import User
from app.db.session import session_scope
from app.services.customers import list_customers, normalize_search_text
from app.ui.events import events


@dataclass
class PaletteItem:
    category: str  # 'navigation', 'action', 'customer'
    title: str
    subtitle: str
    icon: str
    hotkey: str | None
    callback: Callable[[], None]


class PaletteItemWidget(QWidget):
    """Visual row for a command palette search result."""

    def __init__(self, item: PaletteItem, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        # Icon
        icon_label = QLabel(item.icon, self)
        icon_label.setStyleSheet("font-size: 15px;")
        layout.addWidget(icon_label)

        # Texts
        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)

        title_label = QLabel(item.title, self)
        title_label.setStyleSheet("color: #FFFFFF; font-weight: 600; font-size: 13px;")
        text_layout.addWidget(title_label)

        if item.subtitle:
            sub_label = QLabel(item.subtitle, self)
            sub_label.setStyleSheet("color: #8A94A6; font-size: 11px;")
            text_layout.addWidget(sub_label)

        layout.addLayout(text_layout, 1)

        # Hotkey badge
        if item.hotkey:
            hk_label = QLabel(item.hotkey, self)
            hk_label.setStyleSheet(
                "background-color: #121A2C; color: #9DBEFF; border: 1px solid #234275; "
                "border-radius: 4px; padding: 2px 7px; font-size: 11px; font-weight: 700; font-family: monospace;"
            )
            layout.addWidget(hk_label)
        elif item.category == "customer":
            badge = QLabel("زبون 👤", self)
            badge.setStyleSheet(
                "background-color: #0E2A1E; color: #4ADE80; border: 1px solid #14532D; "
                "border-radius: 4px; padding: 2px 6px; font-size: 11px; font-weight: 600;"
            )
            layout.addWidget(badge)


class CommandPaletteDialog(QDialog):
    """Raycast/Spotlight-style floating command palette."""

    action_triggered = Signal()

    def __init__(self, current_user: User | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.current_user = current_user
        self.setObjectName("commandPaletteDialog")
        self.setFixedSize(620, 440)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._static_items: list[PaletteItem] = []
        self._filtered_items: list[PaletteItem] = []

        self._build_ui()
        self._init_static_items()
        self._filter_items("")

    def _build_ui(self) -> None:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        card = QFrame(self)
        card.setObjectName("paletteCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 14, 14, 14)
        card_layout.setSpacing(10)

        # Search bar header
        search_box = QHBoxLayout()
        search_box.setContentsMargins(8, 6, 8, 6)
        search_box.setSpacing(8)

        search_icon = QLabel("🔍", card)
        search_icon.setStyleSheet("font-size: 16px; color: #3B92D9;")
        search_box.addWidget(search_icon)

        self.search_input = QLineEdit(card)
        self.search_input.setObjectName("paletteSearchInput")
        self.search_input.setPlaceholderText("ابحث عن زبون، صفحة، أو إجراء سريع... (Esc للإلغاء)")
        self.search_input.textChanged.connect(self._filter_items)
        search_box.addWidget(self.search_input, 1)

        esc_hint = QLabel("Esc", card)
        esc_hint.setStyleSheet(
            "background-color: #121A2C; color: #8A94A6; border: 1px solid #1F2A3D; "
            "border-radius: 4px; padding: 2px 6px; font-size: 10px; font-family: monospace;"
        )
        search_box.addWidget(esc_hint)

        card_layout.addLayout(search_box)

        # Separator line
        sep = QFrame(card)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: #1F2E47;")
        card_layout.addWidget(sep)

        # Results list
        self.list_widget = QListWidget(card)
        self.list_widget.setObjectName("paletteList")
        self.list_widget.setSpacing(2)
        self.list_widget.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: none; } "
            "QListWidget::item { border-radius: 6px; padding: 2px; } "
            "QListWidget::item:selected { background-color: #0758CD; } "
            "QListWidget::item:hover:!selected { background-color: #121A2C; }"
        )
        self.list_widget.itemActivated.connect(self._execute_selected)
        card_layout.addWidget(self.list_widget, 1)

        # Footer hint
        footer = QHBoxLayout()
        footer.setContentsMargins(6, 4, 6, 0)
        hint_label = QLabel("↑ ↓ للتنقل   •   Enter للتنفيذ   •   Esc للإغلاق", card)
        hint_label.setStyleSheet("color: #64748B; font-size: 11px;")
        footer.addWidget(hint_label)
        card_layout.addLayout(footer)

        outer_layout.addWidget(card)

        card.setStyleSheet(
            "QFrame#paletteCard { "
            "  background-color: #0A0E17; "
            "  border: 1px solid #1F2E47; "
            "  border-radius: 12px; "
            "} "
            "QLineEdit#paletteSearchInput { "
            "  background: transparent; "
            "  border: none; "
            "  color: #FFFFFF; "
            "  font-size: 14px; "
            "}"
        )

    def _init_static_items(self) -> None:
        """Populate navigation and quick actions."""
        # Navigation
        self._static_items.append(
            PaletteItem("navigation", "لوحة التحكم", "نظرة عامة على المقابيض والمتأخرات", "📊", "Ctrl+1", lambda: events.navigate_to.emit(0))
        )
        self._static_items.append(
            PaletteItem("navigation", "إدارة الزبائن", "قائمة الزبائن، تفاصيل الديون والسجلات", "👥", "Ctrl+2", lambda: events.navigate_to.emit(1))
        )
        self._static_items.append(
            PaletteItem("navigation", "تسجيل بيع جديد", "إنشاء عقد بيع جديد بالتقسيط أو نقداً", "🛒", "Ctrl+3", lambda: events.navigate_to.emit(2))
        )
        self._static_items.append(
            PaletteItem("navigation", "متابعة الأقساط", "استعراض وجباية الأقساط الشهرية", "💳", "Ctrl+4", lambda: events.navigate_to.emit(3))
        )
        if self.current_user is None or self.current_user.role == "owner":
            self._static_items.append(
                PaletteItem("navigation", "استيراد البيانات", "استيراد الجداول من ملفات Excel", "📥", "Ctrl+5", lambda: events.navigate_to.emit(4))
            )
        self._static_items.append(
            PaletteItem("navigation", "التقارير المالية", "تصدير وطباعة تقارير الأرباح والمبيعات", "📈", "Ctrl+6", lambda: events.navigate_to.emit(5))
        )
        if self.current_user is None or self.current_user.role == "owner":
            self._static_items.append(
                PaletteItem("navigation", "إعدادات النظام", "إدارة المستخدمين، الأمان، والتخصيص", "⚙️", "Ctrl+7", lambda: events.navigate_to.emit(6))
            )

        # Quick Actions
        self._static_items.append(
            PaletteItem("action", "بيع جديد بالتقسيط", "فتح نموذج تسجيل بيع جديد", "➕", "Ctrl+N", lambda: events.navigate_to.emit(2))
        )
        self._static_items.append(
            PaletteItem("action", "قفل شاشة التطبيق", "حماية الجلسة وقفل الواجهة فوراً", "🔒", "Ctrl+L", self._lock_app)
        )
        self._static_items.append(
            PaletteItem("action", "اختصارات لوحة المفاتيح", "عرض قائمة جميع اختصارات التطبيق", "⌨️", "F1", self._show_shortcuts)
        )

    def _lock_app(self) -> None:
        parent = self.parent()
        if hasattr(parent, "_lock_after_idle"):
            parent._lock_after_idle()

    def _show_shortcuts(self) -> None:
        parent = self.parent()
        if hasattr(parent, "_open_shortcuts_dialog"):
            parent._open_shortcuts_dialog()

    def _filter_items(self, query: str) -> None:
        self.list_widget.clear()
        self._filtered_items.clear()
        norm_query = normalize_search_text(query)

        # 1. Matching static items
        for item in self._static_items:
            if not norm_query or (
                norm_query in normalize_search_text(item.title)
                or norm_query in normalize_search_text(item.subtitle)
            ):
                self._filtered_items.append(item)

        # 2. Dynamic customer search if query is non-empty
        if norm_query:
            try:
                today = date.today()
                with session_scope() as session:
                    summaries = list_customers(
                        session,
                        year=today.year,
                        month=today.month,
                        today=today,
                        search=query,
                    )
                    # Limit to top 6 customers in palette
                    for s in summaries[:6]:
                        c_id = s.id
                        c_name = s.full_name
                        c_phone = s.phone or "لا يوجد هاتف"
                        c_balance = f"الرصيد: {s.remaining_balance:,} دج"
                        sub = f"{c_phone} • {c_balance}"
                        self._filtered_items.append(
                            PaletteItem(
                                category="customer",
                                title=c_name,
                                subtitle=sub,
                                icon="👤",
                                hotkey=None,
                                callback=lambda cid=c_id: events.open_customer.emit(cid),
                            )
                        )
            except Exception:
                pass

        # Populate QListWidget
        for item in self._filtered_items:
            list_item = QListWidgetItem(self.list_widget)
            widget = PaletteItemWidget(item)
            list_item.setSizeHint(widget.sizeHint())
            self.list_widget.addItem(list_item)
            self.list_widget.setItemWidget(list_item, widget)

        if self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Down:
            curr = self.list_widget.currentRow()
            if curr < self.list_widget.count() - 1:
                self.list_widget.setCurrentRow(curr + 1)
            return
        elif event.key() == Qt.Key.Key_Up:
            curr = self.list_widget.currentRow()
            if curr > 0:
                self.list_widget.setCurrentRow(curr - 1)
            return
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._execute_selected()
            return
        elif event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def _execute_selected(self) -> None:
        row = self.list_widget.currentRow()
        if 0 <= row < len(self._filtered_items):
            item = self._filtered_items[row]
            self.accept()
            item.callback()
