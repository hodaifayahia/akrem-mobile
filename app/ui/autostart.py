"""Start AkremMobile in the background when the user signs in to Windows.

The installer adds the entry by default; the tray menu turns it on or off.
Both use the same per-user registry value, so no administrator rights are
needed. At sign-in the app starts hidden in the tray (``--background``): the
payment checks and Windows notifications run, and the login window only
appears when the tray icon is opened.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AkremMobile"
BACKGROUND_FLAG = "--background"

_LOG = logging.getLogger(__name__)


def startup_command(executable: str | None = None) -> str | None:
    """The command Windows runs at sign-in; ``None`` when not an installed (frozen) build."""
    if executable is None:
        if not getattr(sys, "frozen", False):
            return None
        executable = sys.executable
    return f'"{Path(executable)}" {BACKGROUND_FLAG}'


def is_supported() -> bool:
    """Whether this copy can register itself (Windows, installed build)."""
    return sys.platform == "win32" and startup_command() is not None


def is_enabled(key_path: str = RUN_KEY_PATH) -> bool:
    """Whether Windows starts AkremMobile at sign-in for this user."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except OSError:
        return False


def set_enabled(enabled: bool, *, key_path: str = RUN_KEY_PATH, command: str | None = None) -> bool:
    """Add or remove the sign-in entry; returns whether the change was made."""
    if sys.platform != "win32":
        return False
    import winreg

    command = command or startup_command()
    if enabled and command is None:
        return False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        _LOG.warning("Could not change the Windows sign-in entry", exc_info=True)
        return False
    return True
