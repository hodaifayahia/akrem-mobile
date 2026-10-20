"""Keep Latin product names in reading order inside right-to-left drop-down lists.

A non-editable ``QComboBox`` (a seller's product list, unit pickers) draws its
list like a menu and its closed box as a label, both in the interface
direction: in Arabic "13 PM" shows as "PM 13". An editable combo (the owner's
product list) lays its items out by their own direction, which is why the
problem only showed on some accounts. Only the drawn text is wrapped in
left-to-right isolate marks; the items' own text, which the sale code reads
with ``currentText``, is never changed.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPersistentModelIndex
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QComboBox, QStyle, QStyledItemDelegate, QStyleOptionComboBox, QStyleOptionViewItem, QStylePainter

from app.i18n.stock_text import ltr_text


class LtrItemDelegate(QStyledItemDelegate):
    """Draw each list item with Latin text kept left-to-right."""

    def initStyleOption(  # noqa: N802 - Qt API
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        super().initStyleOption(option, index)
        option.text = ltr_text(option.text)


def ensure_ltr_items(combo: QComboBox) -> None:
    """Give ``combo`` the item delegate; Qt replaces it again when the style changes."""
    if not isinstance(combo.itemDelegate(), LtrItemDelegate):
        combo.setItemDelegate(LtrItemDelegate(combo))


def paint_combo(combo: QComboBox) -> None:
    """Draw a non-editable combo like ``QComboBox.paintEvent``, with its text kept in reading order.

    Call only while ``combo`` handles its paint event.
    """
    painter = QStylePainter(combo)
    painter.setPen(combo.palette().color(QPalette.ColorRole.Text))
    option = QStyleOptionComboBox()
    combo.initStyleOption(option)
    painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
    if combo.currentIndex() < 0 and combo.placeholderText():
        option.palette.setBrush(QPalette.ColorRole.ButtonText, option.palette.placeholderText())
        option.currentText = combo.placeholderText()
    option.currentText = ltr_text(option.currentText)
    painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, option)
    painter.end()
