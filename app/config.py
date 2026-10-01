"""Application paths, settings, and runtime configuration."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "AkremMobile"
APP_VERSION = "0.1.0"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_DIR = PROJECT_ROOT / "app" / "resources"
DATABASE_FILENAME = "akremmobile.sqlite3"


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
