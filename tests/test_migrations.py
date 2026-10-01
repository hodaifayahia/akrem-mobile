"""Startup migration behavior."""

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.db.migrate import upgrade_database


def test_upgrade_database_creates_current_schema(tmp_path: Path) -> None:
    """Apply all Alembic migrations to an empty SQLite database."""
    db_path = tmp_path / "migration-test.sqlite3"
    upgrade_database(f"sqlite+pysqlite:///{db_path.as_posix()}")

    engine = create_engine(f"sqlite+pysqlite:///{db_path.as_posix()}")
    try:
        table_names = set(inspect(engine).get_table_names())
        assert {
            "categories", "customers", "sales", "installments", "payments",
            "settings", "users", "audit_logs", "products",
        } <= table_names
        with engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "0002_product_catalog"
    finally:
        engine.dispose()
