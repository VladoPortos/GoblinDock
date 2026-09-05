"""Wave 2 — job state-machine reliability (claim-cancel, crash recovery).

Uses an isolated temp SQLite DB. Run:
  GOBLINDOCK_SECRET_KEY=<64hex> .venv/bin/python tests/test_wave2.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# isolate: fresh DB file BEFORE importing app.db (engine binds at import)
_DB = "/tmp/gd-wave2-test.sqlite3"
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", "/tmp/gd-data-test")

from app.db import init_db, session_scope          # noqa: E402
from app import worker                              # noqa: E402
from app.models import Job, Deployment, Image, IpAllocation  # noqa: E402
from sqlmodel import select                                 # noqa: E402

init_db()


def test_claim_skips_cancelled():
    with session_scope() as s:
        s.add(Job(type="destroy", status="queued", cancel_requested=True))   # id 1
        s.add(Job(type="deploy", status="queued"))                            # id 2
    # first claim hits the cancelled queued job → marks it canceled, runs nothing
    jid = worker._claim_next_job()
    assert jid is None, f"expected None (cancelled skipped), got {jid}"
    with session_scope() as s:
        j1 = s.get(Job, 1)
        assert j1.status == "canceled", j1.status
    # next claim picks the normal job and runs it
    jid = worker._claim_next_job()
    assert jid == 2, jid
    with session_scope() as s:
        assert s.get(Job, 2).status == "running"
    print("test_claim_skips_cancelled OK")


def test_recover_orphans():
    with session_scope() as s:
        dep = Deployment(name="d1", status="working", vmid=None)
        img = Image(kind="golden", name="g1", build_status="building", template_vmid=8002)
        s.add(dep)
        s.add(img)
    with session_scope() as s:
        dep = s.exec(__import__("sqlmodel").select(Deployment)).first()
        img = s.exec(__import__("sqlmodel").select(Image)).first()
        s.add(IpAllocation(network_id=1, ip="10.0.0.5", deployment_id=dep.id, state="reserved"))
        s.add(Job(type="deploy", status="running", deployment_id=dep.id))
        s.add(Job(type="image_build", status="running", image_id=img.id))
        dep_id, img_id = dep.id, img.id

    worker._recover_orphans()

    with session_scope() as s:
        from sqlmodel import select
        jobs = s.exec(select(Job)).all()
        assert all(j.status == "failed" for j in jobs if j.type in ("deploy", "image_build")), \
            [j.status for j in jobs]
        dep = s.get(Deployment, dep_id)
        img = s.get(Image, img_id)
        assert dep.status == "error", dep.status
        assert img.build_status == "failed", img.build_status
        assert img.template_vmid == 8002, "ghost vmid must be kept for cleanup"
        allocs = s.exec(select(IpAllocation).where(IpAllocation.deployment_id == dep_id)).all()
        assert allocs == [], "interrupted deploy's IP reservation must be freed"
    print("test_recover_orphans OK")


def test_recover_orphans_commits_running_jobs_before_reconciliation_and_ignores_waiting():
    """Restart recovery must reconcile committed running jobs and leave future waiting jobs alone."""
    with session_scope() as s:
        running_dep = Deployment(name="running-dep", status="working")
        waiting_dep = Deployment(name="waiting-dep", status="working")
        s.add(running_dep); s.add(waiting_dep); s.flush()
        s.add(IpAllocation(network_id=2, ip="10.0.0.6", deployment_id=running_dep.id))
        s.add(IpAllocation(network_id=2, ip="10.0.0.7", deployment_id=waiting_dep.id))
        running_job = Job(type="deploy", status="running", deployment_id=running_dep.id)
        waiting_job = Job(type="deploy", status="waiting", deployment_id=waiting_dep.id)
        s.add(running_job); s.add(waiting_job); s.flush()
        running_job_id, waiting_job_id = running_job.id, waiting_job.id
        waiting_dep_id = waiting_dep.id

    reconciled = []
    original = worker._reconcile_failed_job

    def observed_reconcile(job_id):
        with session_scope() as s:
            assert s.get(Job, job_id).status == "failed", "reconciliation must see committed restart state"
        reconciled.append(job_id)
        original(job_id)

    worker._reconcile_failed_job = observed_reconcile
    try:
        worker._recover_orphans()
    finally:
        worker._reconcile_failed_job = original

    assert reconciled == [running_job_id]
    with session_scope() as s:
        assert s.get(Job, waiting_job_id).status == "waiting"
        assert s.get(Deployment, waiting_dep_id).status == "working"
        waiting_allocs = s.exec(select(IpAllocation).where(
            IpAllocation.deployment_id == waiting_dep_id)).all()
        assert len(waiting_allocs) == 1


if __name__ == "__main__":
    test_claim_skips_cancelled()
    test_recover_orphans()
    test_recover_orphans_commits_running_jobs_before_reconciliation_and_ignores_waiting()
    print("\nALL WAVE 2 UNIT TESTS PASSED")
