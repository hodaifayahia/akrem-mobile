"""Localization package supporting Arabic (ar), English (en), and French (fr)."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.i18n import ar as _ar_module
from app.i18n import en as _en_module
from app.i18n import fr as _fr_module

SUPPORTED_LANGUAGES: dict[str, str] = {
    "ar": "العربية",
    "en": "English",
    "fr": "Français",
}
DEFAULT_LANGUAGE = "ar"

_current_language = DEFAULT_LANGUAGE

_MODULES = {
    "ar": _ar_module,
    "en": _en_module,
    "fr": _fr_module,
}


def get_language() -> str:
    """Return the active language code ('ar', 'en', or 'fr')."""
    return _current_language


def is_rtl(lang: str | None = None) -> bool:
    """Return whether the specified or current language uses right-to-left layout."""
    code = lang or _current_language
    return code == "ar"


def get_layout_direction(lang: str | None = None) -> Qt.LayoutDirection:
    """Return Qt LayoutDirection corresponding to the active language."""
    return Qt.LayoutDirection.RightToLeft if is_rtl(lang) else Qt.LayoutDirection.LeftToRight


def set_language(lang_code: str) -> None:
    """Set the active language ('ar', 'en', or 'fr') and update Qt layout direction."""
    global _current_language
    if lang_code not in SUPPORTED_LANGUAGES:
        lang_code = DEFAULT_LANGUAGE
    _current_language = lang_code

    app = QApplication.instance()
    if app is not None:
        app.setLayoutDirection(get_layout_direction())

    try:
        from app.ui.events import events

        events.language_changed.emit(lang_code)
    except Exception:
        pass


def tr(key: str, **kwargs: Any) -> str:
    """Translate a key in the active language, with optional format interpolation."""
    module = _MODULES.get(_current_language, _ar_module)
    val = getattr(module, key, getattr(_ar_module, key, key))
    if isinstance(val, str) and kwargs:
        return val.format(**kwargs)
    return str(val)


class _I18nProxy:
    """Dynamic translation proxy that resolves attribute access to the active language."""

    def __getattr__(self, name: str) -> Any:
        module = _MODULES.get(_current_language, _ar_module)
        if hasattr(module, name):
            return getattr(module, name)
        return getattr(_ar_module, name)

    def __getitem__(self, name: str) -> Any:
        return getattr(self, name)

    def __dir__(self) -> list[str]:
        return dir(_ar_module)

    @property
    def __name__(self) -> str:
        return "app.i18n.ar"

    @property
    def __file__(self) -> str:
        return _ar_module.__file__

    def __repr__(self) -> str:
        return f"<I18nProxy language='{_current_language}'>"


# Exported proxy so existing `from app.i18n import ar` dynamically adapts to active language
ar = _I18nProxy()
en = _en_module
fr = _fr_module

__all__ = [
    "SUPPORTED_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "get_language",
    "set_language",
    "is_rtl",
    "get_layout_direction",
    "tr",
    "ar",
    "en",
    "fr",
]
