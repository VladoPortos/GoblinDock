"""Wave 15 — QA findings: cancellation reconciliation, rebuild abort-on-destroy-fail,
network connection edit, widget template tenant-scoping.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave15.py
Run (Windows):        $env:GOBLINDOCK_DEV='1'; .venv\\Scripts\\python.exe tests\\test_wave15.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave15-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from sqlmodel import select                            # noqa: E402
from app.db import init_db, session_scope             # noqa: E402
from app import worker                                 # noqa: E402
from app.models import (                               # noqa: E402
    Connection, Deployment, Image, IpAllocation, Job, Network, Template, User)
from app.security import hash_password                 # noqa: E402

init_db()


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _mk_user(email, role="user"):
    with session_scope() as s:
        u = User(email=email, name="U", password_hash=hash_password("StrongPass12!"), role=role)
        s.add(u); s.flush()
        return u.id


def _mk_conn():
    with session_scope() as s:
        c = Connection(name="px-w15-" + os.urandom(3).hex(), host="127.0.0.1",
                       token_id="t@pve!x", node="pve")
        s.add(c); s.flush()
        return c.id


def _mk_net(conn_id):
    with session_scope() as s:
        n = Network(connection_id=conn_id, name="lan", mode="static",
                    subnet_cidr="10.0.50.0/24", gateway="10.0.50.1",
                    range_start="10.0.50.10", range_end="10.0.50.200")
        s.add(n); s.flush()
        return n.id


def _mk_dep_with_alloc(conn_id, net_id, vmid=None, status="working", owner_id=None):
    with session_scope() as s:
        d = Deployment(name="d-" + os.urandom(3).hex(), owner_id=owner_id,
                       connection_id=conn_id, network_id=net_id, vmid=vmid,
                       node="pve", status=status)
        s.add(d); s.flush()
        s.add(IpAllocation(network_id=net_id, ip=f"10.0.50.{20 + (d.id % 150)}",
                           deployment_id=d.id, state="reserved"))
        return d.id


def _allocs(dep_id):
    with session_scope() as s:
        return s.exec(select(IpAllocation).where(IpAllocation.deployment_id == dep_id)).all()


def _expect_http(code, fn):
    from fastapi import HTTPException
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == code, (e.status_code, e.detail)
        return e
    raise AssertionError(f"expected HTTPException {code}")


def _net_body(**kw):
    from app import api
    base = dict(connectionId=1, name="n", mode="dhcp", bridge="vmbr0", vlan=None,
                subnet_cidr="", gateway="", range_start="", range_end="", dns="")
    base.update(kw)
    return api.NetworkBody(**base)


def _stub_px(record=None, present=()):
    """Fake Proxmox: records destroy() targets and reports `present` vmids via list_qemu."""
    class _Px:
        node = "pve"
        def __init__(self, conn): pass
        def destroy(self, vmid, node=None):
            if record is not None:
                record.append(vmid)
            return "UPID:destroy"
        def wait_task(self, upid, node=None, cancelled=None, timeout=0): pass
        def list_qemu(self, node=None):
            return [{"vmid": v} for v in present]
    return _Px


# --------------------------------------------------------------------------- #
# Finding 1 — cancellation reconciliation (HIGH)                               #
# --------------------------------------------------------------------------- #
def test_cancel_queued_deploy_deletes_deployment():
    """A deploy cancelled while still queued never created a VM → its deployment row
    and IP reservation must be removed, not stranded in 'working'."""
    cid, nid = _mk_conn(), None
    nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=None, status="working")
    with session_scope() as s:
        s.add(Job(type="deploy", status="queued", cancel_requested=True,
                  deployment_id=did, connection_id=cid))
    assert worker._claim_next_job() is None
    with session_scope() as s:
        assert s.get(Deployment, did) is None, "cancelled deploy must delete its deployment"
        jobs = s.exec(select(Job).where(Job.deployment_id == did)).all()
        assert jobs and all(j.status == "canceled" for j in jobs), [j.status for j in jobs]
    assert _allocs(did) == [], "cancelled deploy must free its IP reservation"
    print("test_cancel_queued_deploy_deletes_deployment OK")


def test_cancel_queued_rebuild_keeps_vm_and_ip():
    """A rebuild cancelled while queued never touched the live VM → keep the VM and
    its IP; only un-stick the status so /state live-probes the real state again."""
    cid = _mk_conn(); nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=9001, status="working")
    with session_scope() as s:
        s.add(Job(type="rebuild", status="queued", cancel_requested=True,
                  deployment_id=did, connection_id=cid))
    orig_px = worker.Proxmox
    worker.Proxmox = _stub_px(present=(9001,))   # old VM still present
    try:
        assert worker._claim_next_job() is None
    finally:
        worker.Proxmox = orig_px
    with session_scope() as s:
        d = s.get(Deployment, did)
        assert d is not None, "cancelled rebuild must KEEP the deployment"
        assert d.status != "working", f"status must leave 'working', got {d.status!r}"
    assert len(_allocs(did)) == 1, "cancelled rebuild must KEEP its IP reservation"
    print("test_cancel_queued_rebuild_keeps_vm_and_ip OK")


def test_cancel_queued_destroy_keeps_vm_and_ip():
    """A destroy cancelled while queued must leave the VM intact (the whole point of
    cancelling a destroy) and take the deployment out of 'working'."""
    cid = _mk_conn(); nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=9002, status="working")
    with session_scope() as s:
        s.add(Job(type="destroy", status="queued", cancel_requested=True,
                  deployment_id=did, connection_id=cid))
    orig_px = worker.Proxmox
    worker.Proxmox = _stub_px(present=(9002,))   # VM still present (cancel spared it)
    try:
        assert worker._claim_next_job() is None
    finally:
        worker.Proxmox = orig_px
    with session_scope() as s:
        d = s.get(Deployment, did)
        assert d is not None, "cancelled destroy must KEEP the deployment"
        assert d.status != "working", f"status must leave 'working', got {d.status!r}"
    assert len(_allocs(did)) == 1, "cancelled destroy must KEEP its IP reservation"
    print("test_cancel_queued_destroy_keeps_vm_and_ip OK")


def _run_execute_with_dispatch(jid, job_type, impl, px=None):
    orig_disp = worker._DISPATCH.get(job_type)
    orig_px = worker.Proxmox
    worker._DISPATCH[job_type] = impl
    if px is not None:
        worker.Proxmox = px
    try:
        worker._execute(jid)
    finally:
        worker._DISPATCH[job_type] = orig_disp
        worker.Proxmox = orig_px


def test_execute_cancel_deploy_destroys_vm_and_deletes():
    """A deploy cancelled while RUNNING may already have created+started a VM. The VM
    must be destroyed (no Proxmox orphan), the IP freed, and the deployment deleted."""
    cid = _mk_conn(); nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=9100, status="working")
    with session_scope() as s:
        j = Job(type="deploy", status="running", deployment_id=did, connection_id=cid)
        s.add(j); s.flush(); jid = j.id
    record = []

    def _boom(ctx, job):
        raise worker.JobCancelled()
    _run_execute_with_dispatch(jid, "deploy", _boom, px=_stub_px(record))

    with session_scope() as s:
        assert s.get(Job, jid).status == "canceled"
        assert s.get(Deployment, did) is None, "cancelled deploy must delete its deployment"
    assert _allocs(did) == [], "cancelled deploy must free its IP reservation"
    assert record == [9100], f"cancelled deploy must destroy its half-built VM, got {record}"
    print("test_execute_cancel_deploy_destroys_vm_and_deletes OK")


def test_execute_cancel_rebuild_keeps_everything():
    """A rebuild cancelled while running must not destroy any VM, must keep the IP, and
    must un-stick the deployment status (not leave it 'working')."""
    cid = _mk_conn(); nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=9200, status="working")
    with session_scope() as s:
        j = Job(type="rebuild", status="running", deployment_id=did, connection_id=cid)
        s.add(j); s.flush(); jid = j.id
    record = []

    def _boom(ctx, job):
        raise worker.JobCancelled()
    _run_execute_with_dispatch(jid, "rebuild", _boom, px=_stub_px(record, present=(9200,)))

    with session_scope() as s:
        assert s.get(Job, jid).status == "canceled"
        d = s.get(Deployment, did)
        assert d is not None and d.status != "working", d and d.status
    assert len(_allocs(did)) == 1, "cancelled rebuild must KEEP its IP reservation"
    assert record == [], f"cancelled rebuild must NOT destroy any VM, got {record}"
    print("test_execute_cancel_rebuild_keeps_everything OK")


def test_execute_cancel_destroy_vm_present_keeps():
    """Cancelling a destroy while the VM still exists must spare the VM and its IP."""
    cid = _mk_conn(); nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=9400, status="working")
    with session_scope() as s:
        j = Job(type="destroy", status="running", deployment_id=did, connection_id=cid)
        s.add(j); s.flush(); jid = j.id
    record = []

    def _boom(ctx, job):
        raise worker.JobCancelled()
    _run_execute_with_dispatch(jid, "destroy", _boom, px=_stub_px(record, present=(9400,)))

    with session_scope() as s:
        assert s.get(Job, jid).status == "canceled"
        d = s.get(Deployment, did)
        assert d is not None and d.status != "working", d and d.status
    assert len(_allocs(did)) == 1, "cancelled destroy with a live VM must KEEP the IP"
    assert record == [], "must not destroy a VM the user asked to spare"
    print("test_execute_cancel_destroy_vm_present_keeps OK")


def test_execute_cancel_destroy_vm_gone_completes():
    """If the destroy task already removed the VM before the cancel was reconciled, the
    state must converge: free the IP and delete the deployment (don't strand it)."""
    cid = _mk_conn(); nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=9401, status="working")
    with session_scope() as s:
        j = Job(type="destroy", status="running", deployment_id=did, connection_id=cid)
        s.add(j); s.flush(); jid = j.id

    def _boom(ctx, job):
        raise worker.JobCancelled()
    _run_execute_with_dispatch(jid, "destroy", _boom, px=_stub_px(present=()))   # VM gone

    with session_scope() as s:
        assert s.get(Job, jid).status == "canceled"
        assert s.get(Deployment, did) is None, "a destroy that already completed must delete the dep"
    assert _allocs(did) == [], "a completed destroy must free the IP"
    print("test_execute_cancel_destroy_vm_gone_completes OK")


def test_execute_cancel_rebuild_vm_gone_errors():
    """Cancelling a rebuild after the old VM was already removed leaves nothing runnable:
    mark the deployment 'error' (rebuild again) but KEEP the reserved IP for the retry."""
    cid = _mk_conn(); nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=9402, status="working")
    with session_scope() as s:
        j = Job(type="rebuild", status="running", deployment_id=did, connection_id=cid)
        s.add(j); s.flush(); jid = j.id

    def _boom(ctx, job):
        raise worker.JobCancelled()
    _run_execute_with_dispatch(jid, "rebuild", _boom, px=_stub_px(present=()))   # old VM gone

    with session_scope() as s:
        assert s.get(Job, jid).status == "canceled"
        d = s.get(Deployment, did)
        assert d is not None and d.status == "error", d and d.status
    assert len(_allocs(did)) == 1, "rebuild retry reuses the reserved IP, so keep it"
    print("test_execute_cancel_rebuild_vm_gone_errors OK")


def test_run_destroy_idempotent_when_vm_already_gone():
    """Destroying a VM that's already absent (e.g. removed in Proxmox directly, or by a
    half-finished prior destroy) must SUCCEED: delete the dep and free the IP."""
    cid = _mk_conn(); nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=9403, status="working")
    with session_scope() as s:
        j = Job(type="destroy", status="running", deployment_id=did, connection_id=cid)
        s.add(j); s.flush(); jid = j.id
        job_copy = Job(**s.get(Job, jid).model_dump())

    class _Px:
        node = "pve"
        def __init__(self, conn): pass
        def stop(self, vmid, node=None): return "UPID:stop"
        def destroy(self, vmid, node=None): raise RuntimeError("500 no such VM")
        def wait_task(self, *a, **k): pass
        def list_qemu(self, node=None): return []   # already gone

    ctx = worker.JobCtx(jid)
    orig_px, orig_sleep = worker.Proxmox, worker.time.sleep
    worker.Proxmox = _Px
    worker.time.sleep = lambda *a, **k: None
    try:
        worker._run_destroy(ctx, job_copy)
    finally:
        worker.Proxmox, worker.time.sleep = orig_px, orig_sleep

    with session_scope() as s:
        assert s.get(Deployment, did) is None, "idempotent destroy of an absent VM must delete the dep"
    assert _allocs(did) == [], "idempotent destroy must free the IP"
    print("test_run_destroy_idempotent_when_vm_already_gone OK")


def test_execute_failure_deploy_frees_ip():
    """A FAILED (not cancelled) deploy produced no surviving VM → mark error + free IP
    (preserves existing behaviour)."""
    cid = _mk_conn(); nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=None, status="working")
    with session_scope() as s:
        j = Job(type="deploy", status="running", deployment_id=did, connection_id=cid)
        s.add(j); s.flush(); jid = j.id

    def _boom(ctx, job):
        raise RuntimeError("boom")
    _run_execute_with_dispatch(jid, "deploy", _boom)

    with session_scope() as s:
        assert s.get(Job, jid).status == "failed"
        assert s.get(Deployment, did).status == "error"
    assert _allocs(did) == [], "failed deploy must free its IP reservation"
    print("test_execute_failure_deploy_frees_ip OK")


def test_execute_failure_rebuild_keeps_ip():
    """A FAILED rebuild leaves the existing VM (and its reserved IP) in place — freeing
    the IP while the VM still holds it could hand that address to another VM."""
    cid = _mk_conn(); nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=9201, status="working")
    with session_scope() as s:
        j = Job(type="rebuild", status="running", deployment_id=did, connection_id=cid)
        s.add(j); s.flush(); jid = j.id

    def _boom(ctx, job):
        raise RuntimeError("boom")
    _run_execute_with_dispatch(jid, "rebuild", _boom)

    with session_scope() as s:
        assert s.get(Job, jid).status == "failed"
        assert s.get(Deployment, did).status == "error"
    assert len(_allocs(did)) == 1, "failed rebuild must KEEP its IP reservation"
    print("test_execute_failure_rebuild_keeps_ip OK")


# --------------------------------------------------------------------------- #
# Finding 2 — rebuild must abort if the old VM survives destroy (MEDIUM)       #
# --------------------------------------------------------------------------- #
def test_vm_exists_helper():
    class _Px:
        def __init__(self, present): self.present = present
        def list_qemu(self, node=None):
            if self.present is None:
                raise RuntimeError("listing down")
            return [{"vmid": v} for v in self.present]
    assert worker._vm_exists(_Px([100, 101]), 100, "pve") is True
    assert worker._vm_exists(_Px([101]), 100, "pve") is False
    assert worker._vm_exists(_Px(None), 100, "pve") is True, "listing failure must fail safe (assume exists)"
    print("test_vm_exists_helper OK")


def test_rebuild_aborts_when_old_vm_survives_destroy():
    cid = _mk_conn(); nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=9300, status="working")
    with session_scope() as s:
        j = Job(type="rebuild", status="running", deployment_id=did, connection_id=cid)
        s.add(j); s.flush(); jid = j.id
    created = []

    class _Px:
        def __init__(self, conn): pass
        def pick_node(self): return "pve"
        def stop(self, vmid, node=None): return "UPID:stop"
        def destroy(self, vmid, node=None): raise RuntimeError("destroy failed: VM busy")
        def wait_task(self, *a, **k): pass
        def list_qemu(self, node=None): return [{"vmid": 9300}]   # old VM SURVIVED
        def create_vm_import(self, *a, **k): created.append(a); return "UPID:create"

    with session_scope() as s:
        job_copy = Job(**s.get(Job, jid).model_dump())
    ctx = worker.JobCtx(jid)
    orig_px, orig_sleep = worker.Proxmox, worker.time.sleep
    worker.Proxmox = _Px
    worker.time.sleep = lambda *a, **k: None
    raised = None
    try:
        worker._run_rebuild(ctx, job_copy)
    except RuntimeError as e:
        raised = e
    finally:
        worker.Proxmox, worker.time.sleep = orig_px, orig_sleep

    assert raised is not None and "aborted" in str(raised), raised
    assert created == [], "rebuild must NOT recreate over the surviving old VM"
    print("test_rebuild_aborts_when_old_vm_survives_destroy OK")


# --------------------------------------------------------------------------- #
# Finding 3 — editing a network's connection must take effect (MEDIUM)         #
# --------------------------------------------------------------------------- #
def test_edit_network_changes_connection():
    from app import api
    admin = _mk_user("w15-neta@x.io", role="admin")
    c1, c2 = _mk_conn(), _mk_conn()
    with session_scope() as s:
        n = Network(connection_id=c1, name="lan", mode="dhcp")
        s.add(n); s.flush(); nid = n.id
    with session_scope() as s:
        api.edit_network(nid, _net_body(connectionId=c2, name="lan2", mode="dhcp"),
                         user=s.get(User, admin), session=s)
    with session_scope() as s:
        n = s.get(Network, nid)
        assert n.connection_id == c2, f"edit must move the network, got conn {n.connection_id}"
        assert n.name == "lan2"
    print("test_edit_network_changes_connection OK")


def test_edit_network_unknown_connection_rejected():
    from app import api
    admin = _mk_user("w15-netb@x.io", role="admin")
    c1 = _mk_conn()
    with session_scope() as s:
        n = Network(connection_id=c1, name="lan", mode="dhcp")
        s.add(n); s.flush(); nid = n.id
    with session_scope() as s:
        _expect_http(400, lambda: api.edit_network(
            nid, _net_body(connectionId=999999, name="x", mode="dhcp"),
            user=s.get(User, admin), session=s))
    print("test_edit_network_unknown_connection_rejected OK")


def test_edit_network_blocks_change_when_in_use():
    from app import api
    admin = _mk_user("w15-netc@x.io", role="admin")
    c1, c2 = _mk_conn(), _mk_conn()
    with session_scope() as s:
        n = Network(connection_id=c1, name="lan", mode="dhcp")
        s.add(n); s.flush(); nid = n.id
        s.add(Deployment(name="user-vm", connection_id=c1, network_id=nid, status="running"))
    with session_scope() as s:
        _expect_http(409, lambda: api.edit_network(
            nid, _net_body(connectionId=c2, name="lan", mode="dhcp"),
            user=s.get(User, admin), session=s))
    # same-connection field edits must still work even while in use
    with session_scope() as s:
        api.edit_network(nid, _net_body(connectionId=c1, name="renamed", mode="dhcp"),
                         user=s.get(User, admin), session=s)
    with session_scope() as s:
        assert s.get(Network, nid).name == "renamed"
    print("test_edit_network_blocks_change_when_in_use OK")


# --------------------------------------------------------------------------- #
# Finding 4 — widget summary must not count other users' private templates     #
# --------------------------------------------------------------------------- #
def test_widget_summary_excludes_other_users_private_templates():
    from app import api
    a = _mk_user("w15-wa@x.io")
    b = _mk_user("w15-wb@x.io")
    adm = _mk_user("w15-wadm@x.io", role="admin")
    cid = _mk_conn()
    with session_scope() as s:
        img = Image(kind="base", name="b-" + os.urandom(2).hex(),
                    source_url="https://example.com/i.img", build_status="ready")
        s.add(img); s.flush()
        s.add(Template(name="a-priv", base_image_id=img.id, connection_id=cid, owner_id=a, public=False))
        s.add(Template(name="b-priv", base_image_id=img.id, connection_id=cid, owner_id=b, public=False))
        s.add(Template(name="pub", base_image_id=img.id, connection_id=cid, owner_id=b, public=True))
    with session_scope() as s:
        out_a = api.widget_summary(user=s.get(User, a), session=s)
        out_b = api.widget_summary(user=s.get(User, b), session=s)
        out_adm = api.widget_summary(user=s.get(User, adm), session=s)
    assert out_a["templates"] == 2, f"A must see own-private + public only (not B's private), got {out_a['templates']}"
    assert out_b["templates"] == 2, out_b["templates"]
    assert out_adm["templates"] == 3, f"admin sees all deployable templates, got {out_adm['templates']}"
    print("test_widget_summary_excludes_other_users_private_templates OK")


if __name__ == "__main__":
    test_cancel_queued_deploy_deletes_deployment()
    test_cancel_queued_rebuild_keeps_vm_and_ip()
    test_cancel_queued_destroy_keeps_vm_and_ip()
    test_execute_cancel_deploy_destroys_vm_and_deletes()
    test_execute_cancel_rebuild_keeps_everything()
    test_execute_cancel_destroy_vm_present_keeps()
    test_execute_cancel_destroy_vm_gone_completes()
    test_execute_cancel_rebuild_vm_gone_errors()
    test_run_destroy_idempotent_when_vm_already_gone()
    test_execute_failure_deploy_frees_ip()
    test_execute_failure_rebuild_keeps_ip()
    test_vm_exists_helper()
    test_rebuild_aborts_when_old_vm_survives_destroy()
    test_edit_network_changes_connection()
    test_edit_network_unknown_connection_rejected()
    test_edit_network_blocks_change_when_in_use()
    test_widget_summary_excludes_other_users_private_templates()
    print("\nALL WAVE 15 UNIT TESTS PASSED")
