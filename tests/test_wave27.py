"""Wave 27 — review Batch B2: secret/credential hygiene.

- The app-managed fleet SSH key (GD_MANAGED_PRIVKEY/PUBKEY — passwordless-sudo on
  every VM) must NOT be revealable/deletable like an ordinary Secret, nor listed in
  /state; and users must not be able to create secrets in the reserved namespace.
- decrypt() gains a strict mode so reveal endpoints can distinguish a real decrypt
  failure (key mismatch / corruption) from a legitimately empty value, instead of
  returning ""+200; audit only after a successful decrypt.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave27.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave27-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from fastapi import HTTPException, Response       # noqa: E402
from starlette.requests import Request            # noqa: E402
from sqlmodel import select                       # noqa: E402
from app.db import init_db, session_scope         # noqa: E402
from app import api                               # noqa: E402
from app.models import Secret, User               # noqa: E402
from app.security import decrypt, encrypt, hash_password  # noqa: E402

init_db()


def _mk_user(email, role="user"):
    with session_scope() as s:
        u = User(email=email, name="U", password_hash=hash_password("StrongPass12!"), role=role)
        s.add(u); s.flush()
        return u.id


def _mk_secret(name, value, scope="global", owner_id=None, raw_enc=None):
    with session_scope() as s:
        sec = Secret(name=name, value_enc=(raw_enc if raw_enc is not None else encrypt(value)),
                     scope=scope, owner_id=owner_id, created_by=owner_id)
        s.add(sec); s.flush()
        return sec.id


def _expect_http(code, fn):
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == code, (e.status_code, e.detail)
        return e
    raise AssertionError(f"expected HTTPException {code}")


# --------------------------------------------------------------------------- #
# B2-1 — fleet-master SSH key is system-internal                               #
# --------------------------------------------------------------------------- #
def test_reveal_managed_key_forbidden():
    adm = _mk_user("w27-a1@x.io", role="admin")
    sid = _mk_secret("GD_MANAGED_PRIVKEY", "-----BEGIN KEY-----")
    with session_scope() as s:
        _expect_http(403, lambda: api.reveal_secret(sid, Response(), user=s.get(User, adm), session=s))
    print("test_reveal_managed_key_forbidden OK")


def test_delete_managed_key_forbidden():
    adm = _mk_user("w27-a2@x.io", role="admin")
    sid = _mk_secret("GD_MANAGED_PUBKEY", "ssh-ed25519 AAAA")
    with session_scope() as s:
        _expect_http(403, lambda: api.del_secret(sid, user=s.get(User, adm), session=s))
    with session_scope() as s:
        assert s.get(Secret, sid) is not None, "managed key must survive the delete attempt"
    print("test_delete_managed_key_forbidden OK")


def test_state_omits_managed_secrets():
    adm = _mk_user("w27-a3@x.io", role="admin")
    _mk_secret("GD_MANAGED_PRIVKEY_X", "x")
    _mk_secret("MY_TOKEN_W27", "v", scope="user", owner_id=adm)
    req = Request({"type": "http", "headers": [], "session": {}})
    with session_scope() as s:
        st = api.state(request=req, user=s.get(User, adm), session=s)
    names = [x["name"] for x in st["SECRETS"]]
    assert "MY_TOKEN_W27" in names, "ordinary secrets still listed"
    assert not any(n.startswith("GD_MANAGED_") for n in names), \
        f"managed secrets must be hidden from /state, got {names}"
    print("test_state_omits_managed_secrets OK")


def test_add_secret_rejects_reserved_name():
    adm = _mk_user("w27-a4@x.io", role="admin")
    with session_scope() as s:
        _expect_http(400, lambda: api.add_secret(
            api.SecretBody(name="GD_MANAGED_HACK", value="x", scope="global"),
            user=s.get(User, adm), session=s))
    print("test_add_secret_rejects_reserved_name OK")


# --------------------------------------------------------------------------- #
# B2-5 — decrypt strict mode                                                   #
# --------------------------------------------------------------------------- #
def test_decrypt_strict_mode():
    assert decrypt("not-a-valid-token") == "", "non-strict fails closed to ''"
    assert decrypt("", strict=True) == "", "empty token is legitimately empty even in strict"
    raised = False
    try:
        decrypt("not-a-valid-token", strict=True)
    except ValueError:
        raised = True
    assert raised, "strict decrypt of a corrupt token must raise"
    # round-trip still works
    assert decrypt(encrypt("hello"), strict=True) == "hello"
    print("test_decrypt_strict_mode OK")


# --------------------------------------------------------------------------- #
# B2-3/4 — reveal distinguishes decrypt failure from empty; no ""+200          #
# --------------------------------------------------------------------------- #
def test_reveal_secret_500_on_undecryptable():
    u = _mk_user("w27-u1@x.io")
    sid = _mk_secret("BROKEN_W27", "", scope="user", owner_id=u, raw_enc="garbage-not-fernet")
    with session_scope() as s:
        _expect_http(500, lambda: api.reveal_secret(sid, Response(), user=s.get(User, u), session=s))
    print("test_reveal_secret_500_on_undecryptable OK")


def test_reveal_secret_returns_value():
    u = _mk_user("w27-u2@x.io")
    sid = _mk_secret("GOOD_W27", "mySecretVal", scope="user", owner_id=u)
    with session_scope() as s:
        out = api.reveal_secret(sid, Response(), user=s.get(User, u), session=s)
    assert out["val"] == "mySecretVal", f"reveal must return the real value, got {out.get('val')!r}"
    print("test_reveal_secret_returns_value OK")


if __name__ == "__main__":
    test_reveal_managed_key_forbidden()
    test_delete_managed_key_forbidden()
    test_state_omits_managed_secrets()
    test_add_secret_rejects_reserved_name()
    test_decrypt_strict_mode()
    test_reveal_secret_500_on_undecryptable()
    test_reveal_secret_returns_value()
    print("\nALL WAVE 27 UNIT TESTS PASSED")
