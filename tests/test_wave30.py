"""Wave 30 — review Batch E: resource limits / availability.

E1: the in-memory login throttle dict must not grow without bound. Empty windows are
    evicted, and an opportunistic sweep drops stale keys once the dict is large, so an
    unauthenticated attacker varying the email can't leak memory. The per-key block at
    8 attempts / 5 min is preserved.
E2 (QA-M2): POST /images/{id}/sync is admin-only and de-duplicates an already
    queued/running sync for the same image+connection, so a non-admin can't pile
    multi-GB downloads onto the single serial worker.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave30.py
"""
import inspect
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave30-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from fastapi import HTTPException                 # noqa: E402
from sqlmodel import select                       # noqa: E402
from app.db import init_db, session_scope         # noqa: E402
from app import api                               # noqa: E402
from app.models import Connection, Image, Job, User  # noqa: E402
from app.security import hash_password             # noqa: E402

init_db()


def _mk_user(email, role="user"):
    with session_scope() as s:
        u = User(email=email, name="U", password_hash=hash_password("StrongPass12!"), role=role)
        s.add(u); s.flush()
        return u.id


def _expect_http(code, fn):
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == code, (e.status_code, e.detail)
        return e
    raise AssertionError(f"expected HTTPException {code}")


# --------------------------------------------------------------------------- #
# E1 — login throttle bounded                                                  #
# --------------------------------------------------------------------------- #
def test_throttle_evicts_empty_window():
    api._login_attempts.clear()
    # a key whose only attempt is older than the 5-min window prunes to empty
    api._login_attempts["stale|1.2.3.4"] = [time.time() - 400]
    api._throttle("stale|1.2.3.4")
    assert "stale|1.2.3.4" not in api._login_attempts, "empty window must be evicted, not kept"
    print("test_throttle_evicts_empty_window OK")


def test_throttle_sweeps_stale_keys_when_large():
    api._login_attempts.clear()
    old = time.time() - 400
    for i in range(api._MAX_THROTTLE_KEYS + 25):
        api._login_attempts[f"junk{i}|9.9.9.9"] = [old]
    assert len(api._login_attempts) > api._MAX_THROTTLE_KEYS
    api._throttle("trigger|1.1.1.1")   # crossing the cap triggers the sweep
    assert len(api._login_attempts) < api._MAX_THROTTLE_KEYS, \
        f"stale keys must be swept, still {len(api._login_attempts)}"
    api._login_attempts.clear()
    print("test_throttle_sweeps_stale_keys_when_large OK")


def test_throttle_still_blocks_after_8_recent():
    api._login_attempts.clear()
    now = time.time()
    api._login_attempts["spam|1.1.1.1"] = [now - i for i in range(8)]   # 8 recent attempts
    _expect_http(429, lambda: api._throttle("spam|1.1.1.1"))
    api._login_attempts.clear()
    print("test_throttle_still_blocks_after_8_recent OK")


# --------------------------------------------------------------------------- #
# E2 — sync_image admin-gated + de-duplicated                                  #
# --------------------------------------------------------------------------- #
def test_sync_image_is_admin_gated():
    dep = inspect.signature(api.sync_image).parameters["user"].default
    assert getattr(dep, "dependency", None) is api.require_admin, \
        "POST /images/{id}/sync must require admin, like every sibling connection/image write"
    print("test_sync_image_is_admin_gated OK")


def test_sync_image_dedupes_active_job():
    adm = _mk_user("w30-adm@x.io", role="admin")
    with session_scope() as s:
        img = Image(kind="base", name="i-" + os.urandom(2).hex(),
                    source_url="https://e/x.img", build_status="ready")
        s.add(img); s.flush(); iid = img.id
        c = Connection(name="w30-c", host="10.0.0.1", token_id="t@pve!x", node="pve")
        s.add(c); s.flush(); cid = c.id
        # an already-queued sync for the same image+connection
        s.add(Job(type="image_sync", image_id=iid, connection_id=cid,
                  created_by=adm, status="queued", title="existing"))
        s.flush()
        first_id = s.exec(select(Job).where(Job.type == "image_sync")).first().id

    with session_scope() as s:
        out = api.sync_image(iid, api.SyncBody(connectionId=cid), user=s.get(User, adm), session=s)
    assert out["jobId"] == first_id, "must return the existing job, not enqueue a duplicate"
    with session_scope() as s:
        n = len(s.exec(select(Job).where(Job.type == "image_sync",
                                         Job.image_id == iid, Job.connection_id == cid)).all())
    assert n == 1, f"a duplicate sync must NOT be enqueued, found {n} jobs"
    print("test_sync_image_dedupes_active_job OK")


if __name__ == "__main__":
    test_throttle_evicts_empty_window()
    test_throttle_sweeps_stale_keys_when_large()
    test_throttle_still_blocks_after_8_recent()
    test_sync_image_is_admin_gated()
    test_sync_image_dedupes_active_job()
    print("\nALL WAVE 30 UNIT TESTS PASSED")
