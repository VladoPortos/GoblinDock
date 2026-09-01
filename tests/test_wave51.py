"""Wave 51 — admin Health page endpoint (/api/system/health).

Version + component liveness + inventory stats for the Settings Health tab.
Admin-only, read-only, contact-free (never probes Proxmox), and it must not
leak filesystem paths or key material.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave51.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave51-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

import app  # noqa: E402
from app import api, worker  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.deps import require_admin  # noqa: E402
from app.models import Connection, Deployment, Image, Job, Template, User  # noqa: E402
from fastapi import HTTPException  # noqa: E402

init_db()


def _expect_http(code, fn):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == code, (exc.status_code, exc.detail)
        return exc
    raise AssertionError(f"expected HTTPException {code}")


def _mk_user(email: str, role: str = "user") -> int:
    with session_scope() as s:
        user = User(email=email, name=email.split("@", 1)[0],
                    password_hash="unused", role=role)
        s.add(user)
        s.flush()
        return user.id


def test_health_is_admin_only():
    uid = _mk_user(f"wave51-user-{os.urandom(3).hex()}@example.com")
    with session_scope() as s:
        _expect_http(403, lambda: require_admin(user=s.get(User, uid)))
    # the dependency chain is the enforcement point — the endpoint itself
    # declares Depends(require_admin)
    import inspect
    params = inspect.signature(api.system_health).parameters
    assert "user" in params and "require_admin" in repr(params["user"].default)


def test_health_reports_version_components_and_inventory():
    suffix = os.urandom(3).hex()
    admin_id = _mk_user(f"wave51-admin-{suffix}@example.com", role="admin")
    with session_scope() as s:
        on = Connection(name=f"w51-on-{suffix}", host="pve", token_id="u@pve!t")
        off = Connection(name=f"w51-off-{suffix}", host="pve2", token_id="u@pve!t",
                         disabled=True)
        image = Image(kind="base", name=f"w51-img-{suffix}",
                      source_url="https://example.com/x.img", build_status="ready")
        s.add(on)
        s.add(off)
        s.add(image)
        s.flush()
        s.add(Template(name=f"w51-tpl-{suffix}", owner_id=admin_id, recipe_json="[]",
                       base_image_id=image.id, connection_id=on.id))
        s.add(Deployment(name=f"w51-vm-{suffix}", owner_id=admin_id,
                         connection_id=on.id, image_id=image.id, status="running"))
        s.add(Job(type="deploy", title="q", connection_id=on.id,
                  created_by=admin_id, status="queued"))
        s.flush()

    with session_scope() as s:
        out = api.system_health(user=s.get(User, admin_id), session=s)

    assert out["version"] == app.__version__
    assert isinstance(out["python"], str) and out["python"].count(".") == 2
    assert out["uptimeSeconds"] >= 0 and out["startedAt"]

    comp = out["components"]
    assert comp["api"] == {"ok": True}
    assert isinstance(comp["worker"]["jobWorkerAlive"], bool)
    assert isinstance(comp["scheduler"]["running"], bool)
    assert comp["database"]["ok"] is True
    assert comp["database"]["sizeBytes"] > 0
    assert str(comp["database"]["journalMode"]).lower() == "wal"
    assert isinstance(comp["backups"]["enabled"], bool)

    inv = out["inventory"]
    assert inv["vms"]["total"] >= 1 and inv["vms"]["byStatus"].get("running", 0) >= 1
    assert inv["connections"]["total"] >= 2
    assert inv["connections"]["disabled"] >= 1
    assert inv["jobs"]["queued"] >= 1
    assert inv["users"] >= 1 and inv["templates"] >= 1 and inv["baseImages"] >= 1
    assert out["disk"]["totalBytes"] > 0


def test_health_reflects_live_worker_threads():
    admin_id = _mk_user(f"wave51-worker-{os.urandom(3).hex()}@example.com", role="admin")
    worker.start_worker()
    try:
        with session_scope() as s:
            out = api.system_health(user=s.get(User, admin_id), session=s)
        assert out["components"]["worker"]["ok"] is True
        assert out["components"]["worker"]["jobWorkerAlive"] is True
        assert out["components"]["worker"]["waitingWorkerAlive"] is True
    finally:
        worker.stop_worker(join_timeout=10)
    deadline = time.time() + 10
    while time.time() < deadline and worker.worker_health()["jobWorkerAlive"]:
        time.sleep(0.2)
    with session_scope() as s:
        out = api.system_health(user=s.get(User, admin_id), session=s)
    assert out["components"]["worker"]["ok"] is False


def test_health_never_leaks_paths_or_key_material():
    admin_id = _mk_user(f"wave51-leak-{os.urandom(3).hex()}@example.com", role="admin")
    original_build = settings.build_info
    settings.build_info = "beta-build@0123456789abcdef"
    try:
        with session_scope() as s:
            out = api.system_health(user=s.get(User, admin_id), session=s)
    finally:
        settings.build_info = original_build
    assert out["build"] == "beta-build@0123456789abcdef"
    serialized = json.dumps(out)
    assert settings.db_path not in serialized
    assert str(settings.data_dir) not in serialized
    assert str(settings.backup_dir) not in serialized
    assert settings.secret_key not in serialized


if __name__ == "__main__":
    test_health_is_admin_only()
    test_health_reports_version_components_and_inventory()
    test_health_reflects_live_worker_threads()
    test_health_never_leaks_paths_or_key_material()
    print("\nALL WAVE 51 UNIT TESTS PASSED")
