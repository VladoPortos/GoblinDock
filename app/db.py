"""SQLite engine + session helpers.

A single SQLite file is the whole data store *and* the job/event log (per the
design spec — no Redis/Postgres). WAL mode lets the web request handlers and the
background worker thread read/write concurrently at homelab scale.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

engine: Engine = create_engine(
    settings.database_url,
    echo=False,
    # The 5s-per-client state poll + the background worker thread all draw from this
    # pool; the default (size=5, overflow=10) can exhaust under concurrent polling and
    # surface as 500s. WAL SQLite is happy with many connections, so widen the pool.
    # pool_pre_ping validates a pooled connection before use, avoiding anomalous reads
    # on a stale connection.
    pool_size=20,
    max_overflow=40,
    pool_timeout=30,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def init_db() -> None:
    # Import models so they register with SQLModel.metadata before create_all.
    from . import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _migrate()


def _migrate() -> None:
    """Add columns introduced after a table was first created. SQLite's create_all
    only creates missing TABLES, not missing COLUMNS, so we ALTER in idempotently
    (no Alembic for a single-file homelab DB)."""
    adds = {
        "connections": [
            ("max_cores", "INTEGER NOT NULL DEFAULT 0"),
            ("max_ram_mb", "INTEGER NOT NULL DEFAULT 0"),
            ("max_disk_gb", "INTEGER NOT NULL DEFAULT 0"),
        ],
        "images": [
            ("disk_gb", "INTEGER NOT NULL DEFAULT 20"),
        ],
        "audit": [
            ("ip", "TEXT NOT NULL DEFAULT ''"),
        ],
        "users": [
            ("session_epoch", "INTEGER NOT NULL DEFAULT 0"),
            ("failed_logins", "INTEGER NOT NULL DEFAULT 0"),
            ("locked_until", "TIMESTAMP"),
            ("widget_key_hash", "TEXT"),
            ("widget_key_prefix", "TEXT NOT NULL DEFAULT ''"),
            ("widget_key_created_at", "TIMESTAMP"),
            ("widget_key_last_used", "TIMESTAMP"),
        ],
    }
    import logging
    log = logging.getLogger("goblindock")
    with engine.begin() as conn:
        for table, cols in adds.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, ddl in cols:
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        # Drift check across ALL tables: a model column missing from an existing table
        # and NOT covered by `adds` above means a migration was forgotten — surface it
        # loudly (it would otherwise be a "no such column" at runtime) instead of only
        # backfilling the two hand-listed tables.
        for tname, tbl in SQLModel.metadata.tables.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({tname})")}
            if not existing:
                continue  # table doesn't exist yet — create_all handles brand-new tables
            covered = {c[0] for c in adds.get(tname, [])}
            for col in tbl.columns:
                if col.name not in existing and col.name not in covered:
                    log.warning("DB drift: model column %s.%s is missing from the table — "
                                "add it to _migrate() in app/db.py", tname, col.name)
        # Match the model's index=True on widget_key_hash for DBs upgraded via the
        # ALTER path above (create_all only builds indexes for brand-new tables).
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_users_widget_key_hash "
            "ON users(widget_key_hash)"
        )
        # Backstop the static-IP allocator against a duplicate (network_id, ip) — a
        # second concurrent reservation of the same address fails at the DB rather than
        # silently double-booking. (Best-effort: skip if legacy rows already collide.)
        try:
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_ipalloc_net_ip "
                "ON ip_allocations(network_id, ip)"
            )
        except Exception as e:  # noqa: BLE001
            log.warning("could not create uq_ipalloc_net_ip — pre-existing duplicate "
                        "(network_id, ip) rows? IP double-booking backstop is ABSENT: %s", e)


def get_session() -> Iterator[Session]:
    """FastAPI dependency — yields a request-scoped session."""
    with Session(engine) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Standalone session for the worker thread / startup code."""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
