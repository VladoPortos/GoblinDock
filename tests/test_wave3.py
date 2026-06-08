"""Wave 3 — WS origin guard + session versioning (revoke on password change).

Run: GOBLINDOCK_SECRET_KEY=<64hex> .venv/bin/python tests/test_wave3.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DB = "/tmp/gd-wave3-test.sqlite3"
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", "/tmp/gd-data-test")

from app.db import init_db, session_scope          # noqa: E402
from app import api                                 # noqa: E402
from app.deps import current_user                   # noqa: E402
from app.config import settings                     # noqa: E402
from app.models import User                         # noqa: E402
from app.security import hash_password              # noqa: E402
from fastapi import HTTPException                    # noqa: E402

init_db()


def _ws(origin=None, host="goblin.example"):
    h = {"host": host}
    if origin is not None:
        h["origin"] = origin
    return SimpleNamespace(headers=h)


def test_ws_origin_ok():
    settings.cors_origins = []
    # no Origin (non-browser client) is allowed
    assert api._ws_origin_ok(_ws(origin=None)) is True
    # same-origin allowed
    assert api._ws_origin_ok(_ws(origin="https://goblin.example", host="goblin.example")) is True
    # cross-origin rejected (CSWSH)
    assert api._ws_origin_ok(_ws(origin="https://evil.example", host="goblin.example")) is False
    # explicit allow-list entry permitted, canonicalized (trailing slash / case)
    settings.cors_origins = ["https://Trusted.Example/"]
    assert api._ws_origin_ok(_ws(origin="https://trusted.example", host="goblin.example")) is True
    settings.cors_origins = []
    print("test_ws_origin_ok OK")


def test_session_epoch_revocation():
    with session_scope() as s:
        s.add(User(email="u@x.io", name="U", password_hash=hash_password("StrongPass12!"),
                   role="admin", session_epoch=0))
    with session_scope() as s:
        uid = s.exec(__import__("sqlmodel").select(User)).first().id

    # a session stamped with the current epoch authenticates
    req_ok = SimpleNamespace(session={"uid": uid, "sv": 0})
    with session_scope() as s:
        u = current_user(req_ok, s)
        assert u.id == uid

    # bump the epoch (as a password change does) → old-epoch session is rejected
    with session_scope() as s:
        u = s.get(User, uid)
        u.session_epoch = 1
        s.add(u)
    req_stale = SimpleNamespace(session={"uid": uid, "sv": 0})
    try:
        with session_scope() as s:
            current_user(req_stale, s)
        assert False, "stale-epoch session should be rejected"
    except HTTPException as e:
        assert e.status_code == 401

    # a freshly re-stamped session works again
    req_new = SimpleNamespace(session={"uid": uid, "sv": 1})
    with session_scope() as s:
        assert current_user(req_new, s).id == uid
    print("test_session_epoch_revocation OK")


if __name__ == "__main__":
    test_ws_origin_ok()
    test_session_epoch_revocation()
    print("\nALL WAVE 3 UNIT TESTS PASSED")
