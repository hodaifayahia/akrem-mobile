"""Export monthly collection, overdue, and owner profit reports."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDateEdit,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from app.config import data_dir
from app.db.models import User
from app.db.session import session_scope
from app.i18n import ar
from app.services import reports
from app.ui.events import events


class ReportsPage(QWidget):
    """Provide structured report cards with one-click Excel exports."""

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self._build_ui()

    def _build_ui(self) -> None:
        """Create header toolbar and interactive report cards."""
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(18)

        # Header with page title and month selector
        header_row = QHBoxLayout()
        heading = QLabel(ar.REPORT_PAGE_TITLE, self)
        heading.setObjectName("pageTitle")
        header_row.addWidget(heading)
        header_row.addStretch(1)

        month_container = QWidget(self)
        month_layout = QHBoxLayout(month_container)
        month_layout.setContentsMargins(0, 0, 0, 0)
        month_layout.setSpacing(8)
        month_label = QLabel(f"📅  {ar.REPORT_MONTH}:", month_container)
        month_label.setStyleSheet("color: #9DBEFF; font-weight: 600; font-size: 13px;")
        month_layout.addWidget(month_label)
        self.month_selector = QDateEdit(month_container)
        self.month_selector.setCalendarPopup(True)
        self.month_selector.setDisplayFormat("MM/yyyy")
        self.month_selector.setDate(QDate.currentDate())
        self.month_selector.setMinimumWidth(130)
        month_layout.addWidget(self.month_selector)
        header_row.addWidget(month_container)
        root.addLayout(header_row)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        cards_widget = QWidget(scroll)
        cards_layout = QVBoxLayout(cards_widget)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(16)
        scroll.setWidget(cards_widget)
        root.addWidget(scroll, 1)

        # Card 1: Monthly Collection Report
        card1 = self._build_report_card(
            icon="📊",
            title=ar.REPORT_COLLECTION_TITLE,
            description="تصدير جدول تفصيلي بجميع مبالغ الاقتطاعات والأقساط المستحقة والمدفوعة والمتبقية للشهر المحدد بتنسيق ملف Excel (.xlsx).",
            accent_color="#3B92D9",
        )
        self.collection_button = QPushButton(f"📥  {ar.REPORT_EXPORT_COLLECTION}", card1)
        self.collection_button.setProperty("variant", "primary")
        self.collection_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collection_button.clicked.connect(self._export_collection)
        card1.layout().addWidget(self.collection_button, alignment=Qt.AlignmentFlag.AlignLeft)
        cards_layout.addWidget(card1)

        # Card 2: Overdue Installments Report
        card2 = self._build_report_card(
            icon="⚠️",
            title=ar.REPORT_OVERDUE_TITLE,
            description="تصدير قائمة كاملة بجميع الزبائن المتأخرين عن السداد بعد انقضاء فترة السماح، مع أرقام هواتفهم وإجمالي المبالغ المتأخرة.",
            accent_color="#EF4444",
        )
        self.overdue_button = QPushButton(f"📥  {ar.REPORT_EXPORT_OVERDUE}", card2)
        self.overdue_button.setProperty("variant", "danger")
        self.overdue_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.overdue_button.clicked.connect(self._export_overdue)
        card2.layout().addWidget(self.overdue_button, alignment=Qt.AlignmentFlag.AlignLeft)
        cards_layout.addWidget(card2)

        # Card 3: Owner Profit Report
        self.profit_card = self._build_report_card(
            icon="💰",
            title=ar.REPORT_PROFIT_TITLE,
            description="تحليل مالي مفصل للأرباح الإجمالية المحققة حسب أشهر الشراء للسنة الحالية، مع مقارنة أسعار الجملة بسعر البيع النهائي.",
            accent_color="#22C55E",
        )
        self.profit_button = QPushButton(f"📥  {ar.REPORT_EXPORT_PROFIT}", self.profit_card)
        self.profit_button.setProperty("variant", "success")
        self.profit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.profit_button.clicked.connect(self._export_profit)
        self.profit_card.layout().addWidget(self.profit_button, alignment=Qt.AlignmentFlag.AlignLeft)
        cards_layout.addWidget(self.profit_card)

        # Card 4: Owner full data export (every table in one workbook)
        self.full_export_card = self._build_report_card(
            icon="🗂️",
            title=ar.REPORT_FULL_TITLE,
            description=ar.REPORT_FULL_DESC,
            accent_color="#9DBEFF",
        )
        self.full_export_button = QPushButton(ar.REPORT_EXPORT_FULL, self.full_export_card)
        self.full_export_button.setProperty("variant", "secondary")
        self.full_export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.full_export_button.clicked.connect(self._export_everything)
        self.full_export_card.layout().addWidget(self.full_export_button, alignment=Qt.AlignmentFlag.AlignLeft)
        cards_layout.addWidget(self.full_export_card)

        cards_layout.addStretch(1)

        is_owner = self.current_user.role == "owner"
        self.profit_card.setVisible(is_owner)
        self.profit_button.setVisible(is_owner)
        self.full_export_card.setVisible(is_owner)

    @staticmethod
    def _build_report_card(icon: str, title: str, description: str, accent_color: str) -> QFrame:
        """Create a styled report action card."""
        card = QFrame()
        card.setObjectName("reportCard")
        card.setStyleSheet(
            f"QFrame#reportCard {{"
            f"  background-color: #0E141D;"
            f"  border: 1px solid #1F2A3D;"
            f"  border-{'right' if QApplication.isRightToLeft() else 'left'}: 3px solid {accent_color};"
            f"  border-radius: 12px;"
            f"  padding: 18px 22px;"
            f"}}"
            f"QFrame#reportCard:hover {{"
            f"  background-color: #121A2C;"
            f"  border-color: {accent_color};"
            f"}}"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)

        icon_label = QLabel(icon, card)
        icon_label.setStyleSheet("font-size: 22px;")
        header.addWidget(icon_label)

        title_label = QLabel(title, card)
        title_label.setObjectName("reportCardTitle")
        header.addWidget(title_label)
        header.addStretch(1)
        layout.addLayout(header)

        desc_label = QLabel(description, card)
        desc_label.setObjectName("reportCardDesc")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        return card

    def _export_collection(self) -> None:
        """Create an Excel report for the selected month's scheduled amounts."""
        selected = self.month_selector.date()
        try:
            with session_scope() as session:
                rows = reports.monthly_collection_rows(
                    session,
                    year=selected.year(),
                    month=selected.month(),
                    today=date.today(),
                )
            destination = self._choose_destination(ar.REPORT_COLLECTION_TITLE)
            if destination is None:
                return
            reports.export_collection_xlsx(rows, destination)
        except (ValueError, PermissionError, RuntimeError, OSError, SQLAlchemyError):
            self._show_error()
            return
        self._show_success()

    def _export_overdue(self) -> None:
        """Create an Excel list of installments late beyond the grace period."""
        try:
            with session_scope() as session:
                rows = reports.overdue_rows(session, today=date.today())
            destination = self._choose_destination(ar.REPORT_OVERDUE_TITLE)
            if destination is None:
                return
            reports.export_overdue_xlsx(rows, destination)
        except (ValueError, PermissionError, RuntimeError, OSError, SQLAlchemyError):
            self._show_error()
            return
        self._show_success()

    def _export_profit(self) -> None:
        """Create the owner-only purchase-month profit report."""
        if self.current_user.role != "owner":
            return
        year = date.today().year
        try:
            with session_scope() as session:
                rows = reports.profit_by_month(
                    session,
                    start_year=year,
                    end_year=year,
                    owner_user_id=int(self.current_user.id),
                )
            destination = self._choose_destination(ar.REPORT_PROFIT_TITLE)
            if destination is None:
                return
            reports.export_profit_xlsx(rows, destination)
        except (ValueError, PermissionError, RuntimeError, OSError, SQLAlchemyError):
            self._show_error()
            return
        self._show_success()

    def _export_everything(self) -> None:
        """Owner-only workbook with every customer, sale, installment and payment."""
        if self.current_user.role != "owner":
            return
        exports = data_dir() / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, ar.REPORT_FULL_TITLE, str(exports / ar.REPORT_FULL_FILENAME), "Excel (*.xlsx)"
        )
        if path:
            self.export_everything_to(path)

    def export_everything_to(self, destination: str) -> bool:
        """Write the full data workbook to ``destination``; returns success."""
        try:
            with session_scope() as session:
                reports.export_full_workbook(
                    session, destination, owner_user_id=int(self.current_user.id)
                )
        except (ValueError, PermissionError, RuntimeError, OSError, SQLAlchemyError):
            self._show_error()
            return False
        self._show_success()
        return True

    def _choose_destination(self, title: str) -> str | None:
        """Ask for an export path, defaulting to the application's export folder."""
        exports = data_dir() / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        name = f"{title}-{date.today().isoformat()}.xlsx"
        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            title,
            str(exports / name),
            "Excel (*.xlsx)",
        )
        return path or None

    def _show_success(self) -> None:
        """Confirm the export completed."""
        events.notify.emit("success", ar.REPORT_PAGE_TITLE, ar.REPORT_EXPORT_DONE, 4500)
        QMessageBox.information(self, ar.REPORT_PAGE_TITLE, ar.REPORT_EXPORT_DONE)

    def _show_error(self) -> None:
        """Show a localized report failure."""
        events.notify.emit("error", ar.REPORT_PAGE_TITLE, ar.REPORT_EXPORT_ERROR, 4500)
        QMessageBox.warning(self, ar.REPORT_PAGE_TITLE, ar.REPORT_EXPORT_ERROR)
