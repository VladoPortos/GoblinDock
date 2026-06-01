"""APScheduler-based periodic tasks — the app's single home for scheduled work.

GoblinDock's worker thread (app/worker.py) drains the Job QUEUE on demand; it is
deliberately NOT a wall-clock timer (its idle counter resets whenever a job runs, so it
can't be relied on to fire "every N hours"). Time-based maintenance lives here instead,
on an APScheduler BackgroundScheduler. The app runs a single uvicorn worker, so exactly
one scheduler instance exists and each job fires once per interval.

Currently scheduled: rotating SQLite backups. New periodic tasks (e.g. log retention,
VM TTL sweeps) should register here rather than growing the worker loop.
"""
from __future__ import annotations

import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from . import backup
from .config import settings

log = logging.getLogger("goblindock")

_scheduler: Optional[BackgroundScheduler] = None


def _safe_backup() -> None:
    # A backup failure must never escape into the scheduler thread — log it and let the
    # next interval try again.
    try:
        backup.backup_now("scheduled")
    except Exception as e:  # noqa: BLE001
        log.warning("scheduled DB backup failed: %s", e)


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    if not settings.backup_enabled:
        log.info("scheduled backups disabled (GOBLINDOCK_BACKUP_ENABLED=0)")
        return
    sched = BackgroundScheduler(daemon=True, timezone="UTC")
    hours = settings.backup_interval_hours
    # First run at now + interval (no backup on every container restart); coalesce +
    # max_instances=1 so a slow/late run can't pile up against the single worker.
    sched.add_job(_safe_backup, "interval", hours=hours, id="db_backup",
                  coalesce=True, max_instances=1, misfire_grace_time=3600,
                  replace_existing=True)
    sched.start()
    _scheduler = sched
    log.info("scheduler started · DB backup every %dh → %s (keep %d)",
             hours, settings.backup_dir, settings.backup_keep)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        finally:
            _scheduler = None
