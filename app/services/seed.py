"""Initial categories and business settings."""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Category, Setting
from app.db.session import session_scope

DEFAULT_CATEGORIES = (
    "أساتذة",
    "منحة البطالة",
    "عسكري",
    "أعمال حرة",
    "غير مصنف",
)
DEFAULT_SETTINGS = {
    "due_mode": "first_of_month",
    "grace_days": 5,
    "rate_presets": {"4": 35, "5": 35, "6": 35, "7": 35, "10": 40, "12": 45},
}


def seed_defaults(session: Session | None = None) -> None:
    """Seed the default categories and settings without overwriting owner edits."""
    if session is not None:
        _seed_in_session(session)
        return
    with session_scope() as scoped_session:
        _seed_in_session(scoped_session)


def _seed_in_session(session: Session) -> None:
    """Insert only default rows that do not exist yet."""
    if session.get_bind().dialect.name == "postgresql":
        # Keep parallel clients from racing on the first-run seed check.
        session.execute(select(func.pg_advisory_xact_lock(809775249632413)))
    category_count = session.scalar(select(func.count()).select_from(Category))
    if category_count == 0:
        session.add_all(Category(name=name, is_default=True) for name in DEFAULT_CATEGORIES)

    existing_settings = set(session.scalars(select(Setting.key)))
    for key, value in DEFAULT_SETTINGS.items():
        if key not in existing_settings:
            session.add(Setting(key=key, value=json.dumps(value, ensure_ascii=False)))
