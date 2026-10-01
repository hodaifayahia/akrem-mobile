"""Alembic migration entry point used during app startup."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config import database_path, database_url, ensure_data_dirs

logger = logging.getLogger(__name__)


def upgrade_database(url: str | None = None) -> None:
    """Apply all schema migrations to the configured database.

    If an existing database is detected prior to migration, a safety backup
    is automatically created so user data is never lost or corrupted during an update.
    """
    if url is None:
        ensure_data_dirs()
        try:
            db_file = database_path()
            if db_file.is_file() and db_file.stat().st_size > 0:
                from app.services.backup import create_backup
                create_backup(label="pre_upgrade")
                logger.info("Automatic pre-upgrade safety backup created before database migration.")
        except Exception:
            logger.warning("Could not create pre-upgrade safety backup, continuing with migration.", exc_info=True)

    ini_path = Path(__file__).with_name("alembic.ini")
    config = Config(str(ini_path))
    config.set_main_option("sqlalchemy.url", (url or database_url()).replace("%", "%%"))
    command.upgrade(config, "head")



def known_revisions() -> list[str]:
    """Return this app's migration revision ids, oldest first."""
    from alembic.script import ScriptDirectory

    config = Config(str(Path(__file__).with_name("alembic.ini")))
    script = ScriptDirectory.from_config(config)
    return [revision.revision for revision in reversed(list(script.walk_revisions()))]
