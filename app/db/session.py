"""SQLAlchemy engine and session helpers."""

from __future__ import annotations

from contextlib import contextmanager
from threading import Lock
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import database_url

_engine_lock = Lock()
_application_engines: dict[str, Engine] = {}


def create_database_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Create an engine and enable SQLite foreign-key enforcement."""
    resolved_url = url or database_url()
    kwargs: dict[str, object] = {
        "echo": echo,
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }
    if resolved_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    elif resolved_url.startswith(("postgresql", "postgres")):
        kwargs["pool_timeout"] = 10
        kwargs["connect_args"] = {"connect_timeout": 8, "prepare_threshold": 0}
    engine = create_engine(resolved_url, **kwargs)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def _application_engine(url: str) -> Engine:
    """Reuse one pooled engine per configured URL for the process lifetime."""
    with _engine_lock:
        engine = _application_engines.get(url)
        if engine is None:
            engine = create_database_engine(url)
            _application_engines[url] = engine
        return engine


def dispose_database_engines() -> None:
    """Close process-wide database pools during a clean application shutdown."""
    with _engine_lock:
        engines = tuple(_application_engines.values())
        _application_engines.clear()
    for engine in engines:
        engine.dispose()


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Yield a session and commit on success or roll back on failure."""
    resolved_engine = engine or _application_engine(database_url())
    factory = sessionmaker(bind=resolved_engine, expire_on_commit=False)
    session = factory()
    try:
        if resolved_engine.dialect.name == "postgresql":
            session.execute(text("SET LOCAL lock_timeout = '10s'"))
            session.execute(text("SET LOCAL statement_timeout = '60s'"))
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
