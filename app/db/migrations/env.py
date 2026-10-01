"""Alembic environment for metadata-aware schema migrations."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, event, pool, text

from app.db.models import Base

config = context.config
if config.config_file_name:
    try:
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    except Exception:
        pass

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a live database connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations using a live database connection."""
    configuration = config.get_section(config.config_ini_section, {})
    connectable = create_engine(
        configuration["sqlalchemy.url"],
        poolclass=pool.NullPool,
    )
    if connectable.dialect.name == "sqlite":
        @event.listens_for(connectable, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    connection = connectable.connect()
    try:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            if connectable.dialect.name == "postgresql":
                # An xact lock serializes migrations and also works through
                # transaction-pooling proxies such as PgBouncer.
                connection.execute(text("SET LOCAL lock_timeout = '10s'"))
                connection.execute(text("SET LOCAL statement_timeout = '60s'"))
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": 809775249632412},
                )
            context.run_migrations()
    finally:
        connection.close()
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
