"""Wave 47 — state-probe, disclosure, and connection/network contracts."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave47-test.sqlite3")
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app import api, serialize as S  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Connection, Deployment, Image, Network, Template, User  # noqa: E402
from app.security import hash_password  # noqa: E402

init_db()


class _Request:
    def __init__(self):
        self.session = {}


def _user(email: str, role: str = "user") -> int:
    with session_scope() as session:
        user = User(
            email=email, name=email.split("@", 1)[0], role=role,
            password_hash=hash_password("Wave47-Strong-Pass!"),
        )
        session.add(user)
        session.flush()
        return user.id


def _connection(name: str) -> int:
    with session_scope() as session:
        conn = Connection(name=name, host="127.0.0.1", token_id="u@p!t")
        session.add(conn)
        session.flush()
        return conn.id


def _expect_http(status: int, call) -> HTTPException:
    try:
        call()
    except HTTPException as exc:
        assert exc.status_code == status, (exc.status_code, exc.detail)
        return exc
    raise AssertionError(f"expected HTTP {status}")


def test_cold_state_uses_no_connection_or_per_vm_probes():
    owner_id = _user("offline@wave47.test")
    conn_id = _connection("offline-wave47")
    with session_scope() as session:
        for index in range(8):
            session.add(Deployment(
                name=f"offline-{index}", owner_id=owner_id,
                connection_id=conn_id, vmid=4700 + index, node="pve",
                status="running",
            ))

    calls = {"version": 0, "inventory": 0, "current": 0}

    class _OfflinePx:
        def __init__(self, conn):
            self.conn = conn
            self.node = conn.node

        def version(self):
            calls["version"] += 1
            raise RuntimeError("offline")

        def list_qemu(self, _node=None):
            calls["inventory"] += 1
            raise AssertionError("offline connection must short-circuit inventory")

        def vm_current(self, _vmid, _node=None):
            calls["current"] += 1
            raise RuntimeError("offline")

    saved_px = api.Proxmox
    api.Proxmox = _OfflinePx
    S._status_cache.clear()
    try:
        with session_scope() as session:
            state = api.state(_Request(), session.get(User, owner_id), session)
    finally:
        api.Proxmox = saved_px

    owned = [vm for vm in state["VMS"] if vm["name"].startswith("offline-")]
    assert len(owned) == 8
    assert {vm["status"] for vm in owned} == {"unknown"}
    assert calls == {"version": 0, "inventory": 0, "current": 0}


def test_online_state_reads_all_vm_statuses_from_background_inventory():
    owner_id = _user("online@wave47.test")
    conn_id = _connection("online-wave47")
    vmids = [4780, 4781, 4782]
    with session_scope() as session:
        for index, vmid in enumerate(vmids):
            session.add(Deployment(
                name=f"online-{index}", owner_id=owner_id,
                connection_id=conn_id, vmid=vmid, node="pve",
                status="running",
            ))

    calls = {"version": 0, "inventory": 0, "current": 0}

    class _OnlinePx:
        def __init__(self, conn):
            self.conn = conn
            self.node = conn.node

        def version(self):
            calls["version"] += 1
            return {"version": "8.4"}

        def list_qemu(self, _node=None):
            calls["inventory"] += 1
            return [
                {"vmid": vmid, "status": "running" if vmid != 4781 else "stopped",
                 "cpu": 0.1, "mem": 128, "maxmem": 1024, "uptime": 60}
                for vmid in vmids
            ]

        def vm_current(self, _vmid, _node=None):
            calls["current"] += 1
            raise AssertionError("state must use the primed inventory")

    from app import inventory
    saved_snapshot = inventory.get_snapshot
    inventory.get_snapshot = lambda cid: {"status":"online", "version":"8.4", "vms":{
        vmid: {"vmid":vmid, "node":"migrated", "type":"qemu", "status":"running" if vmid != 4781 else "stopped",
               "cpu":0.1,"mem":128,"maxmem":1024,"uptime":60} for vmid in vmids
    }, "stale":False, "error":None}
    saved_px = api.Proxmox
    api.Proxmox = _OnlinePx
    S._status_cache.clear()
    try:
        with session_scope() as session:
            state = api.state(_Request(), session.get(User, owner_id), session)
    finally:
        api.Proxmox = saved_px
        inventory.get_snapshot = saved_snapshot

    by_name = {vm["name"]: vm["status"] for vm in state["VMS"]}
    assert by_name == {"online-0": "running", "online-1": "stopped", "online-2": "running"}
    assert calls == {"version": 0, "inventory": 0, "current": 0}


def test_base_image_source_url_is_admin_only_in_state():
    admin_id = _user("admin-source@wave47.test", "admin")
    user_id = _user("user-source@wave47.test")
    secret_url = "https://download.example/base.qcow2?signature=wave47-secret"
    with session_scope() as session:
        session.add(Image(kind="base", name="source-wave47", source_url=secret_url))

    saved_px_cache = api._px_cache
    api._px_cache = lambda _conns: {}
    try:
        with session_scope() as session:
            regular = api.state(_Request(), session.get(User, user_id), session)
            admin = api.state(_Request(), session.get(User, admin_id), session)
    finally:
        api._px_cache = saved_px_cache

    regular_image = next(i for i in regular["BASE_IMAGES"] if i["name"] == "source-wave47")
    admin_image = next(i for i in admin["BASE_IMAGES"] if i["name"] == "source-wave47")
    assert secret_url not in str(regular_image)
    assert admin_image["source_url"] == secret_url


def test_vm_detail_preserves_unknown_status_and_disables_console_on_probe_failure():
    owner_id = _user("detail@wave47.test")
    conn_id = _connection("detail-wave47")
    with session_scope() as session:
        dep = Deployment(
            name="detail-wave47", owner_id=owner_id, connection_id=conn_id,
            vmid=4790, node="pve", status="running",
        )
        session.add(dep)
        session.flush()
        dep_id = dep.id

    class _BrokenPx:
        def __init__(self, conn):
            self.conn = conn

        def vm_current(self, _vmid, _node=None):
            raise RuntimeError("node unavailable")

    saved_px = api.Proxmox
    api.Proxmox = _BrokenPx
    try:
        with session_scope() as session:
            detail = api.vm_detail(
                dep_id, session.get(User, owner_id), session,
            )
    finally:
        api.Proxmox = saved_px

    assert detail["status"] == "running", "stored status remains historical context"
    assert detail["live"] == {"status": "unknown"}
    assert detail["liveError"] == "Live VM status is unavailable"
    assert detail["consoleReady"] is False


def test_referenced_network_cannot_move_and_mismatch_fails_closed():
    admin_id = _user("network-admin@wave47.test", "admin")
    conn_a = _connection("network-a-wave47")
    conn_b = _connection("network-b-wave47")
    with session_scope() as session:
        image = Image(kind="base", name="network-image-wave47", source_url="https://example.test/base.img")
        network = Network(connection_id=conn_a, name="network-wave47", mode="dhcp")
        session.add(image)
        session.add(network)
        session.flush()
        template = Template(
            name="network-template-wave47", owner_id=admin_id, public=True,
            base_image_id=image.id, connection_id=conn_a, network_id=network.id,
            recipe_json="[]",
        )
        session.add(template)
        session.flush()
        network_id, template_id = network.id, template.id

    body = api.NetworkBody(connectionId=conn_b, name="network-wave47", mode="dhcp")
    with session_scope() as session:
        _expect_http(409, lambda: api.edit_network(
            network_id, body, session.get(User, admin_id), session,
        ))

    # Simulate legacy/corrupt mismatched data and verify both presentation and deploy
    # fail closed rather than silently selecting the connection's default DHCP network.
    with session_scope() as session:
        session.get(Network, network_id).connection_id = conn_b
    with session_scope() as session:
        template = session.get(Template, template_id)
        admin = session.get(User, admin_id)
        assert S.template_dict(session, template, viewer=admin)["deployable"] is False
        _expect_http(400, lambda: api._deploy_transaction(
            api.DeployBody(templateId=template_id, name="network-mismatch-wave47"),
            admin, session,
        ))


def test_connection_numeric_contracts_match_deployment_limits():
    common = {"name": "numeric-wave47", "host": "pve", "token_id": "u@p!t", "token_secret": "x"}
    for field, value in (
        ("port", 0), ("port", 65536), ("max_cores", 257),
        ("max_ram_gb", 1025), ("max_disk_gb", 16385),
    ):
        try:
            api.ConnBody(**common, **{field: value})
        except ValidationError:
            pass
        else:
            raise AssertionError(f"ConnBody accepted invalid {field}={value}")
    for field, value in (
        ("port", 0), ("port", 65536), ("max_cores", 257),
        ("max_ram_gb", 1025), ("max_disk_gb", 16385),
    ):
        try:
            api.ConnEditBody(**{field: value})
        except ValidationError:
            pass
        else:
            raise AssertionError(f"ConnEditBody accepted invalid {field}={value}")
    with session_scope() as session:
        legacy = Connection(
            name="legacy-numeric-wave47", host="pve", port=0,
            token_id="u@p!t", max_cores=999, max_ram_mb=9999 * 1024,
            max_disk_gb=99999,
        )
        session.add(legacy)
        session.flush()
        serialized = S.connection_dict(session, legacy)
    assert serialized["port"] == 8006
    assert serialized["url"].endswith(":8006")
    assert (serialized["maxCores"], serialized["maxRamGb"], serialized["maxDiskGb"]) == (
        256, 1024, 16384,
    )


if __name__ == "__main__":
    test_cold_state_uses_no_connection_or_per_vm_probes()
    test_online_state_reads_all_vm_statuses_from_background_inventory()
    test_base_image_source_url_is_admin_only_in_state()
    test_vm_detail_preserves_unknown_status_and_disables_console_on_probe_failure()
    test_referenced_network_cannot_move_and_mismatch_fails_closed()
    test_connection_numeric_contracts_match_deployment_limits()
    print("\nALL WAVE 47 UNIT TESTS PASSED")
