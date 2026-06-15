"""Wave 18 — connection-authoritative per-VM resource ceilings.

The connection's "max per VM" limits (Settings → Proxmox connection) are the single
authoritative ceiling for CPU, RAM and disk. A value of 0 means UNLIMITED for that
dimension — identical to how disk already behaved. Two regressions are covered:

  1. Template save/edit must store the authored vCPU/RAM/disk VERBATIM (it used to
     silently clamp cpu→1 / ram→2 to the global default cap, while disk survived).
  2. Deploy clamps to the CONNECTION ceiling; 0 there = unlimited (not "fall back to
     the global 1-core/2-GB default").

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave18.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave18-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from app.db import session_scope  # noqa: E402
from app.db import init_db  # noqa: E402

init_db()


def _mk_user(email, role="user"):
    from app.models import User
    from app.security import hash_password
    with session_scope() as s:
        u = User(email=email, name="U", password_hash=hash_password("StrongPass12!"), role=role)
        s.add(u); s.flush()
        return u.id


def _mk_conn_base_net(max_cores=0, max_ram_mb=0, max_disk_gb=0):
    """Connection (with given per-VM ceilings) + base image + network. Returns ids."""
    from app.models import Connection, Image, Network
    with session_scope() as s:
        c = Connection(name="px-w18-" + os.urandom(3).hex(), host="127.0.0.1",
                       token_id="t@pve!x", node="pve",
                       max_cores=max_cores, max_ram_mb=max_ram_mb, max_disk_gb=max_disk_gb)
        s.add(c); s.flush()
        img = Image(kind="base", name="b-ubuntu-" + os.urandom(2).hex(), os_family="ubuntu",
                    source_url="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
                    build_status="ready")
        s.add(img); s.flush()
        net = Network(connection_id=c.id, name="lan", mode="dhcp")
        s.add(net); s.flush()
        return c.id, img.id, net.id


def _mk_template(conn_id, base_id, net_id, cpu=1, ram=2, disk=20):
    from app.models import Template
    with session_scope() as s:
        t = Template(name="t18-" + os.urandom(3).hex(), recipe_json="[]",
                     base_image_id=base_id, connection_id=conn_id, network_id=net_id,
                     default_cpu=cpu, default_ram=ram, default_disk=disk, public=True)
        s.add(t); s.flush()
        return t.id


def _deployed(name):
    """Return the deployment's (cpu, ram, disk) read while still bound to the session."""
    from app.models import Deployment
    from sqlmodel import select
    with session_scope() as s:
        d = s.exec(select(Deployment).where(Deployment.name == name)).first()
        return (d.cpu, d.ram, d.disk)


# --------------------------------------------------------------------------- #
# 1. template save/edit stores resources verbatim (no silent clamp)            #
# --------------------------------------------------------------------------- #
def test_template_save_stores_resources_verbatim():
    from app import api
    from app.models import Template, User
    from sqlmodel import select
    uid = _mk_user("w18-save@example.com")
    conn_id, base_id, net_id = _mk_conn_base_net()
    with session_scope() as s:
        user = s.get(User, uid)
        api.save_template(api.TemplateBody(
            name="big", recipe=[], cpu=8, ram=16, disk=100,
            baseImageId=base_id, connectionId=conn_id, networkId=net_id),
            user=user, session=s)
    with session_scope() as s:
        t = s.exec(select(Template).where(Template.name == "big")).first()
        assert t.default_cpu == 8, f"cpu clamped: {t.default_cpu}"
        assert t.default_ram == 16, f"ram clamped: {t.default_ram}"
        assert t.default_disk == 100, f"disk wrong: {t.default_disk}"
    print("test_template_save_stores_resources_verbatim OK")


def test_template_edit_stores_resources_verbatim():
    from app import api
    from app.models import Template, User
    from sqlmodel import select
    uid = _mk_user("w18-edit@example.com")
    conn_id, base_id, net_id = _mk_conn_base_net()
    with session_scope() as s:
        user = s.get(User, uid)
        api.save_template(api.TemplateBody(
            name="edit18", recipe=[], cpu=2, ram=4, disk=20,
            baseImageId=base_id, connectionId=conn_id, networkId=net_id),
            user=user, session=s)
        tid = s.exec(select(Template).where(Template.name == "edit18")).first().id
    with session_scope() as s:
        user = s.get(User, uid)
        api.edit_template_ep(tid, api.TemplateBody(
            name="edit18", recipe=[], cpu=12, ram=24, disk=200,
            baseImageId=base_id, connectionId=conn_id, networkId=net_id),
            user=user, session=s)
    with session_scope() as s:
        t = s.get(Template, tid)
        assert (t.default_cpu, t.default_ram, t.default_disk) == (12, 24, 200), \
            (t.default_cpu, t.default_ram, t.default_disk)
    print("test_template_edit_stores_resources_verbatim OK")


# --------------------------------------------------------------------------- #
# 2. deploy honours the connection ceiling; 0 = unlimited                      #
# --------------------------------------------------------------------------- #
def test_deploy_unlimited_when_connection_cap_zero():
    from app import api
    from app.models import User
    uid = _mk_user("w18-unlim@example.com")
    conn_id, base_id, net_id = _mk_conn_base_net(max_cores=0, max_ram_mb=0, max_disk_gb=0)
    tid = _mk_template(conn_id, base_id, net_id)
    with session_scope() as s:
        user = s.get(User, uid)
        api.deploy(api.DeployBody(templateId=tid, name="unlim-vm",
                                  cpu=8, ram=16, disk=500), user=user, session=s)
    got = _deployed("unlim-vm")
    assert got == (8, 16, 500), got
    print("test_deploy_unlimited_when_connection_cap_zero OK")


def test_deploy_clamps_to_connection_cap():
    from app import api
    from app.models import User
    uid = _mk_user("w18-clamp@example.com")
    # connection allows max 4 cores / 8 GB / 50 GB disk per VM
    conn_id, base_id, net_id = _mk_conn_base_net(max_cores=4, max_ram_mb=8192, max_disk_gb=50)
    tid = _mk_template(conn_id, base_id, net_id)
    with session_scope() as s:
        user = s.get(User, uid)
        api.deploy(api.DeployBody(templateId=tid, name="clamp-vm",
                                  cpu=8, ram=16, disk=500), user=user, session=s)
    got = _deployed("clamp-vm")
    assert got == (4, 8, 50), got
    print("test_deploy_clamps_to_connection_cap OK")


def test_deploy_uses_template_default_when_unspecified():
    from app import api
    from app.models import User
    uid = _mk_user("w18-default@example.com")
    conn_id, base_id, net_id = _mk_conn_base_net()  # unlimited
    tid = _mk_template(conn_id, base_id, net_id, cpu=6, ram=12, disk=80)
    with session_scope() as s:
        user = s.get(User, uid)
        api.deploy(api.DeployBody(templateId=tid, name="default-vm"), user=user, session=s)
    got = _deployed("default-vm")
    assert got == (6, 12, 80), got
    print("test_deploy_uses_template_default_when_unspecified OK")


if __name__ == "__main__":
    test_template_save_stores_resources_verbatim()
    test_template_edit_stores_resources_verbatim()
    test_deploy_unlimited_when_connection_cap_zero()
    test_deploy_clamps_to_connection_cap()
    test_deploy_uses_template_default_when_unspecified()
    print("\nALL WAVE 18 UNIT TESTS PASSED")
