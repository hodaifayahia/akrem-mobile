"""Per-PC local settings and the configurable backup folder."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from app import config
from app.db.models import Base
from app.db.session import create_database_engine
from app.services import backup


@pytest.fixture
def data_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app at an isolated data folder with a file-backed database."""
    folder = tmp_path / "app_data"
    folder.mkdir()
    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(folder))
    monkeypatch.delenv("AKREMMOBILE_DATABASE_URL", raising=False)
    engine = create_database_engine(f"sqlite+pysqlite:///{(folder / 'akremmobile.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return folder.resolve()


def test_local_settings_round_trip_and_delete(data_folder: Path) -> None:
    """Values persist as JSON in the data folder; None removes a key."""
    assert config.local_settings_path() == data_folder / "local_settings.json"
    assert config.read_local_setting("missing", "fallback") == "fallback"
    config.write_local_setting("folder", "D:/نسخ")
    config.write_local_setting("count", 3)
    assert config.read_local_setting("folder") == "D:/نسخ"
    assert json.loads(config.local_settings_path().read_text(encoding="utf-8")) == {
        "folder": "D:/نسخ",
        "count": 3,
    }
    config.write_local_setting("folder", None)
    config.write_local_setting("never-set", None)
    assert config.read_local_setting("folder") is None
    assert config.read_local_setting("count") == 3
    leftovers = [path.name for path in data_folder.iterdir() if path.name.endswith(".tmp")]
    assert leftovers == []


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", ""])
def test_corrupt_settings_file_is_treated_as_empty(data_folder: Path, content: str) -> None:
    """A damaged settings file falls back to defaults and is replaced on write."""
    config.local_settings_path().write_text(content, encoding="utf-8")
    assert config.read_local_setting("backup_directory") is None
    assert backup.backup_directory() == data_folder / "backups"
    config.write_local_setting("key", "value")
    assert config.read_local_setting("key") == "value"


def test_custom_folder_is_used_by_all_backup_operations(
    data_folder: Path, tmp_path: Path
) -> None:
    """Manual, automated, and uploaded backups and listings follow the chosen folder."""
    custom = tmp_path / "usb" / "AkremBackups"
    assert backup.backup_directory() == backup.default_backup_directory()
    assert backup.default_backup_directory() == data_folder / "backups"

    effective = backup.set_backup_directory(str(custom))
    assert effective == custom.resolve()
    assert custom.is_dir()
    assert backup.backup_directory() == custom.resolve()

    now = datetime(2026, 10, 1, 9, 0, 0)
    manual = backup.create_backup(now=now)
    automated = backup.create_automated_checkpoint_backup(
        min_interval_seconds=0, now=datetime(2026, 10, 1, 10, 0, 0)
    )
    uploaded = backup.import_uploaded_backup(manual)
    assert automated is not None
    for path in (manual, automated, uploaded):
        assert path.parent == custom.resolve()
    assert {item.path for item in backup.list_backups()} == {manual, automated, uploaded}
    assert backup.daily_backups_count(today=date(2026, 10, 1)) >= 2
    assert backup.automated_backups_count(today=date(2026, 10, 1)) == 1
    assert backup.has_backup_for_today(today=date(2026, 10, 1))
    assert not (data_folder / "backups").exists() or not any(
        (data_folder / "backups").iterdir()
    )


def test_reset_to_default_folder(data_folder: Path, tmp_path: Path) -> None:
    """Passing None (or blank text) forgets the custom folder."""
    backup.set_backup_directory(tmp_path / "custom")
    assert backup.set_backup_directory(None) == data_folder / "backups"
    assert backup.backup_directory() == data_folder / "backups"
    assert config.read_local_setting(backup.BACKUP_DIRECTORY_SETTING) is None
    backup.set_backup_directory(tmp_path / "custom")
    assert backup.set_backup_directory("  ") == data_folder / "backups"
    path = backup.create_backup(now=datetime(2026, 10, 1, 9, 0, 0))
    assert path.parent == data_folder / "backups"


def test_file_path_is_rejected_as_not_directory(data_folder: Path, tmp_path: Path) -> None:
    """A file (or a path beneath a file) cannot be a backup folder."""
    blocker = tmp_path / "plain.txt"
    blocker.write_text("x", encoding="utf-8")
    for candidate in (blocker, blocker / "inside"):
        with pytest.raises(backup.BackupLocationError) as caught:
            backup.set_backup_directory(candidate)
        assert caught.value.code == "not_directory"
        assert isinstance(caught.value, ValueError)
    assert backup.backup_directory() == data_folder / "backups"


def test_unwritable_folder_is_rejected(
    data_folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder that refuses new files is not saved as the backup location."""
    target = tmp_path / "read_only"

    def refuse(directory: Path, name_hint: str) -> Path:
        raise PermissionError(13, "Permission denied", str(directory))

    monkeypatch.setattr(backup, "_make_temporary_path", refuse)
    with pytest.raises(backup.BackupLocationError) as caught:
        backup.set_backup_directory(target)
    assert caught.value.code == "not_writable"
    assert caught.value.path == target.resolve()
    assert backup.backup_directory() == data_folder / "backups"


def test_write_probe_leaves_no_files(data_folder: Path, tmp_path: Path) -> None:
    """Validating a folder does not leave probe files behind."""
    target = tmp_path / "clean"
    backup.set_backup_directory(target)
    assert list(target.iterdir()) == []


def test_pre_upgrade_backup_uses_configured_folder(
    data_folder: Path, tmp_path: Path
) -> None:
    """The automatic backup taken before migrations goes to the chosen folder."""
    from app.db.migrate import upgrade_database

    custom = tmp_path / "custom_backups"
    backup.set_backup_directory(custom)
    # A database created by create_all has no alembic history; stamp it at head
    # first so the upgrade itself is a no-op and only the safety backup matters.
    from alembic import command
    from alembic.config import Config

    alembic_config = Config(str(Path(config.PROJECT_ROOT) / "app" / "db" / "alembic.ini"))
    alembic_config.set_main_option(
        "sqlalchemy.url", config.database_url().replace("%", "%%")
    )
    command.stamp(alembic_config, "head")
    upgrade_database()
    names = [path.name for path in custom.iterdir()]
    assert any("_pre_upgrade" in name for name in names)


def test_backups_from_an_older_app_version_can_be_restored_and_are_upgraded(tmp_path, monkeypatch):
    """A backup taken before a schema change stays restorable after updating the app."""
    from sqlalchemy import create_engine, inspect

    from app.db.migrate import known_revisions, upgrade_database
    from app.services import backup as backup_service

    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AKREMMOBILE_DATABASE_URL", raising=False)
    revisions = known_revisions()
    assert revisions[0] == "0001_initial" and len(revisions) >= 4

    old = tmp_path / "old_version.sqlite3"
    url = f"sqlite+pysqlite:///{old.as_posix()}"
    from alembic import command
    from alembic.config import Config
    from pathlib import Path as _Path

    import app.db.migrate as migrate_module

    config = Config(str(_Path(migrate_module.__file__).with_name("alembic.ini")))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, revisions[1])  # an app version from two updates ago

    backup_service.validate_backup(old)  # accepted: older known revision

    from app.config import database_path

    database_path().parent.mkdir(parents=True, exist_ok=True)
    upgrade_database()  # current app database exists
    from app.db.session import dispose_database_engines

    dispose_database_engines()
    backup_service.restore_backup(old)
    upgrade_database()  # what the next start does
    columns = {c["name"] for c in inspect(create_engine(f"sqlite:///{database_path().as_posix()}")).get_columns("users")}
    assert "totp_enabled" in columns


def test_backup_from_an_unknown_newer_version_is_rejected(tmp_path):
    import sqlite3

    import pytest

    from app.services import backup as backup_service

    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('9999_future')")
    with pytest.raises(ValueError):
        backup_service.validate_backup(path)
