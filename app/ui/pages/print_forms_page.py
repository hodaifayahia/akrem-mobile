"""Owner page: the texts of the printed commitment form (تعهد والتزام) and receipts.

The editor on one side changes the shop identity, the commitment wording
(intro, numbered terms, signature boxes, last line) and the receipt footer;
lists can grow, shrink and be reordered. The other side shows the real
document, rendered with the current text and sample data, as you type.
Nothing changes in print until "Save" is pressed.
"""

from __future__ import annotations

from dataclasses import asdict

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from sqlalchemy.exc import SQLAlchemyError

from app.config import data_dir, ensure_data_dirs
from app.db.models import User
from app.db.session import session_scope
from app.i18n import ar
from app.services import auth, pdf_forms, print_templates
from app.services.print_templates import PrintTemplates
from app.ui import icons
from app.ui.events import events

_PREVIEW_DELAY_MS = 250


class LineListEditor(QWidget):
    """An editable list of text lines: add, remove, reorder; double-click a line to edit it."""

    changed = Signal()

    def __init__(self, *, max_items: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.max_items = max_items
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.list = QListWidget(self)
        self.list.setWordWrap(True)
        self.list.setAlternatingRowColors(True)
        self.list.setEditTriggers(
            QListWidget.EditTrigger.DoubleClicked | QListWidget.EditTrigger.EditKeyPressed
            | QListWidget.EditTrigger.SelectedClicked
        )
        self.list.setMinimumHeight(110)
        self.list.itemChanged.connect(lambda _item: self.changed.emit())
        self.list.currentRowChanged.connect(lambda _row: self._sync_buttons())
        layout.addWidget(self.list)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.add_button = self._button("plus", ar.PRINT_LIST_ADD, self._add, text=True)
        self.remove_button = self._button("trash", ar.PRINT_LIST_REMOVE, self._remove)
        self.up_button = self._button("arrow-up", ar.PRINT_LIST_UP, lambda: self._move(-1))
        self.down_button = self._button("arrow-down", ar.PRINT_LIST_DOWN, lambda: self._move(1))
        buttons.addWidget(self.add_button)
        buttons.addStretch(1)
        for button in (self.up_button, self.down_button, self.remove_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        hint = QLabel(ar.PRINT_LIST_HINT, self)
        hint.setObjectName("sectionHint")
        layout.addWidget(hint)
        self._sync_buttons()

    def _button(self, icon_name: str, tip: str, slot, *, text: bool = False) -> QWidget:
        if text:
            button = QPushButton(icons.icon(icon_name, "primary-glow"), tip, self)
            button.setProperty("variant", "secondary")
        else:
            button = QToolButton(self)
            button.setIcon(icons.icon(icon_name, "text"))
            button.setIconSize(QSize(16, 16))
            button.setObjectName("iconButton")
        button.setToolTip(tip)
        button.setAccessibleName(tip)
        button.clicked.connect(slot)
        return button

    def set_lines(self, lines: tuple[str, ...] | list[str]) -> None:
        """Replace the lines without emitting ``changed``."""
        self.list.blockSignals(True)
        self.list.clear()
        for line in lines:
            self.list.addItem(self._item(line))
        self.list.blockSignals(False)
        self._sync_buttons()

    def lines(self) -> list[str]:
        """The non-blank lines, in order."""
        texts = (self.list.item(row).text().strip() for row in range(self.list.count()))
        return [text for text in texts if text]

    def _item(self, text: str) -> QListWidgetItem:
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        return item

    def _add(self) -> None:
        if self.list.count() >= self.max_items:
            return
        item = self._item(ar.PRINT_LIST_NEW_LINE)
        self.list.addItem(item)
        self.list.setCurrentItem(item)
        self.list.editItem(item)
        self._sync_buttons()
        self.changed.emit()

    def _remove(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        self.list.takeItem(row)
        self._sync_buttons()
        self.changed.emit()

    def _move(self, step: int) -> None:
        row = self.list.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.list.count():
            return
        item = self.list.takeItem(row)
        self.list.insertItem(target, item)
        self.list.setCurrentRow(target)
        self.changed.emit()

    def _sync_buttons(self) -> None:
        row = self.list.currentRow()
        count = self.list.count()
        self.add_button.setEnabled(count < self.max_items)
        self.remove_button.setEnabled(row >= 0)
        self.up_button.setEnabled(row > 0)
        self.down_button.setEnabled(0 <= row < count - 1)


class PrintFormsPage(QWidget):
    """Edit the printed documents' texts (owner only) with a live preview."""

    PREVIEW_KINDS = (
        ("commitment", "PRINT_PREVIEW_COMMITMENT"),
        ("cash_receipt", "PRINT_PREVIEW_CASH"),
        ("payment_receipt", "PRINT_PREVIEW_PAYMENT"),
    )

    def __init__(self, current_user: User, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self._saved: PrintTemplates = print_templates.default_templates()
        self._loading = False
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(_PREVIEW_DELAY_MS)
        self._preview_timer.timeout.connect(self.refresh_preview)
        self._build()
        self.reload()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(12)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel(ar.NAV_PRINT_FORMS, self)
        title.setObjectName("pageTitle")
        subtitle = QLabel(ar.PAGE_SUBTITLE_PRINT_FORMS, self)
        subtitle.setObjectName("pageSubtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        self.status_label = QLabel(self)
        self.status_label.setObjectName("sectionHint")
        header.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignBottom)
        self.reset_button = QPushButton(icons.icon("refresh", "text"), ar.PRINT_RESET, self)
        self.reset_button.setProperty("variant", "secondary")
        self.reset_button.clicked.connect(self._reset)
        self.save_button = QPushButton(icons.icon("check", "#FFFFFF"), ar.PRINT_SAVE, self)
        self.save_button.clicked.connect(self.save)
        header.addWidget(self.reset_button, 0, Qt.AlignmentFlag.AlignBottom)
        header.addWidget(self.save_button, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(16)
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._scroll(self._build_shop_tab()), ar.PRINT_TAB_SHOP)
        self.tabs.addTab(self._scroll(self._build_commitment_tab()), ar.PRINT_TAB_COMMITMENT)
        self.tabs.addTab(self._scroll(self._build_receipt_tab()), ar.PRINT_TAB_RECEIPT)
        self.tabs.currentChanged.connect(self._follow_tab)
        body.addWidget(self.tabs, 5)
        body.addWidget(self._build_preview(), 4)
        root.addLayout(body, 1)

    def _scroll(self, content: QWidget) -> QScrollArea:
        area = QScrollArea(self)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(content)
        return area

    def _line(self, parent: QWidget, *, ltr: bool = False) -> QLineEdit:
        field = QLineEdit(parent)
        field.setMaxLength(print_templates.MAX_TEXT_LENGTH)
        if ltr:
            field.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        field.textChanged.connect(self._edited)
        return field

    def _form(self, parent: QWidget) -> QFormLayout:
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        return form

    def _build_shop_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        form = self._form(page)
        self.shop_name = self._line(page, ltr=True)
        self.shop_location = self._line(page)
        self.header_lines = LineListEditor(max_items=print_templates.MAX_LIST_ITEMS, parent=page)
        self.header_lines.changed.connect(self._edited)
        self.shop_phone = self._line(page, ltr=True)
        self.shop_address = self._line(page)
        self.business_nis = self._line(page, ltr=True)
        self.business_nif = self._line(page, ltr=True)
        self.business_rc = self._line(page, ltr=True)
        self.watermark = QCheckBox(ar.PRINT_WATERMARK, page)
        self.watermark.toggled.connect(self._edited)
        form.addRow(ar.PRINT_SHOP_NAME, self.shop_name)
        form.addRow(ar.PRINT_SHOP_LOCATION, self.shop_location)
        form.addRow(ar.PRINT_HEADER_LINES, self.header_lines)
        form.addRow(ar.PRINT_SHOP_PHONE, self.shop_phone)
        form.addRow(ar.PRINT_SHOP_ADDRESS, self.shop_address)
        form.addRow(ar.PRINT_NIS, self.business_nis)
        form.addRow(ar.PRINT_NIF, self.business_nif)
        form.addRow(ar.PRINT_RC, self.business_rc)
        form.addRow("", self.watermark)
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    def _build_commitment_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        form = self._form(page)
        self.commitment_title = self._line(page)
        self.commitment_intro = QPlainTextEdit(page)
        self.commitment_intro.setMinimumHeight(96)
        self.commitment_intro.setMaximumHeight(130)
        self.commitment_intro.textChanged.connect(self._edited)
        placeholders = QLabel(ar.PRINT_PLACEHOLDERS_HINT, page)
        placeholders.setObjectName("sectionHint")
        placeholders.setWordWrap(True)
        placeholders.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        intro_box = QVBoxLayout()
        intro_box.setSpacing(4)
        intro_box.addWidget(self.commitment_intro)
        intro_box.addWidget(placeholders)
        self.commitment_clauses = LineListEditor(max_items=print_templates.MAX_LIST_ITEMS, parent=page)
        self.commitment_clauses.changed.connect(self._edited)
        self.commitment_signatures = LineListEditor(max_items=print_templates.MAX_SIGNATURES, parent=page)
        self.commitment_signatures.changed.connect(self._edited)
        self.commitment_footer = self._line(page)
        self.owner_figures = QCheckBox(ar.PRINT_OWNER_FIGURES, page)
        self.owner_figures.toggled.connect(self._edited)
        form.addRow(ar.PRINT_TITLE, self.commitment_title)
        form.addRow(ar.PRINT_INTRO, intro_box)
        form.addRow(ar.PRINT_CLAUSES, self.commitment_clauses)
        form.addRow(ar.PRINT_SIGNATURES, self.commitment_signatures)
        form.addRow(ar.PRINT_FOOTER, self.commitment_footer)
        form.addRow("", self.owner_figures)
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    def _build_receipt_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        form = self._form(page)
        self.receipt_footer = LineListEditor(max_items=print_templates.MAX_LIST_ITEMS, parent=page)
        self.receipt_footer.changed.connect(self._edited)
        form.addRow(ar.PRINT_RECEIPT_FOOTER, self.receipt_footer)
        layout.addLayout(form)
        note = QLabel(ar.PRINT_RECEIPT_NOTE, page)
        note.setObjectName("sectionHint")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    def _build_preview(self) -> QWidget:
        card = QFrame(self)
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        top = QHBoxLayout()
        caption = QVBoxLayout()
        caption.setSpacing(0)
        heading = QLabel(ar.PRINT_PREVIEW, card)
        heading.setObjectName("sectionTitle")
        note = QLabel(ar.PRINT_PREVIEW_NOTE, card)
        note.setObjectName("sectionHint")
        caption.addWidget(heading)
        caption.addWidget(note)
        top.addLayout(caption, 1)
        self.open_pdf_button = QPushButton(icons.icon("print", "text"), ar.PRINT_PREVIEW_PDF, card)
        self.open_pdf_button.setProperty("variant", "secondary")
        self.open_pdf_button.clicked.connect(self.open_preview_pdf)
        top.addWidget(self.open_pdf_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.preview_group = QButtonGroup(card)
        self.preview_group.setExclusive(True)
        self.preview_buttons: dict[str, QPushButton] = {}
        for kind, key in self.PREVIEW_KINDS:
            button = QPushButton(getattr(ar, key), card)
            button.setCheckable(True)
            button.setProperty("chip", True)
            button.clicked.connect(lambda _checked=False, value=kind: self.show_preview(value))
            self.preview_group.addButton(button)
            self.preview_buttons[kind] = button
            chips.addWidget(button)
        chips.addStretch(1)
        layout.addLayout(chips)

        self.preview_scroll = QScrollArea(card)
        self.preview_scroll.setWidgetResizable(True)
        self.preview_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.preview_label = QLabel(self.preview_scroll)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.preview_scroll.setWidget(self.preview_label)
        layout.addWidget(self.preview_scroll, 1)
        self.preview_kind = "commitment"
        self.preview_buttons["commitment"].setChecked(True)
        return card

    # ------------------------------------------------------------------ data
    def reload(self) -> None:
        """Load the saved texts into the editor."""
        try:
            with session_scope() as session:
                self._saved = print_templates.load(session)
        except SQLAlchemyError:
            self._saved = print_templates.default_templates()
        self._fill(self._saved)

    def _fill(self, templates: PrintTemplates) -> None:
        self._loading = True
        try:
            self.shop_name.setText(templates.shop_name)
            self.shop_location.setText(templates.shop_location)
            self.header_lines.set_lines(templates.header_lines)
            self.shop_phone.setText(templates.shop_phone)
            self.shop_address.setText(templates.shop_address)
            self.business_nis.setText(templates.business_nis)
            self.business_nif.setText(templates.business_nif)
            self.business_rc.setText(templates.business_rc)
            self.watermark.setChecked(templates.watermark)
            self.commitment_title.setText(templates.commitment_title)
            self.commitment_intro.setPlainText(templates.commitment_intro)
            self.commitment_clauses.set_lines(templates.commitment_clauses)
            self.commitment_signatures.set_lines(templates.commitment_signatures)
            self.commitment_footer.setText(templates.commitment_footer)
            self.owner_figures.setChecked(templates.commitment_owner_figures)
            self.receipt_footer.set_lines(templates.receipt_footer_lines)
        finally:
            self._loading = False
        self._update_status()
        self.schedule_preview()

    def values(self) -> PrintTemplates:
        """The texts as typed (normalized like the saved setting)."""
        return print_templates.normalize({
            "shop_name": self.shop_name.text(),
            "shop_location": self.shop_location.text(),
            "header_lines": self.header_lines.lines(),
            "shop_phone": self.shop_phone.text(),
            "shop_address": self.shop_address.text(),
            "business_nis": self.business_nis.text(),
            "business_nif": self.business_nif.text(),
            "business_rc": self.business_rc.text(),
            "commitment_title": self.commitment_title.text(),
            "commitment_intro": self.commitment_intro.toPlainText(),
            "commitment_clauses": self.commitment_clauses.lines(),
            "commitment_signatures": self.commitment_signatures.lines(),
            "commitment_footer": self.commitment_footer.text(),
            "commitment_owner_figures": self.owner_figures.isChecked(),
            "receipt_footer_lines": self.receipt_footer.lines(),
            "watermark": self.watermark.isChecked(),
        })

    def is_dirty(self) -> bool:
        """Whether the editor differs from what is saved."""
        return asdict(self.values()) != asdict(self._saved)

    def save(self) -> bool:
        """Store the texts (owner only); documents printed afterwards use them."""
        try:
            with session_scope() as session:
                self._saved = print_templates.save(session, int(self.current_user.id), self.values())
        except (auth.AuthorizationError, SQLAlchemyError, ValueError):
            QMessageBox.warning(self, ar.NAV_PRINT_FORMS, ar.PRINT_SAVE_ERROR)
            return False
        self._fill(self._saved)
        events.notify.emit("success", ar.NAV_PRINT_FORMS, ar.PRINT_SAVED, 3500)
        return True

    def _reset(self) -> None:
        answer = QMessageBox.question(
            self, ar.PRINT_RESET, ar.PRINT_RESET_CONFIRM,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.reset_to_defaults()

    def reset_to_defaults(self) -> bool:
        """Forget the owner's texts and show the original wording."""
        try:
            with session_scope() as session:
                self._saved = print_templates.reset(session, int(self.current_user.id))
        except (auth.AuthorizationError, SQLAlchemyError):
            QMessageBox.warning(self, ar.NAV_PRINT_FORMS, ar.PRINT_SAVE_ERROR)
            return False
        self._fill(self._saved)
        return True

    def _edited(self, *_args: object) -> None:
        if self._loading:
            return
        self._update_status()
        self.schedule_preview()

    def _update_status(self) -> None:
        dirty = self.is_dirty()
        self.status_label.setText(ar.PRINT_UNSAVED if dirty else ar.PRINT_ALL_SAVED)
        self.save_button.setEnabled(dirty)

    # --------------------------------------------------------------- preview
    def _follow_tab(self, index: int) -> None:
        """Show the document the open tab edits (the receipt tab shows the sale receipt)."""
        if index == 1:
            self.show_preview("commitment")
        elif index == 2 and self.preview_kind == "commitment":
            self.show_preview("cash_receipt")

    def show_preview(self, kind: str) -> None:
        """Switch the preview to another document."""
        self.preview_kind = kind
        self.preview_buttons[kind].setChecked(True)
        self.refresh_preview()

    def schedule_preview(self) -> None:
        """Re-render shortly after the last keystroke."""
        self._preview_timer.start()

    def refresh_preview(self) -> None:
        """Render the selected document with the current (unsaved) text."""
        viewport = self.preview_scroll.viewport().width()
        width = max(320, viewport - 8)
        ratio = self.devicePixelRatioF()
        image = pdf_forms.preview_image(self.preview_kind, self.values(), int(width * ratio))
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(ratio)
        self.preview_label.setPixmap(pixmap)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt event override
        super().resizeEvent(event)
        self.schedule_preview()

    def open_preview_pdf(self) -> None:
        """Write the selected sample document as a PDF and open it."""
        ensure_data_dirs()
        path = data_dir() / "exports" / f"preview_{self.preview_kind}.pdf"
        try:
            output = pdf_forms.write_preview_pdf(self.preview_kind, self.values(), path)
        except OSError:
            QMessageBox.warning(self, ar.PDF_SAVE_DIALOG_TITLE, ar.PDF_GENERATE_ERROR)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output)))
