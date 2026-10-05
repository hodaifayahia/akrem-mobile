"""Per-PC preferences (window, language, tray, notifications) stored with QSettings.

Every preference goes through :func:`app_settings`. In normal use it is the
native store (the registry on Windows, an INI file elsewhere), exactly as
``QSettings(APP_NAME, APP_NAME)``. Tests and the packaged self-test call
:func:`use_settings_folder` so nothing they change reaches the real profile:
the two-argument ``QSettings`` constructor always opens the registry on
Windows and ignores ``QSettings.setPath``.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings

from app.config import APP_NAME


def app_settings() -> QSettings:
    """Open this PC's preferences for the current user."""
    return QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, APP_NAME, APP_NAME)


def use_settings_folder(folder: str) -> None:
    """Keep preferences as INI files under ``folder`` instead of the user's profile."""
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, folder)
