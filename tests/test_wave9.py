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
    assert {"base_image_id", "connection_id", "network_id"} <= tcols, tcols
    assert "golden_image_id" not in tcols, tcols
    dcols = _cols("deployments")
    assert "template_id" in dcols and "recipe_id" not in dcols, dcols
    assert "deploy_inputs_json" in dcols, dcols
    # data survived the rename
    from app.models import Deployment, Template
    from sqlmodel import select
    with session_scope() as s:
        t = s.exec(select(Template).where(Template.name == "Legacy Recipe")).first()
        assert t is not None and t.description == "pre-rework row"
        assert t.base_image_id is None
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


def _mk_conn_base_net():
    """connection + base image + network on the connection; returns (conn_id, img_id, net_id)."""
    from app.models import Connection, Image, Network
    with session_scope() as s:
        c = Connection(name="px-test-" + os.urandom(3).hex(), host="127.0.0.1",
                       token_id="t@pve!x", node="pve")
        s.add(c); s.flush()
        img = Image(kind="base", name="g-ubuntu", os_family="ubuntu",
                    source_url="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
                    build_status="ready")
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
    conn_id, img_id, net_id = _mk_conn_base_net()
    with session_scope() as s:
        user = s.get(User, uid)
        api.save_template(api.TemplateBody(
            name="web", description="d", recipe=[],
            baseImageId=img_id, connectionId=conn_id, networkId=net_id),
            user=user, session=s)
    with session_scope() as s:
        t = s.exec(select(Template).where(Template.name == "web")).first()
        assert t and t.base_image_id == img_id and t.connection_id == conn_id and t.network_id == net_id
        assert t.description == "d"
    # bad refs are rejected
    with session_scope() as s:
        user = s.get(User, uid)
        _expect_http(400, lambda: api.save_template(api.TemplateBody(
            name="bad", recipe=[], baseImageId=999999), user=user, session=s))
        _expect_http(400, lambda: api.save_template(api.TemplateBody(
            name="bad2", recipe=[], networkId=net_id), user=user, session=s))
    print("test_template_crud_with_refs OK")


def test_template_edit_refs():
    from app import api
    from app.models import Template, User
    from sqlmodel import select
    uid = _mk_user("tpl-edit@example.com")
    conn_id, img_id, net_id = _mk_conn_base_net()
    with session_scope() as s:
        user = s.get(User, uid)
        api.save_template(api.TemplateBody(name="edit-me", recipe=[]), user=user, session=s)
    with session_scope() as s:
        tid = s.exec(select(Template).where(Template.name == "edit-me")).first().id
    # set refs via edit
    with session_scope() as s:
        user = s.get(User, uid)
        api.edit_template_ep(tid, api.TemplateBody(
            name="edit-me", recipe=[], baseImageId=img_id, connectionId=conn_id,
            networkId=net_id, os_family="debian"), user=user, session=s)
    with session_scope() as s:
        t = s.get(Template, tid)
        assert t.base_image_id == img_id and t.connection_id == conn_id and t.network_id == net_id, \
            (t.base_image_id, t.connection_id, t.network_id)
        assert t.os_family == "debian", t.os_family
    # bad image id on edit → 400
    with session_scope() as s:
        user = s.get(User, uid)
        _expect_http(400, lambda: api.edit_template_ep(tid, api.TemplateBody(
            name="edit-me", recipe=[], baseImageId=999999), user=user, session=s))
    print("test_template_edit_refs OK")


def test_ask_map_and_merge():
    from app.recipes import ask_map, merge_deploy_inputs
    recipe = [
        {"id": "s-os", "name": "OS Setup", "blocks": [
            {"ref": "b-hostname", "name": "Set Hostname",
             "inputs": {"hostname": "default-host"}, "ask": ["hostname"]},
        ]},
        {"id": "s-inst", "name": "Install", "blocks": [
            {"ref": "b-apt", "name": "APT", "inputs": {"packages": ["curl"]}},
        ]},
    ]
    assert ask_map(recipe) == {"0.0": ["hostname"]}
    # valid override is applied
    merged = merge_deploy_inputs(recipe, {"0.0": {"hostname": "my-vm"}})
    assert merged[0]["blocks"][0]["inputs"]["hostname"] == "my-vm"
    # original recipe is never mutated
    assert recipe[0]["blocks"][0]["inputs"]["hostname"] == "default-host"
    # non-ask input name on a valid address is ignored
    merged = merge_deploy_inputs(recipe, {"0.0": {"evil": "x"}})
    assert "evil" not in merged[0]["blocks"][0]["inputs"]
    # address without ask flags is ignored
    merged = merge_deploy_inputs(recipe, {"1.0": {"packages": ["nc"]}})
    assert merged[1]["blocks"][0]["inputs"]["packages"] == ["curl"]
    # junk addresses / shapes don't crash
    assert merge_deploy_inputs(recipe, {"9.9": {"a": 1}, "x.y": {"b": 2}, "0.0": "notadict"}) == recipe
    assert merge_deploy_inputs(recipe, {}) == recipe
    assert merge_deploy_inputs(recipe, [1, 2, 3]) == recipe  # non-dict overrides ignored
    # malformed recipes don't crash the defense layer
    assert ask_map(["junk", {"blocks": ["notadict"]}]) == {}
    assert merge_deploy_inputs(["junk"], {"0.0": {"a": 1}}) == ["junk"]
    print("test_ask_map_and_merge OK")


def _mk_template(base_id, conn_id, net_id=None, owner=None, public=True, ask=True):
    from app.models import Template
    recipe = [{"id": "s-os", "name": "OS Setup", "blocks": [
        {"ref": "b-hostname", "name": "Set Hostname",
         "inputs": {"hostname": ""}, **({"ask": ["hostname"]} if ask else {})},
    ]}]
    with session_scope() as s:
        t = Template(name="t-" + os.urandom(3).hex(), recipe_json=json.dumps(recipe),
                     base_image_id=base_id, connection_id=conn_id, network_id=net_id,
                     owner_id=owner, public=public)
        s.add(t); s.flush()
        return t.id


def _mk_golden(conn_id):
    # TEMPORARY shim — removed in templates-only Task 2 when DeployBody drops goldenImageId
    from app.models import Image
    with session_scope() as s:
        img = Image(kind="golden", name="g-shim-" + os.urandom(2).hex(), os_family="ubuntu",
                    connection_id=conn_id, template_vmid=9001, build_status="ready")
        s.add(img); s.flush()
        return img.id


def test_deploy_with_inputs():
    from app import api
    from app.models import Deployment, User
    from app.seed import seed_blocks
    from sqlmodel import select
    seed_blocks()  # b-hostname schema must exist for type checks
    uid = _mk_user("deployer@example.com")
    conn_id, base_id, net_id = _mk_conn_base_net()
    golden_id = _mk_golden(conn_id)
    tid = _mk_template(base_id, conn_id, net_id)

    def _deploy(**kw):
        body = api.DeployBody(goldenImageId=golden_id, **kw)
        with session_scope() as s:
            return api.deploy(body, user=s.get(User, uid), session=s)

    # happy path: answer persists on the deployment row
    r = _deploy(templateId=tid, name="vm-a",
                deployInputs={"0.0": {"hostname": "my-host"}})
    assert r["ok"]
    with session_scope() as s:
        d = s.exec(select(Deployment).where(Deployment.name == "vm-a")).first()
        assert d.template_id == tid
        assert json.loads(d.deploy_inputs_json) == {"0.0": {"hostname": "my-host"}}

    # ask-flagged text input left unanswered → 400 (stored default is empty)
    _expect_http(400, lambda: _deploy(templateId=tid, name="vm-b", deployInputs={}))
    # non-ask input override → 400
    _expect_http(400, lambda: _deploy(templateId=tid, name="vm-c",
                 deployInputs={"0.0": {"hostname": "h", "evil": "x"}}))
    # unknown address → 400
    _expect_http(400, lambda: _deploy(templateId=tid, name="vm-d",
                 deployInputs={"5.0": {"hostname": "h"}}))
    # deployInputs without a template → 400
    _expect_http(400, lambda: _deploy(name="vm-e", deployInputs={"0.0": {"hostname": "h"}}))
    # wrong value type → 400
    _expect_http(400, lambda: _deploy(templateId=tid, name="vm-f",
                 deployInputs={"0.0": {"hostname": ["not", "a", "string"]}}))
    # someone else's PRIVATE template → 404 (no id enumeration)
    other = _mk_user("other@example.com")
    priv = _mk_template(base_id, conn_id, owner=other, public=False)
    _expect_http(404, lambda: _deploy(templateId=priv, name="vm-g",
                 deployInputs={"0.0": {"hostname": "h"}}))
    # whitespace-only supplied answer → 400 even though a stored value exists
    tid2 = _mk_template(base_id, conn_id)
    with session_scope() as s:
        from app.models import Template
        t = s.get(Template, tid2)
        rec = json.loads(t.recipe_json)
        rec[0]["blocks"][0]["inputs"]["hostname"] = "stored-host"
        t.recipe_json = json.dumps(rec)
        s.add(t)
    _expect_http(400, lambda: _deploy(templateId=tid2, name="vm-h",
                 deployInputs={"0.0": {"hostname": "   "}}))
    # unanswered with a non-empty stored value → OK, and nothing persisted as override
    r2 = _deploy(templateId=tid2, name="vm-i", deployInputs={})
    assert r2["ok"]
    with session_scope() as s:
        from app.models import Deployment
        from sqlmodel import select
        d2 = s.exec(select(Deployment).where(Deployment.name == "vm-i")).first()
        assert json.loads(d2.deploy_inputs_json) == {}, d2.deploy_inputs_json
    print("test_deploy_with_inputs OK")


if __name__ == "__main__":
    test_migration_renames_and_extends()
    test_migration_idempotent()
    test_template_crud_with_refs()
    test_template_edit_refs()
    test_ask_map_and_merge()
    test_deploy_with_inputs()
    print("\nALL WAVE 9 UNIT TESTS PASSED")
