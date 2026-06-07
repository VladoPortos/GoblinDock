"""Wave 10 — templates-only: base image + connection on templates (golden pointer dropped).

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave10.py
Run (Windows):        $env:GOBLINDOCK_DEV='1'; .venv\\Scripts\\python.exe tests\\test_wave10.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave10-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from app.db import engine, init_db, session_scope  # noqa: E402

init_db()


def _cols(table):
    with engine.begin() as conn:
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def _expect_http(code, fn):
    from fastapi import HTTPException
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == code, (e.status_code, e.detail)
        return e
    raise AssertionError(f"expected HTTPException {code}")


def _mk_user(email, role="user"):
    from app.models import User
    from app.security import hash_password
    with session_scope() as s:
        u = User(email=email, name="U", password_hash=hash_password("StrongPass12!"), role=role)
        s.add(u); s.flush()
        return u.id


def _mk_conn_base_net():
    """connection + base image + network on the connection; returns (conn_id, base_id, net_id)."""
    from app.models import Connection, Image, Network
    with session_scope() as s:
        c = Connection(name="px-t10-" + os.urandom(3).hex(), host="127.0.0.1",
                       token_id="t@pve!x", node="pve")
        s.add(c); s.flush()
        img = Image(kind="base", name="b-ubuntu-" + os.urandom(2).hex(), os_family="ubuntu",
                    source_url="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
                    build_status="ready")
        s.add(img); s.flush()
        net = Network(connection_id=c.id, name="lan", mode="dhcp")
        s.add(net); s.flush()
        return c.id, img.id, net.id


def test_schema_templates_only():
    tcols = _cols("templates")
    assert {"base_image_id", "connection_id"} <= tcols, tcols
    assert "golden_image_id" not in tcols, tcols
    print("test_schema_templates_only OK")


def test_template_refs_validation():
    from app import api
    from app.models import Template, User
    from sqlmodel import select
    uid = _mk_user("t10-crud@example.com")
    conn_id, base_id, net_id = _mk_conn_base_net()
    with session_scope() as s:
        user = s.get(User, uid)
        api.save_template(api.TemplateBody(
            name="web10", description="d", recipe=[],
            baseImageId=base_id, connectionId=conn_id, networkId=net_id),
            user=user, session=s)
    with session_scope() as s:
        t = s.exec(select(Template).where(Template.name == "web10")).first()
        assert t and t.base_image_id == base_id and t.connection_id == conn_id and t.network_id == net_id
    with session_scope() as s:
        user = s.get(User, uid)
        # baseImageId must reference a BASE image
        _expect_http(400, lambda: api.save_template(api.TemplateBody(
            name="bad", recipe=[], baseImageId=999999), user=user, session=s))
        # network requires a connection
        _expect_http(400, lambda: api.save_template(api.TemplateBody(
            name="bad2", recipe=[], networkId=net_id), user=user, session=s))
    # network on a different connection → 400
    conn2, _b2, net2 = _mk_conn_base_net()
    with session_scope() as s:
        user = s.get(User, uid)
        _expect_http(400, lambda: api.save_template(api.TemplateBody(
            name="bad3", recipe=[], baseImageId=base_id, connectionId=conn_id,
            networkId=net2), user=user, session=s))
    print("test_template_refs_validation OK")


if __name__ == "__main__":
    test_schema_templates_only()
    test_template_refs_validation()
    print("\nALL WAVE 10 UNIT TESTS PASSED")
