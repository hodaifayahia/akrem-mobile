"""Design tokens and the direction-aware application stylesheet.

``app/resources/theme.qss`` is a template. ``@token-name`` placeholders are
replaced with values from :data:`TOKENS`, ``@start`` / ``@end`` become the
physical sides for the active layout direction, and ``@resources`` becomes
the absolute resource folder (for ``url()`` images).

Qt already mirrors padding, ``text-align`` and sub-control positions for most
widgets in right-to-left mode, so the stylesheet writes those in logical
left-to-right terms. Qt does *not* mirror borders, so any one-sided border
must use ``border-@start`` or ``border-@end`` instead of a physical side.
"""

from __future__ import annotations

import re
from functools import lru_cache

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QGraphicsDropShadowEffect, QWidget

from app.config import RESOURCE_DIR

THEME_PATH = RESOURCE_DIR / "theme.qss"

TOKENS: dict[str, str] = {
    # Surfaces
    "bg": "#05070B",
    "bg-raised": "#080B12",
    "surface": "#0E141D",
    "surface-2": "#121A2C",
    "surface-3": "#17223A",
    "overlay": "#0A0F18",
    # Lines
    "border": "#1F2A3D",
    "border-strong": "#2A3954",
    # Brand
    "primary": "#0758CD",
    "primary-hover": "#1667DD",
    "primary-pressed": "#0549AD",
    "primary-glow": "#3B92D9",
    "primary-soft": "rgba(7, 88, 205, 0.18)",
    "highlight": "#9DBEFF",
    "silver": "#C1C1C3",
    # Text
    "text": "#EBF0FF",
    "text-muted": "#8A94A6",
    "text-disabled": "#4A5568",
    # Status
    "pending": "#F59E0B",
    "pending-soft": "rgba(245, 158, 11, 0.14)",
    "failed": "#EF4444",
    "failed-soft": "rgba(239, 68, 68, 0.14)",
    "paid": "#22C55E",
    "paid-soft": "rgba(34, 197, 94, 0.14)",
    "info-soft": "rgba(59, 146, 217, 0.14)",
    # Shape
    "radius-sm": "8px",
    "radius-md": "12px",
    "radius-lg": "16px",
}

#: Client-type colour keys (stored on the category row) and their swatches.
TYPE_COLOR_HEX: dict[str, str] = {
    "blue": "#3B82F6",
    "teal": "#14B8A6",
    "green": "#22C55E",
    "amber": "#F59E0B",
    "orange": "#F97316",
    "rose": "#F43F5E",
    "violet": "#8B5CF6",
    "slate": "#94A3B8",
}

#: Spacing scale in pixels; layouts should only use these values.
SPACE = {"xxs": 4, "xs": 8, "sm": 12, "md": 16, "lg": 20, "xl": 24, "xxl": 32}

_TOKEN_RE = re.compile(r"@([a-z][a-z0-9-]*)")
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def token(name: str) -> str:
    """Return one design token value, raising ``KeyError`` for unknown names."""
    return TOKENS[name]


def qcolor(name: str) -> QColor:
    """Return a design token as a ``QColor`` for custom painting."""
    return QColor(TOKENS[name])


def type_color(key: str | None) -> QColor:
    """Return the swatch for a client-type colour key, defaulting to slate."""
    return QColor(TYPE_COLOR_HEX.get(key or "", TYPE_COLOR_HEX["slate"]))


def render_stylesheet(direction: Qt.LayoutDirection, template: str | None = None) -> str:
    """Expand tokens and logical sides in the stylesheet template."""
    source = _COMMENT_RE.sub("", template if template is not None else _template_text())
    rtl = direction == Qt.LayoutDirection.RightToLeft
    sides = {"start": "right" if rtl else "left", "end": "left" if rtl else "right"}

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in sides:
            return sides[name]
        if name == "resources":
            return RESOURCE_DIR.as_posix()
        if name in TOKENS:
            return TOKENS[name]
        raise KeyError(f"Unknown theme token: @{name}")

    return _TOKEN_RE.sub(replace, source)


def apply_theme(app: QApplication, direction: Qt.LayoutDirection | None = None) -> None:
    """Set the application's layout direction and matching stylesheet."""
    resolved = direction if direction is not None else app.layoutDirection()
    if app.layoutDirection() != resolved:
        app.setLayoutDirection(resolved)
    sheet = render_stylesheet(resolved)
    if app.styleSheet() != sheet:
        # Re-polishing every widget is expensive; skip it when nothing changed.
        app.setStyleSheet(sheet)


def add_shadow(widget: QWidget, *, blur: int = 32, y_offset: int = 10, alpha: int = 150) -> None:
    """Give an elevated surface (popup, dialog card, toast) a soft drop shadow.

    Shadows are reserved for a few floating surfaces: a graphics effect on a
    large, frequently repainted widget is expensive.
    """
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, y_offset)
    effect.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(effect)


def repolish(widget: QWidget) -> None:
    """Re-apply stylesheet rules after a dynamic property changed."""
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


@lru_cache(maxsize=1)
def _template_text() -> str:
    return THEME_PATH.read_text(encoding="utf-8")
