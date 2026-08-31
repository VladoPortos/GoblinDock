"""Wave 36 — July 2026 security and state-invariant review fixes.

Run (Linux/WSL/CI):
  GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave36.py
"""
import os
import sys
import tempfile
import threading
import json
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave36-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault(
    "GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test")
)

from starlette.requests import Request  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import api, recipes, worker  # noqa: E402
from app import serialize as S  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import engine, init_db, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    Block, Connection, Deployment, Image, IpAllocation, Job, Network, Secret,
    Template, User, Variable,
)
from app.security import encrypt, hash_password  # noqa: E402

init_db()


def _mk_user(email: str, role: str = "user") -> int:
    with session_scope() as s:
        user = User(
            email=email,
            name=email.split("@", 1)[0],
            password_hash=hash_password("StrongPass12!"),
            role=role,
        )
        s.add(user)
        s.flush()
        return user.id


def _expect_http(code: int, fn):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == code, (exc.status_code, exc.detail)
        return exc
    raise AssertionError(f"expected HTTPException {code}")


def test_non_admin_lookup_cannot_resolve_global_values():
    uid = _mk_user("w36-user@example.com")
    with session_scope() as s:
        s.add(Secret(scope="global", name="GLOBAL_TOKEN", value_enc=encrypt("admin-secret")))
        s.add(Variable(scope="global", name="GLOBAL_VALUE", value="admin-variable"))
        s.add(Secret(scope="user", owner_id=uid, name="OWN_TOKEN", value_enc=encrypt("own-secret")))

    lookup = worker._secret_lookup_factory(uid, allow_global=False)
    assert lookup("secrets", "OWN_TOKEN") == "own-secret"
    assert lookup("secrets", "GLOBAL_TOKEN") == "", "normal users must not consume global secrets"
    assert lookup("variable", "GLOBAL_VALUE") == "", "normal users must not consume global variables"
    print("test_non_admin_lookup_cannot_resolve_global_values OK")


def test_admin_lookup_can_resolve_global_values():
    uid = _mk_user("w36-admin@example.com", role="admin")
    with session_scope() as s:
        s.add(Secret(scope="global", name="ADMIN_GLOBAL", value_enc=encrypt("allowed")))

    lookup = worker._secret_lookup_factory(uid, allow_global=True)
    assert lookup("secrets", "ADMIN_GLOBAL") == "allowed"
    print("test_admin_lookup_can_resolve_global_values OK")


def test_ownerless_lookup_cannot_resolve_global_values():
    with session_scope() as s:
        s.add(Secret(scope="global", name="OWNERLESS_GLOBAL", value_enc=encrypt("blocked")))
        s.add(Variable(scope="global", name="OWNERLESS_VARIABLE", value="blocked"))

    lookup = worker._secret_lookup_factory(None, allow_global=False)
    assert lookup("secrets", "OWNERLESS_GLOBAL") == ""
    assert lookup("variable", "OWNERLESS_VARIABLE") == ""
    print("test_ownerless_lookup_cannot_resolve_global_values OK")


def test_owner_secret_context_uses_deployment_owner_role():
    owner = _mk_user("w36-owner@example.com")
    actor = _mk_user("w36-actor@example.com", role="admin")
    assert actor != owner
    assert worker._owner_secret_context(owner) == (owner, False)
    assert worker._owner_secret_context(actor) == (actor, True)
    print("test_owner_secret_context_uses_deployment_owner_role OK")


def test_deploy_worker_uses_owner_not_admin_actor_for_secret_context():
    owner = _mk_user("w36-worker-owner@example.com")
    actor = _mk_user("w36-worker-actor@example.com", role="admin")
    with session_scope() as s:
        conn = Connection(name="w36-owner-context", host="pve", token_id="u@pve!t")
        s.add(conn)
        s.flush()
        dep = Deployment(
            name="owner-context-vm", owner_id=owner, connection_id=conn.id,
            node="pve", status="working",
        )
        s.add(dep)
        s.flush()
        job = Job(
            type="rebuild", status="running", deployment_id=dep.id,
            connection_id=conn.id, created_by=actor,
            context_json='{"src_url":"https://example.com/base.img"}',
        )
        s.add(job)
        s.flush()
        jid = job.id

    observed = []

    class StopAtOwner(RuntimeError):
        pass

    class OwnerPx:
        def __init__(self, _conn):
            pass

        def next_free_vmid(self, *_args, **_kwargs):
            return 8003

        def storage_has_volume(self, *_args, **_kwargs):
            return True

        def iso_volume_path(self, filename):
            return f"local:import/{filename}"

        def create_vm_import(self, *_args, **_kwargs):
            return "UPID:create"

        def wait_task(self, *_args, **_kwargs):
            return None

    original_px = worker.Proxmox
    original_key = worker._ssh_pubkey

    def observe_key(owner_id, *, allow_global=False):
        observed.append((owner_id, allow_global))
        raise StopAtOwner("inspection complete")

    worker.Proxmox = OwnerPx
    worker._ssh_pubkey = observe_key
    try:
        with session_scope() as s:
            job_copy = Job(**s.get(Job, jid).model_dump())
        try:
            worker._run_deploy(worker.JobCtx(jid), job_copy)
        except StopAtOwner:
            pass
        else:
            raise AssertionError("test must stop after observing owner context")
    finally:
        worker.Proxmox = original_px
        worker._ssh_pubkey = original_key

    assert observed == [(owner, False)], observed
    print("test_deploy_worker_uses_owner_not_admin_actor_for_secret_context OK")


def test_state_hides_global_secret_metadata_from_normal_users():
    uid = _mk_user("w36-state-user@example.com")
    with session_scope() as s:
        s.add(Secret(scope="global", name="HIDDEN_GLOBAL", value_enc=encrypt("x")))
        s.add(Variable(scope="global", name="HIDDEN_VARIABLE", value="x"))
        s.add(Secret(scope="user", owner_id=uid, name="VISIBLE_OWN", value_enc=encrypt("x")))

    req = Request({"type": "http", "headers": [], "session": {}})
    with session_scope() as s:
        state = api.state(request=req, user=s.get(User, uid), session=s)

    assert {row["name"] for row in state["SECRETS"]} == {"VISIBLE_OWN"}
    assert state["VARIABLES"] == []
    print("test_state_hides_global_secret_metadata_from_normal_users OK")


def test_create_call_collision_never_destroys_selected_vmid():
    uid = _mk_user("w36-collision@example.com")
    with session_scope() as s:
        conn = Connection(name="w36-collision", host="pve", token_id="u@pve!t")
        s.add(conn)
        s.flush()
        dep = Deployment(
            name="collision-vm", owner_id=uid, connection_id=conn.id,
            node="pve", status="working",
        )
        s.add(dep)
        s.flush()
        job = Job(
            type="deploy", status="running", deployment_id=dep.id,
            connection_id=conn.id, created_by=uid,
            context_json='{"src_url":"https://example.com/base.img"}',
        )
        s.add(job)
        s.flush()
        jid = job.id

    destroyed = []

    class CollisionPx:
        def __init__(self, _conn):
            pass

        def next_free_vmid(self, *_args, **_kwargs):
            return 8001

        def storage_has_volume(self, *_args, **_kwargs):
            return True

        def iso_volume_path(self, filename):
            return f"local:import/{filename}"

        def create_vm_import(self, *_args, **_kwargs):
            raise RuntimeError("VM 8001 already exists")

        def destroy(self, vmid, node=None):
            destroyed.append(vmid)

    original = worker.Proxmox
    worker.Proxmox = CollisionPx
    try:
        with session_scope() as s:
            job_copy = Job(**s.get(Job, jid).model_dump())
        try:
            worker._run_deploy(worker.JobCtx(jid), job_copy)
        except RuntimeError as exc:
            assert "already exists" in str(exc)
        else:
            raise AssertionError("the create collision must fail the deploy")
    finally:
        worker.Proxmox = original

    assert destroyed == [], f"pre-existing VMID must not be cleaned up, got {destroyed}"
    print("test_create_call_collision_never_destroys_selected_vmid OK")


def test_create_uses_connection_clamped_cpu_and_ram():
    uid = _mk_user("w36-create-limits@example.com")
    with session_scope() as s:
        conn = Connection(
            name="w36-create-limits", host="pve", token_id="u@pve!t",
            max_cores=4, max_ram_mb=8192, max_disk_gb=50,
        )
        s.add(conn)
        s.flush()
        dep = Deployment(
            name="limited-vm", owner_id=uid, connection_id=conn.id,
            node="pve", status="working",
        )
        s.add(dep)
        s.flush()
        job = Job(
            type="deploy", status="running", deployment_id=dep.id,
            connection_id=conn.id, created_by=uid,
            context_json=(
                '{"src_url":"https://example.com/base.img",'
                '"cpu":8,"ram":16,"disk":100}'
            ),
        )
        s.add(job)
        s.flush()
        jid = job.id

    observed = {}

    class StopAfterCreate(RuntimeError):
        pass

    class LimitPx:
        def __init__(self, _conn):
            pass

        def next_free_vmid(self, *_args, **_kwargs):
            return 8002

        def storage_has_volume(self, *_args, **_kwargs):
            return True

        def iso_volume_path(self, filename):
            return f"local:import/{filename}"

        def create_vm_import(self, _vmid, _name, _path, *, cores, ram_mb, node=None):
            observed.update(cores=cores, ram_mb=ram_mb)
            raise StopAfterCreate("inspection complete")

    original = worker.Proxmox
    worker.Proxmox = LimitPx
    try:
        with session_scope() as s:
            job_copy = Job(**s.get(Job, jid).model_dump())
        try:
            worker._run_deploy(worker.JobCtx(jid), job_copy)
        except StopAfterCreate:
            pass
        else:
            raise AssertionError("test stub must stop after observing create parameters")
    finally:
        worker.Proxmox = original

    assert observed == {"cores": 4, "ram_mb": 8192}, observed
    print("test_create_uses_connection_clamped_cpu_and_ram OK")


def test_worker_resource_clamp_treats_zero_as_unlimited():
    assert worker._clamp_resource(8, 0) == 8
    assert worker._clamp_resource(16384, 0) == 16384
    assert worker._clamp_resource(100, 0) == 100
    print("test_worker_resource_clamp_treats_zero_as_unlimited OK")


def test_worker_resource_clamp_honors_nonzero_limit():
    assert worker._clamp_resource(8, 4) == 4
    assert worker._clamp_resource(2048, 4096) == 2048
    assert worker._clamp_resource(0, 0) == 1
    print("test_worker_resource_clamp_honors_nonzero_limit OK")


def test_ansible_startup_exception_fails_phase():
    with session_scope() as s:
        job = Job(type="deploy", status="running")
        s.add(job)
        s.flush()
        jid = job.id

    saved = {
        name: getattr(worker, name)
        for name in ("run_playbook", "has_ansible_blocks", "compile_ansible")
    }
    worker.has_ansible_blocks = lambda *_a, **_k: True
    worker.compile_ansible = lambda *_a, **_k: "- hosts: all\n  tasks: []"
    worker.run_playbook = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("runner missing"))
    try:
        try:
            worker._run_ansible_phase(
                worker.JobCtx(jid), [{"blocks": []}], {}, None,
                "10.0.0.5", "KEY", "cfg",
            )
        except RuntimeError as exc:
            assert "ansible run failed to start" in str(exc)
        else:
            raise AssertionError("ansible startup failure must propagate")
    finally:
        for name, value in saved.items():
            setattr(worker, name, value)
    print("test_ansible_startup_exception_fails_phase OK")


def _mk_deployable_template(uid: int, *, static: bool = False) -> tuple[int, int]:
    with session_scope() as s:
        conn = Connection(name="w36-conn-" + os.urandom(3).hex(), host="pve", token_id="u@pve!t")
        image = Image(
            kind="base", name="w36-image-" + os.urandom(3).hex(),
            source_url="https://example.com/base.img", build_status="ready",
        )
        s.add(conn)
        s.add(image)
        s.flush()
        network = Network(
            connection_id=conn.id, name="w36-net-" + os.urandom(3).hex(),
            mode="static" if static else "dhcp", bridge="vmbr0",
            subnet_cidr="10.36.0.0/24" if static else "",
            gateway="10.36.0.1" if static else "",
            range_start="10.36.0.10" if static else "",
            range_end="10.36.0.10" if static else "",
        )
        s.add(network)
        s.flush()
        template = Template(
            name="w36-template-" + os.urandom(3).hex(), owner_id=uid,
            base_image_id=image.id, connection_id=conn.id, network_id=network.id,
            public=False,
        )
        s.add(template)
        s.flush()
        return template.id, network.id


def test_exhausted_static_pool_leaves_no_partial_deployment_or_job():
    uid = _mk_user("w36-pool@example.com")
    template_id, network_id = _mk_deployable_template(uid, static=True)
    with session_scope() as s:
        occupied = Deployment(name="occupied", owner_id=uid, network_id=network_id)
        s.add(occupied)
        s.flush()
        s.add(IpAllocation(network_id=network_id, ip="10.36.0.10", deployment_id=occupied.id))
        before_deps = len(s.exec(select(Deployment)).all())
        before_jobs = len(s.exec(select(Job)).all())

    with Session(engine) as s:
        try:
            api.deploy(
                api.DeployBody(templateId=template_id, name="must-rollback"),
                user=s.get(User, uid), session=s,
            )
        except HTTPException as exc:
            assert exc.status_code == 409 and "exhausted" in str(exc.detail)
        else:
            raise AssertionError("an exhausted pool must reject deployment")

    with session_scope() as s:
        assert len(s.exec(select(Deployment)).all()) == before_deps
        assert len(s.exec(select(Job)).all()) == before_jobs
        assert not s.exec(select(Deployment).where(Deployment.name == "must-rollback")).first()
    print("test_exhausted_static_pool_leaves_no_partial_deployment_or_job OK")


def _assert_bad_static_pool_admission_rolls_back(*, start, end, gateway, code):
    uid = _mk_user("w36-bad-pool-" + os.urandom(3).hex() + "@example.com")
    template_id, network_id = _mk_deployable_template(uid, static=True)
    with session_scope() as s:
        network = s.get(Network, network_id)
        network.range_start = start
        network.range_end = end
        network.gateway = gateway
        s.add(network)
        before = tuple(len(s.exec(select(model)).all()) for model in (
            Deployment, Job, IpAllocation,
        ))

    with Session(engine) as s:
        exc = _expect_http(code, lambda: api.deploy(
            api.DeployBody(templateId=template_id, name="bad-static-must-rollback"),
            user=s.get(User, uid), session=s,
        ))
        if code == 409:
            assert "exhausted" in str(exc.detail)

    with session_scope() as s:
        after = tuple(len(s.exec(select(model)).all()) for model in (
            Deployment, Job, IpAllocation,
        ))
        assert after == before, (before, after)
        assert not s.exec(select(Deployment).where(
            Deployment.name == "bad-static-must-rollback",
        )).first()


def test_incomplete_static_pool_admission_is_400_and_rolls_back_every_row():
    _assert_bad_static_pool_admission_rolls_back(
        start="10.36.0.10", end="", gateway="10.36.0.1", code=400,
    )
    print("test_incomplete_static_pool_admission_is_400_and_rolls_back_every_row OK")


def test_zero_usable_legacy_static_pool_is_409_and_rolls_back_every_row():
    _assert_bad_static_pool_admission_rolls_back(
        start="10.36.0.0", end="10.36.0.1", gateway="10.36.0.1", code=409,
    )
    print("test_zero_usable_legacy_static_pool_is_409_and_rolls_back_every_row OK")


def test_concurrent_deploy_admission_cannot_exceed_quota():
    uid = _mk_user("w36-quota@example.com")
    template_id, _network_id = _mk_deployable_template(uid)
    old_limit = settings.max_vms_per_user
    original_enforce = api._enforce_quota
    both_entered = threading.Event()
    calls_lock = threading.Lock()
    calls = 0

    def synchronized_enforce(session, user, kind):
        nonlocal calls
        with calls_lock:
            calls += 1
            if calls == 2:
                both_entered.set()
        both_entered.wait(timeout=0.5)
        return original_enforce(session, user, kind)

    def attempt(name):
        with Session(engine) as s:
            try:
                api.deploy(
                    api.DeployBody(templateId=template_id, name=name),
                    user=s.get(User, uid), session=s,
                )
                return "created"
            except HTTPException as exc:
                return exc.status_code

    settings.max_vms_per_user = 1
    api._enforce_quota = synchronized_enforce
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, ("quota-a", "quota-b")))
    finally:
        api._enforce_quota = original_enforce
        settings.max_vms_per_user = old_limit

    assert sorted(outcomes, key=str) == [429, "created"], outcomes
    with session_scope() as s:
        owned = s.exec(select(Deployment).where(Deployment.owner_id == uid)).all()
        assert len(owned) == 1, [d.name for d in owned]
    print("test_concurrent_deploy_admission_cannot_exceed_quota OK")


def test_orphan_recovery_keeps_allocations_until_absence_is_confirmed():
    with session_scope() as s:
        for index, job_type in enumerate(("deploy", "rebuild", "destroy"), start=1):
            dep = Deployment(name=f"orphan-{job_type}", status="working", vmid=8010 + index)
            s.add(dep)
            s.flush()
            s.add(IpAllocation(
                network_id=3600 + index, ip=f"10.36.1.{index}", deployment_id=dep.id,
            ))
            s.add(Job(type=job_type, status="running", deployment_id=dep.id))

    worker._recover_orphans()

    with session_scope() as s:
        remaining = set()
        for allocation in s.exec(select(IpAllocation)).all():
            dep = s.get(Deployment, allocation.deployment_id)
            if dep and dep.name.startswith("orphan-"):
                remaining.add(dep.name)
    assert remaining == {"orphan-deploy", "orphan-rebuild", "orphan-destroy"}, remaining
    print("test_orphan_recovery_keeps_allocations_until_absence_is_confirmed OK")


def test_cleanup_retry_is_throttled_and_drops_ownership_only_after_confirmed_absence():
    """Unknown cleanup stays owned, a sub-minute retry is skipped, and later absence releases it."""
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    suffix = os.urandom(3).hex()
    with session_scope() as s:
        conn = Connection(name=f"cleanup-{suffix}", host="pve", token_id="u@pve!token", node="pve")
        s.add(conn); s.flush()
        dep = Deployment(name=f"cleanup-{suffix}", connection_id=conn.id, node="pve",
                         vmid=8360, status="cleanup_pending", error="cleanup not confirmed")
        s.add(dep); s.flush()
        s.add(IpAllocation(network_id=3660, ip="10.36.6.60", deployment_id=dep.id))
        s.add(Job(type="destroy", status="canceled", deployment_id=dep.id,
                  connection_id=conn.id))
        dep_id = dep.id

    attempts = []

    class _Px:
        node = "pve"
        def __init__(self, conn): pass
        def list_qemu(self, node=None):
            with session_scope() as s:
                stamped = s.get(Deployment, dep_id).cleanup_last_attempt_at
                assert stamped is not None, "attempt timestamp must commit before Proxmox work"
                attempts.append(stamped)
            if len(attempts) == 1:
                raise RuntimeError("inventory unavailable")
            return []

    saved = worker.Proxmox
    worker.Proxmox = _Px
    try:
        worker._retry_cleanup_pending(now=now)
        with session_scope() as s:
            dep = s.get(Deployment, dep_id)
            assert dep is not None and dep.status == "cleanup_pending"
            assert len(s.exec(select(IpAllocation).where(
                IpAllocation.deployment_id == dep_id)).all()) == 1

        worker._retry_cleanup_pending(now=now + timedelta(seconds=59))
        assert len(attempts) == 1, "cleanup newer than 60 seconds must be throttled"

        worker._retry_cleanup_pending(now=now + timedelta(seconds=60))
    finally:
        worker.Proxmox = saved

    assert len(attempts) == 2
    with session_scope() as s:
        assert s.get(Deployment, dep_id) is None
        assert s.exec(select(IpAllocation).where(
            IpAllocation.deployment_id == dep_id)).all() == []


def test_cleanup_retry_stamps_each_target_immediately_before_external_work():
    """A slow first cleanup must not make a later target immediately eligible again."""
    t0 = datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc)
    suffix = os.urandom(3).hex()
    with session_scope() as s:
        conn = Connection(name=f"cleanup-order-{suffix}", host="pve",
                          token_id="u@pve!token", node="pve")
        s.add(conn); s.flush()
        first = Deployment(name=f"cleanup-first-{suffix}", connection_id=conn.id,
                           node="pve", vmid=8361, status="cleanup_pending",
                           cleanup_origin="destroy")
        second = Deployment(name=f"cleanup-second-{suffix}", connection_id=conn.id,
                            node="pve", vmid=8362, status="cleanup_pending",
                            cleanup_origin="destroy")
        s.add(first); s.add(second); s.flush()
        s.add(Job(type="destroy", status="canceled", deployment_id=first.id,
                  connection_id=conn.id))
        s.add(Job(type="destroy", status="canceled", deployment_id=second.id,
                  connection_id=conn.id))
        first_id, second_id = first.id, second.id

    calls = []

    class _Px:
        node = "pve"
        def __init__(self, conn): pass
        def list_qemu(self, node=None):
            calls.append(node)
            raise RuntimeError("inventory unavailable")

    clock = iter((t0, t0 + timedelta(seconds=61)))
    saved_px, saved_utcnow = worker.Proxmox, worker.utcnow
    worker.Proxmox = _Px
    worker.utcnow = lambda: next(clock)
    try:
        worker._retry_cleanup_pending()
        with session_scope() as s:
            first_stamp = s.get(Deployment, first_id).cleanup_last_attempt_at
            second_stamp = s.get(Deployment, second_id).cleanup_last_attempt_at
        assert first_stamp.replace(tzinfo=timezone.utc) == t0
        assert second_stamp.replace(tzinfo=timezone.utc) == t0 + timedelta(seconds=61)

        calls.clear()
        worker._retry_cleanup_pending(now=t0 + timedelta(seconds=70))
    finally:
        worker.Proxmox, worker.utcnow = saved_px, saved_utcnow

    assert calls == ["pve"], "only the first target is old enough for another attempt"


def test_rebuild_admission_uses_lifecycle_admission_lock():
    uid = _mk_user("w36-rebuild-lock@example.com")
    template_id, network_id = _mk_deployable_template(uid, static=True)
    with session_scope() as s:
        template = s.get(Template, template_id)
        dep = Deployment(
            name="legacy-static-rebuild", owner_id=uid,
            connection_id=template.connection_id, image_id=template.base_image_id,
            template_id=template.id, network_id=network_id, vmid=8009,
            status="running",
        )
        s.add(dep)
        s.flush()
        dep_id = dep.id

    entered_build = threading.Event()
    result = []
    original = api._build_job_ctx

    def observed_build(*_args, **_kwargs):
        entered_build.set()
        return "{}"

    def rebuild():
        with Session(engine) as s:
            try:
                result.append(api.vm_rebuild(dep_id, user=s.get(User, uid), session=s))
            except Exception as exc:  # captured for assertion in the parent thread
                result.append(exc)

    api._build_job_ctx = observed_build
    thread = threading.Thread(target=rebuild)
    try:
        with api._lifecycle_admission_lock:
            thread.start()
            assert not entered_build.wait(timeout=0.25), \
                "rebuild must wait behind deployment/IP admission lock"
        thread.join(timeout=3)
    finally:
        api._build_job_ctx = original
        if thread.is_alive():
            thread.join(timeout=3)

    assert entered_build.is_set(), "rebuild should proceed after the lock is released"
    assert result and isinstance(result[0], dict), result
    print("test_rebuild_admission_uses_lifecycle_admission_lock OK")


def test_rebuild_replaces_invalid_legacy_reservation_in_job_context():
    uid = _mk_user("w36-rebuild-invalid-reservation@example.com")
    template_id, network_id = _mk_deployable_template(uid, static=True)
    with session_scope() as s:
        template = s.get(Template, template_id)
        network = s.get(Network, network_id)
        network.subnet_cidr = "10.36.8.0/24"
        network.gateway = "10.36.8.1"
        network.range_start = "10.36.8.0"
        network.range_end = "10.36.8.3"
        dep = Deployment(
            name="legacy-invalid-rebuild", owner_id=uid,
            connection_id=template.connection_id, image_id=template.base_image_id,
            template_id=template.id, network_id=network.id, vmid=8036,
            status="running",
        )
        s.add(network)
        s.add(dep)
        s.flush()
        allocation = IpAllocation(
            network_id=network.id, ip="10.36.8.1",
            deployment_id=dep.id, state="reserved",
        )
        s.add(allocation)
        s.flush()
        dep_id, allocation_id = dep.id, allocation.id

    with Session(engine) as s:
        result = api.vm_rebuild(dep_id, user=s.get(User, uid), session=s)

    with session_scope() as s:
        job = s.get(Job, result["jobId"])
        ctx = json.loads(job.context_json)
        rows = s.exec(select(IpAllocation).where(
            IpAllocation.deployment_id == dep_id,
        )).all()
        assert ctx["static_ip"] == "10.36.8.2"
        assert ctx["ipconfig0"] == "ip=10.36.8.2/24,gw=10.36.8.1"
        assert [(row.id, row.ip, row.state) for row in rows] == [
            (allocation_id, "10.36.8.2", "reserved"),
        ]
    print("test_rebuild_replaces_invalid_legacy_reservation_in_job_context OK")


def _mk_lifecycle_deployment(uid: int, *, status: str = "running") -> int:
    template_id, network_id = _mk_deployable_template(uid)
    with session_scope() as s:
        template = s.get(Template, template_id)
        dep = Deployment(
            name="lifecycle-" + os.urandom(3).hex(), owner_id=uid,
            connection_id=template.connection_id, image_id=template.base_image_id,
            template_id=template.id, network_id=network_id, node="pve", vmid=8365,
            status=status,
        )
        s.add(dep)
        s.flush()
        return dep.id


def test_sequential_duplicate_destroy_returns_one_active_job():
    """A repeated delete must return the first job instead of admitting a duplicate."""
    uid = _mk_user("w36-destroy-sequential@example.com")
    dep_id = _mk_lifecycle_deployment(uid)

    results = []
    for _ in range(2):
        with Session(engine) as s:
            results.append(api.vm_destroy(dep_id, user=s.get(User, uid), session=s))

    assert results[0]["jobId"] == results[1]["jobId"], results
    with session_scope() as s:
        jobs = s.exec(select(Job).where(
            Job.deployment_id == dep_id, Job.type == "destroy",
        )).all()
        assert len(jobs) == 1, [job.id for job in jobs]
    print("test_sequential_duplicate_destroy_returns_one_active_job OK")


def test_concurrent_duplicate_destroy_returns_one_active_job():
    """Two synchronized delete requests must serialize their query-and-insert decision."""
    uid = _mk_user("w36-destroy-concurrent@example.com")
    dep_id = _mk_lifecycle_deployment(uid)
    audit_barrier = threading.Barrier(2)
    original_record_audit = api.record_audit

    def synchronized_audit(*args, **kwargs):
        try:
            audit_barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return original_record_audit(*args, **kwargs)

    def destroy():
        with Session(engine) as s:
            return api.vm_destroy(dep_id, user=s.get(User, uid), session=s)

    api.record_audit = synchronized_audit
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: destroy(), range(2)))
    finally:
        api.record_audit = original_record_audit

    assert results[0]["jobId"] == results[1]["jobId"], results
    with session_scope() as s:
        jobs = s.exec(select(Job).where(
            Job.deployment_id == dep_id, Job.type == "destroy",
        )).all()
        assert len(jobs) == 1, [job.id for job in jobs]
    print("test_concurrent_duplicate_destroy_returns_one_active_job OK")


def test_active_lifecycle_jobs_reject_conflicting_admission_for_every_live_status():
    """Queued, running, and waiting work must all block incompatible lifecycle jobs."""
    uid = _mk_user("w36-lifecycle-conflicts@example.com")
    cases = [
        ("rebuild", active_type, active_status)
        for active_type in ("deploy", "rebuild", "destroy")
        for active_status in ("queued", "running", "waiting")
    ] + [
        ("destroy", active_type, active_status)
        for active_type in ("deploy", "rebuild")
        for active_status in ("queued", "running", "waiting")
    ]

    for requested, active_type, active_status in cases:
        dep_id = _mk_lifecycle_deployment(uid)
        with session_scope() as s:
            s.add(Job(
                type=active_type, status=active_status, deployment_id=dep_id,
                connection_id=s.get(Deployment, dep_id).connection_id, created_by=uid,
            ))
        with Session(engine) as s:
            before = len(s.exec(select(Job).where(Job.deployment_id == dep_id)).all())
            if requested == "rebuild":
                call = lambda: api.vm_rebuild(dep_id, user=s.get(User, uid), session=s)
            else:
                call = lambda: api.vm_destroy(dep_id, user=s.get(User, uid), session=s)
            exc = _expect_http(409, call)
            assert active_type in str(exc.detail), (requested, active_type, active_status, exc.detail)
            after = len(s.exec(select(Job).where(Job.deployment_id == dep_id)).all())
            assert after == before, (requested, active_type, active_status, before, after)
    print("test_active_lifecycle_jobs_reject_conflicting_admission_for_every_live_status OK")


def test_active_lifecycle_jobs_reject_direct_actions_before_proxmox_prerequisites():
    """Every active lifecycle type/status must block every direct power action first."""
    uid = _mk_user("w36-direct-lifecycle-conflicts@example.com")
    saved_proxmox = api.Proxmox
    api.Proxmox = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("active lifecycle rejection must precede Proxmox access")
    )
    try:
        for action in ("start", "stop", "restart"):
            for active_type in ("deploy", "rebuild", "destroy"):
                for active_status in ("queued", "running", "waiting"):
                    dep_id = _mk_lifecycle_deployment(uid)
                    with session_scope() as s:
                        dep = s.get(Deployment, dep_id)
                        # The lifecycle guard must win before VMID/connection admission.
                        dep.vmid = None
                        dep.connection_id = None
                        s.add(dep)
                        s.add(Job(
                            type=active_type, status=active_status,
                            deployment_id=dep_id, created_by=uid,
                        ))
                    with Session(engine) as s:
                        exc = _expect_http(409, lambda: api.vm_action(
                            dep_id, api.ActionBody(action=action),
                            user=s.get(User, uid), session=s,
                        ))
                        assert active_type in str(exc.detail), (
                            action, active_type, active_status, exc.detail,
                        )
    finally:
        api.Proxmox = saved_proxmox
    print("test_active_lifecycle_jobs_reject_direct_actions_before_proxmox_prerequisites OK")


def test_cleanup_pending_rejects_every_vm_lifecycle_operation():
    """An ambiguously owned VM must not accept direct, rebuild, or destroy operations."""
    uid = _mk_user("w36-cleanup-lifecycle@example.com")
    dep_id = _mk_lifecycle_deployment(uid, status="cleanup_pending")
    original_proxmox = api.Proxmox
    api.Proxmox = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("cleanup-pending lifecycle rejection must precede Proxmox access")
    )
    try:
        for action in ("start", "stop", "restart"):
            with Session(engine) as s:
                exc = _expect_http(409, lambda: api.vm_action(
                    dep_id, api.ActionBody(action=action),
                    user=s.get(User, uid), session=s,
                ))
                assert "cleanup" in str(exc.detail).lower(), (action, exc.detail)
        for operation in (api.vm_rebuild, api.vm_destroy):
            with Session(engine) as s:
                exc = _expect_http(409, lambda: operation(
                    dep_id, user=s.get(User, uid), session=s,
                ))
                assert "cleanup" in str(exc.detail).lower(), (operation.__name__, exc.detail)
    finally:
        api.Proxmox = original_proxmox

    with session_scope() as s:
        assert s.exec(select(Job).where(Job.deployment_id == dep_id)).all() == []
    print("test_cleanup_pending_rejects_every_vm_lifecycle_operation OK")


def test_template_write_rejects_malformed_recipe_shape():
    uid = _mk_user("w36-malformed@example.com")
    with session_scope() as s:
        _expect_http(400, lambda: api.save_template(
            api.TemplateBody(name="malformed", recipe=[None]),
            user=s.get(User, uid), session=s,
        ))
    print("test_template_write_rejects_malformed_recipe_shape OK")


def test_template_write_rejects_private_block_from_another_user():
    owner = _mk_user("w36-block-owner@example.com")
    other = _mk_user("w36-block-other@example.com")
    with session_scope() as s:
        block = Block(
            key="c-w36-private", kind="custom", builtin=False, owner_id=owner,
            name="private", phase="cloudinit", cloudinit_template="echo ok",
        )
        s.add(block)
    with session_scope() as s:
        _expect_http(400, lambda: api.save_template(
            api.TemplateBody(
                name="forbidden-ref",
                recipe=[{"blocks": [{"ref": "c-w36-private", "inputs": {}}]}],
            ),
            user=s.get(User, other), session=s,
        ))
    print("test_template_write_rejects_private_block_from_another_user OK")


def test_template_write_rejects_non_object_inputs():
    uid = _mk_user("w36-input-shape@example.com")
    with session_scope() as s:
        s.add(Block(
            key="c-w36-inputs", kind="custom", builtin=False, owner_id=uid,
            name="inputs", phase="cloudinit", cloudinit_template="echo ok",
        ))
    with session_scope() as s:
        _expect_http(400, lambda: api.save_template(
            api.TemplateBody(
                name="bad-inputs",
                recipe=[{"blocks": [{"ref": "c-w36-inputs", "inputs": ["bad"]}]}],
            ),
            user=s.get(User, uid), session=s,
        ))
    print("test_template_write_rejects_non_object_inputs OK")


def test_legacy_non_object_inputs_compile_as_empty():
    block = Block(
        key="c-w36-legacy-inputs", name="legacy inputs", phase="cloudinit",
        cloudinit_template="echo {value}",
        input_schema_json='[{"name":"value","default":"safe"}]',
    )
    compiled = recipes.compile_cloudinit(
        [{"blocks": [{"ref": block.key, "inputs": ["bad"]}]}],
        {block.key: block}, lambda _ns, _name: "",
    )
    assert compiled[-1] == "echo safe", compiled
    print("test_legacy_non_object_inputs_compile_as_empty OK")


def test_legacy_non_object_inputs_fail_deploy_validation_cleanly():
    uid = _mk_user("w36-legacy-deploy-inputs@example.com")
    with session_scope() as s:
        block = Block(
            key="c-w36-legacy-deploy", kind="custom", builtin=False, owner_id=uid,
            name="legacy deploy", phase="cloudinit", cloudinit_template="echo {value}",
            input_schema_json='[{"name":"value","type":"text"}]',
        )
        s.add(block)
        template = Template(
            name="legacy-deploy-inputs", owner_id=uid,
            recipe_json=json.dumps([{"blocks": [{
                "ref": block.key, "ask": ["value"], "inputs": ["bad"],
            }]}]),
        )
        s.add(template)
        s.flush()
        _expect_http(400, lambda: api._validate_deploy_inputs(s, template, {}))
    print("test_legacy_non_object_inputs_fail_deploy_validation_cleanly OK")


def test_compile_preview_normalizes_non_object_inputs():
    uid = _mk_user("w36-preview-inputs@example.com")
    with session_scope() as s:
        block = Block(
            key="c-w36-preview", kind="custom", builtin=False, owner_id=uid,
            name="preview", phase="ansible",
            ansible_template="- name: preview\n  ansible.builtin.debug: {msg: {password_yamlq}}",
            input_schema_json='[{"name":"password","type":"password"}]',
        )
        s.add(block)
    with session_scope() as s:
        result = api.compile_template(
            api.CompileBody(recipe=[{"blocks": [{
                "ref": "c-w36-preview", "inputs": ["bad"],
            }]}]),
            user=s.get(User, uid), session=s,
        )
    assert "yaml" in result and "tasks:" in result["yaml"]
    print("test_compile_preview_normalizes_non_object_inputs OK")


def test_legacy_malformed_recipe_serializes_without_crashing():
    uid = _mk_user("w36-legacy-recipe@example.com")
    with session_scope() as s:
        template = Template(
            name="legacy-malformed", owner_id=uid, public=False,
            recipe_json='[null, {"blocks": [null]}]',
        )
        s.add(template)
        s.flush()
        result = S.template_dict(s, template, viewer=s.get(User, uid))
    assert result["blocks"] == []
    print("test_legacy_malformed_recipe_serializes_without_crashing OK")


def _mk_reference_fixture(uid: int, *, block_key: str | None = None):
    with session_scope() as s:
        conn = Connection(name="w36-ref-" + os.urandom(3).hex(), host="pve", token_id="u@pve!t")
        image = Image(
            kind="base", name="w36-ref-image-" + os.urandom(3).hex(),
            source_url="https://example.com/ref.img", build_status="ready",
        )
        s.add(conn)
        s.add(image)
        s.flush()
        network = Network(connection_id=conn.id, name="w36-ref-net-" + os.urandom(3).hex())
        s.add(network)
        s.flush()
        recipe = [{"blocks": [{"ref": block_key, "inputs": {}}]}] if block_key else []
        template = Template(
            name="w36-ref-template-" + os.urandom(3).hex(), owner_id=uid,
            base_image_id=image.id, connection_id=conn.id, network_id=network.id,
            recipe_json=json.dumps(recipe),
        )
        s.add(template)
        s.flush()
        return template.id, image.id, conn.id, network.id


def test_delete_template_rejects_live_deployment_reference():
    uid = _mk_user("w36-delete-template@example.com", role="admin")
    template_id, image_id, conn_id, network_id = _mk_reference_fixture(uid)
    with session_scope() as s:
        s.add(Deployment(
            name="uses-template", owner_id=uid, template_id=template_id,
            image_id=image_id, connection_id=conn_id, network_id=network_id,
        ))
    with session_scope() as s:
        _expect_http(409, lambda: api.delete_template_ep(
            template_id, user=s.get(User, uid), session=s,
        ))
    print("test_delete_template_rejects_live_deployment_reference OK")


def test_delete_block_rejects_template_reference():
    uid = _mk_user("w36-delete-block@example.com", role="admin")
    with session_scope() as s:
        block = Block(
            key="c-w36-used", kind="custom", builtin=False, owner_id=uid,
            name="used", phase="cloudinit", cloudinit_template="echo ok",
        )
        s.add(block)
    _mk_reference_fixture(uid, block_key="c-w36-used")
    with session_scope() as s:
        _expect_http(409, lambda: api.delete_block(
            "c-w36-used", user=s.get(User, uid), session=s,
        ))
    print("test_delete_block_rejects_template_reference OK")


def test_delete_image_rejects_template_reference():
    uid = _mk_user("w36-delete-image@example.com", role="admin")
    _template_id, image_id, _conn_id, _network_id = _mk_reference_fixture(uid)
    with session_scope() as s:
        _expect_http(409, lambda: api.delete_image(
            image_id, user=s.get(User, uid), session=s,
        ))
    print("test_delete_image_rejects_template_reference OK")


def test_delete_connection_rejects_template_reference():
    uid = _mk_user("w36-delete-connection@example.com", role="admin")
    _template_id, _image_id, conn_id, _network_id = _mk_reference_fixture(uid)
    with session_scope() as s:
        _expect_http(409, lambda: api.delete_connection(
            conn_id, user=s.get(User, uid), session=s,
        ))
    print("test_delete_connection_rejects_template_reference OK")


def test_delete_connection_rejects_active_job_reference():
    uid = _mk_user("w36-delete-connection-job@example.com", role="admin")
    template_id, _image_id, conn_id, _network_id = _mk_reference_fixture(uid)
    with session_scope() as s:
        s.delete(s.get(Template, template_id))
        s.add(Job(type="image_sync", status="queued", connection_id=conn_id, created_by=uid))
    with session_scope() as s:
        _expect_http(409, lambda: api.delete_connection(
            conn_id, user=s.get(User, uid), session=s,
        ))
    print("test_delete_connection_rejects_active_job_reference OK")


def test_delete_network_rejects_template_reference():
    uid = _mk_user("w36-delete-network@example.com", role="admin")
    _template_id, _image_id, _conn_id, network_id = _mk_reference_fixture(uid)
    with session_scope() as s:
        _expect_http(409, lambda: api.delete_network(
            network_id, user=s.get(User, uid), session=s,
        ))
    print("test_delete_network_rejects_template_reference OK")


if __name__ == "__main__":
    test_non_admin_lookup_cannot_resolve_global_values()
    test_admin_lookup_can_resolve_global_values()
    test_ownerless_lookup_cannot_resolve_global_values()
    test_owner_secret_context_uses_deployment_owner_role()
    test_deploy_worker_uses_owner_not_admin_actor_for_secret_context()
    test_state_hides_global_secret_metadata_from_normal_users()
    test_create_call_collision_never_destroys_selected_vmid()
    test_create_uses_connection_clamped_cpu_and_ram()
    test_worker_resource_clamp_treats_zero_as_unlimited()
    test_worker_resource_clamp_honors_nonzero_limit()
    test_ansible_startup_exception_fails_phase()
    test_exhausted_static_pool_leaves_no_partial_deployment_or_job()
    test_incomplete_static_pool_admission_is_400_and_rolls_back_every_row()
    test_zero_usable_legacy_static_pool_is_409_and_rolls_back_every_row()
    test_concurrent_deploy_admission_cannot_exceed_quota()
    test_orphan_recovery_keeps_allocations_until_absence_is_confirmed()
    test_cleanup_retry_is_throttled_and_drops_ownership_only_after_confirmed_absence()
    test_cleanup_retry_stamps_each_target_immediately_before_external_work()
    test_rebuild_admission_uses_lifecycle_admission_lock()
    test_rebuild_replaces_invalid_legacy_reservation_in_job_context()
    test_sequential_duplicate_destroy_returns_one_active_job()
    test_concurrent_duplicate_destroy_returns_one_active_job()
    test_active_lifecycle_jobs_reject_conflicting_admission_for_every_live_status()
    test_active_lifecycle_jobs_reject_direct_actions_before_proxmox_prerequisites()
    test_cleanup_pending_rejects_every_vm_lifecycle_operation()
    test_template_write_rejects_malformed_recipe_shape()
    test_template_write_rejects_private_block_from_another_user()
    test_template_write_rejects_non_object_inputs()
    test_legacy_non_object_inputs_compile_as_empty()
    test_legacy_non_object_inputs_fail_deploy_validation_cleanly()
    test_compile_preview_normalizes_non_object_inputs()
    test_legacy_malformed_recipe_serializes_without_crashing()
    test_delete_template_rejects_live_deployment_reference()
    test_delete_block_rejects_template_reference()
    test_delete_image_rejects_template_reference()
    test_delete_connection_rejects_template_reference()
    test_delete_connection_rejects_active_job_reference()
    test_delete_network_rejects_template_reference()
    print("\nALL WAVE 36 UNIT TESTS PASSED")
