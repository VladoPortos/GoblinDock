"""Wave 29 — review Batch D: transport & at-rest hardening.

D1: Proxmox TLS verification defaults ON (verify_tls=True). New connections verify
    by default; the probe form reuses the stored value when unset (None) rather than
    inferring "unset" from a falsy False.
D2: the data dir / DB / rotating backups are created owner-only (0700 dir, 0600 file)
    so a co-mounted reader can't copy them and offline-crack the Argon2 hashes / read
    the plaintext audit log.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave29.py
"""
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave29-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from sqlmodel import select                       # noqa: E402
from app.db import init_db, session_scope         # noqa: E402
from app import api, backup                        # noqa: E402
from app.models import Connection, User            # noqa: E402
from app.security import hash_password             # noqa: E402

init_db()


def _mk_admin(email):
    with session_scope() as s:
        u = User(email=email, name="A", password_hash=hash_password("StrongPass12!"), role="admin")
        s.add(u); s.flush()
        return u.id


# --------------------------------------------------------------------------- #
# D1 — TLS verification defaults ON                                            #
# --------------------------------------------------------------------------- #
def test_connbody_defaults_verify_on():
    assert api.ConnBody(name="c", host="h", token_id="t@pve!x", token_secret="s").verify_tls is True
    assert api.ConnProbeBody(host="h").verify_tls is None, "probe verify_tls is unset (None) by default"
    print("test_connbody_defaults_verify_on OK")


def test_add_connection_defaults_verify_on():
    adm = _mk_admin("w29-a1@x.io")
    with session_scope() as s:
        api.add_connection(api.ConnBody(name="w29-c1", host="10.0.0.9",
                                        token_id="t@pve!x", token_secret="s"),
                           user=s.get(User, adm), session=s)
    with session_scope() as s:
        c = s.exec(select(Connection).where(Connection.name == "w29-c1")).first()
        assert c is not None and c.verify_tls is True, "new connections must verify TLS by default"
    print("test_add_connection_defaults_verify_on OK")


class _PxStub:
    """Captures the verify_tls of the transient Connection the probe builds."""
    seen = {}

    def __init__(self, conn):
        _PxStub.seen["verify_tls"] = conn.verify_tls

    def version(self):
        return {"version": "8.1"}

    def nodes(self):
        return [{"node": "pve", "status": "online"}]

    def storage_status(self, node):
        return [{"storage": "local", "content": "images", "type": "dir", "avail": 0}]

    def bridges(self, node):
        return ["vmbr0"]


def test_probe_reuses_stored_verify_tls_when_unset():
    """Editing an existing self-signed (verify_tls=False) connection and re-probing
    WITHOUT re-sending verify_tls must reuse the stored False, not the secure default."""
    adm = _mk_admin("w29-a2@x.io")
    with session_scope() as s:
        c = Connection(name="w29-stored", host="10.0.0.10", token_id="t@pve!x",
                       token_secret_enc="", verify_tls=False, node="pve")
        s.add(c); s.flush(); cid = c.id
    orig = api.Proxmox
    api.Proxmox = _PxStub
    try:
        with session_scope() as s:
            api.probe_connection(api.ConnProbeBody(host="10.0.0.10", conn_id=cid),
                                 user=s.get(User, adm), session=s)
        assert _PxStub.seen["verify_tls"] is False, "unset probe must reuse the stored verify_tls"
        # an explicit True overrides the stored value
        with session_scope() as s:
            api.probe_connection(api.ConnProbeBody(host="10.0.0.10", conn_id=cid, verify_tls=True),
                                 user=s.get(User, adm), session=s)
        assert _PxStub.seen["verify_tls"] is True, "an explicit verify_tls wins over stored"
    finally:
        api.Proxmox = orig
    print("test_probe_reuses_stored_verify_tls_when_unset OK")


# --------------------------------------------------------------------------- #
# D2 — DB / backups owner-only                                                 #
# --------------------------------------------------------------------------- #
def test_backup_files_owner_only():
    dest = backup.backup_now("test")
    fmode = stat.S_IMODE(os.stat(dest).st_mode)
    dmode = stat.S_IMODE(os.stat(dest.parent).st_mode)
    assert fmode == 0o600, f"backup file must be 0o600, got {oct(fmode)}"
    assert dmode == 0o700, f"backup dir must be 0o700, got {oct(dmode)}"
    print("test_backup_files_owner_only OK")


def test_db_file_owner_only():
    mode = stat.S_IMODE(os.stat(_DB).st_mode)
    assert mode == 0o600, f"DB file must be 0o600, got {oct(mode)}"
    print("test_db_file_owner_only OK")


if __name__ == "__main__":
    test_connbody_defaults_verify_on()
    test_add_connection_defaults_verify_on()
    test_probe_reuses_stored_verify_tls_when_unset()
    test_backup_files_owner_only()
    test_db_file_owner_only()
    print("\nALL WAVE 29 UNIT TESTS PASSED")
