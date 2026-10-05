"""Sharing the records between several PCs: one shop PC runs PostgreSQL, the others connect to it.

The choice is stored per PC (``local_settings.json`` in ``%APPDATA%/AkremMobile``),
never in the shared database, so every PC can point at the server on its own:

* ``test_connection`` says whether the server answers, the login works and
  whether it already holds the shop's records;
* ``prepare_server`` creates or updates the tables on the server;
* ``copy_local_data`` moves this PC's records into an empty server once (the
  first PC that already has the shop's data);
* ``use_server`` / ``use_this_pc`` switch this PC; the app then restarts.

The ``AKREMMOBILE_DATABASE_URL`` environment variable, when set, still wins.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.config import (
    DATABASE_URL_SETTING,
    database_path,
    database_url,
    read_local_setting,
    write_local_setting,
)

_LOG = logging.getLogger(__name__)
DEFAULT_PORT = 5432
DEFAULT_DATABASE = "akremmobile"
DEFAULT_USER = "akrem"
CONNECT_TIMEOUT_SECONDS = 6

CheckCode = Literal["ok", "unreachable", "login_failed", "no_database", "error"]


@dataclass(frozen=True)
class ServerDetails:
    """Where the shop's PostgreSQL server is and how to sign in to it."""

    host: str
    password: str
    port: int = DEFAULT_PORT
    database: str = DEFAULT_DATABASE
    user: str = DEFAULT_USER

    def url(self) -> str:
        """SQLAlchemy URL (psycopg driver), with the password safely escaped."""
        host = (self.host or "").strip()
        if not host:
            raise ValueError("The server address is required")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("The port must be between 1 and 65535")
        return URL.create(
            "postgresql+psycopg",
            username=(self.user or DEFAULT_USER).strip(),
            password=self.password or None,
            host=host,
            port=self.port,
            database=(self.database or DEFAULT_DATABASE).strip(),
        ).render_as_string(hide_password=False)


@dataclass(frozen=True)
class ConnectionCheck:
    """Outcome of ``test_connection``."""

    code: CheckCode
    server_version: str = ""
    has_tables: bool = False
    has_data: bool = False
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.code == "ok"


def current_server() -> ServerDetails | None:
    """The server this PC is set to use, or None when it uses its own local database."""
    raw = read_local_setting(DATABASE_URL_SETTING)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        url = make_url(raw)
    except Exception:  # noqa: BLE001 - a hand-edited setting must not stop the app
        return None
    if url.get_backend_name() != "postgresql":
        return None
    return ServerDetails(
        host=url.host or "", password=url.password or "", port=url.port or DEFAULT_PORT,
        database=url.database or DEFAULT_DATABASE, user=url.username or DEFAULT_USER,
    )


def is_shared() -> bool:
    """Whether the app currently runs on a PostgreSQL server (setting or environment)."""
    return make_url(database_url()).get_backend_name() == "postgresql"


def test_connection(details: ServerDetails) -> ConnectionCheck:
    """Try to sign in to the server and look at what it holds; never raises."""
    try:
        url = details.url()
    except ValueError as error:
        return ConnectionCheck("error", detail=str(error))
    engine = create_engine(url, connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS})
    try:
        with engine.connect() as connection:
            version = str(connection.execute(text("SHOW server_version")).scalar() or "")
            tables = set(inspect(connection).get_table_names())
            has_data = False
            if "users" in tables:
                has_data = bool(connection.execute(text("SELECT count(*) FROM users")).scalar())
            return ConnectionCheck("ok", version, "users" in tables, has_data)
    except OperationalError as error:
        return ConnectionCheck(_failure_code(str(error)), detail=str(error.orig or error))
    except SQLAlchemyError as error:
        return ConnectionCheck("error", detail=str(error))
    finally:
        engine.dispose()


def _failure_code(message: str) -> CheckCode:
    lowered = message.casefold()
    if "password authentication failed" in lowered or "no pg_hba.conf entry" in lowered:
        return "login_failed"
    if "does not exist" in lowered and "database" in lowered:
        return "no_database"
    return "unreachable"


def prepare_server(details: ServerDetails) -> None:
    """Create or update the application tables on the server (safe to repeat)."""
    from app.db.migrate import upgrade_database

    upgrade_database(details.url())


def copy_local_data(details: ServerDetails) -> dict[str, int]:
    """Copy this PC's records into the server once; refused when the server already holds records."""
    from app.db.migrate_sqlite_to_postgres import migrate_sqlite_to_postgres

    path = database_path()
    if not path.is_file():
        raise ValueError("This PC has no local records to copy")
    return migrate_sqlite_to_postgres(f"sqlite+pysqlite:///{path.as_posix()}", details.url())


def local_has_data() -> bool:
    """Whether this PC's own database file already holds an account (the shop's records)."""
    import sqlite3

    path = database_path()
    if not path.is_file():
        return False
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            return bool(connection.execute("SELECT count(*) FROM users").fetchone()[0])
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def use_server(details: ServerDetails) -> None:
    """Make this PC use the server from the next start (the password stays on this PC only)."""
    write_local_setting(DATABASE_URL_SETTING, details.url())


def use_this_pc() -> None:
    """Make this PC go back to its own local database from the next start."""
    write_local_setting(DATABASE_URL_SETTING, None)


def server_row_counts(details: ServerDetails) -> dict[str, int]:
    """Rows per application table on the server (used to confirm a copy)."""
    from app.db.models import Base

    engine = create_engine(details.url(), connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS})
    try:
        with engine.connect() as connection:
            return {
                table.name: int(connection.execute(select(text("count(*)")).select_from(table)).scalar() or 0)
                for table in Base.metadata.sorted_tables
            }
    finally:
        engine.dispose()
