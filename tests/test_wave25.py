"""Wave 25 — review Batch A: deploy/cancel state machine.

Cancellation must be signalled by an explicit `JobCancelled` exception TYPE, never
inferred from the text of an error message (a genuine failure whose message happens
to contain the word "cancel" — e.g. a VM named `cancel-svc` failing its ansible
phase — must NOT be treated as a user cancel and must NOT destroy the VM). The
post-boot ansible phase must also honour cancel and map a cancel-terminated run to
JobCancelled rather than a generic failure.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave25.py
Run (Windows):        $env:GOBLINDOCK_DEV='1'; .venv\\Scripts\\python.exe tests\\test_wave25.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave25-test.sqlite3")
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
    Connection, Deployment, IpAllocation, Job, Network)

init_db()


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _mk_conn(ssh_key_path=""):
    with session_scope() as s:
        c = Connection(name="px-w25-" + os.urandom(3).hex(), host="127.0.0.1",
                       token_id="t@pve!x", node="pve", ssh_key_path=ssh_key_path)
        s.add(c); s.flush()
        return c.id


def _mk_net(conn_id):
    with session_scope() as s:
        n = Network(connection_id=conn_id, name="lan", mode="static",
                    subnet_cidr="10.0.60.0/24", gateway="10.0.60.1",
                    range_start="10.0.60.10", range_end="10.0.60.200")
        s.add(n); s.flush()
        return n.id


def _mk_dep_with_alloc(conn_id, net_id, vmid=None, status="working"):
    with session_scope() as s:
        d = Deployment(name="d-" + os.urandom(3).hex(), connection_id=conn_id,
                       network_id=net_id, vmid=vmid, node="pve", status=status)
        s.add(d); s.flush()
        s.add(IpAllocation(network_id=net_id, ip=f"10.0.60.{20 + (d.id % 150)}",
                           deployment_id=d.id, state="reserved"))
        return d.id


def _allocs(dep_id):
    with session_scope() as s:
        return s.exec(select(IpAllocation).where(IpAllocation.deployment_id == dep_id)).all()


def _stub_px(record=None, present=()):
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


# --------------------------------------------------------------------------- #
# A1 — cancellation is a TYPE, not a string match (HIGH)                       #
# --------------------------------------------------------------------------- #
def test_failure_with_cancel_in_message_is_failed_not_canceled():
    """THE HIGH: a genuine deploy failure whose error text contains the word
    'cancel' (e.g. ansible phase of a VM named 'cancel-svc' fails) must be classified
    FAILED — the VM must NOT be destroyed and the deployment must survive in 'error'
    for inspection, exactly like any other failure."""
    cid = _mk_conn(); nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=9500, status="working")
    with session_scope() as s:
        j = Job(type="deploy", status="running", deployment_id=did, connection_id=cid)
        s.add(j); s.flush(); jid = j.id
    record = []

    def _boom(ctx, job):
        raise RuntimeError("ansible cancel-svc failed (status=failed, rc=2)")
    _run_execute_with_dispatch(jid, "deploy", _boom, px=_stub_px(record))

    with session_scope() as s:
        assert s.get(Job, jid).status == "failed", "a 'cancel'-worded FAILURE must not be a cancel"
        d = s.get(Deployment, did)
        assert d is not None and d.status == "error", "failed deploy keeps the dep in 'error'"
    assert record == [], f"a failure must NOT destroy the VM, got destroys={record}"
    print("test_failure_with_cancel_in_message_is_failed_not_canceled OK")


def test_jobcancelled_marks_canceled_and_tears_down_deploy():
    """An explicit JobCancelled from the job impl IS a user cancel: the half-built VM
    is destroyed, the IP freed and the deployment dropped."""
    cid = _mk_conn(); nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=9501, status="working")
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
    assert record == [9501], f"cancelled deploy must destroy its half-built VM, got {record}"
    print("test_jobcancelled_marks_canceled_and_tears_down_deploy OK")


# --------------------------------------------------------------------------- #
# A2 — cancel honoured in the post-boot ansible phase (MEDIUM)                 #
# --------------------------------------------------------------------------- #
def _ansible_phase_with_status(status, rc):
    """Drive _run_ansible_phase past its early-returns with the recipe machinery
    stubbed, forcing run_playbook to return (status, rc); return the raised
    exception (or None)."""
    with session_scope() as s:
        j = Job(type="deploy", status="running"); s.add(j); s.flush(); jid = j.id
    ctx = worker.JobCtx(jid)
    saved = {k: getattr(worker, k) for k in
             ("run_playbook", "has_ansible_blocks", "compile_ansible", "_blocks_by_key")}
    worker.run_playbook = lambda *a, **k: (status, rc)
    worker.has_ansible_blocks = lambda *a, **k: True
    worker.compile_ansible = lambda *a, **k: "- hosts: all\n  tasks: []"
    worker._blocks_by_key = lambda: {}
    try:
        worker._run_ansible_phase(ctx, [{"ref": "x"}], None, "10.0.60.5", "KEY", "cfg")
        return None
    except BaseException as e:  # noqa: BLE001
        return e
    finally:
        for k, v in saved.items():
            setattr(worker, k, v)


def test_ansible_phase_canceled_status_raises_jobcancelled():
    err = _ansible_phase_with_status("canceled", 0)
    assert isinstance(err, worker.JobCancelled), f"canceled run must raise JobCancelled, got {err!r}"
    print("test_ansible_phase_canceled_status_raises_jobcancelled OK")


def test_ansible_phase_failed_status_raises_runtimeerror_not_cancel():
    err = _ansible_phase_with_status("failed", 2)
    assert isinstance(err, RuntimeError) and not isinstance(err, worker.JobCancelled), \
        f"failed run must raise a plain failure, got {err!r}"
    print("test_ansible_phase_failed_status_raises_runtimeerror_not_cancel OK")


def test_run_playbook_threads_cancel_callback():
    """ansible_exec.run_playbook must pass a cancel_callback to ansible_runner that
    reflects the supplied `cancelled` predicate (so a cancel mid-run is honoured)."""
    import types
    from app import ansible_exec
    captured = {}

    fake = types.ModuleType("ansible_runner")

    class _Res:
        status = "successful"
        rc = 0

    def _run(**kw):
        captured.update(kw)
        return _Res()
    fake.run = _run

    orig = sys.modules.get("ansible_runner")
    sys.modules["ansible_runner"] = fake
    try:
        ansible_exec.run_playbook("- hosts: all", "10.0.60.5", "goblin", "KEY",
                                  cancelled=lambda: True)
        cb = captured.get("cancel_callback")
        assert callable(cb) and cb() is True, "cancel_callback must report True when cancelled"
        ansible_exec.run_playbook("- hosts: all", "10.0.60.5", "goblin", "KEY",
                                  cancelled=lambda: False)
        assert captured["cancel_callback"]() is False
        ansible_exec.run_playbook("- hosts: all", "10.0.60.5", "goblin", "KEY")  # cancelled=None
        assert captured["cancel_callback"]() is False, "no predicate → never cancelled"
    finally:
        if orig is not None:
            sys.modules["ansible_runner"] = orig
        else:
            sys.modules.pop("ansible_runner", None)
    print("test_run_playbook_threads_cancel_callback OK")


# --------------------------------------------------------------------------- #
# A3 — orphaned cloud-init snippet cleaned up on a cancelled deploy (LOW)       #
# --------------------------------------------------------------------------- #
def test_canceled_deploy_cleans_orphan_snippet():
    """A cancelled deploy destroys its half-built VM; the node-side cloud-init snippet
    (gd-deploy-<vmid>.yml — holds the root-pw hash + pubkeys) must be removed too, not
    left orphaned on the node."""
    cid = _mk_conn(ssh_key_path="/keys/id_managed")
    nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=9600, status="working")
    with session_scope() as s:
        j = Job(type="deploy", status="canceled", deployment_id=did, connection_id=cid)
        s.add(j); s.flush(); jid = j.id
    calls = []
    saved_del, saved_px = worker.delete_snippet_over_ssh, worker.Proxmox
    worker.delete_snippet_over_ssh = lambda conn, name: calls.append(name)
    worker.Proxmox = _stub_px()
    try:
        worker._reconcile_canceled_job(jid)
    finally:
        worker.delete_snippet_over_ssh, worker.Proxmox = saved_del, saved_px
    assert calls == ["gd-deploy-9600.yml"], f"canceled deploy must delete its snippet, got {calls}"
    with session_scope() as s:
        assert s.get(Deployment, did) is None, "canceled deploy still drops the deployment"
    print("test_canceled_deploy_cleans_orphan_snippet OK")


def test_snippet_cleanup_skipped_without_ssh_key():
    """No SSH key on the connection → no snippet was written, so don't attempt SSH."""
    cid = _mk_conn(ssh_key_path="")
    nid = _mk_net(cid)
    did = _mk_dep_with_alloc(cid, nid, vmid=9601, status="working")
    with session_scope() as s:
        j = Job(type="deploy", status="canceled", deployment_id=did, connection_id=cid)
        s.add(j); s.flush(); jid = j.id
    calls = []
    saved_del, saved_px = worker.delete_snippet_over_ssh, worker.Proxmox
    worker.delete_snippet_over_ssh = lambda conn, name: calls.append(name)
    worker.Proxmox = _stub_px()
    try:
        worker._reconcile_canceled_job(jid)
    finally:
        worker.delete_snippet_over_ssh, worker.Proxmox = saved_del, saved_px
    assert calls == [], f"no ssh key → must not attempt snippet delete, got {calls}"
    print("test_snippet_cleanup_skipped_without_ssh_key OK")


# --------------------------------------------------------------------------- #
# A4 — record the EFFECTIVE disk size when a grow-resize fails (LOW)           #
# --------------------------------------------------------------------------- #
def test_scsi0_size_parse():
    class _Px:
        def __init__(self, cfg): self.cfg = cfg
        def vm_config(self, vmid, node=None): return self.cfg
    assert worker._scsi0_size_gb(_Px({"scsi0": "local-lvm:vm-100-disk-0,size=20G"}), 100, "pve") == 20
    assert worker._scsi0_size_gb(_Px({"scsi0": "ssd:vm-1-disk-0,size=2048M"}), 1, "pve") == 2
    assert worker._scsi0_size_gb(_Px({}), 1, "pve") is None, "no scsi0 → unknown"

    class _Boom:
        def vm_config(self, *a, **k): raise RuntimeError("node down")
    assert worker._scsi0_size_gb(_Boom(), 1, "pve") is None, "config read failure → unknown"
    print("test_scsi0_size_parse OK")


def test_effective_disk_records_actual_on_resize_failure():
    assert worker._effective_disk_gb(True, 40, 3) == 40, "resize ok → record the grow target"
    assert worker._effective_disk_gb(False, 40, 3) == 3, "resize failed → record the ACTUAL size"
    assert worker._effective_disk_gb(False, 40, None) == 40, "actual unknown → fall back to requested"
    print("test_effective_disk_records_actual_on_resize_failure OK")


if __name__ == "__main__":
    test_failure_with_cancel_in_message_is_failed_not_canceled()
    test_jobcancelled_marks_canceled_and_tears_down_deploy()
    test_ansible_phase_canceled_status_raises_jobcancelled()
    test_ansible_phase_failed_status_raises_runtimeerror_not_cancel()
    test_run_playbook_threads_cancel_callback()
    test_canceled_deploy_cleans_orphan_snippet()
    test_snippet_cleanup_skipped_without_ssh_key()
    test_scsi0_size_parse()
    test_effective_disk_records_actual_on_resize_failure()
    print("\nALL WAVE 25 UNIT TESTS PASSED")
