"""Wave 24 — Unified User block + auto-generated VM root password.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave24.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GOBLINDOCK_DEV", "1")
# Point the DB at a throwaway file BEFORE importing app.config/app.db (config reads env at import).
os.environ["GOBLINDOCK_DB"] = tempfile.mktemp(suffix=".sqlite3")


def test_deployment_has_password_columns():
    from app.db import init_db, engine
    init_db()
    with engine.begin() as c:
        cols = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(deployments)")}
    assert "root_password_enc" in cols, cols
    assert "cred_user" in cols, cols
    print("test_deployment_has_password_columns OK")


def test_password_helpers():
    import crypt
    from app.security import gen_vm_password, crypt_sha512, encrypt, decrypt
    p = gen_vm_password()
    assert len(p) == 20, len(p)
    assert not (set(p) & set("O0lI1")), "ambiguous chars must be excluded"
    assert gen_vm_password() != gen_vm_password(), "must be random"
    h = crypt_sha512("hunter2hunter2")
    assert h.startswith("$6$"), h
    assert crypt.crypt("hunter2hunter2", h) == h, "hash must verify"
    assert decrypt(encrypt(p)) == p, "encrypt/decrypt round-trip"
    print("test_password_helpers OK")


def test_auto_root_password_setting():
    from app import appsettings
    # default ON when unset
    assert appsettings.auto_root_password_enabled() is True
    appsettings.set_setting(appsettings.AUTO_ROOT_PASSWORD, "0")
    assert appsettings.auto_root_password_enabled() is False
    appsettings.set_setting(appsettings.AUTO_ROOT_PASSWORD, "1")
    assert appsettings.auto_root_password_enabled() is True
    print("test_auto_root_password_setting OK")


if __name__ == "__main__":
    test_deployment_has_password_columns()
    test_password_helpers()
    test_auto_root_password_setting()
    print("\nALL WAVE 24 UNIT TESTS PASSED")
