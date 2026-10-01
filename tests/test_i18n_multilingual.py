"""Comprehensive tests for multilingual support (Arabic, English, French)."""

from __future__ import annotations

import os
import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication
from sqlalchemy.engine import Engine

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.config import APP_NAME
from app.db.models import User
from app.db.session import session_scope
from app.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    ar,
    en,
    fr,
    get_language,
    get_layout_direction,
    is_rtl,
    set_language,
    tr,
)
from app.services import auth
from app.services.seed import seed_defaults
from app.services.settings import get_value
from app.ui.events import events
from app.ui.main_window import MainWindow
from app.ui.pages.settings_page import SettingsPage
from app.ui.widgets.topbar import AppTopBar


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def reset_language_state():
    """Ensure tests start and end in a clean Arabic default state."""
    set_language("ar")
    yield
    set_language("ar")


@pytest.fixture
def seeded_db(memory_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    monkeypatch.setattr("app.db.session._application_engine", lambda url: memory_engine)
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        owner = auth.create_first_owner(
            session,
            username="owner_i18n",
            password="SecretPassword123!",
            password_confirmation="SecretPassword123!",
        )
        return {"owner_id": owner.id}


class TestI18nParity:
    """Ensure complete key parity and validity across Arabic, English, and French."""

    def test_key_parity_across_all_languages(self):
        ar_keys = {k for k in dir(ar) if k.isupper() and not k.startswith("_")}
        en_keys = {k for k in dir(en) if k.isupper() and not k.startswith("_")}
        fr_keys = {k for k in dir(fr) if k.isupper() and not k.startswith("_")}

        assert len(ar_keys) >= 490, f"Expected 490+ keys, got {len(ar_keys)}"
        assert ar_keys == en_keys, f"Missing in EN: {ar_keys - en_keys}, Extra in EN: {en_keys - ar_keys}"
        assert ar_keys == fr_keys, f"Missing in FR: {ar_keys - fr_keys}, Extra in FR: {fr_keys - ar_keys}"

    def test_sidebar_items_parity(self):
        assert len(ar.SIDEBAR_ITEMS) == 7
        assert len(en.SIDEBAR_ITEMS) == 7
        assert len(fr.SIDEBAR_ITEMS) == 7
        assert en.SIDEBAR_ITEMS[0] == "Dashboard"
        assert fr.SIDEBAR_ITEMS[0] == "Tableau de bord"
        assert ar.SIDEBAR_ITEMS[0] == "لوحة التحكم"

    def test_no_empty_translations(self):
        for key in dir(ar):
            if key.isupper() and not key.startswith("_"):
                val_en = getattr(en, key)
                val_fr = getattr(fr, key)
                assert val_en, f"Empty English translation for {key}"
                assert val_fr, f"Empty French translation for {key}"


class TestI18nEngine:
    """Test runtime language switching, proxying, and interpolation."""

    def test_language_switching_and_properties(self):
        set_language("en")
        assert get_language() == "en"
        assert is_rtl() is False
        assert is_rtl("en") is False
        assert get_layout_direction() == Qt.LayoutDirection.LeftToRight
        assert ar.LOGIN_BUTTON == "Sign In"
        assert ar.APP_TITLE == "AkremMobile — Installment & Sales Management"

        set_language("fr")
        assert get_language() == "fr"
        assert is_rtl() is False
        assert is_rtl("fr") is False
        assert get_layout_direction() == Qt.LayoutDirection.LeftToRight
        assert ar.LOGIN_BUTTON == "Se connecter"
        assert ar.APP_TITLE == "AkremMobile — Gestion des Ventes & Facilités"

        set_language("ar")
        assert get_language() == "ar"
        assert is_rtl() is True
        assert is_rtl("ar") is True
        assert get_layout_direction() == Qt.LayoutDirection.RightToLeft
        assert ar.LOGIN_BUTTON == "دخول"

    def test_fallback_on_unknown_language(self):
        set_language("invalid_lang_code")
        assert get_language() == DEFAULT_LANGUAGE
        assert is_rtl() is True

    def test_tr_function_formatting(self):
        set_language("ar")
        ar_badge = tr("SET_BACKUP_AUTO_BADGE", count=3)
        assert "3" in ar_badge

        set_language("en")
        en_badge = tr("SET_BACKUP_AUTO_BADGE", count=3)
        assert en_badge == "Automated Backups: 3/3 today"

        set_language("fr")
        fr_badge = tr("SET_BACKUP_AUTO_BADGE", count=2)
        assert fr_badge == "Sauvegardes automatiques : 2/3 aujourd'hui"


class TestTopBarMultilingual:
    """Test language dropdown selector and breadcrumb updates in AppTopBar."""

    def test_topbar_language_selection(self, qapp):
        topbar = AppTopBar()

        assert hasattr(topbar, "lang_selector")
        assert topbar.lang_selector.count() == 3

        # Default is Arabic
        assert topbar.lang_selector.currentData() == "ar"

        signals_received = []
        events.language_changed.connect(signals_received.append)

        # Change to English via UI selector
        topbar.lang_selector.setCurrentIndex(1)  # English
        assert get_language() == "en"
        assert "en" in signals_received

        # Verify breadcrumb updated to English
        topbar.set_active_page(0)
        assert en.SIDEBAR_ITEMS[0] in topbar.page_title_label.text()
        assert "Overview of cash flow and receivables" in topbar.page_sub_label.text()

        # Change to French via UI selector
        topbar.lang_selector.setCurrentIndex(2)  # French
        assert get_language() == "fr"
        assert "fr" in signals_received
        assert fr.SIDEBAR_ITEMS[0] in topbar.page_title_label.text()
        assert "Vue d'ensemble des encaissements et créances" in topbar.page_sub_label.text()


class TestMainWindowMultilingual:
    """Test dynamic UI adjustment in MainWindow when language is switched."""

    def test_main_window_language_updates(self, qapp, memory_engine: Engine, seeded_db: dict[str, int]):
        with session_scope(memory_engine) as session:
            user = session.get(User, seeded_db["owner_id"])
            window = MainWindow(current_user=user)

            # Initial state (Arabic)
            assert window.layoutDirection() == Qt.LayoutDirection.RightToLeft
            assert window.windowTitle() == ar.APP_TITLE

            # Switch to English
            set_language("en")
            assert window.layoutDirection() == Qt.LayoutDirection.LeftToRight
            assert window.windowTitle() == en.APP_TITLE
            assert "Dashboard" in window.buttons[0].text()
            assert "Customers" in window.buttons[1].text()
            assert window.role_badge.text() == en.ROLE_OWNER
            assert window.lock_button.text() == en.SIDEBAR_LOCK_SCREEN

            # Switch to French
            set_language("fr")
            assert window.layoutDirection() == Qt.LayoutDirection.LeftToRight
            assert window.windowTitle() == fr.APP_TITLE
            assert "Tableau de bord" in window.buttons[0].text()
            assert "Clients" in window.buttons[1].text()
            assert window.role_badge.text() == fr.ROLE_OWNER
            assert window.lock_button.text() == fr.SIDEBAR_LOCK_SCREEN

            # Switch back to Arabic
            set_language("ar")
            assert window.layoutDirection() == Qt.LayoutDirection.RightToLeft
            assert window.role_badge.text() == "المالك"


class TestSettingsPageMultilingual:
    """Test language preference configuration and persistence in SettingsPage."""

    def test_settings_page_language_persistence(self, qapp, memory_engine: Engine, seeded_db: dict[str, int], monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("app.db.session._application_engine", lambda url: memory_engine)
        from PySide6.QtWidgets import QMessageBox
        monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: QMessageBox.StandardButton.Ok)

        with session_scope(memory_engine) as session:
            owner = session.get(User, seeded_db["owner_id"])
            page = SettingsPage(current_user=owner)

            assert hasattr(page, "language_selector")
            assert page.language_selector.count() == 3

            # Select French
            page.language_selector.setCurrentIndex(2)
            assert page.language_selector.currentData() == "fr"

            page._save_owner_settings()

            # Check DB persistence
            saved_lang = get_value(session, "language", "ar")
            assert saved_lang == "fr"

            # Check QSettings persistence
            qsettings = QSettings(APP_NAME, APP_NAME)
            assert qsettings.value("language") == "fr"
            assert get_language() == "fr"
