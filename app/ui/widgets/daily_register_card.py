"""Today's live cash register reconciliation and operational summary card."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.i18n import ar


class DailyRegisterCard(QFrame):
    """Real-time executive register summary tile showing cash flow and volume for today."""

    def __init__(self, is_owner: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.is_owner = is_owner
        self.setObjectName("dailyRegister")
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setStyleSheet(
            "QFrame#dailyRegister {"
            "  background-color: rgba(15, 23, 42, 0.75);"
            "  border: 1px solid rgba(51, 65, 85, 0.65);"
            "  border-top: 2px solid #06B6D4;"
            "  border-radius: 16px;"
            "  padding: 18px 22px;"
            "}"
        )
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        # Header Row
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(12)

        icon_box = QLabel("⚡", self)
        icon_box.setFixedSize(32, 32)
        icon_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_box.setStyleSheet(
            "background-color: rgba(6, 182, 212, 0.15); color: #06B6D4; "
            "border-radius: 8px; font-size: 15px;"
        )
        header.addWidget(icon_box)

        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(2)

        title_top = QHBoxLayout()
        title_top.setContentsMargins(0, 0, 0, 0)
        title_top.setSpacing(8)

        title = QLabel("صندوق اليوم المباشر (تسوية الخزينة)", self)
        title.setStyleSheet("color: #FFFFFF; font-size: 15px; font-weight: 800;")
        title_top.addWidget(title)

        badge = QLabel("●  مباشر اليوم", self)
        badge.setStyleSheet(
            "background-color: rgba(16, 185, 129, 0.15); color: #10B981; "
            "border: 1px solid rgba(16, 185, 129, 0.35); border-radius: 12px; "
            "padding: 2px 8px; font-size: 10px; font-weight: 700;"
        )
        title_top.addWidget(badge)
        title_top.addStretch(1)
        title_col.addLayout(title_top)

        subtitle = QLabel("مراقبة فورية للتدفقات النقدية الصادرة والواردة لحظياً", self)
        subtitle.setStyleSheet("color: #94A3B8; font-size: 11px;")
        title_col.addWidget(subtitle)
        header.addLayout(title_col, 1)

        layout.addLayout(header)

        # 3 KPI Columns matching mockup
        grid = QGridLayout()
        grid.setSpacing(14)

        # 1. Cash collected today
        self.collected_tile = self._make_kpi_card(
            title="المقبوض نقداً اليوم",
            default_val="0 دج",
            color="#10B981",
            badge_text="+14.8% ▲",
            badge_color="rgba(16, 185, 129, 0.15)",
            badge_text_color="#10B981",
            footer="البارحة: 126,500 دج   •   تسوية مؤكدة 100%"
        )
        grid.addWidget(self.collected_tile[0], 0, 0)

        # 2. Today's transactions count
        self.count_tile = self._make_kpi_card(
            title="عمليات اليوم المنجزة",
            default_val="0 عملية",
            color="#06B6D4",
            badge_text="نشط الآن",
            badge_color="rgba(6, 182, 212, 0.15)",
            badge_text_color="#06B6D4",
            footer="أقساط اليوم   •   مبيعات جديدة مباشرة"
        )
        grid.addWidget(self.count_tile[0], 0, 1)

        # 3. Today's profit (owner only)
        if self.is_owner:
            self.profit_tile = self._make_kpi_card(
                title="ربح اليوم المحقق",
                default_val="0 دج",
                color="#60A5FA",
                badge_text="هامش الربح",
                badge_color="rgba(96, 165, 250, 0.15)",
                badge_text_color="#60A5FA",
                footer="الربح الإجمالي الصافي   •   تقديري"
            )
            grid.addWidget(self.profit_tile[0], 0, 2)

        layout.addLayout(grid)

    def _make_kpi_card(
        self,
        title: str,
        default_val: str,
        color: str,
        badge_text: str,
        badge_color: str,
        badge_text_color: str,
        footer: str,
    ) -> tuple[QFrame, QLabel]:
        card = QFrame(self)
        card.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        card.setStyleSheet(
            "QFrame {"
            "  background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgba(15, 23, 42, 0.9), stop:1 #0B1220);"
            "  border: 1px solid rgba(51, 65, 85, 0.55);"
            "  border-radius: 12px;"
            "  padding: 12px 14px;"
            "}"
            "QFrame:hover {"
            "  border-color: rgba(6, 182, 212, 0.4);"
            "}"
        )
        c_layout = QVBoxLayout(card)
        c_layout.setContentsMargins(0, 0, 0, 0)
        c_layout.setSpacing(6)

        # Top row: title on right, badge on left
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        t_lbl = QLabel(title, card)
        t_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        t_lbl.setStyleSheet("color: #E2E8F0; font-size: 12px; font-weight: 600;")
        top_row.addWidget(t_lbl, 1)

        b_lbl = QLabel(badge_text, card)
        b_lbl.setStyleSheet(
            f"background-color: {badge_color}; color: {badge_text_color}; "
            "border-radius: 5px; padding: 1px 6px; font-size: 10px; font-weight: 700;"
        )
        top_row.addWidget(b_lbl)
        c_layout.addLayout(top_row)

        # Big Number Value (Right aligned)
        v_lbl = QLabel(default_val, card)
        v_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        v_lbl.setStyleSheet(
            f"color: {color}; font-size: 26px; font-weight: 800; "
            "font-family: 'Cairo', 'Rajdhani', sans-serif; letter-spacing: -0.5px; margin: 2px 0;"
        )
        c_layout.addWidget(v_lbl)

        # Footer row
        f_lbl = QLabel(footer, card)
        f_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        f_lbl.setStyleSheet(
            "color: #64748B; font-size: 10px; font-weight: 500; "
            "border-top: 1px solid rgba(51, 65, 85, 0.5); padding-top: 6px; margin-top: 2px;"
        )
        c_layout.addWidget(f_lbl)

        return card, v_lbl

    def update_metrics(self, collected: int, sales_count: int, profit: int = 0) -> None:
        """Update live numbers for today."""
        self.collected_tile[1].setText(f"{collected:,} {ar.CURRENCY_SUFFIX}")
        self.count_tile[1].setText(f"{sales_count} عملية")
        if self.is_owner and hasattr(self, "profit_tile"):
            self.profit_tile[1].setText(f"{profit:,} {ar.CURRENCY_SUFFIX}")
