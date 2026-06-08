"""WAL-safe SQLite backups + rotation.

The whole GoblinDock datastore is a single SQLite file (see app/db.py), so an
automatic rotating snapshot is the cheapest possible disaster-recovery net. We use
sqlite3's *online* backup API (``Connection.backup``) rather than copying the file:
under WAL a plain ``cp`` can miss un-checkpointed ``-wal`` pages and yield a stale or
torn copy, whereas the backup API copies a transactionally-consistent image even while
the worker/web threads keep writing.

Kept deliberately dependency-free and side-effect-isolated (its own short-lived
sqlite3 connections, never the pooled SQLAlchemy engine) so it is safe to call from the
scheduler thread and trivial to unit-test.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

log = logging.getLogger("goblindock")

# Backups are named so rotation can glob EXACTLY these and never touch the live DB
# (goblindock.sqlite3 / -wal / -shm) or anything else that shares the directory.
_PREFIX = "goblindock-"
_SUFFIX = ".sqlite3"
_GLOB = f"{_PREFIX}*{_SUFFIX}"


def backup_dir() -> Path:
    return Path(settings.backup_dir)


def _timestamp() -> str:
    # Microsecond resolution so a manual "backup now" can't collide with (overwrite) a
    # scheduled one that fires in the same second; filenames stay lexicographically sortable.
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def list_backups() -> list[dict]:
    """Newest-first list of existing backup files with size + mtime."""
    d = backup_dir()
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob(_GLOB), key=lambda x: x.name, reverse=True):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({
            "name": p.name,
            "bytes": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        })
    return out


def _rotate(keep: int) -> int:
    """Keep the newest `keep` backups (by filename, which is timestamp-sortable);
    delete the rest. Only ever touches files matching the backup glob. Returns the
    number deleted."""
    d = backup_dir()
    files = sorted(d.glob(_GLOB), key=lambda x: x.name, reverse=True)
    deleted = 0
    for old in files[max(1, keep):]:
        try:
            old.unlink()
            deleted += 1
        except OSError as e:  # noqa: PERF203
            log.warning("backup rotation could not delete %s: %s", old.name, e)
    return deleted


def backup_now(reason: str = "scheduled") -> Path:
    """Write one WAL-safe snapshot of the live DB into the backups dir and rotate.

    Raises on failure (the caller — the scheduler wrapper or the admin endpoint —
    decides how loud to be); never partially leaves a half-written file under the
    backup glob because sqlite3's backup writes to its own destination file and we
    only rotate after it closes cleanly.
    """
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{_PREFIX}{_timestamp()}{_SUFFIX}"

    # Dedicated short-lived connections — NOT the pooled SQLAlchemy engine. A generous
    # busy_timeout lets the page-by-page copy ride out a concurrent write instead of
    # raising "database is locked".
    src = sqlite3.connect(settings.db_path, timeout=30)
    try:
        src.execute("PRAGMA busy_timeout=30000")
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    deleted = _rotate(settings.backup_keep)
    log.info("DB backup (%s) → %s (%d byte) · rotated %d old",
             reason, dest.name, dest.stat().st_size, deleted)
    return dest
