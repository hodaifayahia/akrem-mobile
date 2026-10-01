"""Switch the interface language and its reading direction at runtime.

Arabic runs right-to-left; English and French run left-to-right. The
direction is set once on ``QApplication`` and every widget inherits it,
which is why widgets must not call ``setLayoutDirection`` themselves.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QSettings, Qt
from PySide6.QtWidgets import QApplication, QLabel

from app.config import APP_NAME
from app.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, get_layout_direction, set_language
from app.ui import icons
from app.ui.theme import apply_theme

LANGUAGE_SETTING = "language"
_LOGICAL_ALIGN = "logicalAlignment"
_HORIZONTAL = (
    Qt.AlignmentFlag.AlignLeft
    | Qt.AlignmentFlag.AlignRight
    | Qt.AlignmentFlag.AlignHCenter
    | Qt.AlignmentFlag.AlignJustify
    | Qt.AlignmentFlag.AlignAbsolute
)


class LabelDirectionFilter(QObject):
    """Align label text to the interface direction, not to the text's own.

    Qt aligns a label by the direction of its *content*: in Arabic, a label
    holding "41,850" or a Latin username drifts to the left edge, and in
    English an Arabic customer name drifts right. This filter turns a label's
    leading/trailing alignment into an absolute side that matches the
    application's layout direction, and re-applies it when the direction
    changes. Centred and already-absolute labels are left alone.
    """

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt API
        kind = event.type()
        if kind in (QEvent.Type.Polish, QEvent.Type.LayoutDirectionChange) and isinstance(watched, QLabel):
            align_to_direction(watched)
        return False


def align_to_direction(label: QLabel) -> None:
    """Pin a leading/trailing label alignment to the current direction."""
    logical = label.property(_LOGICAL_ALIGN)
    if logical is None:
        current = label.alignment()
        horizontal = current & _HORIZONTAL
        if horizontal & Qt.AlignmentFlag.AlignAbsolute or horizontal & (
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignJustify
        ):
            return
        trailing = bool(horizontal & Qt.AlignmentFlag.AlignRight)
        logical = "trailing" if trailing else "leading"
        label.setProperty(_LOGICAL_ALIGN, logical)
    vertical = label.alignment() & ~_HORIZONTAL
    rtl = label.isRightToLeft()
    to_right = (logical == "leading") == rtl
    side = Qt.AlignmentFlag.AlignRight if to_right else Qt.AlignmentFlag.AlignLeft
    label.setAlignment(Qt.AlignmentFlag.AlignAbsolute | side | vertical)


_label_filter: LabelDirectionFilter | None = None


def install_label_direction_filter(app: QApplication) -> None:
    """Install :class:`LabelDirectionFilter` once for the whole application."""
    global _label_filter
    if _label_filter is None:
        _label_filter = LabelDirectionFilter(app)
        app.installEventFilter(_label_filter)


def saved_language() -> str | None:
    """Return this PC's saved interface language, if one was chosen."""
    value = QSettings(APP_NAME, APP_NAME).value(LANGUAGE_SETTING, None)
    return value if value in SUPPORTED_LANGUAGES else None


def apply_language(code: str) -> None:
    """Activate a language, its layout direction and the matching stylesheet.

    ``set_language`` emits ``events.language_changed`` so open screens can
    refresh their text. The direction and stylesheet are applied first so
    those screens rebuild in the new direction.
    """
    resolved = code if code in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
    app = QApplication.instance()
    if isinstance(app, QApplication):
        install_label_direction_filter(app)
        icons.clear_cache()
        apply_theme(app, get_layout_direction(resolved))
    set_language(resolved)


def change_language(code: str) -> None:
    """Apply a language chosen by the user and remember it on this PC.

    The language is a per-PC preference (stored in QSettings, not in the
    shared database) so one seller's choice doesn't change another PC.
    """
    QSettings(APP_NAME, APP_NAME).setValue(LANGUAGE_SETTING, code)
    apply_language(code)
