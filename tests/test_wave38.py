"""Wave 38 — first-run /auth/setup admin-takeover race fix (own DB: needs empty users).

Run (Linux/WSL/CI):
  GOBLINDOCK_DEV=1 .venv/bin/python -m pytest tests/test_wave38.py
"""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave38-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault(
    "GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test")
)

from fastapi import HTTPException  # noqa: E402
from sqlmodel import select  # noqa: E402

from app import api  # noqa: E402
from app import seed  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import User  # noqa: E402

init_db()


def _wipe_users():
    with session_scope() as s:
        for u in s.exec(select(User)).all():
            s.delete(u)


def _req():
    return SimpleNamespace(session={}, client=SimpleNamespace(host="203.0.113.9"), headers={})


def _expect_http(code: int, fn):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == code, (exc.status_code, exc.detail)
        return exc
    raise AssertionError(f"expected HTTPException {code}")


# These run in definition order against one DB that starts with zero users.
def test_prod_setup_requires_the_log_token():
    settings.dev_mode = False
    api._setup_token = None
    body_no_token = api.SetupBody(email="admin@corp.example", name="Admin",
                                  password="StrongPass12!", token="")
    with session_scope() as s:
        exc = _expect_http(403, lambda: api.auth_setup(body_no_token, _req(), session=s))
    assert "setup token" in exc.detail.lower(), exc.detail
    # The failed attempt minted + logged a token; the operator (who can read logs) uses it.
    tok = api._setup_token
    assert tok
    with session_scope() as s:
        res = api.auth_setup(
            api.SetupBody(email="admin@corp.example", name="Admin",
                          password="StrongPass12!", token=tok),
            _req(), session=s)
    assert res["ok"] is True
    assert api._setup_token is None, "token must be consumed on success"
    print("test_prod_setup_requires_the_log_token OK")


def test_setup_closed_once_admin_exists_even_with_token():
    settings.dev_mode = False
    with session_scope() as s:
        _expect_http(400, lambda: api.auth_setup(
            api.SetupBody(email="second@corp.example", name="Two",
                          password="StrongPass12!", token="anything"),
            _req(), session=s))
    print("test_setup_closed_once_admin_exists_even_with_token OK")


def test_dev_setup_needs_no_token():
    # Wipe the admin (fresh-file, no deployments/templates → FK-safe) and confirm dev
    # mode still allows zero-config web setup without a token.
    _wipe_users()
    settings.dev_mode = True
    api._setup_token = None
    with session_scope() as s:
        res = api.auth_setup(
            api.SetupBody(email="dev@local", name="Dev", password="StrongPass12!", token=""),
            _req(), session=s)
    assert res["ok"] is True
    print("test_dev_setup_needs_no_token OK")


def test_env_admin_seed_enforces_password_policy():
    """A weak GOBLINDOCK_ADMIN_PASSWORD must NOT create an env-seeded admin; a strong one
    still does."""
    orig = (settings.admin_email, settings.admin_password, settings.admin_name)
    try:
        _wipe_users()
        settings.admin_email = "boot@corp.example"
        settings.admin_name = "Boot"
        settings.admin_password = "weak"          # < policy
        seed.maybe_seed_admin()
        with session_scope() as s:
            assert s.exec(select(User)).first() is None, "weak env admin must be skipped"
        settings.admin_password = "StrongPass12!"  # meets policy
        seed.maybe_seed_admin()
        with session_scope() as s:
            admin = s.exec(select(User)).first()
            assert admin is not None and admin.role == "admin", "strong env admin must seed"
    finally:
        settings.admin_email, settings.admin_password, settings.admin_name = orig
    print("test_env_admin_seed_enforces_password_policy OK")


if __name__ == "__main__":
    test_prod_setup_requires_the_log_token()
    test_setup_closed_once_admin_exists_even_with_token()
    test_dev_setup_needs_no_token()
    test_env_admin_seed_enforces_password_policy()
    print("wave38 OK")
