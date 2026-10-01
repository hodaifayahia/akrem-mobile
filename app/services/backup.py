"""Online-safe local SQLite backups and atomic database restoration."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import data_dir, database_url
from app.db.models import Base

_BACKUP_PREFIX = "akremmobile_"
_BACKUP_SUFFIX = ".sqlite3"
_DEFAULT_RETENTION = 30


@dataclass(frozen=True, slots=True)
class BackupInfo:
    """Metadata for a completed backup file."""

    path: Path
    created_at: datetime
    size_bytes: int
    backup_type: str = "manual"


def backup_directory() -> Path:
    """Return the application backup directory under its configured data folder."""
    return data_dir() / "backups"


def _detect_backup_type(path: Path) -> str:
    """Classify a backup file as automated, uploaded, pre_upgrade, or manual based on naming."""
    name = path.name.lower()
    if "_auto" in name:
        return "auto"
    if "_uploaded" in name or "_imported" in name:
        return "uploaded"
    if "_pre_upgrade" in name:
        return "pre_upgrade"
    return "manual"


def daily_backups_count(
    directory: str | Path | None = None,
    *,
    today: date | None = None,
) -> int:
    """Return the total number of backups existing for the specified date."""
    backup_dir = Path(directory) if directory is not None else backup_directory()
    day = today or date.today()
    pattern = f"{_BACKUP_PREFIX}{day:%Y-%m-%d}_*{_BACKUP_SUFFIX}"
    if not backup_dir.exists():
        return 0
    return sum(1 for path in backup_dir.glob(pattern) if path.is_file())


def automated_backups_count(
    directory: str | Path | None = None,
    *,
    today: date | None = None,
) -> int:
    """Return the number of automated checkpoint backups for the specified date."""
    backup_dir = Path(directory) if directory is not None else backup_directory()
    day = today or date.today()
    pattern = f"{_BACKUP_PREFIX}{day:%Y-%m-%d}_*_auto*{_BACKUP_SUFFIX}"
    if not backup_dir.exists():
        return 0
    return sum(1 for path in backup_dir.glob(pattern) if path.is_file())


def has_backup_for_today(
    directory: str | Path | None = None,
    *,
    today: date | None = None,
) -> bool:
    """Return whether a timestamped application backup exists for the local date."""
    return daily_backups_count(directory=directory, today=today) > 0


def list_backups(directory: str | Path | None = None) -> list[BackupInfo]:
    """Return matching backups, newest first by file timestamp and name."""
    backup_dir = Path(directory) if directory is not None else backup_directory()
    paths = [
        path for path in backup_dir.glob(f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}")
        if path.is_file()
    ]
    paths.sort(key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True)
    return [
        BackupInfo(
            path=path,
            created_at=datetime.fromtimestamp(path.stat().st_mtime),
            size_bytes=path.stat().st_size,
            backup_type=_detect_backup_type(path),
        )
        for path in paths
    ]


def create_backup(
    directory: str | Path | None = None,
    *,
    now: datetime | None = None,
    skip_if_exists_today: bool = False,
    retention: int = _DEFAULT_RETENTION,
    label: str | None = None,
) -> Path:
    """Create a consistent, timestamped copy using SQLite's online backup API.

    When ``skip_if_exists_today`` is enabled, the newest backup for the selected
    local date is returned unchanged. Old matching backups beyond ``retention``
    are pruned only after the new backup has been completed and validated.
    """
    if isinstance(retention, bool) or not isinstance(retention, int) or retention < 1:
        raise ValueError("Backup retention must be a positive whole number")
    source_path = _configured_database_path()
    if not source_path.is_file():
        raise FileNotFoundError(f"Configured SQLite database does not exist: {source_path}")

    backup_dir = Path(directory) if directory is not None else backup_directory()
    backup_dir.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now().astimezone()
    if skip_if_exists_today:
        existing = _latest_backup_for_day(backup_dir, current_time.date())
        if existing is not None:
            return existing

    backup_path = _new_backup_path(backup_dir, current_time, label=label)
    temp_path = _make_temporary_path(backup_dir, backup_path.name)
    try:
        _copy_sqlite_database(source_path, temp_path)
        validate_backup(temp_path)
        _sync_file(temp_path)
        os.replace(temp_path, backup_path)
        try:
            os.utime(backup_path, (current_time.timestamp(), current_time.timestamp()))
        except OSError:
            pass
        _sync_directory(backup_dir)
        _prune_backups(backup_dir, retention)
    except Exception:
        _remove_if_present(temp_path)
        raise
    return backup_path


def _backup_file_time(path: Path) -> float:
    """Return the timestamp for a backup from its name or file stats."""
    parts = path.stem.split("_")
    if len(parts) >= 3:
        try:
            dt = datetime.strptime(f"{parts[1]}_{parts[2]}", "%Y-%m-%d_%H-%M-%S").astimezone()
            return dt.timestamp()
        except ValueError:
            pass
    return path.stat().st_mtime


def create_automated_checkpoint_backup(
    directory: str | Path | None = None,
    *,
    max_per_day: int = 3,
    min_interval_seconds: int = 3600,
    now: datetime | None = None,
    retention: int = _DEFAULT_RETENTION,
) -> Path | None:
    """Create an automated checkpoint backup if fewer than ``max_per_day`` exist for today.

    Enforces ``min_interval_seconds`` between automated checkpoints to avoid redundant
    snapshots during rapid app restarts.
    Returns the Path to the new backup if created, or None if the limit or cooldown applies.
    """
    backup_dir = Path(directory) if directory is not None else backup_directory()
    current_time = now or datetime.now().astimezone()
    today_count = automated_backups_count(backup_dir, today=current_time.date())
    if today_count >= max_per_day:
        return None

    if today_count > 0 and min_interval_seconds > 0:
        latest = _latest_automated_backup_for_day(backup_dir, current_time.date())
        if latest is not None:
            elapsed = current_time.timestamp() - _backup_file_time(latest)
            if elapsed < min_interval_seconds:
                return None

    return create_backup(
        directory=backup_dir,
        now=current_time,
        skip_if_exists_today=False,
        retention=retention,
        label="auto",
    )


def import_uploaded_backup(
    source_path: str | Path,
    directory: str | Path | None = None,
    *,
    now: datetime | None = None,
    retention: int = _DEFAULT_RETENTION,
) -> Path:
    """Validate an uploaded SQLite backup file and save it into the backup repository.

    Performs full SQLite integrity and application schema verification prior to importing.
    Returns the path to the newly saved backup file.
    """
    src = Path(source_path).expanduser().resolve()
    if not src.is_file():
        raise ValueError(f"Uploaded backup file does not exist: {src}")

    validate_backup(src)

    backup_dir = Path(directory) if directory is not None else backup_directory()
    backup_dir.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now().astimezone()
    dest_path = _new_backup_path(backup_dir, current_time, label="uploaded")

    temp_path = _make_temporary_path(backup_dir, dest_path.name)
    try:
        _copy_sqlite_database(src, temp_path)
        validate_backup(temp_path)
        _sync_file(temp_path)
        os.replace(temp_path, dest_path)
        os.utime(dest_path, None)
        _sync_directory(backup_dir)
        _prune_backups(backup_dir, retention)
    except Exception:
        _remove_if_present(temp_path)
        raise
    return dest_path


def validate_backup(path: str | Path) -> None:
    """Validate SQLite integrity and the complete current application schema.

    Raises ``ValueError`` for unreadable/corrupt databases or when any table in
    the active SQLAlchemy model metadata is absent.
    """
    backup_path = Path(path).expanduser().resolve()
    if not backup_path.is_file():
        raise ValueError(f"Backup file does not exist: {backup_path}")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(_read_only_uri(backup_path), uri=True, timeout=5)
        connection.execute("PRAGMA query_only=ON")
        integrity_results = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        if integrity_results != ["ok"]:
            details = "; ".join(str(result) for result in integrity_results[:5])
            raise ValueError(f"Backup failed SQLite integrity_check: {details or 'unknown error'}")

        existing_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        required_tables = set(Base.metadata.tables)
        missing_tables = sorted(required_tables - existing_tables)
        if missing_tables:
            raise ValueError(
                "Backup is missing required application tables: " + ", ".join(missing_tables)
            )

        missing_columns: list[str] = []
        for table_name, table in Base.metadata.tables.items():
            quoted_table = table_name.replace('"', '""')
            existing_columns = {
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{quoted_table}")')
            }
            missing_columns.extend(
                f"{table_name}.{column.name}"
                for column in table.columns
                if column.name not in existing_columns
            )
        if missing_columns:
            raise ValueError(
                "Backup is missing required application columns: "
                + ", ".join(sorted(missing_columns))
            )
    except sqlite3.Error as error:
        raise ValueError(f"File is not a readable SQLite backup: {backup_path}") from error
    finally:
        if connection is not None:
            connection.close()


def restore_backup(path: str | Path) -> Path:
    """Atomically replace the configured local SQLite database with a validated backup.

    The application should be closed before restoring. SQLite connections already
    open in another process keep using the old database file after atomic replace.
    """
    source_path = Path(path).expanduser().resolve()
    destination_path = _configured_database_path()
    if not source_path.is_file():
        raise ValueError(f"Backup file does not exist: {source_path}")
    if source_path == destination_path:
        raise ValueError("The configured database itself cannot be used as a restore backup")
    validate_backup(source_path)

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _make_temporary_path(destination_path.parent, destination_path.name + ".restore")
    try:
        _copy_sqlite_database(source_path, temp_path)
        validate_backup(temp_path)
        _sync_file(temp_path)

        # Remove stale WAL state from the old file after checkpointing it. A closed
        # application is required so another connection cannot recreate sidecars.
        _close_wal_sidecars(destination_path)
        try:
            os.replace(temp_path, destination_path)
        except OSError as error:
            raise ValueError(
                "Could not atomically replace the database. Close AkremMobile and retry."
            ) from error
        _sync_directory(destination_path.parent)
    except Exception:
        _remove_if_present(temp_path)
        raise
    return destination_path


def _configured_database_path() -> Path:
    """Resolve the configured SQLAlchemy URL to a local file-backed SQLite path."""
    configured_url = database_url()
    try:
        parsed_url = make_url(configured_url)
    except Exception as error:
        raise ValueError("Backup and restore require a configured local SQLite database URL") from error
    if parsed_url.get_backend_name() != "sqlite":
        raise ValueError(
            "Backup and restore are available only for local SQLite databases; "
            "shared-server database support is not configured"
        )
    raw_path = parsed_url.database
    if (
        raw_path is None
        or raw_path in {"", ":memory:"}
        or raw_path.startswith("file:")
        or parsed_url.query.get("mode") == "memory"
    ):
        raise ValueError("Backup and restore require a file-backed local SQLite database")
    return Path(raw_path).expanduser().resolve()


def _copy_sqlite_database(source: Path, destination: Path) -> None:
    """Copy a live/read-only SQLite source into a destination via sqlite3 backup."""
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(
            _read_only_uri(source.resolve()), uri=True, timeout=30
        )
        destination_connection = sqlite3.connect(str(destination), timeout=30)
        source_connection.backup(destination_connection, pages=256, sleep=0.05)
        destination_connection.commit()
    except sqlite3.Error as error:
        raise ValueError(f"SQLite backup operation failed: {error}") from error
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()


def _close_wal_sidecars(database: Path) -> None:
    """Checkpoint a closed local database and remove only its stale WAL files."""
    if not database.exists():
        return
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(database), timeout=5)
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        connection.execute("PRAGMA journal_mode=DELETE").fetchone()
    except sqlite3.Error as error:
        raise ValueError(
            "Could not safely close the local SQLite WAL; close the application and retry"
        ) from error
    finally:
        if connection is not None:
            connection.close()
    _remove_if_present(Path(str(database) + "-wal"))
    _remove_if_present(Path(str(database) + "-shm"))


def _latest_backup_for_day(directory: Path, day: date) -> Path | None:
    """Find the newest matching daily backup, if one exists."""
    pattern = f"{_BACKUP_PREFIX}{day:%Y-%m-%d}_*{_BACKUP_SUFFIX}"
    matches = [path for path in directory.glob(pattern) if path.is_file()]
    return max(matches, key=lambda path: (path.stat().st_mtime_ns, path.name), default=None)


def _latest_automated_backup_for_day(directory: Path, day: date) -> Path | None:
    """Find the newest matching automated daily backup, if one exists."""
    pattern = f"{_BACKUP_PREFIX}{day:%Y-%m-%d}_*_auto*{_BACKUP_SUFFIX}"
    matches = [path for path in directory.glob(pattern) if path.is_file()]
    return max(matches, key=lambda path: (path.stat().st_mtime_ns, path.name), default=None)


def _new_backup_path(
    directory: Path,
    current_time: datetime,
    label: str | None = None,
) -> Path:
    """Choose a unique timestamped filename without overwriting an older backup."""
    stem = f"{_BACKUP_PREFIX}{current_time:%Y-%m-%d_%H-%M-%S}"
    if label:
        stem = f"{stem}_{label}"
    candidate = directory / f"{stem}{_BACKUP_SUFFIX}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{counter:03d}{_BACKUP_SUFFIX}"
        counter += 1
    return candidate


def _make_temporary_path(directory: Path, name_hint: str) -> Path:
    """Create a private temporary file in the same directory for atomic rename."""
    file_descriptor, filename = tempfile.mkstemp(
        prefix=f".{name_hint}.", suffix=".tmp", dir=directory
    )
    os.close(file_descriptor)
    return Path(filename)


def _prune_backups(directory: Path, retention: int) -> None:
    """Remove older matching backups after a successful write."""
    paths = [
        path for path in directory.glob(f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}")
        if path.is_file()
    ]
    paths.sort(key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True)
    for old_path in paths[retention:]:
        old_path.unlink(missing_ok=True)


def _read_only_uri(path: Path) -> str:
    """Build a percent-escaped read-only SQLite file URI."""
    return path.resolve().as_uri() + "?mode=ro"


def _sync_file(path: Path) -> None:
    """Flush file contents before an atomic replace where supported."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            if os.name != "nt":
                raise
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    """Flush a directory entry after rename on platforms that support it."""
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _remove_if_present(path: Path) -> None:
    """Remove a temporary file or SQLite sidecar if it exists."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
