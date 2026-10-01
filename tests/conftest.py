"""Shared in-memory database fixture."""

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine

from app.db.models import Base
from app.db.session import create_database_engine


@pytest.fixture
def memory_engine() -> Iterator[Engine]:
    """Create a fresh SQLite database with foreign keys enabled."""
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
