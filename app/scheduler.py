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


def _safe_prune_history() -> None:
    # Retention prune must never escape into the scheduler thread. Retention is the
    # UI-set job_retention_days (0 = keep forever), read inside prune_old_jobs().
    try:
        from .api import prune_old_jobs
        n = prune_old_jobs()
        if n:
            log.info("pruned %d job(s) past the history retention window", n)
    except Exception as e:  # noqa: BLE001
        log.warning("job-history prune failed: %s", e)


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    sched = BackgroundScheduler(daemon=True, timezone="UTC")
    # Rotating DB backups — optional (only if enabled).
    if settings.backup_enabled:
        hours = settings.backup_interval_hours
        sched.add_job(_safe_backup, "interval", hours=hours, id="db_backup",
                      coalesce=True, max_instances=1, misfire_grace_time=3600,
                      replace_existing=True)
        log.info("scheduled DB backup every %dh → %s (keep %d)",
                 hours, settings.backup_dir, settings.backup_keep)
    else:
        log.info("scheduled backups disabled (GOBLINDOCK_BACKUP_ENABLED=0)")
    # Job-history retention — always on, daily.
    sched.add_job(_safe_prune_history, "interval", hours=24, id="job_history_prune",
                  coalesce=True, max_instances=1, misfire_grace_time=3600,
                  replace_existing=True)
    sched.start()
    _scheduler = sched
    log.info("scheduler started")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        finally:
            _scheduler = None
