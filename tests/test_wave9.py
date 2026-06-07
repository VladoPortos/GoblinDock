"""Wave 9 — recipes→templates rework: migration, template CRUD, ask-on-deploy.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave9.py
Run (Windows):        $env:GOBLINDOCK_DEV='1'; .venv\\Scripts\\python.exe tests\\test_wave9.py
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave9-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

# ---- build a pre-rework (recipes-era) schema BEFORE app.db imports/binds ----
_con = sqlite3.connect(_DB)
_con.executescript("""
CREATE TABLE recipes (
  id INTEGER PRIMARY KEY,
  name VARCHAR NOT NULL,
  description VARCHAR NOT NULL DEFAULT '',
  os_family VARCHAR NOT NULL DEFAULT 'ubuntu',
  recipe_json VARCHAR NOT NULL DEFAULT '[]',
  default_cpu INTEGER NOT NULL DEFAULT 1,
  default_ram INTEGER NOT NULL DEFAULT 2,
  default_disk INTEGER NOT NULL DEFAULT 20,
  owner_id INTEGER,
  public BOOLEAN NOT NULL DEFAULT 1,
  created_at TIMESTAMP
);
INSERT INTO recipes (name, description, recipe_json)
  VALUES ('Legacy Recipe', 'pre-rework row', '[]');
CREATE TABLE deployments (
  id INTEGER PRIMARY KEY,
  name VARCHAR NOT NULL,
  owner_id INTEGER, connection_id INTEGER, image_id INTEGER, recipe_id INTEGER,
  vmid INTEGER, node VARCHAR NOT NULL DEFAULT '',
  network_id INTEGER, cpu INTEGER NOT NULL DEFAULT 1, ram INTEGER NOT NULL DEFAULT 2,
  disk INTEGER NOT NULL DEFAULT 20, ip VARCHAR NOT NULL DEFAULT '',
  mac VARCHAR NOT NULL DEFAULT '', status VARCHAR NOT NULL DEFAULT 'working',
  tags VARCHAR NOT NULL DEFAULT '', notes VARCHAR NOT NULL DEFAULT '',
  error VARCHAR NOT NULL DEFAULT '', created_at TIMESTAMP
);
INSERT INTO deployments (name, recipe_id) VALUES ('legacy-vm', 1);
""")
_con.commit()
_con.close()

from app.db import engine, init_db, session_scope  # noqa: E402

init_db()


def _cols(table):
    with engine.begin() as conn:
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def _tables():
    with engine.begin() as conn:
        return {r[0] for r in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'")}


def test_migration_renames_and_extends():
    tables = _tables()
    assert "templates" in tables, tables
    assert "recipes" not in tables, tables
    tcols = _cols("templates")
    assert {"golden_image_id", "network_id"} <= tcols, tcols
    dcols = _cols("deployments")
    assert "template_id" in dcols and "recipe_id" not in dcols, dcols
    assert "deploy_inputs_json" in dcols, dcols
    # data survived the rename
    from app.models import Deployment, Template
    from sqlmodel import select
    with session_scope() as s:
        t = s.exec(select(Template).where(Template.name == "Legacy Recipe")).first()
        assert t is not None and t.description == "pre-rework row"
        assert t.golden_image_id is None
        d = s.exec(select(Deployment).where(Deployment.name == "legacy-vm")).first()
        assert d is not None and d.template_id == 1
        assert d.deploy_inputs_json == "{}"
    print("test_migration_renames_and_extends OK")


def test_migration_idempotent():
    init_db()  # second run must be a clean no-op
    from app.models import Template
    from sqlmodel import select
    with session_scope() as s:
        rows = s.exec(select(Template).where(Template.name == "Legacy Recipe")).all()
        assert len(rows) == 1, len(rows)
    print("test_migration_idempotent OK")


def _mk_user(email, role="user"):
    from app.models import User
    from app.security import hash_password
    with session_scope() as s:
        u = User(email=email, name="U", password_hash=hash_password("StrongPass12!"), role=role)
        s.add(u)
        s.flush()
        return u.id


def _mk_conn_golden_net():
    """connection + ready golden image + network on it; returns (conn_id, img_id, net_id)."""
    from app.models import Connection, Image, Network
    with session_scope() as s:
        c = Connection(name="px-test-" + os.urandom(3).hex(), host="127.0.0.1",
                       token_id="t@pve!x", node="pve")
        s.add(c); s.flush()
        img = Image(kind="golden", name="g-ubuntu", os_family="ubuntu",
                    connection_id=c.id, template_vmid=9001, build_status="ready")
        s.add(img); s.flush()
        net = Network(connection_id=c.id, name="lan", mode="dhcp")
        s.add(net); s.flush()
        return c.id, img.id, net.id


def _expect_http(code, fn):
    from fastapi import HTTPException
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == code, (e.status_code, e.detail)
        return e
    raise AssertionError(f"expected HTTPException {code}")


def test_template_crud_with_refs():
    from app import api
    from app.models import Template, User
    from sqlmodel import select
    uid = _mk_user("tpl-crud@example.com")
    conn_id, img_id, net_id = _mk_conn_golden_net()
    with session_scope() as s:
        user = s.get(User, uid)
        api.save_template(api.TemplateBody(
            name="web", description="d", recipe=[],
            goldenImageId=img_id, networkId=net_id), user=user, session=s)
    with session_scope() as s:
        t = s.exec(select(Template).where(Template.name == "web")).first()
        assert t and t.golden_image_id == img_id and t.network_id == net_id
        assert t.description == "d"
    # bad refs are rejected
    with session_scope() as s:
        user = s.get(User, uid)
        _expect_http(400, lambda: api.save_template(api.TemplateBody(
            name="bad", recipe=[], goldenImageId=999999), user=user, session=s))
        _expect_http(400, lambda: api.save_template(api.TemplateBody(
            name="bad2", recipe=[], networkId=net_id), user=user, session=s))
    # network on a DIFFERENT connection than the image → 400
    from app.models import Network
    with session_scope() as s:
        other = Network(connection_id=999, name="other", mode="dhcp")
        s.add(other); s.flush(); other_id = other.id
    with session_scope() as s:
        user = s.get(User, uid)
        _expect_http(400, lambda: api.save_template(api.TemplateBody(
            name="bad3", recipe=[], goldenImageId=img_id, networkId=other_id),
            user=user, session=s))
    print("test_template_crud_with_refs OK")


if __name__ == "__main__":
    test_migration_renames_and_extends()
    test_migration_idempotent()
    test_template_crud_with_refs()
    print("\nALL WAVE 9 UNIT TESTS PASSED")
