"""Automated 3-times daily backups and external backup upload tests."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from sqlalchemy import select

from app.db.models import Base, User
from app.db.session import create_database_engine, session_scope
from app.services import backup
from app.services.auth import create_first_owner
from app.ui.pages.settings_page import SettingsPage


@pytest.fixture
def backup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Configure an isolated file-backed SQLite database and backup environment."""
    data_directory = tmp_path / "app_data"
    data_directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(data_directory))
    monkeypatch.delenv("AKREMMOBILE_DATABASE_URL", raising=False)

    db_path = data_directory / "akremmobile.sqlite3"
    engine = create_database_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    engine.dispose()

    yield {
        "data_dir": data_directory,
        "db_path": db_path,
        "backup_dir": data_directory / "backups",
    }


def test_automated_checkpoint_limit_and_detection(backup_env: dict):
    """Ensure automated checkpoints are limited to 3 per day and classified correctly."""
    bdir = backup_env["backup_dir"]
    today = date(2026, 9, 30)
    t0 = datetime(2026, 9, 30, 9, 0, 0)
    t1 = datetime(2026, 9, 30, 12, 0, 0)
    t2 = datetime(2026, 9, 30, 15, 0, 0)
    t3 = datetime(2026, 9, 30, 18, 0, 0)

    assert backup.automated_backups_count(bdir, today=today) == 0

    # 1st automated checkpoint
    cp1 = backup.create_automated_checkpoint_backup(
        bdir, max_per_day=3, min_interval_seconds=0, now=t0
    )
    assert cp1 is not None
    assert "_auto" in cp1.name
    assert backup.automated_backups_count(bdir, today=today) == 1

    # 2nd automated checkpoint
    cp2 = backup.create_automated_checkpoint_backup(
        bdir, max_per_day=3, min_interval_seconds=0, now=t1
    )
    assert cp2 is not None
    assert backup.automated_backups_count(bdir, today=today) == 2

    # 3rd automated checkpoint
    cp3 = backup.create_automated_checkpoint_backup(
        bdir, max_per_day=3, min_interval_seconds=0, now=t2
    )
    assert cp3 is not None
    assert backup.automated_backups_count(bdir, today=today) == 3

    # 4th automated checkpoint attempt should be blocked since 3 checkpoints are already taken
    cp4 = backup.create_automated_checkpoint_backup(
        bdir, max_per_day=3, min_interval_seconds=0, now=t3
    )
    assert cp4 is None
    assert backup.automated_backups_count(bdir, today=today) == 3

    # Verify list_backups reports backup_type="auto"
    backups = backup.list_backups(bdir)
    assert len(backups) == 3
    for info in backups:
        assert info.backup_type == "auto"


def test_automated_checkpoint_min_interval(backup_env: dict):
    """Enforce minimum cooldown interval between automatic checkpoints."""
    bdir = backup_env["backup_dir"]
    t0 = datetime(2026, 9, 30, 10, 0, 0)

    # First checkpoint succeeds
    cp1 = backup.create_automated_checkpoint_backup(
        bdir, max_per_day=3, min_interval_seconds=3600, now=t0
    )
    assert cp1 is not None

    # Immediate second checkpoint within 1 hour is skipped
    t_soon = t0 + timedelta(minutes=15)
    cp_throttled = backup.create_automated_checkpoint_backup(
        bdir, max_per_day=3, min_interval_seconds=3600, now=t_soon
    )
    assert cp_throttled is None
    assert backup.automated_backups_count(bdir, today=t0.date()) == 1

    # Checkpoint after cooldown interval succeeds
    t_later = t0 + timedelta(minutes=65)
    cp2 = backup.create_automated_checkpoint_backup(
        bdir, max_per_day=3, min_interval_seconds=3600, now=t_later
    )
    assert cp2 is not None
    assert backup.automated_backups_count(bdir, today=t0.date()) == 2


def test_import_uploaded_backup_valid(backup_env: dict, tmp_path: Path):
    """Importing a valid external SQLite backup saves it into the backup repository."""
    bdir = backup_env["backup_dir"]
    external_file = tmp_path / "client_backup.sqlite3"

    # Create a valid external backup file from the current valid schema
    source_backup = backup.create_backup(tmp_path / "temp_backups", label="original")
    external_file.write_bytes(source_backup.read_bytes())

    imported = backup.import_uploaded_backup(external_file, directory=bdir)
    assert imported.is_file()
    assert "_uploaded" in imported.name
    assert imported.parent == bdir

    # Check that list_backups detects it as 'uploaded'
    backups = backup.list_backups(bdir)
    uploaded = [b for b in backups if b.path == imported]
    assert len(uploaded) == 1
    assert uploaded[0].backup_type == "uploaded"


def test_import_uploaded_backup_invalid_failures(backup_env: dict, tmp_path: Path):
    """Invalid files (non-existent, corrupt, or missing tables) are rejected with clear errors."""
    bdir = backup_env["backup_dir"]

    # 1. Non-existent file
    with pytest.raises(ValueError, match="does not exist"):
        backup.import_uploaded_backup(tmp_path / "missing.sqlite3", directory=bdir)

    # 2. Corrupt / text file
    garbage_file = tmp_path / "corrupt.sqlite3"
    garbage_file.write_text("THIS IS NOT A SQLITE DATABASE", encoding="utf-8")
    with pytest.raises(ValueError, match="not a readable SQLite"):
        backup.import_uploaded_backup(garbage_file, directory=bdir)

    # 3. Valid SQLite file but missing application tables
    empty_db = tmp_path / "empty_schema.sqlite3"
    conn = sqlite3.connect(str(empty_db))
    conn.execute("CREATE TABLE random_unrelated (id INTEGER PRIMARY KEY);")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="missing required application tables"):
        backup.import_uploaded_backup(empty_db, directory=bdir)


def test_settings_page_backup_ui_and_upload(backup_env: dict, tmp_path: Path):
    """Test SettingsPage displays the 3-daily auto-backup status and upload button."""
    app = QApplication.instance() or QApplication([])

    db_path = backup_env["db_path"]
    engine = create_database_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")

    with session_scope(engine) as session:
        owner = create_first_owner(
            session,
            username="testowner",
            password="Password123!",
            password_confirmation="Password123!",
        )

    page = SettingsPage(current_user=owner)

    # Verify upload button exists and is enabled
    assert hasattr(page, "upload_backup_button")
    assert page.upload_backup_button.isEnabled()
    assert hasattr(page, "backup_auto_badge")

    # Initial auto count is 0/3
    assert "0/3" in page.backup_auto_badge.text()

    # Create 3 automated checkpoints
    for _ in range(3):
        backup.create_automated_checkpoint_backup(min_interval_seconds=0)

    page._refresh_backups()
    assert "3/3" in page.backup_auto_badge.text()

    # Create valid external backup file
    valid_external = tmp_path / "uploaded_snapshot.sqlite3"
    orig = backup.create_backup(tmp_path / "scratch")
    valid_external.write_bytes(orig.read_bytes())

    # Simulate upload without instant restore (user clicks No)
    with patch("PySide6.QtWidgets.QFileDialog.getOpenFileName", return_value=(str(valid_external), "")):
        with patch("PySide6.QtWidgets.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
            with patch("PySide6.QtWidgets.QMessageBox.information"):
                page._upload_backup()

    # Verify table row exists and shows uploaded type
    backups = backup.list_backups()
    has_uploaded = any(b.backup_type == "uploaded" for b in backups)
    assert has_uploaded

    page.deleteLater()


def test_main_window_auto_backup_orchestration(backup_env: dict):
    """Ensure MainWindow registers the auto-backup schedule timer and handles checkpoints."""
    app = QApplication.instance() or QApplication([])

    db_path = backup_env["db_path"]
    engine = create_database_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")

    with session_scope(engine) as session:
        owner = create_first_owner(
            session,
            username="mwowner",
            password="Password123!",
            password_confirmation="Password123!",
        )

    from app.ui.main_window import MainWindow

    window = MainWindow(current_user=owner)
    assert hasattr(window, "_auto_backup_timer")
    assert window._auto_backup_timer.isActive()

    # Trigger scheduled checkpoint
    window._run_scheduled_checkpoint_backup()
    assert backup.automated_backups_count() >= 1

    window.close()
    window.deleteLater()

