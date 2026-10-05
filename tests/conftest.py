"""Shared fixtures: in-memory database and isolated per-PC preferences."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from app.db.models import Base
from app.db.session import create_database_engine


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path: Path) -> Iterator[None]:
    """Keep QSettings out of the real profile (the registry on Windows) for every test."""
    from PySide6.QtCore import QSettings

    from app.ui.preferences import use_settings_folder

    previous_format = QSettings.defaultFormat()
    use_settings_folder(str(tmp_path / "settings"))
    try:
        yield
    finally:
        QSettings.setDefaultFormat(previous_format)


@pytest.fixture
def memory_engine() -> Iterator[Engine]:
    """Create a fresh SQLite database with foreign keys enabled."""
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
