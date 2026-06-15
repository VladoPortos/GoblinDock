"""Wave 22 — "Load from Proxmox" connection-discovery probe endpoint.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave22.py
Run (Windows):        $env:GOBLINDOCK_DEV='1'; .venv\\Scripts\\python.exe tests\\test_wave22.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave22-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from app.db import init_db, session_scope  # noqa: E402

init_db()


# A storage fixture that exercises both the VM-storage ('images') and the
# ISO-storage ('iso'/'import') content filters, plus a store with neither.
_STORAGES = [
    {"storage": "local-zfs", "type": "zfspool", "content": "images,rootdir",
     "total": 500 * 1024 ** 3, "used": 100 * 1024 ** 3, "avail": 400 * 1024 ** 3},
    {"storage": "local", "type": "dir", "content": "iso,vztmpl,import,snippets",
     "total": 100 * 1024 ** 3, "used": 30 * 1024 ** 3, "avail": 70 * 1024 ** 3},
    {"storage": "backups", "type": "dir", "content": "backup",
     "total": 200 * 1024 ** 3, "used": 0, "avail": 200 * 1024 ** 3},
]


class _StubPx:
    """Captures the Connection it was built with, returns the storage fixtures."""
    last_conn = None

    def __init__(self, conn):
        _StubPx.last_conn = conn

    def version(self):
        return {"version": "8.2.4", "release": "8.2"}

    def nodes(self):
        return [{"node": "pve", "status": "online"},
                {"node": "pve2", "status": "offline"}]

    def storage_status(self, node=None):
        return list(_STORAGES)

    def bridges(self, node=None):
        return ["vmbr0", "vmbr1"]


def _mk_user(email, role="admin"):
    from app.models import User
    from app.security import hash_password
    with session_scope() as s:
        u = User(email=email, name="U", password_hash=hash_password("StrongPass12!"), role=role)
        s.add(u); s.flush()
        return u.id


def test_probe_categorizes_storages():
    from app import api
    uid = _mk_user("w22-cat@example.com")
    orig = api.Proxmox
    api.Proxmox = _StubPx
    try:
        with session_scope() as s:
            user = s.get(api.User, uid)
            out = api.probe_connection(api.ConnProbeBody(
                host="10.0.0.5", port=8006, token_id="g@pve!app",
                token_secret="topsecret", verify_tls=False), user=user, session=s)
    finally:
        api.Proxmox = orig

    assert out["ok"] is True, out
    assert out["version"] == "8.2.4", out
    # only the ONLINE node is offered, online first
    assert out["nodes"] == ["pve"], out["nodes"]
    assert out["bridges"] == ["vmbr0", "vmbr1"], out["bridges"]

    by_name = {st["name"]: st for st in out["storages"]}
    # content is split into a list
    assert by_name["local-zfs"]["content"] == ["images", "rootdir"], by_name["local-zfs"]
    assert "iso" in by_name["local"]["content"] and "import" in by_name["local"]["content"]
    # freeGb derived from avail bytes
    assert by_name["local-zfs"]["freeGb"] == 400, by_name["local-zfs"]["freeGb"]

    # VM-storage filter ('images') matches local-zfs, not local/backups
    vm = [st["name"] for st in out["storages"] if "images" in st["content"]]
    assert vm == ["local-zfs"], vm
    # ISO-storage filter ('import' or 'iso') matches local
    iso = [st["name"] for st in out["storages"]
           if "import" in st["content"] or "iso" in st["content"]]
    assert iso == ["local"], iso
    print("test_probe_categorizes_storages OK")


def test_probe_reuses_stored_secret():
    from app import api
    from app.models import Connection
    from app.security import encrypt, decrypt
    uid = _mk_user("w22-reuse@example.com")
    # a stored connection whose encrypted secret we expect to be reused
    with session_scope() as s:
        c = Connection(name="px-w22", host="10.0.0.9", port=8006,
                       token_id="stored@pve!tok",
                       token_secret_enc=encrypt("STORED-SECRET-XYZ"),
                       verify_tls=True, node="pve")
        s.add(c); s.flush(); conn_id = c.id

    orig = api.Proxmox
    api.Proxmox = _StubPx
    _StubPx.last_conn = None
    try:
        with session_scope() as s:
            user = s.get(api.User, uid)
            # blank token_secret + valid conn_id → reuse the stored secret
            out = api.probe_connection(api.ConnProbeBody(
                host="10.0.0.9", token_secret="", conn_id=conn_id),
                user=user, session=s)
    finally:
        api.Proxmox = orig

    assert out["ok"] is True, out
    built = _StubPx.last_conn
    assert built is not None, "Proxmox was never constructed"
    # the transient conn carried the STORED encrypted secret (decrypts to original)
    assert decrypt(built.token_secret_enc) == "STORED-SECRET-XYZ", built.token_secret_enc
    # blank token_id reused the stored one; stored verify_tls/port reused too
    assert built.token_id == "stored@pve!tok", built.token_id
    assert built.verify_tls is True, built.verify_tls

    # transient connection must NOT have been persisted (no extra row, no commit)
    from sqlmodel import select
    with session_scope() as s:
        names = [r.name for r in s.exec(select(Connection)).all()]
        assert names.count("(probe)") == 0, names
    print("test_probe_reuses_stored_secret OK")


def test_probe_redacts_errors():
    from app import api
    uid = _mk_user("w22-err@example.com")

    class _BoomPx:
        def __init__(self, conn):
            pass

        def version(self):
            raise RuntimeError("SECRET host 10.9.9.9 token leak — do not echo")

    orig = api.Proxmox
    api.Proxmox = _BoomPx
    try:
        with session_scope() as s:
            user = s.get(api.User, uid)
            out = api.probe_connection(api.ConnProbeBody(
                host="10.9.9.9", token_id="g@pve!app", token_secret="x"),
                user=user, session=s)
    finally:
        api.Proxmox = orig

    assert out["ok"] is False, out
    assert "SECRET" not in out["error"] and "10.9.9.9" not in out["error"], out["error"]
    assert "leak" not in out["error"], out["error"]
    assert "Proxmox API" in out["error"], out["error"]
    print("test_probe_redacts_errors OK")


def test_probe_unknown_conn_id_404():
    from app import api
    from fastapi import HTTPException
    uid = _mk_user("w22-404@example.com")
    orig = api.Proxmox
    api.Proxmox = _StubPx
    try:
        with session_scope() as s:
            user = s.get(api.User, uid)
            try:
                api.probe_connection(api.ConnProbeBody(
                    host="x", conn_id=999999), user=user, session=s)
                raise AssertionError("expected HTTPException 404")
            except HTTPException as e:
                assert e.status_code == 404, e.status_code
    finally:
        api.Proxmox = orig
    print("test_probe_unknown_conn_id_404 OK")


if __name__ == "__main__":
    test_probe_categorizes_storages()
    test_probe_reuses_stored_secret()
    test_probe_redacts_errors()
    test_probe_unknown_conn_id_404()
    print("\nALL WAVE 22 UNIT TESTS PASSED")
