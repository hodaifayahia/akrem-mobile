"""Copy an existing local AkremMobile database into a fresh PostgreSQL database."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, Engine, Table, create_engine, inspect, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine import make_url

from app.config import database_url
from app.db.migrate import upgrade_database
from app.db.models import Base


def migrate_sqlite_to_postgres(
    source_url: str | None = None,
    target_url: str | None = None,
) -> dict[str, int]:
    """Copy all application rows, preserving IDs, into a fresh PostgreSQL target.

    The target may already have an empty migrated schema, but it must contain no
    application rows. The source is read-only. Set the source and target URLs in
    environment variables to avoid exposing database credentials in command args.
    """
    source = source_url or os.environ.get("AKREMMOBILE_SOURCE_DATABASE_URL")
    target = target_url or database_url()
    if not source:
        raise ValueError("Set AKREMMOBILE_SOURCE_DATABASE_URL to the source SQLite database")
    source_parsed = make_url(source)
    target_parsed = make_url(target)
    if source_parsed.get_backend_name() != "sqlite":
        raise ValueError("The source database must be SQLite")
    if target_parsed.get_backend_name() != "postgresql":
        raise ValueError("AKREMMOBILE_DATABASE_URL must point to PostgreSQL")
    if target_parsed.drivername != "postgresql+psycopg":
        raise ValueError("Use the PostgreSQL URL driver postgresql+psycopg")
    if source_parsed.database in (None, "", ":memory:"):
        raise ValueError("The source must be a file-backed SQLite database")
    if not Path(source_parsed.database).expanduser().is_file():
        raise ValueError("The source SQLite database file does not exist")

    source_engine = create_engine(source, connect_args={"check_same_thread": False})
    target_engine = create_engine(
        target,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 8},
    )
    try:
        _require_current_source_schema(source_engine)
        upgrade_database(target)
        return _copy_all_rows(source_engine, target_engine)
    finally:
        source_engine.dispose()
        target_engine.dispose()


def _require_current_source_schema(engine: Engine) -> None:
    """Reject a local source missing any current model table or column."""
    source_inspector = inspect(engine)
    source_tables = set(source_inspector.get_table_names())
    missing_tables = sorted(set(Base.metadata.tables) - source_tables)
    if missing_tables:
        raise ValueError("Source database is missing tables: " + ", ".join(missing_tables))
    missing_columns: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        source_columns = {
            column["name"] for column in source_inspector.get_columns(table_name)
        }
        missing_columns.extend(
            f"{table_name}.{column.name}"
            for column in table.columns
            if column.name not in source_columns
        )
    if missing_columns:
        raise ValueError(
            "Source database is missing columns: " + ", ".join(sorted(missing_columns))
        )


def _copy_all_rows(source: Engine, target: Engine) -> dict[str, int]:
    """Copy metadata tables in foreign-key order in one target transaction."""
    copied: dict[str, int] = {}
    with source.connect() as source_connection, target.begin() as target_connection:
        existing_counts = {
            table.name: target_connection.scalar(select(text("count(*)")).select_from(table)) or 0
            for table in Base.metadata.sorted_tables
        }
        populated = [name for name, count in existing_counts.items() if count]
        if populated:
            raise ValueError(
                "PostgreSQL already has application data; refusing to merge: "
                + ", ".join(populated)
            )

        for table in Base.metadata.sorted_tables:
            rows: list[dict[str, Any]] = []
            for source_row in source_connection.execute(select(table)).mappings():
                row = dict(source_row)
                _normalize_utc_timestamps(table, row)
                rows.append(row)
            if rows:
                target_connection.execute(table.insert(), rows)
            copied[table.name] = len(rows)
            if target.dialect.name == "postgresql" and "id" in table.c:
                _reset_postgresql_id_sequence(target_connection, table.name)

        populated_after = {
            table.name: target_connection.scalar(select(text("count(*)")).select_from(table)) or 0
            for table in Base.metadata.sorted_tables
        }
        if populated_after != copied:
            raise RuntimeError("PostgreSQL row counts did not match the source copy")
    return copied


def _normalize_utc_timestamps(table: Table, row: dict[str, Any]) -> None:
    """Interpret SQLite's naive values for timezone-aware columns as UTC."""
    for column in table.columns:
        value = row.get(column.name)
        if not isinstance(column.type, DateTime) or not column.type.timezone:
            continue
        if isinstance(value, datetime):
            row[column.name] = (
                value.replace(tzinfo=timezone.utc)
                if value.tzinfo is None
                else value.astimezone(timezone.utc)
            )


def _reset_postgresql_id_sequence(connection: Connection, table_name: str) -> None:
    """Advance a SERIAL/identity sequence beyond explicitly copied primary keys."""
    safe_table = table_name.replace('"', '""')
    connection.execute(
        text(
            "SELECT setval(pg_get_serial_sequence(:table_name, 'id'), "
            f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM \"{safe_table}\""
        ),
        {"table_name": table_name},
    )


def main() -> int:
    """Run the one-time copy using URLs from the process environment."""
    try:
        counts = migrate_sqlite_to_postgres()
    except Exception as error:
        print(f"Database transfer failed: {error}")
        return 1
    total = sum(counts.values())
    print(f"Copied {total} rows across {len(counts)} application tables.")
    for table, count in counts.items():
        print(f"{table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
