"""Keyboard shortcuts help modal dialog."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class ShortcutsDialog(QDialog):
    """Modern modal dialog displaying all available keyboard shortcuts."""

    SHORTCUTS = [
        ("التنقل والأوامر العامة", [
            ("Ctrl + K", "البحث الشامل وشريط الأوامر (Spotlight)"),
            ("Ctrl + N", "تسجيل بيع جديد بالتقسيط"),
            ("Ctrl + L", "قفل الشاشة الفوري لحماية الجلسة"),
            ("F1", "إظهار دليل اختصارات لوحة المفاتيح"),
            ("Esc", "إغلاق النوافذ المنبثقة والشاشات"),
        ]),
        ("التنقل السريع بين الشاشات", [
            ("Ctrl + 1", "لوحة التحكم (Dashboard)"),
            ("Ctrl + 2", "إدارة الزبائن والديون (Customers)"),
            ("Ctrl + 3", "بيع جديد بالتقسيط (New Sale)"),
            ("Ctrl + 4", "متابعة وجباية الأقساط (Payments)"),
            ("Ctrl + 5", "استيراد البيانات من Excel (Import)"),
            ("Ctrl + 6", "التقارير المالية والإحصائيات (Reports)"),
            ("Ctrl + 7", "إعدادات النظام والأمان (Settings)"),
        ]),
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("اختصارات لوحة المفاتيح")
        self.setFixedSize(580, 520)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)

        # Header
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)

        icon_label = QLabel("⌨️", self)
        icon_label.setStyleSheet("font-size: 24px;")
        header.addWidget(icon_label)

        title_col = QVBoxLayout()
        title_col.setSpacing(3)
        title = QLabel("اختصارات لوحة المفاتيح", self)
        title.setStyleSheet("color: #FFFFFF; font-size: 18px; font-weight: 700;")
        title_col.addWidget(title)

        subtitle = QLabel("تنقل وأنجز عمليات البيع والجباية بسرعة واحترافية", self)
        subtitle.setStyleSheet("color: #8A94A6; font-size: 12px;")
        title_col.addWidget(subtitle)
        header.addLayout(title_col, 1)

        layout.addLayout(header)

        # Content card
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        content_widget = QWidget()
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(18)

        for section_title, items in self.SHORTCUTS:
            group_card = QFrame(content_widget)
            group_card.setObjectName("shortcutGroup")
            group_layout = QVBoxLayout(group_card)
            group_layout.setContentsMargins(14, 12, 14, 12)
            group_layout.setSpacing(10)

            sec_label = QLabel(section_title, group_card)
            sec_label.setStyleSheet("color: #9DBEFF; font-size: 13px; font-weight: 700;")
            group_layout.addWidget(sec_label)

            grid = QGridLayout()
            grid.setHorizontalSpacing(16)
            grid.setVerticalSpacing(8)

            for row_idx, (keys, desc) in enumerate(items):
                desc_label = QLabel(desc, group_card)
                desc_label.setStyleSheet("color: #C1C9D8; font-size: 13px;")

                keys_layout = QHBoxLayout()
                keys_layout.setSpacing(4)
                for part in keys.split("+"):
                    part_str = part.strip()
                    badge = QLabel(part_str, group_card)
                    badge.setStyleSheet(
                        "background-color: #121A2C; color: #FFFFFF; border: 1px solid #234275; "
                        "border-radius: 5px; padding: 3px 8px; font-size: 11px; font-weight: 700; "
                        "font-family: monospace;"
                    )
                    keys_layout.addWidget(badge)
                    if part != keys.split("+")[-1]:
                        plus = QLabel("+", group_card)
                        plus.setStyleSheet("color: #64748B; font-weight: 700;")
                        keys_layout.addWidget(plus)

                grid.addWidget(desc_label, row_idx, 0, Qt.AlignmentFlag.AlignRight)
                grid.addLayout(keys_layout, row_idx, 1, Qt.AlignmentFlag.AlignLeft)

            group_layout.addLayout(grid)
            group_card.setStyleSheet(
                "QFrame#shortcutGroup { "
                "  background-color: #0E1522; "
                "  border: 1px solid #1F2E47; "
                "  border-radius: 10px; "
                "}"
            )
            content_layout.addWidget(group_card)

        scroll.setWidget(content_widget)
        layout.addWidget(scroll, 1)

        # Close button
        btn_layout = QHBoxLayout()
        btn_layout.addStretch(1)
        close_btn = QPushButton("إغلاق", self)
        close_btn.setProperty("variant", "secondary")
        close_btn.setFixedWidth(100)
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)
