"""Wave 35 — feature: auto-start a VM after snapshot rollback.

Rolling back a disk-only snapshot of a running VM leaves it stopped (Proxmox stops it
to revert the disk). rollback_vm_snapshot now takes start=True by default and brings
the VM back up if it isn't already running, so the operator lands on a running VM at
the rollback point. A RAM snapshot already resumes running, so no extra start fires.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave35.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave35-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from app.db import init_db, session_scope         # noqa: E402
from app import api                               # noqa: E402
from app.models import Connection, Deployment, User  # noqa: E402
from app.security import hash_password             # noqa: E402

init_db()


def _setup():
    with session_scope() as s:
        u = User(email="w35-" + os.urandom(2).hex() + "@x.io", name="U",
                 password_hash=hash_password("StrongPass12!"))
        s.add(u); s.flush()
        c = Connection(name="w35-c-" + os.urandom(2).hex(), host="10.0.0.1", token_id="t@pve!x", node="pve")
        s.add(c); s.flush()
        d = Deployment(name="vm", owner_id=u.id, connection_id=c.id, vmid=9990, node="pve", status="running")
        s.add(d); s.flush()
        return u.id, d.id


def _px_stub(status_after_rollback):
    calls = {"start": 0}

    class _Px:
        node = "pve"
        def __init__(self, conn): pass
        def pick_node(self): return "pve"
        def rollback_snapshot(self, vmid, name, node=None): return "UPID:rollback"
        def wait_task(self, upid, node=None, timeout=0, **k): pass
        def vm_current(self, vmid, node=None): return {"status": status_after_rollback}
        def start(self, vmid, node=None): calls["start"] += 1; return "UPID:start"
    return _Px, calls


def _rollback(uid, did, start, status_after):
    PxStub, calls = _px_stub(status_after)
    orig = api.Proxmox
    api.Proxmox = PxStub
    try:
        with session_scope() as s:
            out = api.rollback_vm_snapshot(did, "snap1", body=api.RollbackBody(start=start),
                                           user=s.get(User, uid), session=s)
        return out, calls
    finally:
        api.Proxmox = orig


def test_rollback_starts_stopped_vm_when_requested():
    uid, did = _setup()
    out, calls = _rollback(uid, did, start=True, status_after="stopped")
    assert out["ok"] and out["started"] is True, out
    assert calls["start"] == 1, "a stopped VM must be started after rollback when start=True"
    print("test_rollback_starts_stopped_vm_when_requested OK")


def test_rollback_skips_start_when_already_running():
    uid, did = _setup()
    out, calls = _rollback(uid, did, start=True, status_after="running")
    assert out["started"] is False, out
    assert calls["start"] == 0, "a RAM snapshot resumes running — must not double-start"
    print("test_rollback_skips_start_when_already_running OK")


def test_rollback_respects_start_false():
    uid, did = _setup()
    out, calls = _rollback(uid, did, start=False, status_after="stopped")
    assert out["started"] is False, out
    assert calls["start"] == 0, "start=False must leave the VM stopped"
    print("test_rollback_respects_start_false OK")


def test_rollback_defaults_start_true():
    assert api.RollbackBody().start is True, "auto-start toggle defaults ON"
    print("test_rollback_defaults_start_true OK")


if __name__ == "__main__":
    test_rollback_starts_stopped_vm_when_requested()
    test_rollback_skips_start_when_already_running()
    test_rollback_respects_start_false()
    test_rollback_defaults_start_true()
    print("\nALL WAVE 35 UNIT TESTS PASSED")
