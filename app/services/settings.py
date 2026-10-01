"""Read and update serialized application settings."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Setting

DEFAULT_RATE_PRESETS: dict[int, int] = {4: 35, 5: 35, 6: 35, 7: 35, 10: 40, 12: 45}


def get_value(session: Session, key: str, default: Any = None) -> Any:
    """Return a JSON-decoded setting value or its caller-provided default."""
    setting = session.get(Setting, key)
    if setting is None:
        return default
    try:
        return json.loads(setting.value)
    except (TypeError, json.JSONDecodeError):
        return setting.value


def set_value(session: Session, key: str, value: Any) -> None:
    """Store one setting as UTF-8 JSON without committing the caller's session."""
    setting = session.get(Setting, key)
    serialized = json.dumps(value, ensure_ascii=False)
    if setting is None:
        session.add(Setting(key=key, value=serialized))
    else:
        setting.value = serialized
    session.flush()


def get_rate_presets(session: Session) -> dict[int, int]:
    """Return normalized owner-configured installment rate presets."""
    raw_value = get_value(session, "rate_presets", DEFAULT_RATE_PRESETS)
    if not isinstance(raw_value, dict):
        return DEFAULT_RATE_PRESETS.copy()
    result: dict[int, int] = {}
    for months, rate in raw_value.items():
        try:
            month_count = int(months)
            rate_percent = int(rate)
        except (TypeError, ValueError):
            continue
        if 2 <= month_count <= 12 and 0 <= rate_percent <= 50:
            result[month_count] = rate_percent
    return result or DEFAULT_RATE_PRESETS.copy()
