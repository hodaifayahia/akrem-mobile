"""Latin product names keep their reading order in Arabic drop-down lists (seller's "PM 13" bug)."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication, QComboBox, QStyleOptionViewItem

from app.ui.locale import apply_language
from app.ui.widgets.ltr_combo import LtrItemDelegate


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    apply_language("ar")
    return app


def _drawn_text(combo: QComboBox, row: int) -> str:
    option = QStyleOptionViewItem()
    combo.itemDelegate().initStyleOption(option, combo.model().index(row, 0))
    return option.text


@pytest.mark.parametrize("editable", [False, True])
def test_a_drop_down_draws_latin_names_left_to_right(qapp: QApplication, editable: bool) -> None:
    combo = QComboBox()
    combo.setEditable(editable)
    combo.addItems(["13 PM", "سامسونج 13"])
    combo.show()
    qapp.processEvents()

    assert isinstance(combo.itemDelegate(), LtrItemDelegate)
    assert _drawn_text(combo, 0) == "⁦13 PM⁩"
    assert _drawn_text(combo, 1) == "سامسونج 13"  # Arabic text is left as it is
    # Only the drawing changes: the sale code still reads the plain name.
    assert combo.currentText() == "13 PM"
    assert combo.findText("13 PM") == 0
    combo.close()
