"""Wave 53 — day-2 resize: CPU/RAM (stopped-only) and per-disk grow.

Fire-and-forget Proxmox config writes, mirroring the Proxmox UI's own rules:
CPU/memory apply to a STOPPED VM (hard gate, clear 409 while running), disk
grow is online-safe and grow-only. The connection's per-VM ceilings are the
same authority as at deploy, the deployment row mirrors the new sizes, and
the guest OS is never touched.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave53.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave53-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from fastapi import HTTPException  # noqa: E402
from sqlmodel import select  # noqa: E402

from app import api  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Audit, Connection, Deployment, Image, Job, User  # noqa: E402

init_db()


def _expect_http(code, fn):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == code, (exc.status_code, exc.detail)
        return exc
    raise AssertionError(f"expected HTTPException {code}")


class FakeProxmox:
    status = "stopped"
    config: dict = {}
    calls: list = []

    def __init__(self, _conn):
        pass

    def vm_current(self, vmid, node=None):
        return {"status": FakeProxmox.status}

    def vm_config(self, vmid, node=None):
        return dict(FakeProxmox.config)

    def set_config(self, vmid, node=None, **params):
        FakeProxmox.calls.append(("set_config", vmid, params))

    def resize_disk(self, vmid, disk, size, node=None):
        FakeProxmox.calls.append(("resize_disk", vmid, disk, size))


def _patch():
    FakeProxmox.status = "stopped"
    FakeProxmox.config = {"scsi0": "local-lvm:vm-1-disk-0,size=20G"}
    FakeProxmox.calls = []
    original = api.Proxmox
    api.Proxmox = FakeProxmox
    return original


def _fixture(*, disabled=False, max_cores=0, max_ram_mb=0, max_disk_gb=0):
    suffix = os.urandom(4).hex()
    with session_scope() as s:
        owner = User(email=f"wave53-{suffix}@example.com", name="owner",
                     password_hash="unused")
        s.add(owner)
        s.flush()
        conn = Connection(name=f"w53-conn-{suffix}", host="pve.example",
                          token_id="a@pve!t", node="pve", disabled=disabled,
                          max_cores=max_cores, max_ram_mb=max_ram_mb,
                          max_disk_gb=max_disk_gb)
        image = Image(kind="base", name=f"w53-img-{suffix}",
                      source_url="https://example.com/x.img", build_status="ready")
        s.add(conn)
        s.add(image)
        s.flush()
        dep = Deployment(name=f"w53-vm-{suffix}", owner_id=owner.id,
                         connection_id=conn.id, image_id=image.id, vmid=8700,
                         node="pve", status="stopped", cpu=1, ram=2, disk=20)
        s.add(dep)
        s.flush()
        return {"owner": owner.id, "conn": conn.id, "dep": dep.id}


def test_parse_disk_entries_filters_and_sizes():
    cfg = {
        "scsi0": "local-lvm:vm-1-disk-0,size=20G",
        "virtio1": "local-lvm:vm-1-disk-1,size=512M",
        "sata2": "tank:vm-1-disk-2,size=1T",
        "ide2": "local-lvm:vm-1-cloudinit,media=cdrom,size=4M",
        "ide0": "none,media=cdrom",
        "efidisk0": "local-lvm:vm-1-efi,size=4M",
        "net0": "virtio=AA:BB,bridge=vmbr0",
        "scsihw": "virtio-scsi-pci",
    }
    entries = api._parse_disk_entries(cfg)
    assert [e["key"] for e in entries] == ["sata2", "scsi0", "virtio1"]
    by_key = {e["key"]: e for e in entries}
    assert by_key["scsi0"]["sizeGb"] == 20 and by_key["scsi0"]["storage"] == "local-lvm"
    assert by_key["virtio1"]["sizeGb"] == 0.5
    assert by_key["sata2"]["sizeGb"] == 1024


def test_cpu_ram_resize_gated_while_running():
    fx = _fixture()
    original = _patch()
    FakeProxmox.status = "running"
    try:
        with session_scope() as s:
            exc = _expect_http(409, lambda: api.vm_resize_config(
                fx["dep"], api.VmResizeBody(cores=4, ramGb=8),
                user=s.get(User, fx["owner"]), session=s))
            assert "stop the VM first" in exc.detail
    finally:
        api.Proxmox = original
    assert FakeProxmox.calls == [], "a running VM must never receive a config write"
    with session_scope() as s:
        dep = s.get(Deployment, fx["dep"])
        assert dep.cpu == 1 and dep.ram == 2


def test_cpu_ram_resize_applies_when_stopped():
    fx = _fixture()
    original = _patch()
    try:
        with session_scope() as s:
            out = api.vm_resize_config(
                fx["dep"], api.VmResizeBody(cores=4, ramGb=8),
                user=s.get(User, fx["owner"]), session=s)
    finally:
        api.Proxmox = original
    assert out == {"ok": True, "cores": 4, "ramGb": 8}
    assert ("set_config", 8700, {"cores": 4, "memory": 8192}) in FakeProxmox.calls
    with session_scope() as s:
        dep = s.get(Deployment, fx["dep"])
        assert dep.cpu == 4 and dep.ram == 8
        audit = s.exec(select(Audit).where(
            Audit.action == "vm.resize", Audit.target_id == str(fx["dep"]),
        ).order_by(Audit.id.desc())).first()
        assert audit and "cores 1→4" in audit.detail and "ram 2G→8G" in audit.detail


def test_resize_respects_connection_ceilings_and_requires_a_field():
    fx = _fixture(max_cores=2, max_ram_mb=2048)
    original = _patch()
    try:
        with session_scope() as s:
            owner = s.get(User, fx["owner"])
            exc = _expect_http(400, lambda: api.vm_resize_config(
                fx["dep"], api.VmResizeBody(cores=4), user=owner, session=s))
            assert "per-VM limit" in exc.detail
            exc = _expect_http(400, lambda: api.vm_resize_config(
                fx["dep"], api.VmResizeBody(ramGb=4), user=owner, session=s))
            assert "per-VM limit" in exc.detail
            _expect_http(400, lambda: api.vm_resize_config(
                fx["dep"], api.VmResizeBody(), user=owner, session=s))
    finally:
        api.Proxmox = original
    assert FakeProxmox.calls == []


def test_disk_grow_refuses_bad_targets():
    fx = _fixture()
    original = _patch()
    FakeProxmox.config = {
        "scsi0": "local-lvm:vm-1-disk-0,size=20G",
        "ide2": "local-lvm:vm-1-cloudinit,media=cdrom,size=4M",
    }
    try:
        with session_scope() as s:
            owner = s.get(User, fx["owner"])
            _expect_http(400, lambda: api.vm_disk_resize(
                fx["dep"], "floppy0", api.DiskResizeBody(sizeGb=40), user=owner, session=s))
            _expect_http(400, lambda: api.vm_disk_resize(
                fx["dep"], "scsi9", api.DiskResizeBody(sizeGb=40), user=owner, session=s))
            _expect_http(400, lambda: api.vm_disk_resize(
                fx["dep"], "ide2", api.DiskResizeBody(sizeGb=40), user=owner, session=s))
            exc = _expect_http(400, lambda: api.vm_disk_resize(
                fx["dep"], "scsi0", api.DiskResizeBody(sizeGb=20), user=owner, session=s))
            assert "grow only" in exc.detail
    finally:
        api.Proxmox = original
    assert all(call[0] != "resize_disk" for call in FakeProxmox.calls)


def test_disk_grow_applies_and_syncs_only_the_root_disk():
    fx = _fixture()
    original = _patch()
    FakeProxmox.config = {
        "scsi0": "local-lvm:vm-1-disk-0,size=20G",
        "virtio1": "tank:vm-1-disk-1,size=100G",
    }
    try:
        with session_scope() as s:
            owner = s.get(User, fx["owner"])
            out = api.vm_disk_resize(
                fx["dep"], "scsi0", api.DiskResizeBody(sizeGb=40), user=owner, session=s)
            assert out == {"ok": True, "disk": "scsi0", "sizeGb": 40}
            out = api.vm_disk_resize(
                fx["dep"], "virtio1", api.DiskResizeBody(sizeGb=150), user=owner, session=s)
            assert out["ok"] is True
    finally:
        api.Proxmox = original
    assert ("resize_disk", 8700, "scsi0", "40G") in FakeProxmox.calls
    assert ("resize_disk", 8700, "virtio1", "150G") in FakeProxmox.calls
    with session_scope() as s:
        dep = s.get(Deployment, fx["dep"])
        assert dep.disk == 40, "root disk grow must sync the deployment row"
        audit = s.exec(select(Audit).where(
            Audit.action == "vm.disk_resize", Audit.target_id == str(fx["dep"]),
        ).order_by(Audit.id.desc())).first()
        assert audit and "virtio1 100G→150G" in audit.detail


def test_disk_grow_respects_connection_ceiling():
    fx = _fixture(max_disk_gb=30)
    original = _patch()
    try:
        with session_scope() as s:
            exc = _expect_http(400, lambda: api.vm_disk_resize(
                fx["dep"], "scsi0", api.DiskResizeBody(sizeGb=40),
                user=s.get(User, fx["owner"]), session=s))
            assert "per-VM limit" in exc.detail
    finally:
        api.Proxmox = original


def test_resize_blocked_on_disabled_source_or_active_job():
    fx = _fixture(disabled=True)
    original = _patch()
    try:
        with session_scope() as s:
            exc = _expect_http(409, lambda: api.vm_resize_config(
                fx["dep"], api.VmResizeBody(cores=2),
                user=s.get(User, fx["owner"]), session=s))
            assert "disabled" in exc.detail
        fx2 = _fixture()
        with session_scope() as s:
            s.add(Job(type="rebuild", title="x", deployment_id=fx2["dep"],
                      connection_id=fx2["conn"], created_by=fx2["owner"],
                      status="running"))
            s.flush()
        with session_scope() as s:
            _expect_http(409, lambda: api.vm_disk_resize(
                fx2["dep"], "scsi0", api.DiskResizeBody(sizeGb=40),
                user=s.get(User, fx2["owner"]), session=s))
    finally:
        api.Proxmox = original


if __name__ == "__main__":
    test_parse_disk_entries_filters_and_sizes()
    test_cpu_ram_resize_gated_while_running()
    test_cpu_ram_resize_applies_when_stopped()
    test_resize_respects_connection_ceilings_and_requires_a_field()
    test_disk_grow_refuses_bad_targets()
    test_disk_grow_applies_and_syncs_only_the_root_disk()
    test_disk_grow_respects_connection_ceiling()
    test_resize_blocked_on_disabled_source_or_active_job()
    print("\nALL WAVE 53 UNIT TESTS PASSED")
