"""Application paths, settings, and runtime configuration."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

APP_NAME = "AkremMobile"
APP_VERSION = "0.1.0"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_DIR = PROJECT_ROOT / "app" / "resources"
DATABASE_FILENAME = "akremmobile.sqlite3"
LOCAL_SETTINGS_FILENAME = "local_settings.json"


def data_dir() -> Path:
    """Return the per-user data folder, honoring an explicit development override."""
    override = os.environ.get("AKREMMOBILE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def database_path() -> Path:
    """Return the local SQLite database path."""
    return data_dir() / DATABASE_FILENAME


def database_url() -> str:
    """Build the SQLAlchemy URL for the configured database backend."""
    configured_url = os.environ.get("AKREMMOBILE_DATABASE_URL")
    if configured_url:
        if configured_url.startswith(("postgresql://", "postgres://")):
            return "postgresql+psycopg://" + configured_url.split("://", 1)[1]
        return configured_url
    path = database_path().as_posix()
    return f"sqlite+pysqlite:///{path}"


def ensure_data_dirs() -> None:
    """Create runtime folders without touching user data on import."""
    root = data_dir()
    for folder in (root, root / "backups", root / "logs", root / "exports"):
        folder.mkdir(parents=True, exist_ok=True)


def local_settings_path() -> Path:
    """Return this PC's settings file (kept out of the possibly shared database)."""
    return data_dir() / LOCAL_SETTINGS_FILENAME


def read_local_setting(key: str, default: Any = None) -> Any:
    """Return a per-PC setting, or ``default`` when missing or the file is unreadable."""
    return _read_local_settings().get(key, default)


def write_local_setting(key: str, value: Any) -> None:
    """Atomically store a JSON-serializable per-PC setting; ``None`` removes the key."""
    settings = _read_local_settings()
    if value is None:
        settings.pop(key, None)
    else:
        settings[key] = value
    path = local_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(settings, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _read_local_settings() -> dict[str, Any]:
    """Load the settings file, treating a missing or corrupt file as empty."""
    try:
        with local_settings_path().open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
