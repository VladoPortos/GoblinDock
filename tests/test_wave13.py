"""Wave 13 — job history: soft-dismiss, history endpoint, prune, purge.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave13.py
"""
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave13-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from app.db import engine, init_db, session_scope  # noqa: E402

init_db()


def _cols(table):
    with engine.begin() as conn:
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def _expect_http(code, fn):
    from fastapi import HTTPException
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == code, (e.status_code, e.detail)
        return e
    raise AssertionError(f"expected HTTPException {code}")


def _mk_user(email, role="user"):
    from app.models import User
    with session_scope() as s:
        u = User(email=email, name="U", password_hash="x", role=role)
        s.add(u); s.flush()
        return u.id


def test_jobs_schema_dismissed_columns():
    cols = _cols("jobs")
    assert {"dismissed", "dismissed_at"} <= cols, cols
    print("test_jobs_schema_dismissed_columns OK")


def _mk_job(uid, status="succeeded"):
    from app.models import Job, JobEvent
    with session_scope() as s:
        j = Job(type="deploy", title="hist-test", status=status, created_by=uid)
        s.add(j); s.flush()
        s.add(JobEvent(job_id=j.id, kind="log", line="hello"))
        s.flush()
        return j.id


def test_delete_dismisses_not_purges():
    from app import api
    from app.models import Job, JobEvent, User
    from sqlmodel import select
    uid = _mk_user("w13-dismiss@example.com")
    jid = _mk_job(uid)
    with session_scope() as s:
        api.delete_job(jid, user=s.get(User, uid), session=s)
    with session_scope() as s:
        j = s.get(Job, jid)
        assert j is not None and j.dismissed is True and j.dismissed_at is not None
        evs = s.exec(select(JobEvent).where(JobEvent.job_id == jid)).all()
        assert len(evs) == 1, "dismiss must keep the job's events"
    print("test_delete_dismisses_not_purges OK")


def test_list_excludes_dismissed():
    from app import api
    from app.models import User
    uid = _mk_user("w13-exclude@example.com")
    jid = _mk_job(uid)
    with session_scope() as s:
        user = s.get(User, uid)
        api.delete_job(jid, user=user, session=s)
        listed = api.list_jobs(user=user, session=s)
        assert all(b["jobId"] != jid for b in listed), "dismissed job must not appear in /jobs"
    # NOTE: the /state JOBS query gets the IDENTICAL `Job.dismissed == False` filter
    # (api.state, lines ~439-442); it's exercised via the running app rather than here
    # because api.state() needs a live Request (ensure_csrf) + the Proxmox probe cache.
    print("test_list_excludes_dismissed OK")


def test_history_shows_all_jobs():
    # History is now an auto-populated log of ALL jobs — dismissed OR not (the dismissed
    # flag only hides a job from the activity bell, never from History).
    from app import api
    from app.models import User
    uid = _mk_user("w13-history@example.com")
    dismissed = _mk_job(uid)
    active = _mk_job(uid)   # never dismissed
    with session_scope() as s:
        user = s.get(User, uid)
        api.delete_job(dismissed, user=user, session=s)   # hide from bell only
        hist_ids = {b["jobId"] for b in api.jobs_history(user=user, session=s)}
        assert dismissed in hist_ids, "history must include a dismissed job"
        assert active in hist_ids, "history must ALSO include a non-dismissed job"
        listed = {b["jobId"] for b in api.list_jobs(user=user, session=s)}
        assert active in listed and dismissed not in listed, "bell shows non-dismissed only"
    print("test_history_shows_all_jobs OK")


def test_purge_permanently_deletes():
    from app import api
    from app.models import Job, JobEvent, User
    from sqlmodel import select
    uid = _mk_user("w13-purge@example.com")
    jid = _mk_job(uid)
    with session_scope() as s:
        user = s.get(User, uid)
        api.delete_job(jid, user=user, session=s)
        api.purge_job_permanently(jid, user=user, session=s)
    with session_scope() as s:
        assert s.get(Job, jid) is None, "purge must delete the job row"
        assert s.exec(select(JobEvent).where(JobEvent.job_id == jid)).all() == []
    print("test_purge_permanently_deletes OK")


if __name__ == "__main__":
    test_jobs_schema_dismissed_columns()
    test_delete_dismisses_not_purges()
    test_list_excludes_dismissed()
    test_history_shows_all_jobs()
    test_purge_permanently_deletes()
    print("\nALL WAVE 13 UNIT TESTS PASSED")
