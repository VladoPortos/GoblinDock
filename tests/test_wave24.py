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


if __name__ == "__main__":
    test_deployment_has_password_columns()
    print("\nALL WAVE 24 UNIT TESTS PASSED")
