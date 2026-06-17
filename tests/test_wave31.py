"""Wave 31 — review Batch F: /state & SSE performance (all Low, hot-path).

F1: /state probed px.version() per connection on every call; under SSE-driven refetch
    that amplifies. Now cached with a short TTL (_CONN_STATUS_CACHE).
F2: vm_dict ran one active-job SELECT per deployment (N+1) on /state. The lookup is now
    batched once in state() and passed in.
F3: GET /images/cached did one uncached Proxmox listing per call. Now TTL-cached per
    connection (_CACHED_IMAGES_CACHE).
F4: SSE generators get a wall-clock lifetime cap (_SSE_MAX_LIFETIME) so a stale
    connection (where is_disconnected can fail behind BaseHTTPMiddleware) can't poll
    forever — EventSource just reconnects.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave31.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave31-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from sqlmodel import select                       # noqa: E402
from app.db import init_db, session_scope         # noqa: E402
from app import api                               # noqa: E402
from app import serialize as S                     # noqa: E402
from app.models import Connection, Deployment, Image, Job, User  # noqa: E402
from app.security import hash_password             # noqa: E402

init_db()


def _mk_user(email, role="user"):
    with session_scope() as s:
        u = User(email=email, name="U", password_hash=hash_password("StrongPass12!"), role=role)
        s.add(u); s.flush()
        return u.id


# --------------------------------------------------------------------------- #
# F1 — connection status cache                                                 #
# --------------------------------------------------------------------------- #
def test_conn_status_cached_within_ttl():
    class _Px:
        def __init__(self): self.calls = 0
        def version(self):
            self.calls += 1
            return {"version": "8.1"}
    api._CONN_STATUS_CACHE.clear()
    px = _Px()
    a = api._conn_status(px, 4242)
    b = api._conn_status(px, 4242)
    assert a == b and a["status"] == "online", a
    assert px.calls == 1, f"px.version() must be cached within the TTL, called {px.calls}x"
    api._CONN_STATUS_CACHE.clear()
    print("test_conn_status_cached_within_ttl OK")


# --------------------------------------------------------------------------- #
# F2 — batched active-job lookup in vm_dict                                    #
# --------------------------------------------------------------------------- #
def test_vm_dict_uses_prebuilt_active_jobs_map():
    uid = _mk_user("w31-f2@x.io")
    with session_scope() as s:
        d1 = Deployment(name="d1", owner_id=uid, status="running")
        d2 = Deployment(name="d2", owner_id=uid, status="running")
        s.add(d1); s.add(d2); s.flush()
        j = Job(type="deploy", status="running", deployment_id=d1.id)
        s.add(j); s.flush()
        d1id, d2id, jid = d1.id, d2.id, j.id
    users = {uid: type("U", (), {"name": "U", "id": uid})()}
    with session_scope() as s:
        d1 = s.get(Deployment, d1id); d2 = s.get(Deployment, d2id); j = s.get(Job, jid)
        me = s.get(User, uid)
        amap = {d1id: j}   # prebuilt map: d1 has an active job, d2 does not
        out1 = S.vm_dict(s, d1, me, {}, users, {}, amap)
        out2 = S.vm_dict(s, d2, me, {}, users, {}, amap)
        # fallback path (no map) must agree with the mapped path
        out1_db = S.vm_dict(s, d1, me, {}, users, {})
    assert out1["status"] == "working", "a dep with an active job (via map) shows working"
    assert out2["status"] != "working", "a dep with NO active job must not show working"
    assert out1_db["status"] == "working", "fallback (no map) must agree with the map path"
    print("test_vm_dict_uses_prebuilt_active_jobs_map OK")


# --------------------------------------------------------------------------- #
# F3 — cached_images TTL cache                                                 #
# --------------------------------------------------------------------------- #
def test_cached_images_cached_within_ttl():
    adm = _mk_user("w31-f3@x.io", role="admin")
    with session_scope() as s:
        s.add(Image(kind="base", name="b1", source_url="https://e/x.img", build_status="ready"))
        c = Connection(name="w31-conn", host="10.0.0.1", token_id="t@pve!x", node="pve")
        s.add(c); s.flush(); cid = c.id

    class _Px:
        calls = {"n": 0}
        def __init__(self, conn): pass
        def storage_volumes(self, node=None):
            _Px.calls["n"] += 1
            return ["local:iso/x.img"]
        def iso_volume_path(self, fn): return "local:iso/" + fn

    api._CACHED_IMAGES_CACHE.clear()
    orig = api.Proxmox
    api.Proxmox = _Px
    try:
        with session_scope() as s:
            r1 = api.cached_images(cid, user=s.get(User, adm), session=s)
        with session_scope() as s:
            r2 = api.cached_images(cid, user=s.get(User, adm), session=s)
    finally:
        api.Proxmox = orig
    assert r1 == r2 and r1["online"] is True, r1
    assert _Px.calls["n"] == 1, f"Proxmox listing must be cached within TTL, called {_Px.calls['n']}x"
    api._CACHED_IMAGES_CACHE.clear()
    print("test_cached_images_cached_within_ttl OK")


# --------------------------------------------------------------------------- #
# F4 — SSE lifetime cap exists and is bounded                                  #
# --------------------------------------------------------------------------- #
def test_sse_lifetime_cap_bounded():
    cap = api._SSE_MAX_LIFETIME
    assert isinstance(cap, (int, float)) and 60 <= cap <= 3600, \
        f"SSE lifetime cap must be a bounded backstop, got {cap!r}"
    print("test_sse_lifetime_cap_bounded OK")


if __name__ == "__main__":
    test_conn_status_cached_within_ttl()
    test_vm_dict_uses_prebuilt_active_jobs_map()
    test_cached_images_cached_within_ttl()
    test_sse_lifetime_cap_bounded()
    print("\nALL WAVE 31 UNIT TESTS PASSED")
