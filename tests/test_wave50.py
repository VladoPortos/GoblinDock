"""Wave 50 — Proxmox recovery: disabled connections and local-only VM cleanup.

An admin can disable a Proxmox source that is offline/in maintenance/retired:
its config and VM records survive, but its VMs leave normal inventory, nothing
polls or targets it, and re-enabling reconciles without loss. Separately, a VM
deleted directly in Proxmox gets an explicit local-only cleanup path that never
sends a delete upstream and refuses when the VM demonstrably still exists.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave50.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave50-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from fastapi import HTTPException  # noqa: E402
from sqlmodel import select  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app import api, worker  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    Audit,
    Connection,
    Deployment,
    Image,
    IpAllocation,
    Job,
    Network,
    Template,
    User,
)
from app.proxmox import VM_ABSENT, VM_PRESENT  # noqa: E402

init_db()


def _request() -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/api/state",
        "headers": [], "session": {},
    })


def _expect_http(code, fn):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == code, (exc.status_code, exc.detail)
        return exc
    raise AssertionError(f"expected HTTPException {code}")


def _fixture(*, disabled=False, vmid=8600):
    suffix = os.urandom(4).hex()
    with session_scope() as s:
        admin = User(email=f"wave50-admin-{suffix}@example.com", name="admin",
                     password_hash="unused", role="admin")
        s.add(admin)
        s.flush()
        conn = Connection(name=f"wave50-conn-{suffix}", host="pve.example",
                          token_id="automation@pve!goblindock", node="pve",
                          disabled=disabled)
        image = Image(kind="base", name=f"wave50-img-{suffix}",
                      source_url="https://example.com/w50.img", build_status="ready")
        s.add(conn)
        s.add(image)
        s.flush()
        tpl = Template(name=f"wave50-tpl-{suffix}", owner_id=admin.id,
                       recipe_json="[]", base_image_id=image.id, connection_id=conn.id)
        dep = Deployment(name=f"wave50-vm-{suffix}", owner_id=admin.id,
                         connection_id=conn.id, image_id=image.id, vmid=vmid,
                         node="pve", status="stopped")
        s.add(tpl)
        s.add(dep)
        s.flush()
        return {"admin": admin.id, "conn": conn.id, "template": tpl.id,
                "dep": dep.id, "image": image.id, "suffix": suffix}


def _state(user_id):
    original = api._px_cache
    api._px_cache = lambda _conns: {}
    try:
        with session_scope() as s:
            return api.state(_request(), user=s.get(User, user_id), session=s)
    finally:
        api._px_cache = original


def test_disabled_connection_hides_vms_and_reports_disabled_status():
    fx = _fixture(disabled=True)
    state = _state(fx["admin"])
    assert all(v["depId"] != fx["dep"] for v in state["VMS"]), "hidden VM leaked"
    row = next(c for c in state["CONNECTIONS"] if c["connId"] == fx["conn"])
    assert row["disabled"] is True
    assert row["status"] == "disabled"
    # the record itself is retained — Settings still shows how many VMs it holds
    assert row["vms"] == 1


def test_reenabling_restores_inventory_without_loss():
    fx = _fixture(disabled=True)
    with session_scope() as s:
        api.edit_connection(fx["conn"], api.ConnEditBody(disabled=False),
                            user=s.get(User, fx["admin"]), session=s)
    state = _state(fx["admin"])
    assert any(v["depId"] == fx["dep"] for v in state["VMS"]), "VM did not come back"
    with session_scope() as s:
        deps = s.exec(select(Deployment).where(
            Deployment.connection_id == fx["conn"])).all()
        assert len(deps) == 1, "re-enable must neither delete nor duplicate records"
        actions = [a.action for a in s.exec(select(Audit).where(
            Audit.target_id == str(fx["conn"]))).all()]
        assert "connection.enable" in actions


def test_disabled_connection_rejects_new_operations():
    fx = _fixture(disabled=True)
    with session_scope() as s:
        admin = s.get(User, fx["admin"])
        exc = _expect_http(409, lambda: api.deploy(api.DeployBody(
            templateId=fx["template"], name=f"wave50-new-{fx['suffix']}",
        ), user=admin, session=s))
        assert "disabled" in exc.detail
        exc = _expect_http(409, lambda: api._vm_rebuild_transaction(
            fx["dep"], admin, s))
        assert "disabled" in exc.detail
        exc = _expect_http(409, lambda: api._vm_destroy_transaction(
            fx["dep"], admin, s))
        assert "Clean up (local only)" in exc.detail
        exc = _expect_http(409, lambda: api.connection_capacity(
            fx["conn"], user=admin, session=s))
        assert "disabled" in exc.detail
        exc = _expect_http(400, lambda: api._validate_template_refs(s, api.TemplateBody(
            name="x", recipe=[], connectionId=fx["conn"],
        )))
        assert "disabled" in exc.detail


def test_vm_action_rejects_disabled_connection():
    fx = _fixture(disabled=True)
    with session_scope() as s:
        admin = s.get(User, fx["admin"])
        exc = _expect_http(409, lambda: api.vm_action(
            fx["dep"], api.ActionBody(action="start"), user=admin, session=s))
        assert "disabled" in exc.detail


def test_queued_job_fails_instead_of_contacting_disabled_source():
    fx = _fixture(disabled=True)
    with session_scope() as s:
        job = Job(type="deploy", title="stranded", connection_id=fx["conn"],
                  created_by=fx["admin"], status="queued")
        s.add(job)
        s.flush()
        job_id = job.id
    assert worker._claim_next_job() is None
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == "failed"
        assert "disabled" in (job.error or "")
    assert worker._px_for_conn(fx["conn"]) is None, \
        "reconciliation must never build a client for a disabled source"


def test_cleanup_local_refuses_while_vm_demonstrably_present():
    fx = _fixture(disabled=False)
    original = api.probe_vm_presence
    api.probe_vm_presence = lambda px, vmid, node: (
        VM_PRESENT, f"VM {vmid} is present in Proxmox inventory")
    try:
        with session_scope() as s:
            exc = _expect_http(409, lambda: api._vm_cleanup_local_transaction(
                fx["dep"], s.get(User, fx["admin"]), s))
            assert "normal" in exc.detail and "Delete" in exc.detail
    finally:
        api.probe_vm_presence = original
    with session_scope() as s:
        assert s.get(Deployment, fx["dep"]) is not None


def test_cleanup_local_confirmed_absent_removes_record_and_ip():
    fx = _fixture(disabled=False)
    with session_scope() as s:
        net = Network(connection_id=fx["conn"], name=f"wave50-net-{fx['suffix']}")
        s.add(net)
        s.flush()
        s.add(IpAllocation(network_id=net.id, deployment_id=fx["dep"],
                           ip="192.0.2.50"))
    original = api.probe_vm_presence
    api.probe_vm_presence = lambda px, vmid, node: (
        VM_ABSENT, f"VM {vmid} is absent from Proxmox inventory")
    try:
        with session_scope() as s:
            out = api._vm_cleanup_local_transaction(
                fx["dep"], s.get(User, fx["admin"]), s)
    finally:
        api.probe_vm_presence = original
    assert out == {"ok": True, "verified": True}
    with session_scope() as s:
        assert s.get(Deployment, fx["dep"]) is None
        assert not s.exec(select(IpAllocation).where(
            IpAllocation.deployment_id == fx["dep"])).all()
        audit = s.exec(select(Audit).where(
            Audit.action == "vm.cleanup_local",
            Audit.target_id == str(fx["dep"])).order_by(Audit.id.desc())).first()
        assert audit is not None
        assert "Proxmox untouched" in audit.detail
        assert "absence confirmed" in audit.detail


def test_cleanup_local_on_disabled_source_is_unverified_and_contact_free():
    fx = _fixture(disabled=True)
    original = api.probe_vm_presence

    def _must_not_probe(px, vmid, node):
        raise AssertionError("a disabled source must never be probed")

    api.probe_vm_presence = _must_not_probe
    try:
        with session_scope() as s:
            out = api._vm_cleanup_local_transaction(
                fx["dep"], s.get(User, fx["admin"]), s)
    finally:
        api.probe_vm_presence = original
    assert out == {"ok": True, "verified": False}
    with session_scope() as s:
        # a freed SQLite rowid can be reused by the next fixture's deployment, so
        # always read the NEWEST cleanup audit for this id
        audit = s.exec(select(Audit).where(
            Audit.action == "vm.cleanup_local",
            Audit.target_id == str(fx["dep"])).order_by(Audit.id.desc())).first()
        assert audit is not None and "unverified" in audit.detail


def test_cleanup_local_blocked_while_lifecycle_job_active():
    fx = _fixture(disabled=False)
    with session_scope() as s:
        s.add(Job(type="rebuild", title="x", deployment_id=fx["dep"],
                  connection_id=fx["conn"], created_by=fx["admin"], status="running"))
        s.flush()
    with session_scope() as s:
        _expect_http(409, lambda: api._vm_cleanup_local_transaction(
            fx["dep"], s.get(User, fx["admin"]), s))


if __name__ == "__main__":
    test_disabled_connection_hides_vms_and_reports_disabled_status()
    test_reenabling_restores_inventory_without_loss()
    test_disabled_connection_rejects_new_operations()
    test_vm_action_rejects_disabled_connection()
    test_queued_job_fails_instead_of_contacting_disabled_source()
    test_cleanup_local_refuses_while_vm_demonstrably_present()
    test_cleanup_local_confirmed_absent_removes_record_and_ip()
    test_cleanup_local_on_disabled_source_is_unverified_and_contact_free()
    test_cleanup_local_blocked_while_lifecycle_job_active()
    print("\nALL WAVE 50 UNIT TESTS PASSED")
