"""Wave 14 — History as an auto-populated job log: DB settings store, configurable
retention prune, purge-all, and admin-gated retention endpoint.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave14.py
"""
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave14-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from app.db import engine, init_db, session_scope  # noqa: E402

init_db()


def _table_names():
    with engine.begin() as conn:
        return {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}


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


def _mk_job(uid, status="succeeded", created_days_ago=0):
    from app.models import Job, JobEvent, utcnow
    with session_scope() as s:
        j = Job(type="deploy", title="w14", status=status, created_by=uid)
        if created_days_ago:
            ts = utcnow() - timedelta(days=created_days_ago)
            j.created_at = ts
            if status in ("succeeded", "failed", "canceled"):
                j.finished_at = ts   # real finished jobs always carry finished_at
        s.add(j); s.flush()
        s.add(JobEvent(job_id=j.id, kind="log", line="hello"))
        s.flush()
        return j.id


def test_settings_table_and_helpers():
    assert "app_settings" in _table_names()
    from app import appsettings
    appsettings.set_setting("w14_probe", "hello")
    assert appsettings.get_setting("w14_probe") == "hello"
    appsettings.set_setting("w14_probe", "world")          # update path
    assert appsettings.get_setting("w14_probe") == "world"
    assert appsettings.get_setting("w14_absent", "def") == "def"
    # bad stored value never raises
    appsettings.set_setting(appsettings.JOB_RETENTION_DAYS, "not-a-number")
    assert appsettings.get_job_retention_days() == 0
    print("test_settings_table_and_helpers OK")


def test_job_retention_endpoints_and_admin_only():
    from app import api, appsettings
    from app.deps import require_admin
    from app.models import User
    admin = _mk_user("w14-admin@example.com", role="admin")
    plain = _mk_user("w14-user@example.com", role="user")
    appsettings.set_setting(appsettings.JOB_RETENTION_DAYS, "0")
    with session_scope() as s:
        assert api.get_job_retention(user=s.get(User, plain))["days"] == 0    # default off
    with session_scope() as s:
        out = api.set_job_retention(api.JobRetentionBody(days=30), user=s.get(User, admin))
        assert out["days"] == 30
    assert appsettings.get_job_retention_days() == 30
    with session_scope() as s:
        assert api.get_job_retention(user=s.get(User, plain))["days"] == 30
    # admin-only guard (the PUT depends on require_admin)
    with session_scope() as s:
        _expect_http(403, lambda: require_admin(user=s.get(User, plain)))
        assert require_admin(user=s.get(User, admin)).id == admin
    # bad input rejected by pydantic bounds (0..3650)
    try:
        api.JobRetentionBody(days=-5)
        raise AssertionError("expected ValidationError for negative days")
    except Exception as e:
        assert "days" in str(e), e
    appsettings.set_setting(appsettings.JOB_RETENTION_DAYS, "0")
    print("test_job_retention_endpoints_and_admin_only OK")


def test_prune_old_jobs_respects_retention():
    from app import api, appsettings
    from app.models import Job
    uid = _mk_user("w14-prune@example.com")
    old_done = _mk_job(uid, status="succeeded", created_days_ago=40)
    fresh_done = _mk_job(uid, status="succeeded", created_days_ago=1)
    old_running = _mk_job(uid, status="running", created_days_ago=40)
    # retention OFF → nothing pruned
    appsettings.set_setting(appsettings.JOB_RETENTION_DAYS, "0")
    assert api.prune_old_jobs() == 0
    with session_scope() as s:
        assert s.get(Job, old_done) is not None
    # retention 30d → only OLD FINISHED jobs go; fresh + running survive
    appsettings.set_setting(appsettings.JOB_RETENTION_DAYS, "30")
    assert api.prune_old_jobs() >= 1
    with session_scope() as s:
        assert s.get(Job, old_done) is None, "old finished job past retention must be pruned"
        assert s.get(Job, fresh_done) is not None, "fresh job survives"
        assert s.get(Job, old_running) is not None, "running job is never pruned"
    appsettings.set_setting(appsettings.JOB_RETENTION_DAYS, "0")
    print("test_prune_old_jobs_respects_retention OK")


def test_purge_all_tenant_scoped():
    from app import api
    from app.models import Job, JobEvent, User
    from sqlmodel import select
    a = _mk_user("w14-pa-a@example.com")
    b = _mk_user("w14-pa-b@example.com")
    a_done = _mk_job(a, status="succeeded")
    a_run = _mk_job(a, status="running")
    b_done = _mk_job(b, status="succeeded")
    with session_scope() as s:
        out = api.purge_all_jobs(user=s.get(User, a), session=s)
        assert out["purged"] >= 1
    with session_scope() as s:
        assert s.get(Job, a_done) is None, "A's finished job purged (+ its events)"
        assert s.exec(select(JobEvent).where(JobEvent.job_id == a_done)).all() == []
        assert s.get(Job, a_run) is not None, "running job is left alone"
        assert s.get(Job, b_done) is not None, "B's job untouched by non-admin A"
    print("test_purge_all_tenant_scoped OK")


def test_purge_all_admin_purges_all():
    from app import api
    from app.models import Job, User
    admin = _mk_user("w14-pa-admin@example.com", role="admin")
    other = _mk_user("w14-pa-other@example.com")
    o_done = _mk_job(other, status="succeeded")
    o_run = _mk_job(other, status="running")
    with session_scope() as s:
        api.purge_all_jobs(user=s.get(User, admin), session=s)   # admin = global
    with session_scope() as s:
        assert s.get(Job, o_done) is None, "admin purge removes ANY user's finished job"
        assert s.get(Job, o_run) is not None, "admin purge still leaves running jobs"
    print("test_purge_all_admin_purges_all OK")


if __name__ == "__main__":
    test_settings_table_and_helpers()
    test_job_retention_endpoints_and_admin_only()
    test_prune_old_jobs_respects_retention()
    test_purge_all_tenant_scoped()
    test_purge_all_admin_purges_all()
    print("\nALL WAVE 14 UNIT TESTS PASSED")
