"""Inline line-icon set rendered from SVG in any theme colour.

Icons are 24x24 stroke drawings authored for this project. Directional icons
(arrows, chevrons, "exit") are listed in :data:`MIRRORED` and are flipped
horizontally when the application runs right-to-left, so "back" and "next"
always point the way the reader moves.
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap, QTransform
from PySide6.QtSvg import QSvgRenderer

from app.ui.theme import TOKENS

_PATHS: dict[str, str] = {
    "dashboard": '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/>'
    '<rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    "users": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6"/>'
    '<path d="M16 4.6a3.5 3.5 0 0 1 0 6.8"/><path d="M18.5 14.3c1.8.8 3 2.7 3 5.7"/>',
    "cart": '<circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/>'
    '<path d="M2.5 3.5h2.6l2.4 11.2a1.5 1.5 0 0 0 1.5 1.2h8.6a1.5 1.5 0 0 0 1.5-1.1l1.9-7.3H6"/>',
    "wallet": '<rect x="2.5" y="5.5" width="19" height="14" rx="2.5"/><path d="M2.5 9.5h19"/>'
    '<path d="M16 14.5h2"/>',
    "import": '<path d="M12 3.5v11"/><path d="M7.5 10l4.5 4.5 4.5-4.5"/>'
    '<path d="M4 15.5v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
    "reports": '<path d="M3.5 20.5h17"/><rect x="5" y="11" width="3" height="7" rx="1"/>'
    '<rect x="10.5" y="6" width="3" height="12" rx="1"/><rect x="16" y="13.5" width="3" height="4.5" rx="1"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v2.4M12 19.1v2.4M4.6 4.6l1.7 1.7M17.7 17.7l1.7 1.7'
    'M2.5 12h2.4M19.1 12h2.4M4.6 19.4l1.7-1.7M17.7 6.3l1.7-1.7"/><circle cx="12" cy="12" r="6.6"/>',
    "bell": '<path d="M6 9.5a6 6 0 0 1 12 0c0 5 2 6.5 2 6.5H4s2-1.5 2-6.5"/><path d="M10 19.5a2 2 0 0 0 4 0"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    "lock": '<rect x="4.5" y="10.5" width="15" height="10" rx="2"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/>',
    "unlock": '<rect x="4.5" y="10.5" width="15" height="10" rx="2"/><path d="M8 10.5V7.5a4 4 0 0 1 7.6-1.7"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "close": '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    "keyboard": '<rect x="2.5" y="6" width="19" height="12" rx="2"/>'
    '<path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>',
    "chevron-forward": '<path d="M9.5 6l6 6-6 6"/>',
    "chevron-back": '<path d="M14.5 6l-6 6 6 6"/>',
    "chevron-down": '<path d="M6 9.5l6 6 6-6"/>',
    "chevron-up": '<path d="M6 14.5l6-6 6 6"/>',
    "exit": '<path d="M14.5 4.5h3a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-3"/><path d="M10 16.5l4.5-4.5L10 7.5"/>'
    '<path d="M14.5 12H3.5"/>',
    "sidebar": '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M9 4v16"/>',
    "tray": '<path d="M3.5 13.5h4.5l1.5 2.5h5l1.5-2.5h4.5"/>'
    '<path d="M5.4 6.2L3.5 13.5v4.5a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-4.5l-1.9-7.3a2 2 0 0 0-1.9-1.4H7.3a2 2 0 0 0-1.9 1.4z"/>',
    "tag": '<path d="M3.5 12.6V4.5a1 1 0 0 1 1-1h8.1l8.4 8.4a1.5 1.5 0 0 1 0 2.1l-6.9 6.9a1.5 1.5 0 0 1-2.1 0z"/>'
    '<circle cx="8.5" cy="8.5" r="1.5"/>',
    "edit": '<path d="M4 20h4L19.3 8.7a2.1 2.1 0 0 0-3-3L5 17v3z"/><path d="M14.5 7.5l3 3"/>',
    "trash": '<path d="M4.5 7h15"/><path d="M9.5 7V4.5h5V7"/><path d="M6.5 7l1 12.5a1.5 1.5 0 0 0 1.5 1.5h6'
    'a1.5 1.5 0 0 0 1.5-1.5l1-12.5"/>',
    "refresh": '<path d="M20 11.5A8 8 0 0 0 5.6 6.6L4 8.5"/><path d="M4 4v4.5h4.5"/>'
    '<path d="M4 12.5a8 8 0 0 0 14.4 4.9l1.6-1.9"/><path d="M20 20v-4.5h-4.5"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "check-circle": '<circle cx="12" cy="12" r="9"/><path d="M8 12.3l2.7 2.7L16 9.6"/>',
    "alert": '<path d="M12 3.5L2.5 20h19z"/><path d="M12 10v4.5"/><path d="M12 17.3h.01"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><path d="M12 7.7h.01"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
    "calendar": '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M3.5 3.5l17 17"/><path d="M10.6 5.6A9.5 9.5 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a17 17 0 0 1-2.8 3.6"/>'
    '<path d="M6.4 6.9C3.9 8.6 2.5 12 2.5 12S6 18.5 12 18.5c1.6 0 3-.4 4.2-1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4.5 20.5c0-4 3.4-6.5 7.5-6.5s7.5 2.5 7.5 6.5"/>',
    "window": '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M3 9h18"/>',
    "power": '<path d="M12 3v8.5"/><path d="M6.6 6.6a7.5 7.5 0 1 0 10.8 0"/>',
    "message": '<path d="M4 5.5h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-9l-5 3.5v-3.5H4a1 1 0 0 1-1-1v-10a1 1 0 0 1 1-1z"/>',
    "trend-up": '<path d="M3.5 17l6-6 4 4 7-7.5"/><path d="M15 7.5h5.5V13"/>',
    "cash": '<rect x="2.5" y="6" width="19" height="12" rx="2"/><circle cx="12" cy="12" r="2.5"/>'
    '<path d="M6 9.5v5M18 9.5v5"/>',
    "upload": '<path d="M12 15.5v-11"/><path d="M7.5 9l4.5-4.5 4.5 4.5"/>'
    '<path d="M4 15.5v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
    "sheet": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>'
    '<path d="M8.5 12.5h7M8.5 16h7M12 11v7"/>',
    "drag": '<path d="M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01"/>',
}

#: Icons whose meaning depends on reading direction.
MIRRORED = frozenset({"chevron-forward", "chevron-back", "exit"})

_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
    'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
)


def names() -> tuple[str, ...]:
    """Return every available icon name."""
    return tuple(sorted(_PATHS))


def pixmap(name: str, color: str = "text-muted", size: int = 18, *, stroke: float = 1.8) -> QPixmap:
    """Render an icon to a device-pixel-ratio aware pixmap.

    ``color`` may be a theme token name (``"primary-glow"``) or any CSS colour.
    """
    rtl = QGuiApplication.layoutDirection() == Qt.LayoutDirection.RightToLeft
    ratio = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    return QPixmap(_render(name, _resolve_color(color), size, stroke, rtl and name in MIRRORED, ratio))


def icon(name: str, color: str = "text-muted", size: int = 18, *, active_color: str | None = None) -> QIcon:
    """Return a ``QIcon``; ``active_color`` is used for the checked/selected state."""
    result = QIcon()
    result.addPixmap(pixmap(name, color, size), QIcon.Mode.Normal, QIcon.State.Off)
    result.addPixmap(pixmap(name, "text-disabled", size), QIcon.Mode.Disabled, QIcon.State.Off)
    if active_color is not None:
        active = pixmap(name, active_color, size)
        result.addPixmap(active, QIcon.Mode.Normal, QIcon.State.On)
        result.addPixmap(active, QIcon.Mode.Active, QIcon.State.Off)
        result.addPixmap(active, QIcon.Mode.Selected, QIcon.State.Off)
    return result


def clear_cache() -> None:
    """Drop rendered icons, e.g. after the layout direction changes."""
    _render.cache_clear()


def _resolve_color(color: str) -> str:
    return TOKENS.get(color, color)


@lru_cache(maxsize=512)
def _render(name: str, color: str, size: int, stroke: float, mirror: bool, ratio: float) -> QPixmap:
    body = _PATHS.get(name)
    if body is None:
        raise KeyError(f"Unknown icon: {name}")
    svg = _SVG.format(color=QColor(color).name(), width=stroke, body=body)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    device = max(1, round(size * ratio))
    image = QPixmap(QSize(device, device))
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, device, device))
    painter.end()
    if mirror:
        image = image.transformed(QTransform().scale(-1, 1))
    image.setDevicePixelRatio(ratio)
    return image
