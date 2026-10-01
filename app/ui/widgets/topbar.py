"""Executive Topbar featuring Breadcrumbs, Spotlight search, Live Clock, and Alerts."""

from __future__ import annotations

from datetime import datetime
from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.i18n import ar, get_language, get_layout_direction, set_language
from app.ui.widgets.notification_center import NotificationCenterPopup


class AppTopBar(QFrame):
    """Modern top navigation bar with search bar, alerts, and live Algerian clock."""

    command_palette_requested = Signal()
    shortcuts_requested = Signal()
    new_sale_requested = Signal()
    customer_opened = Signal(int)

    ARABIC_DAYS = ["الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
    ARABIC_MONTHS = [
        "", "جانفي", "فيفري", "مارس", "أفريل", "ماي", "جوان",
        "جويلية", "أوت", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"
    ]
    ENGLISH_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    ENGLISH_MONTHS = [
        "", "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December"
    ]
    FRENCH_DAYS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    FRENCH_MONTHS = [
        "", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
        "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"
    ]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("appTopBar")
        self.setFixedHeight(62)
        self.setLayoutDirection(get_layout_direction())

        self.notification_popup = NotificationCenterPopup(self)
        self.notification_popup.customer_opened.connect(self.customer_opened)

        self._active_page_idx = 0
        self._build_ui()
        self._setup_clock()
        self.refresh_notification_badge()

        from app.ui.events import events
        events.language_changed.connect(self._on_global_language_changed)

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 8, 20, 8)
        layout.setSpacing(14)

        # 1. Breadcrumb & Active Page Indicator
        breadcrumb_col = QVBoxLayout()
        breadcrumb_col.setContentsMargins(0, 0, 0, 0)
        breadcrumb_col.setSpacing(2)

        self.page_title_label = QLabel("📊 لوحة التحكم", self)
        self.page_title_label.setStyleSheet("color: #FFFFFF; font-size: 15px; font-weight: 700;")
        breadcrumb_col.addWidget(self.page_title_label)

        self.page_sub_label = QLabel("نظرة عامة على المقابيض والديون", self)
        self.page_sub_label.setStyleSheet("color: #8A94A6; font-size: 11px;")
        breadcrumb_col.addWidget(self.page_sub_label)

        layout.addLayout(breadcrumb_col)

        layout.addSpacing(16)

        # 2. Spotlight Search Bar Button (Ctrl+K)
        self.search_btn = QPushButton(self)
        self.search_btn.setObjectName("spotlightButton")
        self.search_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.search_btn.setMinimumWidth(240)
        self.search_btn.setMaximumWidth(400)
        self.search_btn.setFixedHeight(38)
        self.search_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: rgba(11, 18, 33, 0.85);"
            "  border: 1px solid rgba(51, 65, 85, 0.65);"
            "  border-radius: 10px;"
            "}"
            "QPushButton:hover {"
            "  border-color: #06B6D4;"
            "  background-color: rgba(15, 23, 42, 0.95);"
            "}"
        )

        search_btn_layout = QHBoxLayout(self.search_btn)
        search_btn_layout.setContentsMargins(12, 0, 10, 0)
        search_btn_layout.setSpacing(8)

        s_icon = QLabel("🔍", self.search_btn)
        s_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        s_icon.setStyleSheet("color: #06B6D4; font-size: 13px;")
        search_btn_layout.addWidget(s_icon)

        self.search_text_label = QLabel("ابحث عن زبون، هاتف، صفحة، أو إجراء...", self.search_btn)
        self.search_text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.search_text_label.setStyleSheet("color: #94A3B8; font-size: 12px; font-weight: 500;")
        search_btn_layout.addWidget(self.search_text_label, 1)

        hk_badge = QLabel("Ctrl+K", self.search_btn)
        hk_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        hk_badge.setStyleSheet(
            "background-color: #0F172A; color: #38BDF8; border: 1px solid #1E293B; "
            "border-radius: 5px; padding: 2px 7px; font-size: 10px; font-weight: 700; font-family: monospace;"
        )
        search_btn_layout.addWidget(hk_badge)

        self.search_btn.clicked.connect(self.command_palette_requested.emit)
        layout.addWidget(self.search_btn)

        layout.addStretch(1)

        # 3. Quick Action: + New Sale
        self.new_sale_btn = QPushButton("➕  بيع جديد", self)
        self.new_sale_btn.setProperty("variant", "primary")
        self.new_sale_btn.setToolTip("تسجيل بيع جديد بالتقسيط (Ctrl+N)")
        self.new_sale_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.new_sale_btn.setStyleSheet(
            "QPushButton {"
            "  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #06B6D4, stop:1 #2563EB);"
            "  color: #FFFFFF; font-weight: 800; font-size: 13px; "
            "  border-radius: 10px; padding: 8px 18px; border: none;"
            "}"
            "QPushButton:hover {"
            "  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #22D3EE, stop:1 #3B82F6);"
            "}"
        )
        self.new_sale_btn.clicked.connect(self.new_sale_requested.emit)
        layout.addWidget(self.new_sale_btn)

        # 4. Keyboard Shortcuts Trigger
        self.kb_btn = QPushButton("⌨️", self)
        self.kb_btn.setFixedSize(38, 38)
        self.kb_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.kb_btn.setToolTip("دليل اختصارات لوحة المفاتيح (F1)")
        self.kb_btn.setStyleSheet(
            "QPushButton { background-color: #0F172A; border: 1px solid #1E293B; border-radius: 10px; font-size: 15px; } "
            "QPushButton:hover { background-color: #1E293B; border-color: #06B6D4; }"
        )
        self.kb_btn.clicked.connect(self.shortcuts_requested.emit)
        layout.addWidget(self.kb_btn)

        # 5. Notification Bell with badge
        self.bell_container = QWidget(self)
        bell_layout = QHBoxLayout(self.bell_container)
        bell_layout.setContentsMargins(0, 0, 0, 0)
        bell_layout.setSpacing(0)

        self.bell_btn = QPushButton("🔔", self.bell_container)
        self.bell_btn.setFixedSize(38, 38)
        self.bell_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bell_btn.setToolTip("مركز التنبيهات والأقساط المتأخرة")
        self.bell_btn.setStyleSheet(
            "QPushButton { background-color: #0F172A; border: 1px solid #1E293B; border-radius: 10px; font-size: 15px; } "
            "QPushButton:hover { background-color: #1E293B; border-color: #06B6D4; }"
        )
        self.bell_btn.clicked.connect(self._toggle_notifications)
        bell_layout.addWidget(self.bell_btn)

        self.bell_badge = QLabel("0", self.bell_container)
        self.bell_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.bell_badge.setStyleSheet(
            "background-color: #EF4444; color: #FFFFFF; font-size: 10px; font-weight: 700; "
            "border-radius: 8px; padding: 1px 5px; min-width: 14px; text-align: center;"
        )
        self.bell_badge.move(20, -2)
        layout.addWidget(self.bell_container)

        # 6. Algerian Live Clock & Date Widget
        clock_card = QFrame(self)
        clock_card.setObjectName("topBarClock")
        clock_layout = QVBoxLayout(clock_card)
        clock_layout.setContentsMargins(10, 4, 10, 4)
        clock_layout.setSpacing(1)

        self.time_label = QLabel("00:00:00", clock_card)
        self.time_label.setStyleSheet("color: #38BDF8; font-weight: 800; font-size: 13px; font-family: monospace;")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        clock_layout.addWidget(self.time_label)

        self.date_label = QLabel("", clock_card)
        self.date_label.setStyleSheet("color: #94A3B8; font-size: 10px;")
        self.date_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        clock_layout.addWidget(self.date_label)

        clock_card.setStyleSheet(
            "QFrame#topBarClock { "
            "  background-color: #0F172A; "
            "  border: 1px solid #1E293B; "
            "  border-radius: 10px; "
            "}"
        )
        layout.addWidget(clock_card)

        # 7. Quick Language Switcher
        self.lang_selector = QComboBox(self)
        self.lang_selector.setObjectName("topBarLangSelector")
        self.lang_selector.addItem("🇩🇿 دج (DZD)", "ar")
        self.lang_selector.addItem("🇬🇧 English", "en")
        self.lang_selector.addItem("🇫🇷 Français", "fr")
        self.lang_selector.setCursor(Qt.CursorShape.PointingHandCursor)
        cur_lang = get_language()
        idx = self.lang_selector.findData(cur_lang)
        if idx >= 0:
            self.lang_selector.setCurrentIndex(idx)
        self.lang_selector.setStyleSheet(
            "QComboBox#topBarLangSelector { background-color: #0E1522; color: #E5EDFF; border: 1px solid #1F2E47; "
            "border-radius: 8px; padding: 4px 8px; font-size: 11px; font-weight: 700; min-height: 28px; } "
            "QComboBox#topBarLangSelector:hover { border-color: #3B92D9; background-color: #162032; } "
            "QComboBox#topBarLangSelector::drop-down { border: none; width: 14px; } "
            "QComboBox#topBarLangSelector QAbstractItemView { background-color: #0E1522; color: #FFFFFF; "
            "selection-background-color: #1A6FE8; selection-color: #FFFFFF; border: 1px solid #1F2E47; padding: 4px; }"
        )
        self.lang_selector.currentIndexChanged.connect(self._on_lang_changed)
        layout.addWidget(self.lang_selector)

        # Topbar CSS
        self.setStyleSheet(
            "QFrame#appTopBar { "
            "  background-color: #070B12; "
            "  border-bottom: 1px solid #162032; "
            "} "
            "QPushButton#spotlightButton { "
            "  background-color: #0E1522; "
            "  border: 1px solid #1F2E47; "
            "  border-radius: 8px; "
            "} "
            "QPushButton#spotlightButton:hover { "
            "  background-color: #121A2C; "
            "  border-color: #3B92D9; "
            "}"
        )

    def _setup_clock(self) -> None:
        self._update_clock()
        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start()

    def _update_clock(self) -> None:
        now = datetime.now()
        self.time_label.setText(now.strftime("%H:%M:%S"))
        lang = get_language()
        if lang == "en":
            day_name = self.ENGLISH_DAYS[now.weekday()]
            month_name = self.ENGLISH_MONTHS[now.month]
            self.date_label.setText(f"{day_name}, {month_name[:3]} {now.day}, {now.year}")
        elif lang == "fr":
            day_name = self.FRENCH_DAYS[now.weekday()]
            month_name = self.FRENCH_MONTHS[now.month]
            self.date_label.setText(f"{day_name} {now.day} {month_name} {now.year}")
        else:
            day_name = self.ARABIC_DAYS[now.weekday()]
            month_name = self.ARABIC_MONTHS[now.month]
            self.date_label.setText(f"{day_name}، {now.day} {month_name} {now.year}")

    def set_active_page(self, page_index: int) -> None:
        """Update breadcrumbs to match current active view in active language."""
        self._active_page_idx = page_index
        lang = get_language()
        icons = ["📊", "👥", "🛒", "💳", "📥", "📈", "⚙️"]
        titles = {
            "ar": [
                "لوحة التحكم",
                "إدارة الزبائن",
                "بيع جديد",
                "الدفعات والمقبوضات",
                "استيراد البيانات",
                "التقارير المالية",
                "الإعدادات العامة",
            ],
            "en": [
                "Dashboard",
                "Customer Management",
                "New Sale",
                "Payments & Collections",
                "Data Import",
                "Financial Reports",
                "Settings",
            ],
            "fr": [
                "Tableau de bord",
                "Gestion des Clients",
                "Nouvelle Vente",
                "Paiements & Encaissements",
                "Import de Données",
                "Rapports Financiers",
                "Paramètres",
            ],
        }
        title_list = titles.get(lang, titles["ar"])
        if 0 <= page_index < len(title_list):
            icon = icons[page_index] if page_index < len(icons) else "📄"
            title = title_list[page_index]
            self.page_title_label.setText(f"{icon}  {title}")
            subs = {
                "ar": [
                    "نظرة عامة على المقابيض والديون",
                    "سجلات الحسابات والملفات الشخصية",
                    "إنشاء عقد بيع بالتقسيط أو نقداً",
                    "سداد وجباية الأقساط الشهرية",
                    "معالجة واستيراد جداول Excel",
                    "كشوفات الأرباح والمبيعات",
                    "إدارة المستخدمين والأمان",
                ],
                "en": [
                    "Overview of cash flow and receivables",
                    "Customer accounts and credit profiles",
                    "Create cash or installment contract",
                    "Collect and monitor monthly installments",
                    "Process and import Excel spreadsheets",
                    "Financial profits and sales statements",
                    "User management and security settings",
                ],
                "fr": [
                    "Vue d'ensemble des encaissements et créances",
                    "Comptes clients et profils de crédit",
                    "Créer un contrat comptant ou facilité",
                    "Suivi et encaissement des mensualités",
                    "Traitement et importation de feuilles Excel",
                    "États financiers, bénéfices et ventes",
                    "Gestion des utilisateurs et sécurité",
                ],
            }
            sub_list = subs.get(lang, subs["ar"])
            if page_index < len(sub_list):
                self.page_sub_label.setText(sub_list[page_index])

    def refresh_translations(self) -> None:
        """Refresh topbar texts when language changes."""
        self.setLayoutDirection(get_layout_direction())
        if hasattr(self, "search_text_label"):
            self.search_text_label.setText(ar.SEARCH_CUSTOMER)
        if hasattr(self, "new_sale_btn"):
            self.new_sale_btn.setText(f"➕ {ar.SIDEBAR_ITEMS[2]}")
            self.new_sale_btn.setToolTip(f"{ar.SIDEBAR_ITEMS[2]} (Ctrl+N)")
        self.set_active_page(self._active_page_idx)
        self._update_clock()

    def _on_lang_changed(self, index: int) -> None:
        code = self.lang_selector.itemData(index)
        if code and code != get_language():
            set_language(code)
            from PySide6.QtCore import QSettings
            from app.config import APP_NAME
            QSettings(APP_NAME, APP_NAME).setValue("language", code)
            try:
                from app.db.session import session_scope
                from app.services import settings
                with session_scope() as session:
                    settings.set_value(session, "language", code)
            except Exception:
                pass

    def _on_global_language_changed(self, code: str) -> None:
        idx = self.lang_selector.findData(code)
        if idx >= 0 and self.lang_selector.currentIndex() != idx:
            self.lang_selector.blockSignals(True)
            self.lang_selector.setCurrentIndex(idx)
            self.lang_selector.blockSignals(False)
        self.refresh_translations()

    def refresh_notification_badge(self) -> None:
        self.notification_popup.refresh_alerts()
        count = self.notification_popup.get_alert_count()
        if count > 0:
            self.bell_badge.setText(str(count))
            self.bell_badge.setVisible(True)
        else:
            self.bell_badge.setVisible(False)

    def _toggle_notifications(self) -> None:
        if self.notification_popup.isVisible():
            self.notification_popup.hide()
        else:
            self.refresh_notification_badge()
            btn_pos = self.bell_btn.mapToGlobal(QPoint(0, self.bell_btn.height() + 8))
            popup_x = btn_pos.x() - 180
            popup_y = btn_pos.y()
            self.notification_popup.move(popup_x, popup_y)
            self.notification_popup.show()

