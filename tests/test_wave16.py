"""Wave 16 — VM snapshots: list/create/delete/rollback endpoints + ownership.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave16.py
Run (Windows):        $env:GOBLINDOCK_DEV='1'; .venv\\Scripts\\python.exe tests\\test_wave16.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave16-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from app.db import init_db, session_scope  # noqa: E402

init_db()


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
    from app.security import hash_password
    with session_scope() as s:
        u = User(email=email, name="U", password_hash=hash_password("StrongPass12!"), role=role)
        s.add(u); s.flush()
        return u.id


def _mk_dep(owner_id, vmid=8001):
    from app.models import Connection, Deployment
    with session_scope() as s:
        c = Connection(name="px-t16-" + os.urandom(3).hex(), host="127.0.0.1",
                       token_id="t@pve!x", node="pve")
        s.add(c); s.flush()
        d = Deployment(name="vm-t16-" + os.urandom(2).hex(), owner_id=owner_id,
                       connection_id=c.id, vmid=vmid, node="pve", status="running")
        s.add(d); s.flush()
        return d.id


class FakePx:
    """Stands in for the Proxmox client: an in-memory snapshot store shared across
    instances (the endpoints construct a fresh Proxmox per request)."""
    snaps: list = []          # [{name, description, snaptime, vmstate}]
    on_snapshot = None        # name the synthetic 'current' entry points at
    fail = False
    calls: list = []

    def __init__(self, conn):
        self.conn = conn

    def pick_node(self):
        return "pve"

    def list_snapshots(self, vmid, node=None):
        if FakePx.fail:
            raise RuntimeError("node offline")
        out = list(FakePx.snaps)
        out.append({"name": "current", "description": "You are here!",
                    "parent": FakePx.on_snapshot})
        return out

    def create_snapshot(self, vmid, name, description="", vmstate=False, node=None):
        if FakePx.fail:
            raise RuntimeError("node offline")
        FakePx.calls.append(("create", vmid, name))
        FakePx.snaps.append({"name": name, "description": description,
                             "snaptime": int(time.time()), "vmstate": 1 if vmstate else 0})
        FakePx.on_snapshot = name
        return "UPID:fake"

    def delete_snapshot(self, vmid, name, node=None):
        FakePx.calls.append(("delete", vmid, name))
        FakePx.snaps = [s for s in FakePx.snaps if s["name"] != name]
        return "UPID:fake"

    def rollback_snapshot(self, vmid, name, node=None):
        FakePx.calls.append(("rollback", vmid, name))
        FakePx.on_snapshot = name
        return "UPID:fake"

    def wait_task(self, upid, node=None, timeout=300, **kw):
        return None


def _patch():
    from app import api
    api.Proxmox = FakePx


def _audit_actions():
    from sqlmodel import select
    from app.models import Audit
    with session_scope() as s:
        return [a.action for a in s.exec(select(Audit)).all()]


def test_snapshot_create_list_roundtrip():
    from app import api
    from app.models import User
    _patch()
    FakePx.snaps, FakePx.on_snapshot, FakePx.fail = [], None, False
    uid = _mk_user("t16-a@example.com")
    dep_id = _mk_dep(uid)
    with session_scope() as s:
        user = s.get(User, uid)
        r = api.create_vm_snapshot(dep_id, api.SnapshotBody(name="before-upgrade",
                                                            description="pre 2.0"),
                                   user=user, session=s)
        assert r["ok"] and r["name"] == "before-upgrade", r
        r2 = api.create_vm_snapshot(dep_id, api.SnapshotBody(), user=user, session=s)
        assert r2["name"].startswith("snap-"), r2
    with session_scope() as s:
        user = s.get(User, uid)
        out = api.list_vm_snapshots(dep_id, user=user, session=s)["snapshots"]
    names = [x["name"] for x in out]
    assert "current" not in names, "synthetic 'current' entry must be filtered out"
    assert set(names) == {"before-upgrade", r2["name"]}, names
    cur = [x for x in out if x["current"]]
    assert len(cur) == 1 and cur[0]["name"] == r2["name"], out
    assert "vm.snapshot.create" in _audit_actions()
    print("test_snapshot_create_list_roundtrip OK")


def test_snapshot_name_validation():
    from app import api
    from app.models import User
    _patch()
    uid = _mk_user("t16-name@example.com")
    dep_id = _mk_dep(uid, vmid=8002)
    with session_scope() as s:
        user = s.get(User, uid)
        for bad in ("1starts-with-digit", "has space", "semi;colon", "x" * 41, "-leading"):
            _expect_http(400, lambda b=bad: api.create_vm_snapshot(
                dep_id, api.SnapshotBody(name=b), user=user, session=s))
        _expect_http(400, lambda: api.delete_vm_snapshot(dep_id, "bad name", user=user, session=s))
        _expect_http(400, lambda: api.rollback_vm_snapshot(dep_id, "bad;name", user=user, session=s))
    print("test_snapshot_name_validation OK")


def test_snapshot_ownership_and_provisioning():
    from app import api
    from app.models import Deployment, User
    _patch()
    owner = _mk_user("t16-own@example.com")
    other = _mk_user("t16-other@example.com")
    admin = _mk_user("t16-admin@example.com", role="admin")
    dep_id = _mk_dep(owner, vmid=8003)
    with session_scope() as s:
        # another non-admin user must get 403; an admin passes
        _expect_http(403, lambda: api.list_vm_snapshots(dep_id, user=s.get(User, other), session=s))
        assert api.list_vm_snapshots(dep_id, user=s.get(User, admin), session=s) is not None
        # unprovisioned VM (no vmid yet) → 400
        d = s.get(Deployment, dep_id)
        d.vmid = None
        s.add(d); s.flush()
        _expect_http(400, lambda: api.list_vm_snapshots(dep_id, user=s.get(User, owner), session=s))
    print("test_snapshot_ownership_and_provisioning OK")


def test_snapshot_rollback_delete_and_failure():
    from app import api
    from app.models import User
    _patch()
    FakePx.snaps, FakePx.on_snapshot, FakePx.fail, FakePx.calls = [], None, False, []
    uid = _mk_user("t16-rb@example.com")
    dep_id = _mk_dep(uid, vmid=8004)
    with session_scope() as s:
        user = s.get(User, uid)
        api.create_vm_snapshot(dep_id, api.SnapshotBody(name="base"), user=user, session=s)
        api.create_vm_snapshot(dep_id, api.SnapshotBody(name="later"), user=user, session=s)
        assert api.rollback_vm_snapshot(dep_id, "base", user=user, session=s)["ok"]
        assert FakePx.on_snapshot == "base"
        assert api.delete_vm_snapshot(dep_id, "later", user=user, session=s)["ok"]
        assert [x["name"] for x in FakePx.snaps] == ["base"]
        # a Proxmox failure surfaces as 502, never a silent success
        FakePx.fail = True
        _expect_http(502, lambda: api.list_vm_snapshots(dep_id, user=user, session=s))
        _expect_http(502, lambda: api.create_vm_snapshot(dep_id, api.SnapshotBody(name="nope"),
                                                         user=user, session=s))
        FakePx.fail = False
    acts = _audit_actions()
    assert "vm.snapshot.rollback" in acts and "vm.snapshot.delete" in acts
    print("test_snapshot_rollback_delete_and_failure OK")


def test_job_chip_phase_note_and_pct():
    """Dashboard job chips: vm_dict's chip carries the live phase + pct, and
    JobCtx.phase_note appends a transient detail (download %) without moving pct."""
    from app import serialize as S
    from app.models import Deployment, Job, User
    from app.worker import JobCtx
    uid = _mk_user("t16-chip@example.com")
    dep_id = _mk_dep(uid, vmid=8005)
    with session_scope() as s:
        job = Job(type="deploy", title="Deploying x", deployment_id=dep_id, status="running")
        s.add(job); s.flush()
        jid = job.id
    ctx = JobCtx(jid)
    ctx.progress(8, "Phase 2 of 6 · Prepare image")
    ctx.phase_note("downloading 62%")
    ctx.phase_note("downloading 89%")   # each note replaces the previous one
    with session_scope() as s:
        job = s.get(Job, jid)
        assert job.pct == 8, job.pct
        assert job.phase == "Phase 2 of 6 · Prepare image · downloading 89%", job.phase
        dep = s.get(Deployment, dep_id)
        me = s.get(User, uid)
        chip = S.vm_dict(s, dep, me, {}, {uid: me}, {})["job"]
        assert chip["pct"] == 8 and chip["phase"].endswith("downloading 89%"), chip
    print("test_job_chip_phase_note_and_pct OK")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\nAll {len(fns)} wave-16 tests passed.")
