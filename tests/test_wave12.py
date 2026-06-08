"""Wave 12 — capacity awareness: node/storage status + capacity endpoint.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave12.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave12-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from app.db import init_db, session_scope  # noqa: E402

init_db()

GB = 1024 ** 3


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


def _mk_conn(storage="local-lvm"):
    from app.models import Connection
    with session_scope() as s:
        c = Connection(name="px-w12-" + os.urandom(3).hex(), host="127.0.0.1",
                       token_id="t@pve!x", node="pve", storage=storage)
        s.add(c); s.flush()
        return c.id


def test_node_status_wrappers():
    from app.models import Connection
    from app.proxmox import Proxmox
    px = Proxmox.__new__(Proxmox)
    px.conn = Connection(name="stub", host="127.0.0.1", token_id="t@pve!x", node="pve")
    px.node = "pve"
    px.storage = "local-lvm"
    px.iso_storage = "local"
    px.snippet_storage = "local"
    px.bridge = "vmbr0"

    class _Status:
        @staticmethod
        def get():
            return {"cpu": 0.25, "cpuinfo": {"cpus": 16},
                    "memory": {"total": 64 * GB, "used": 32 * GB}}

    class _Storage:
        @staticmethod
        def get():
            return [{"storage": "local-lvm", "type": "lvmthin",
                     "total": 900 * GB, "used": 300 * GB, "avail": 600 * GB}]

    class _Node:
        status = _Status()
        storage = _Storage()

    class _Nodes:
        def __call__(self, node):
            return _Node()

    class _Api:
        nodes = _Nodes()

    px.api = _Api()
    st = px.node_status("pve")
    assert st["cpuinfo"]["cpus"] == 16
    stores = px.storage_status("pve")
    assert stores[0]["storage"] == "local-lvm"
    print("test_node_status_wrappers OK")


def _stub_px(online_cores=16, store="local-lvm"):
    GB_ = 1024 ** 3

    class _StubPx:
        def __init__(self, conn): self.storage = conn.storage
        def pick_node(self): return "pve"
        def node_status(self, node=None):
            return {"cpu": 0.25, "cpuinfo": {"cpus": online_cores},
                    "memory": {"total": 64 * GB_, "used": 32 * GB_}}
        def storage_status(self, node=None):
            return [{"storage": store, "type": "lvmthin",
                     "total": 900 * GB_, "used": 300 * GB_, "avail": 600 * GB_},
                    {"storage": "backup-nfs", "type": "nfs",
                     "total": 4000 * GB_, "used": 1000 * GB_, "avail": 3000 * GB_}]
    return _StubPx


def test_capacity_endpoint_shape():
    from app import api
    from app.models import User
    uid = _mk_user("w12-cap@example.com", role="admin")
    conn_id = _mk_conn(storage="local-lvm")
    orig = api.Proxmox
    api.Proxmox = _stub_px()
    api._CAPACITY_CACHE.clear()
    try:
        with session_scope() as s:
            out = api.connection_capacity(conn_id, user=s.get(User, uid), session=s)
        assert out["online"] is True
        assert out["node"] == "pve"
        assert out["cpu"]["cores"] == 16 and out["cpu"]["usedPct"] == 25
        assert out["mem"]["totalGb"] == 64 and out["mem"]["freeGb"] == 32
        assert out["storage"]["name"] == "local-lvm" and out["storage"]["freeGb"] == 600
        assert any(s2["name"] == "backup-nfs" for s2 in out["stores"])  # admin sees full list
    finally:
        api.Proxmox = orig
    print("test_capacity_endpoint_shape OK")


def test_capacity_non_admin_redacted():
    from app import api
    from app.models import User
    uid = _mk_user("w12-cap-user@example.com", role="user")
    conn_id = _mk_conn(storage="local-lvm")
    orig = api.Proxmox
    api.Proxmox = _stub_px()
    api._CAPACITY_CACHE.clear()
    try:
        with session_scope() as s:
            out = api.connection_capacity(conn_id, user=s.get(User, uid), session=s)
        assert out["online"] is True
        assert "stores" not in out, "non-admin must not receive the full store list"
        assert out["storage"]["name"] == "", "non-admin must not learn the storage backend name"
        assert out["storage"]["freeGb"] == 600  # deploy-store headroom still present
    finally:
        api.Proxmox = orig
    print("test_capacity_non_admin_redacted OK")


def test_capacity_offline_and_404():
    from app import api
    from app.models import User
    uid = _mk_user("w12-cap-off@example.com", role="admin")
    conn_id = _mk_conn()
    # 404 for missing connection
    with session_scope() as s:
        _expect_http(404, lambda: api.connection_capacity(999999, user=s.get(User, uid), session=s))

    class _DownPx:
        def __init__(self, conn): pass
        def pick_node(self): raise RuntimeError("connection refused")
    orig = api.Proxmox
    api.Proxmox = _DownPx
    api._CAPACITY_CACHE.clear()
    try:
        with session_scope() as s:
            out = api.connection_capacity(conn_id, user=s.get(User, uid), session=s)
        assert out == {"online": False}
    finally:
        api.Proxmox = orig
    print("test_capacity_offline_and_404 OK")


if __name__ == "__main__":
    test_node_status_wrappers()
    test_capacity_endpoint_shape()
    test_capacity_non_admin_redacted()
    test_capacity_offline_and_404()
    print("\nALL WAVE 12 UNIT TESTS PASSED")
