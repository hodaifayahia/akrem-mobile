"""Navigation sidebar: brand, grouped navigation, user card and collapse toggle.

The sidebar sits on the reading-start side automatically: it is the first
widget in a horizontal layout, and Qt mirrors layouts in right-to-left mode.
Nothing here hard-codes "left" or "right".
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.config import RESOURCE_DIR
from app.db.models import User
from app.i18n import ar
from app.ui import icons
from app.ui.theme import qcolor, repolish

#: Navigation entries: (page index, icon name). Page titles come from ar.SIDEBAR_ITEMS.
NAV_ICONS = ("dashboard", "users", "cart", "wallet", "import", "reports", "settings")
MAIN_PAGES = (0, 1, 2, 3)
MANAGE_PAGES = (4, 5, 6)
OWNER_ONLY_PAGES = frozenset({4, 6})

EXPANDED_WIDTH = 248
COLLAPSED_WIDTH = 72


class NavButton(QPushButton):
    """Navigation item with an icon, optional count badge and active indicator.

    The indicator bar is painted on the reading-start edge, so it appears on
    the right in Arabic and on the left in English or French.
    """

    def __init__(self, icon_name: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.icon_name = icon_name
        self._title = text
        self._collapsed = False
        self.setObjectName("navButton")
        self.setProperty("active", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(18, 18))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.badge = QLabel(self)
        self.badge.setObjectName("navBadge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.badge.hide()
        self._refresh_icon()

    def set_active(self, active: bool) -> None:
        """Mark this item as the current page."""
        if self.property("active") is active:
            return
        self.setProperty("active", active)
        self._refresh_icon()
        repolish(self)

    def set_title(self, text: str) -> None:
        """Update the label after a language change."""
        self._title = text
        self.setText("" if self._collapsed else text)
        self.setToolTip(text if self._collapsed else "")
        self.setAccessibleName(text)

    def set_collapsed(self, collapsed: bool) -> None:
        """Show only the icon (with a tooltip) when the rail is collapsed."""
        self._collapsed = collapsed
        self.setProperty("collapsed", collapsed)
        self.set_title(self._title)
        repolish(self)
        self._place_badge()

    def set_badge(self, count: int) -> None:
        """Show a small count bubble; zero hides it."""
        if count <= 0:
            self.badge.hide()
            return
        self.badge.setText("99+" if count > 99 else str(count))
        self.badge.adjustSize()
        self.badge.show()
        self._place_badge()

    def refresh_icons(self) -> None:
        """Re-render the icon (after a theme or direction change)."""
        self._refresh_icon()

    def _refresh_icon(self) -> None:
        color = "highlight" if self.property("active") else "text-muted"
        self.setIcon(icons.icon(self.icon_name, color, 18, active_color="text"))

    def _place_badge(self) -> None:
        if self.badge.isHidden():
            return
        size = self.badge.sizeHint()
        width = max(size.width(), 18)
        y = (self.height() - 18) // 2 if not self._collapsed else 3
        if self._collapsed:
            x = self.width() - width - 6
        else:
            x = 10 if self.isRightToLeft() else self.width() - width - 10
        self.badge.setGeometry(x, y, width, 18)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt callback name
        """Keep the badge on the reading-end edge."""
        super().resizeEvent(event)
        self._place_badge()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt callback name
        """Paint the button, then the start-edge indicator when active."""
        super().paintEvent(event)
        if not self.property("active"):
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(qcolor("primary-glow"))
        bar_height = max(16, self.height() - 18)
        top = (self.height() - bar_height) / 2
        x = self.width() - 3 if self.isRightToLeft() else 0
        painter.drawRoundedRect(QRectF(x, top, 3, bar_height), 1.5, 1.5)
        painter.end()


class Sidebar(QFrame):
    """Application navigation rail."""

    page_requested = Signal(int)
    lock_requested = Signal()
    password_requested = Signal()
    collapsed_changed = Signal(bool)

    def __init__(self, current_user: User | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_user = current_user
        self.setObjectName("sidebar")
        self.buttons: list[NavButton] = []
        self.password_button: NavButton | None = None
        self.lock_button: NavButton | None = None
        self._section_labels: list[tuple[QLabel, str]] = []
        self._collapsed = False
        self._build()
        self.setFixedWidth(EXPANDED_WIDTH)

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 16, 12, 14)
        layout.setSpacing(2)
        layout.addWidget(self._build_brand())
        layout.addSpacing(10)

        self._add_section(layout, "NAV_SECTION_MAIN")
        for index in MAIN_PAGES:
            layout.addWidget(self._make_nav(index))
        self._add_section(layout, "NAV_SECTION_MANAGE")
        for index in MANAGE_PAGES:
            layout.addWidget(self._make_nav(index))

        if self.current_user is not None and self.current_user.role != "owner":
            self.password_button = NavButton("lock", ar.SET_CHANGE_PASSWORD, self)
            self.password_button.clicked.connect(self.password_requested.emit)
            layout.addWidget(self.password_button)

        layout.addStretch(1)
        if self.current_user is not None:
            layout.addWidget(self._build_user_card())
        layout.addSpacing(6)
        layout.addWidget(self._build_collapse_toggle())

    def _build_brand(self) -> QWidget:
        brand = QWidget(self)
        row = QHBoxLayout(brand)
        row.setContentsMargins(6, 0, 6, 0)
        row.setSpacing(10)
        self.logo = QLabel(brand)
        self.logo.setFixedSize(40, 40)
        logo_path = RESOURCE_DIR / "logo.png"
        if logo_path.exists():
            pixmap = QPixmap(str(logo_path)).scaled(
                40, 40, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            self.logo.setPixmap(pixmap)
        row.addWidget(self.logo)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(0)
        self.wordmark = QLabel(ar.BRAND_NAME, brand)
        self.wordmark.setObjectName("wordmark")
        self.tagline = QLabel(ar.BRAND_TAGLINE, brand)
        self.tagline.setObjectName("wordmarkSub")
        text_col.addWidget(self.wordmark)
        text_col.addWidget(self.tagline)
        self.brand_text = QWidget(brand)
        self.brand_text.setLayout(text_col)
        row.addWidget(self.brand_text, 1)
        return brand

    def _add_section(self, layout: QVBoxLayout, key: str) -> None:
        label = QLabel(getattr(ar, key), self)
        label.setObjectName("sidebarSection")
        self._section_labels.append((label, key))
        layout.addWidget(label)

    def _make_nav(self, index: int) -> NavButton:
        button = NavButton(NAV_ICONS[index], ar.SIDEBAR_ITEMS[index], self)
        button.clicked.connect(lambda _checked=False, i=index: self.page_requested.emit(i))
        if self._is_restricted(index):
            button.setVisible(False)
        # Sections list pages in index order, so buttons[i] is page i.
        self.buttons.append(button)
        return button

    def _build_user_card(self) -> QFrame:
        assert self.current_user is not None
        card = QFrame(self)
        card.setObjectName("sidebarUserBadge")
        self.user_card = card
        column = QVBoxLayout(card)
        column.setContentsMargins(10, 10, 10, 10)
        column.setSpacing(8)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(10)
        self.avatar = QLabel(_initials(self.current_user.username), card)
        self.avatar.setObjectName("avatar")
        self.avatar.setFixedSize(34, 34)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.avatar)

        identity = QVBoxLayout()
        identity.setContentsMargins(0, 0, 0, 0)
        identity.setSpacing(0)
        self.user_name_label = QLabel(self.current_user.username, card)
        self.user_name_label.setObjectName("sidebarUserName")
        self.role_badge = QLabel(self._role_text(), card)
        self.role_badge.setObjectName("sidebarUserRole")
        identity.addWidget(self.user_name_label)
        identity.addWidget(self.role_badge)
        self.identity = QWidget(card)
        self.identity.setLayout(identity)
        top.addWidget(self.identity, 1)
        column.addLayout(top)

        self.lock_button = NavButton("lock", ar.SIDEBAR_LOCK_SCREEN, card)
        self.lock_button.clicked.connect(self.lock_requested.emit)
        column.addWidget(self.lock_button)
        return card

    def _build_collapse_toggle(self) -> QToolButton:
        self.collapse_button = QToolButton(self)
        self.collapse_button.setObjectName("iconButton")
        self.collapse_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collapse_button.setFixedSize(34, 34)
        self.collapse_button.setIconSize(QSize(18, 18))
        self.collapse_button.clicked.connect(lambda: self.set_collapsed(not self._collapsed))
        self._refresh_collapse_button()
        return self.collapse_button

    # ------------------------------------------------------------------ state
    def set_active(self, index: int) -> None:
        """Highlight the navigation item for the visible page."""
        for button_index, button in enumerate(self.buttons):
            button.set_active(button_index == index)

    def set_badge(self, index: int, count: int) -> None:
        """Show a count on one navigation item (e.g. overdue payments)."""
        if 0 <= index < len(self.buttons):
            self.buttons[index].set_badge(count)

    def is_collapsed(self) -> bool:
        """Return whether only icons are shown."""
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        """Switch between the full sidebar and the compact icon rail."""
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self.setFixedWidth(COLLAPSED_WIDTH if collapsed else EXPANDED_WIDTH)
        self.brand_text.setVisible(not collapsed)
        for label, _key in self._section_labels:
            label.setVisible(not collapsed)
        for button in self._all_buttons():
            button.set_collapsed(collapsed)
        if hasattr(self, "identity"):
            self.identity.setVisible(not collapsed)
        self._refresh_collapse_button()
        self.collapsed_changed.emit(collapsed)

    def retranslate(self) -> None:
        """Refresh every label from the active language."""
        self.wordmark.setText(ar.BRAND_NAME)
        self.tagline.setText(ar.BRAND_TAGLINE)
        for label, key in self._section_labels:
            label.setText(getattr(ar, key))
        for index, button in enumerate(self.buttons):
            button.set_title(ar.SIDEBAR_ITEMS[index])
        if self.password_button is not None:
            self.password_button.set_title(ar.SET_CHANGE_PASSWORD)
        if self.lock_button is not None:
            self.lock_button.set_title(ar.SIDEBAR_LOCK_SCREEN)
        if hasattr(self, "role_badge"):
            self.role_badge.setText(self._role_text())
        for button in self._all_buttons():
            button.refresh_icons()
        self._refresh_collapse_button()

    # ---------------------------------------------------------------- helpers
    def _all_buttons(self) -> list[NavButton]:
        extra = [button for button in (self.password_button, self.lock_button) if button is not None]
        return [*self.buttons, *extra]

    def _is_restricted(self, index: int) -> bool:
        return (
            self.current_user is not None
            and self.current_user.role != "owner"
            and index in OWNER_ONLY_PAGES
        )

    def _role_text(self) -> str:
        if self.current_user is None:
            return ""
        return ar.ROLE_OWNER if self.current_user.role == "owner" else ar.ROLE_SELLER

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt callback name
        """Draw the 1px divider on the edge that faces the page content."""
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(qcolor("border"))
        x = 0 if self.isRightToLeft() else self.width() - 1
        painter.drawLine(x, 0, x, self.height())
        painter.end()

    def _refresh_collapse_button(self) -> None:
        # "back" points toward the sidebar's own edge, i.e. "collapse".
        name = "chevron-forward" if self._collapsed else "chevron-back"
        self.collapse_button.setIcon(icons.icon(name, "text-muted", 18))
        tip = ar.SIDEBAR_EXPAND if self._collapsed else ar.SIDEBAR_COLLAPSE
        self.collapse_button.setToolTip(tip)
        self.collapse_button.setAccessibleName(tip)


def _initials(username: str) -> str:
    """Return up to two initials for the avatar bubble."""
    parts = [part for part in username.replace("_", " ").replace(".", " ").split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()
