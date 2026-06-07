"""Wave 8 — per-user widget API key + Homepage summary endpoint.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave8.py
Run (Windows):        $env:GOBLINDOCK_DEV='1'; .venv\\Scripts\\python.exe tests\\test_wave8.py
"""
import hashlib
import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ephemeral dev secret key + isolated SQLite DB in a cross-platform temp dir
# (the other waves hardcode /tmp; tempfile keeps this runnable on Windows too).
os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave8-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from fastapi import HTTPException  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.db import _migrate, engine, init_db, session_scope  # noqa: E402
from app.models import User, utcnow  # noqa: E402
from app.security import (  # noqa: E402
    WIDGET_KEY_PREFIX,
    hash_password,
    hash_widget_key,
    new_widget_key,
)

init_db()

_WIDGET_COLS = {"widget_key_hash", "widget_key_prefix",
                "widget_key_created_at", "widget_key_last_used"}


def _mk_user(email, name="U", role="user", disabled=False, **kw):
    """Create a user, return its id."""
    with session_scope() as s:
        u = User(email=email, name=name,
                 password_hash=hash_password("StrongPass12!"),
                 role=role, disabled=disabled, **kw)
        s.add(u)
        s.flush()
        return u.id


def _expect_401(fn):
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == 401, e.status_code
        return
    assert False, "expected HTTPException 401"


def _auth(tok):
    """Invoke the widget_key_user dependency with an X-API-Key header."""
    from app.deps import widget_key_user
    headers = {} if tok is None else {"x-api-key": tok}
    with session_scope() as s:
        return widget_key_user(SimpleNamespace(headers=headers), s)


def test_widget_key_helpers():
    t1 = new_widget_key()
    t2 = new_widget_key()
    # tagged, high-entropy, unique per call
    assert WIDGET_KEY_PREFIX == "gdwk_", WIDGET_KEY_PREFIX
    assert t1.startswith(WIDGET_KEY_PREFIX), t1
    assert t1 != t2
    assert len(t1) >= 40, len(t1)
    # deterministic, 64-char hex, unique per token
    assert hash_widget_key(t1) == hash_widget_key(t1)
    assert len(hash_widget_key(t1)) == 64
    assert all(c in "0123456789abcdef" for c in hash_widget_key(t1))
    assert hash_widget_key(t1) != hash_widget_key(t2)
    # KEYED (HMAC), not a bare sha256 — see CodeQL py/weak-sensitive-data-hashing
    assert hash_widget_key(t1) != hashlib.sha256(t1.encode("utf-8")).hexdigest()
    # empty input hashes stably and never collides with a real token
    assert hash_widget_key("") == hash_widget_key("")
    assert hash_widget_key("") != hash_widget_key(t1)
    print("test_widget_key_helpers OK")


def test_user_widget_key_columns():
    # the four key columns exist on the users table
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
    assert _WIDGET_COLS <= cols, cols
    # _migrate is idempotent — re-running must not raise and columns persist
    _migrate()
    with engine.begin() as conn:
        cols2 = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
    assert _WIDGET_COLS <= cols2, cols2
    # values round-trip through the DB; timestamps default to NULL
    uid = _mk_user("cols@x.io", widget_key_hash="abc123", widget_key_prefix="gdwk_ab")
    with session_scope() as s:
        u = s.get(User, uid)
        assert u.widget_key_hash == "abc123"
        assert u.widget_key_prefix == "gdwk_ab"
        assert u.widget_key_created_at is None
        assert u.widget_key_last_used is None
    print("test_user_widget_key_columns OK")


def test_widget_key_user_auth():
    from app.deps import widget_key_user

    token = new_widget_key()
    uid = _mk_user("auth@x.io", widget_key_hash=hash_widget_key(token),
                   widget_key_prefix=token[:9])

    # valid key -> the owning user
    with session_scope() as s:
        u = widget_key_user(SimpleNamespace(headers={"x-api-key": token}), s)
        assert u.id == uid
    # ...and last_used is stamped (was NULL)
    with session_scope() as s:
        first_used = s.get(User, uid).widget_key_last_used
        assert first_used is not None

    # a second poll within the throttle window does NOT rewrite last_used
    with session_scope() as s:
        widget_key_user(SimpleNamespace(headers={"x-api-key": token}), s)
    with session_scope() as s:
        assert s.get(User, uid).widget_key_last_used == first_used

    # every failure mode returns a generic 401 (no oracle)
    _expect_401(lambda: _auth(None))             # missing header
    _expect_401(lambda: _auth(""))               # empty
    _expect_401(lambda: _auth("not-a-key"))      # malformed (no gdwk_ tag)
    _expect_401(lambda: _auth(new_widget_key()))  # well-formed but unknown

    # disabled user -> 401 even with the correct key
    dtok = new_widget_key()
    _mk_user("disabled@x.io", disabled=True,
             widget_key_hash=hash_widget_key(dtok), widget_key_prefix=dtok[:9])
    _expect_401(lambda: _auth(dtok))

    # revoked key (hash cleared) -> the old token no longer authenticates
    with session_scope() as s:
        u = s.get(User, uid)
        u.widget_key_hash = None
        s.add(u)
    _expect_401(lambda: _auth(token))
    print("test_widget_key_user_auth OK")


def test_widget_summary_counts():
    from app import api
    from app.models import Connection, Deployment, Image, Job, Template

    aid = _mk_user("summary-a@x.io")
    bid = _mk_user("summary-b@x.io")
    with session_scope() as s:
        for i, st in enumerate(("running", "running", "stopped", "error", "working")):
            s.add(Deployment(name=f"a-{i}-{st}", owner_id=aid, status=st))
        s.add(Deployment(name="b-running", owner_id=bid, status="running"))
        # jobs: queued/running are "active"; succeeded is not
        s.add(Job(title="qa", created_by=aid, status="queued"))
        s.add(Job(title="ra", created_by=aid, status="running"))
        s.add(Job(title="da", created_by=aid, status="succeeded"))
        s.add(Job(title="qb", created_by=bid, status="running"))
        # one deployable template (base_image_id + connection_id both set)
        base = Image(kind="base", name="base1", source_url="https://example.com/img.img",
                     build_status="ready")
        s.add(base)
        s.flush()
        conn = Connection(name="c1", host="h", port=8006, token_id="t", token_secret_enc=b"x")
        s.add(conn)
        s.flush()
        s.add(Template(name="tpl1", base_image_id=base.id, connection_id=conn.id,
                       owner_id=aid, public=True))
        # a template missing connection_id must NOT count
        s.add(Template(name="tpl-no-conn", base_image_id=base.id, owner_id=aid, public=True))

    # non-admin A sees ONLY their own VMs/jobs
    with session_scope() as s:
        out = api.widget_summary(user=s.get(User, aid), session=s)
    assert out["vms_total"] == 5, out
    assert out["vms_running"] == 2, out
    assert out["vms_stopped"] == 1, out
    assert out["vms_error"] == 1, out
    assert out["vms_working"] == 1, out
    assert out["jobs_active"] == 2, out
    assert out["templates"] == 1, out

    # admin sees ALL VMs/jobs
    admin_id = _mk_user("summary-admin@x.io", role="admin")
    with session_scope() as s:
        out = api.widget_summary(user=s.get(User, admin_id), session=s)
    assert out["vms_total"] == 6, out
    assert out["vms_running"] == 3, out
    assert out["jobs_active"] == 3, out
    assert out["templates"] == 1, out
    print("test_widget_summary_counts OK")


def test_widget_summary_probe_free():
    """The summary must derive everything from SQLite — never hit Proxmox."""
    import app.api as api

    uid = _mk_user("summary-probe@x.io")
    sentinel = {"called": False}

    def _boom(*a, **k):
        sentinel["called"] = True
        raise AssertionError("widget_summary must not build the Proxmox cache")

    orig = api._px_cache
    api._px_cache = _boom
    try:
        with session_scope() as s:
            out = api.widget_summary(user=s.get(User, uid), session=s)
        assert out["vms_total"] == 0, out
        assert sentinel["called"] is False
    finally:
        api._px_cache = orig
    print("test_widget_summary_probe_free OK")


def test_me_dict_widget_key_status():
    from app import serialize as S

    uid = _mk_user("me@x.io")
    with session_scope() as s:
        me = S.me_dict(s.get(User, uid))
    # no key yet
    assert me["widgetKey"]["present"] is False, me["widgetKey"]
    assert me["widgetKey"]["prefix"] == ""
    # the secret material is NEVER serialized
    assert "widget_key_hash" not in json.dumps(me)

    # with a key set, status reflects present + prefix only
    tok = new_widget_key()
    with session_scope() as s:
        u = s.get(User, uid)
        u.widget_key_hash = hash_widget_key(tok)
        u.widget_key_prefix = tok[:9]
        u.widget_key_created_at = utcnow()
        s.add(u)
    with session_scope() as s:
        me = S.me_dict(s.get(User, uid))
    assert me["widgetKey"]["present"] is True
    assert me["widgetKey"]["prefix"] == tok[:9]
    blob = json.dumps(me)
    assert tok not in blob                      # plaintext token never serialized
    assert hash_widget_key(tok) not in blob     # hash never serialized
    print("test_me_dict_widget_key_status OK")


def test_widget_key_generate_revoke():
    from app import api
    from app.deps import widget_key_user
    from app.models import Audit

    uid = _mk_user("genrev@x.io")

    # generate returns the token ONCE + a prefix; only the hash is persisted
    with session_scope() as s:
        out = api.gen_widget_key(user=s.get(User, uid), session=s)
    token = out["key"]
    assert token.startswith("gdwk_")
    assert out["prefix"] == token[:9]
    with session_scope() as s:
        u = s.get(User, uid)
        assert u.widget_key_hash == hash_widget_key(token)
        assert u.widget_key_hash != token          # stored hashed, not plaintext
        assert u.widget_key_prefix == token[:9]
        assert u.widget_key_created_at is not None

    # the issued key authenticates the summary endpoint
    with session_scope() as s:
        assert widget_key_user(SimpleNamespace(headers={"x-api-key": token}), s).id == uid

    # regenerate invalidates the previous token
    with session_scope() as s:
        token2 = api.gen_widget_key(user=s.get(User, uid), session=s)["key"]
    assert token2 != token
    _expect_401(lambda: _auth(token))           # old token is dead
    with session_scope() as s:
        assert widget_key_user(SimpleNamespace(headers={"x-api-key": token2}), s).id == uid

    # revoke clears the key; the token stops working and status flips
    with session_scope() as s:
        api.revoke_widget_key(user=s.get(User, uid), session=s)
    with session_scope() as s:
        u = s.get(User, uid)
        assert u.widget_key_hash is None
        assert u.widget_key_prefix == ""
    _expect_401(lambda: _auth(token2))

    # generate + revoke are recorded in the audit log
    with session_scope() as s:
        actions = {a.action for a in
                   s.exec(select(Audit).where(Audit.user_id == uid)).all()}
    assert "profile.widget_key.generate" in actions, actions
    assert "profile.widget_key.revoke" in actions, actions
    print("test_widget_key_generate_revoke OK")


if __name__ == "__main__":
    test_widget_key_helpers()
    test_user_widget_key_columns()
    test_widget_key_user_auth()
    test_widget_summary_counts()
    test_widget_summary_probe_free()
    test_me_dict_widget_key_status()
    test_widget_key_generate_revoke()
    print("\nALL WAVE 8 UNIT TESTS PASSED")
