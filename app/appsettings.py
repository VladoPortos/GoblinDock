"""DB-backed key/value settings — runtime-editable from the UI (unlike the
env-driven values in app/config.py). Tiny by design: get/set a string by key.
"""
from __future__ import annotations

from sqlmodel import Session

from .db import engine
from .models import Setting

# Keys
JOB_RETENTION_DAYS = "job_retention_days"   # "0" = keep forever (no auto-prune)


def get_setting(key: str, default: str = "") -> str:
    with Session(engine) as s:
        row = s.get(Setting, key)
        return row.value if row else default


def set_setting(key: str, value: str) -> None:
    with Session(engine) as s:
        row = s.get(Setting, key)
        if row:
            row.value = value
        else:
            row = Setting(key=key, value=value)
        s.add(row)
        s.commit()


def get_job_retention_days() -> int:
    """Days to keep job history; 0 = forever. Never raises on a bad stored value."""
    try:
        return max(0, int(get_setting(JOB_RETENTION_DAYS, "0") or 0))
    except (TypeError, ValueError):
        return 0
