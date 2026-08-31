"""Wave 37 — immutable encrypted deployment execution plans.

Run (Windows): $env:GOBLINDOCK_DEV='1'; .venv\\Scripts\\python.exe tests\\test_wave37.py
"""
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave37-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from fastapi import HTTPException  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

from app import api, appsettings, execution_plan, worker  # noqa: E402
from app.db import _migrate, engine, init_db, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    Block, Connection, Deployment, Image, IpAllocation, Job, Network, Template,
    User, utcnow,
)
from app.security import hash_password  # noqa: E402
from app import serialize as S  # noqa: E402
from app.proxmox import Proxmox, ProxmoxError  # noqa: E402

init_db()


def _mk_plan_fixture(command: str) -> tuple[int, int, str]:
    suffix = os.urandom(3).hex()
    with session_scope() as s:
        user = User(
            email=f"wave37-{suffix}@example.com", name="wave37",
            password_hash=hash_password("StrongPass12!"),
        )
        conn = Connection(name=f"w37-conn-{suffix}", host="pve", token_id="u@pve!token")
        image = Image(
            kind="base", name=f"w37-image-{suffix}", source_url="https://example.com/base.img",
            build_status="ready",
        )
        s.add(user)
        s.add(conn)
        s.add(image)
        s.flush()
        net = Network(connection_id=conn.id, name=f"w37-net-{suffix}")
        s.add(net)
        block_key = f"c-wave37-plan-{suffix}"
        s.add(Block(
            key=block_key, kind="custom", builtin=False, owner_id=user.id,
            name="mutable block", phase="ansible",
            input_schema_json='[{"name":"command"},{"name":"hostname"}]',
            ansible_template=f"- name: {command}\\n  ansible.builtin.debug: {{ msg: {{{{ command }}}} }}",
        ))
        s.flush()
        template = Template(
            name=f"w37-template-{suffix}", owner_id=user.id, public=False,
            base_image_id=image.id, connection_id=conn.id, network_id=net.id,
            recipe_json=json.dumps([{"blocks": [{
                "ref": block_key, "ask": ["hostname"],
                "inputs": {"command": command},
            }]}]),
        )
        s.add(template)
        s.flush()
        return user.id, template.id, block_key


def _deploy(template_id: int, user_id: int, deploy_inputs: dict) -> dict:
    with Session(engine) as s:
        return api.deploy(
            api.DeployBody(templateId=template_id, name="wave37-vm", deployInputs=deploy_inputs),
            user=s.get(User, user_id), session=s,
        )


def _load_materialized_job_plan(job_id: int):
    with session_scope() as s:
        job = s.get(Job, job_id)
        return execution_plan.materialize_execution_plan(
            execution_plan.open_execution_plan(job.execution_plan_enc)
        )


def test_execution_plan_is_encrypted_and_immutable():
    """Changing live template/block rows must not change an admitted job's commands."""
    uid, template_id, block_key = _mk_plan_fixture(command="old-command")
    result = _deploy(template_id, uid, {"0.0": {"hostname": "accepted-host"}})
    with session_scope() as s:
        job = s.get(Job, result["jobId"])
        assert job.execution_plan_enc
        assert "old-command" not in job.execution_plan_enc
        plan = execution_plan.open_execution_plan(job.execution_plan_enc)
        assert plan["recipe"][0]["blocks"][0]["inputs"]["command"] == "old-command"
        assert plan["deploy_inputs"] == {"0.0": {"hostname": "accepted-host"}}
        s.get(Template, template_id).recipe_json = '[{"blocks":[]}]'
        s.exec(select(Block).where(Block.key == block_key)).one().ansible_template = "changed"
    recipe, blocks = _load_materialized_job_plan(result["jobId"])
    assert recipe[0]["blocks"][0]["inputs"]["hostname"] == "accepted-host"
    assert "old-command" in blocks[block_key].ansible_template


def test_execution_plan_rejects_malformed_ciphertext():
    """A corrupted plan must fail closed rather than produce a partial execution."""
    try:
        execution_plan.open_execution_plan("not-a-valid-token")
    except ValueError as exc:
        assert str(exc) == "invalid execution plan"
    else:
        raise AssertionError("malformed execution plans must be rejected")


def test_job_detail_does_not_disclose_execution_plan_or_captured_command():
    """Job responses must not reveal the encrypted snapshot or its command content."""
    uid, template_id, _ = _mk_plan_fixture(command="private-command")
    result = _deploy(template_id, uid, {"0.0": {"hostname": "detail-host"}})
    with session_scope() as s:
        detail = S.job_detail(s, s.get(Job, result["jobId"]), viewer=s.get(User, uid))
    rendered = json.dumps(detail)
    assert "execution_plan_enc" not in detail
    assert "private-command" not in rendered


def test_cleanup_pending_serialization_preserves_owned_identity_and_error():
    """Cleanup ownership must stay visible and must not be overwritten by a live status probe."""
    suffix = os.urandom(3).hex()
    with session_scope() as s:
        user = User(email=f"cleanup-{suffix}@example.com", name="cleanup",
                    password_hash=hash_password("StrongPass12!"))
        conn = Connection(name=f"cleanup-{suffix}", host="pve", token_id="u@pve!token", node="pve")
        s.add(user); s.add(conn); s.flush()
        dep = Deployment(name=f"cleanup-{suffix}", owner_id=user.id, connection_id=conn.id,
                         node="pve", vmid=8370, ip="10.37.0.70", status="cleanup_pending",
                         error="cleanup not confirmed")
        s.add(dep); s.flush()

        class _Px:
            def vm_current(self, vmid, node):
                raise AssertionError("cleanup_pending must not be live-probed")

        out = S.vm_dict(s, dep, user, {conn.id: _Px()}, {user.id: user}, {conn.id: conn},
                        active_jobs={})

    assert out["status"] == "cleanup_pending"
    assert out["vmid"] == 8370
    assert out["ip"] == "10.37.0.70"
    assert out["err"] == "cleanup not confirmed"


def test_cleanup_pending_serialization_uses_owned_static_allocation_when_ip_is_empty():
    """Canceled deploy ownership must expose its reserved IP before post-boot sets Deployment.ip."""
    suffix = os.urandom(3).hex()
    with session_scope() as s:
        user = User(email=f"cleanup-ip-{suffix}@example.com", name="cleanup-ip",
                    password_hash=hash_password("StrongPass12!"))
        conn = Connection(name=f"cleanup-ip-{suffix}", host="pve",
                          token_id="u@pve!token", node="pve")
        s.add(user); s.add(conn); s.flush()
        network = Network(connection_id=conn.id, name=f"cleanup-ip-{suffix}", mode="static")
        s.add(network); s.flush()
        dep = Deployment(name=f"cleanup-ip-{suffix}", owner_id=user.id,
                         connection_id=conn.id, network_id=network.id, node="pve",
                         vmid=8373, ip="", status="cleanup_pending",
                         cleanup_origin="deploy", error="cleanup not confirmed")
        s.add(dep); s.flush()
        s.add(IpAllocation(network_id=network.id, ip="10.37.0.73",
                           deployment_id=dep.id, state="reserved"))
        s.flush()
        out = S.vm_dict(s, dep, user, {}, {user.id: user}, {conn.id: conn}, active_jobs={})

    assert out["ip"] == "10.37.0.73"


def test_cleanup_origin_migration_is_additive_and_idempotent():
    """An upgraded deployments table gains nullable cleanup provenance exactly once."""
    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(deployments)")}
        if "cleanup_origin" in columns:
            conn.exec_driver_sql("ALTER TABLE deployments DROP COLUMN cleanup_origin")
    _migrate()
    _migrate()
    with engine.begin() as conn:
        rows = {row[1]: row for row in conn.exec_driver_sql("PRAGMA table_info(deployments)")}
    assert "cleanup_origin" in rows
    assert rows["cleanup_origin"][2].upper() == "TEXT"
    assert rows["cleanup_origin"][3] == 0, "cleanup_origin must remain nullable"


def test_cleanup_retry_uses_persisted_origin_after_job_retention_prunes_history():
    """Cleanup behavior must survive permanent deletion of the canceled source job."""
    suffix = os.urandom(3).hex()
    old = utcnow() - timedelta(days=10)
    with session_scope() as s:
        conn = Connection(name=f"cleanup-origin-{suffix}", host="pve",
                          token_id="u@pve!token", node="pve")
        s.add(conn); s.flush()
        deploy_dep = Deployment(name=f"origin-deploy-{suffix}", connection_id=conn.id,
                                node="pve", vmid=8371, status="cleanup_pending",
                                cleanup_origin="deploy")
        destroy_dep = Deployment(name=f"origin-destroy-{suffix}", connection_id=conn.id,
                                 node="pve", vmid=8372, status="cleanup_pending",
                                 cleanup_origin="destroy")
        s.add(deploy_dep); s.add(destroy_dep); s.flush()
        deploy_job = Job(type="deploy", status="canceled", deployment_id=deploy_dep.id,
                         connection_id=conn.id, finished_at=old)
        destroy_job = Job(type="destroy", status="canceled", deployment_id=destroy_dep.id,
                          connection_id=conn.id, finished_at=old)
        s.add(deploy_job); s.add(destroy_job); s.flush()
        deploy_dep_id, destroy_dep_id = deploy_dep.id, destroy_dep.id
        deploy_job_id, destroy_job_id = deploy_job.id, destroy_job.id

    saved_retention = appsettings.get_job_retention_days
    appsettings.get_job_retention_days = lambda: 1
    try:
        assert api.prune_old_jobs() >= 2
    finally:
        appsettings.get_job_retention_days = saved_retention
    with session_scope() as s:
        assert s.get(Job, deploy_job_id) is None
        assert s.get(Job, destroy_job_id) is None

    destroys = []

    class _Px:
        node = "pve"
        def __init__(self, conn): pass
        def destroy(self, vmid, node=None):
            destroys.append(vmid)
            return "UPID:destroy"
        def wait_task(self, *args, **kwargs): return None
        def list_qemu(self, node=None): return [{"vmid": 8371}, {"vmid": 8372}]

    saved_px = worker.Proxmox
    worker.Proxmox = _Px
    try:
        worker._retry_cleanup_pending(now=utcnow())
    finally:
        worker.Proxmox = saved_px

    assert destroys == [8371]
    with session_scope() as s:
        deploy_dep = s.get(Deployment, deploy_dep_id)
        destroy_dep = s.get(Deployment, destroy_dep_id)
        assert deploy_dep.status == "cleanup_pending"
        assert deploy_dep.cleanup_origin == "deploy"
        assert destroy_dep.status == "stopped"
        assert destroy_dep.cleanup_origin is None


def test_legacy_queued_job_persists_execution_plan_once():
    """A pre-snapshot queued job must be upgraded to a persisted plan before execution."""
    uid, template_id, block_key = _mk_plan_fixture(command="legacy-command")
    with session_scope() as s:
        template = s.get(Template, template_id)
        deployment = Deployment(
            name="legacy-plan-vm", owner_id=uid, template_id=template.id,
            connection_id=template.connection_id, image_id=template.base_image_id,
            network_id=template.network_id,
            deploy_inputs_json='{"0.0":{"hostname":"legacy-host"}}',
        )
        s.add(deployment)
        s.flush()
        job = Job(
            type="deploy", status="queued", deployment_id=deployment.id,
            connection_id=deployment.connection_id, created_by=uid,
        )
        s.add(job)
        s.flush()
        job_copy = Job(**job.model_dump())
        deployment_copy = Deployment(**deployment.model_dump())
        job_id = job.id

    _plan, recipe, blocks = worker._load_materialized_job_plan(job_copy, deployment_copy)
    assert recipe[0]["blocks"][0]["inputs"]["hostname"] == "legacy-host"
    assert "legacy-command" in blocks[block_key].ansible_template
    with session_scope() as s:
        persisted = s.get(Job, job_id).execution_plan_enc
    assert persisted

    worker._load_materialized_job_plan(job_copy, deployment_copy)
    with session_scope() as s:
        assert s.get(Job, job_id).execution_plan_enc == persisted


# --------------------------------------------------------------------------- #
# Deployment cloud-init preflight                                             #
# --------------------------------------------------------------------------- #
def _mk_worker_job(*, recipe: list[dict], blocks: dict[str, Block], ssh_key_path: str) -> tuple[int, object]:
    """Store a detached execution-plan-shaped deploy job for worker boundary tests."""
    suffix = os.urandom(3).hex()
    with session_scope() as s:
        user = User(
            email=f"wave37-worker-{suffix}@example.com", name="wave37-worker",
            password_hash=hash_password("StrongPass12!"),
        )
        conn = Connection(
            name=f"w37-worker-conn-{suffix}", host="pve", token_id="u@pve!token",
            node="pve", ssh_key_path=ssh_key_path,
        )
        dep = Deployment(
            name=f"w37-worker-vm-{suffix}", owner_id=None, connection_id=None,
            node="pve", status="working",
        )
        s.add(user)
        s.add(conn)
        s.flush()
        user_id = user.id
        dep.owner_id = user.id
        dep.connection_id = conn.id
        s.add(dep)
        s.flush()
        job = Job(
            type="deploy", status="running", deployment_id=dep.id,
            connection_id=conn.id, created_by=user.id,
            context_json='{"src_url":"https://example.com/base.img"}',
        )
        s.add(job)
        s.flush()
        job_id = job.id

    original = worker._load_materialized_job_plan
    worker._load_materialized_job_plan = lambda _job, _dep: (
        {"owner_id": user_id}, recipe, blocks,
    )
    return job_id, original


def _cloudinit_recipe() -> tuple[list[dict], dict[str, Block]]:
    block = Block(
        key="w37-cloudinit", name="first boot", phase="cloudinit",
        input_schema_json="[]", cloudinit_template="echo ready",
    )
    return [{"blocks": [{"ref": "w37-cloudinit", "inputs": {}}]}], {block.key: block}


def _ansible_recipe() -> tuple[list[dict], dict[str, Block]]:
    block = Block(
        key="w37-ansible", name="post boot", phase="ansible",
        input_schema_json="[]", ansible_template="- debug: msg=ready",
    )
    return [{"blocks": [{"ref": "w37-ansible", "inputs": {}}]}], {block.key: block}


@contextmanager
def _fake_proxmox(calls: list[str], *, native_params: list[dict] | None = None):
    class FakeProxmox:
        def __init__(self, _conn):
            self.node = "pve"

        def next_free_vmid(self, *_args, **_kwargs):
            return 8501

        def storage_has_volume(self, *_args, **_kwargs):
            return True

        def iso_volume_path(self, filename):
            return f"local:import/{filename}"

        def validate_snippet_volume(self, _volid, node=None):
            calls.append("validate")

        def create_vm_import(self, *_args, **_kwargs):
            calls.append("create")
            if native_params is not None:
                return "UPID:create"
            raise RuntimeError("stop after create")

        def wait_task(self, *_args, **_kwargs):
            return None

        def set_config(self, *_args, **params):
            if native_params is not None:
                native_params.append(params)
            raise RuntimeError("stop after native config")

    saved = worker.Proxmox
    worker.Proxmox = FakeProxmox
    try:
        yield
    finally:
        worker.Proxmox = saved


@contextmanager
def _patched_worker(**replacements):
    saved = {name: getattr(worker, name) for name in replacements}
    try:
        for name, value in replacements.items():
            setattr(worker, name, value)
        yield
    finally:
        for name, value in saved.items():
            setattr(worker, name, value)


def _run_worker_job(job_id: int):
    with session_scope() as s:
        job = Job(**s.get(Job, job_id).model_dump())
    worker._run_deploy(worker.JobCtx(job_id), job)


def test_recipe_without_ssh_key_fails_before_vm_creation():
    """An Ansible-only admitted recipe still needs the managed-key snippet channel."""
    recipe, blocks = _ansible_recipe()
    job_id, original_plan_loader = _mk_worker_job(recipe=recipe, blocks=blocks, ssh_key_path="")
    calls = []
    try:
        with _fake_proxmox(calls):
            try:
                _run_worker_job(job_id)
            except ProxmoxError as exc:
                assert "SSH key" in str(exc)
            else:
                raise AssertionError("a recipe deployment without a snippet key must fail")
    finally:
        worker._load_materialized_job_plan = original_plan_loader
    assert calls == [], f"required cloud-init failure must precede create, got {calls}"


def test_recipe_free_deployment_uses_native_ciuser_without_snippet():
    """A plain VM remains deployable without the optional SSH snippet channel."""
    job_id, original_plan_loader = _mk_worker_job(recipe=[], blocks={}, ssh_key_path="")
    calls, params = [], []
    try:
        with _fake_proxmox(calls, native_params=params), _patched_worker(
            auto_root_password_enabled=lambda: False,
            _managed_keypair=lambda: ("managed-private", "ssh-ed25519 managed"),
            _ssh_pubkey=lambda *_args, **_kwargs: "",
        ):
            try:
                _run_worker_job(job_id)
            except RuntimeError as exc:
                assert str(exc) == "stop after native config"
            else:
                raise AssertionError("test double must stop after native cloud-init config")
    finally:
        worker._load_materialized_job_plan = original_plan_loader
    assert calls == ["create"], calls
    assert params and params[0]["ciuser"] == "goblin", params


def test_required_snippet_upload_validate_then_create():
    """A required first-boot recipe is uploaded and visible before VM submission."""
    recipe, blocks = _cloudinit_recipe()
    job_id, original_plan_loader = _mk_worker_job(
        recipe=recipe, blocks=blocks, ssh_key_path="/keys/id_managed",
    )
    calls = []
    try:
        with _fake_proxmox(calls), _patched_worker(
            write_snippet_over_ssh=lambda *_args: calls.append("upload") or "local:snippets/gd-deploy-8501.yml",
            delete_snippet_over_ssh=lambda *_args: calls.append("delete"),
            auto_root_password_enabled=lambda: False,
            _managed_keypair=lambda: ("managed-private", "ssh-ed25519 managed"),
            _ssh_pubkey=lambda *_args, **_kwargs: "",
        ):
            try:
                _run_worker_job(job_id)
            except RuntimeError as exc:
                assert str(exc) == "stop after create"
            else:
                raise AssertionError("test double must stop at VM submission")
    finally:
        worker._load_materialized_job_plan = original_plan_loader
    assert calls[:3] == ["upload", "validate", "create"], calls


def test_validate_snippet_volume_requires_visible_snippet_on_enabled_storage():
    """A successful upload is not usable until the configured store lists that volume."""
    calls = []

    class Content:
        def get(self, **kwargs):
            calls.append(("content", kwargs))
            return [{"volid": "local:snippets/gd-deploy-8501.yml"}]

    class Storage:
        content = Content()

        def get(self):
            calls.append(("stores", {}))
            return [{"storage": "local", "content": "images, snippets", "active": 1}]

        def __call__(self, _store):
            return self

    class Node:
        storage = Storage()

    class Nodes:
        def __call__(self, _node):
            return Node()

    px = object.__new__(Proxmox)
    px.snippet_storage = "local"
    px.api = type("Api", (), {"nodes": Nodes()})()
    assert px.validate_snippet_volume("local:snippets/gd-deploy-8501.yml", node="pve") is None
    assert calls == [("stores", {}), ("content", {"content": "snippets"})], calls


def test_validate_snippet_volume_rejects_inactive_storage():
    """A disabled store must not be treated as a delivery target even if it lists snippets."""
    class Content:
        def get(self, **_kwargs):
            return [{"volid": "local:snippets/gd-deploy-8501.yml"}]

    class Storage:
        content = Content()

        def get(self):
            return [{"storage": "local", "content": "snippets", "active": 0}]

        def __call__(self, _store):
            return self

    class Node:
        storage = Storage()

    class Nodes:
        def __call__(self, _node):
            return Node()

    px = object.__new__(Proxmox)
    px.snippet_storage = "local"
    px.api = type("Api", (), {"nodes": Nodes()})()
    try:
        px.validate_snippet_volume("local:snippets/gd-deploy-8501.yml", node="pve")
    except ProxmoxError as exc:
        assert "active" in str(exc)
    else:
        raise AssertionError("inactive snippet storage must be rejected")


# --------------------------------------------------------------------------- #
# Durable post-boot guest-IP waits                                             #
# --------------------------------------------------------------------------- #
def _retire_waiting_jobs() -> None:
    """Keep this module's shared SQLite fixture from leaking active jobs across tests."""
    with session_scope() as s:
        for job in s.exec(select(Job).where(Job.status.in_(("queued", "waiting")))).all():
            job.status = "failed"
            job.error = "test fixture retired"
            job.finished_at = utcnow()
            s.add(job)


def _mk_captured_waiting_job(*, age: timedelta = timedelta(minutes=1),
                             cancel_requested: bool = False,
                             reserve_ip: bool = False) -> tuple[int, int, int, str]:
    _retire_waiting_jobs()
    user_id, template_id, block_key = _mk_plan_fixture(command="captured-command")
    result = _deploy(template_id, user_id, {"0.0": {"hostname": "captured-host"}})
    with session_scope() as s:
        job = s.get(Job, result["jobId"])
        dep = s.get(Deployment, result["depId"])
        job.status = "waiting"
        job.started_at = utcnow() - timedelta(minutes=2)
        job.waiting_since = utcnow() - age
        job.cancel_requested = cancel_requested
        job.finished_at = None
        job.phase = "Waiting for guest IP"
        dep.vmid = 8600 + (job.id % 200)
        dep.node = "pve"
        dep.status = "working"
        s.add(job)
        s.add(dep)
        if reserve_ip:
            s.add(IpAllocation(
                network_id=dep.network_id, ip=f"10.37.1.{job.id % 200 + 10}",
                deployment_id=dep.id, state="reserved",
            ))
        s.flush()
        return job.id, dep.id, job.connection_id, block_key


def test_missing_guest_ip_defers_required_ansible_without_false_success():
    """A missing agent IP must not let an Ansible deployment become successful."""
    _retire_waiting_jobs()
    user_id, template_id, _block_key = _mk_plan_fixture(command="defer-command")
    result = _deploy(template_id, user_id, {"0.0": {"hostname": "defer-host"}})
    with session_scope() as s:
        job = s.get(Job, result["jobId"])
        job.status = "running"
        job.started_at = utcnow()
        s.add(job)

    class _Px:
        def __init__(self, _conn): self.node = "pve"
        def pick_node(self): return "pve"
        def next_free_vmid(self, *_args, **_kwargs): return 8610
        def iso_volume_path(self, filename): return f"local:import/{filename}"
        def create_vm_import(self, *_args, **_kwargs): return "UPID:create"
        def wait_task(self, *_args, **_kwargs): return None
        def set_config(self, *_args, **_kwargs): return None
        def resize_disk(self, *_args, **_kwargs): return None
        def start(self, *_args, **_kwargs): return "UPID:start"
        def mac_of(self, *_args, **_kwargs): return "52:54:00:37:00:10"
        def vm_config(self, *_args, **_kwargs): return {"scsi0": "size=20G"}

    def _unexpected_ansible(*_args, **_kwargs):
        raise AssertionError("Ansible must not run without an IP")

    with _patched_worker(
        Proxmox=_Px,
        _preflight_deploy_cloud_init=lambda *_args, **_kwargs: worker.DeployPreflight(
            {"ciuser": "goblin"}, "managed-private", "", "",
        ),
        _ensure_base_disk=lambda *_args, **_kwargs: "base.img",
        _wait_for_ip=lambda *_args, **_kwargs: None,
        run_playbook=_unexpected_ansible,
    ):
        worker._execute(result["jobId"])

    with session_scope() as s:
        job = s.get(Job, result["jobId"])
        dep = s.get(Deployment, result["depId"])
        assert job.status == "waiting"
        assert job.waiting_since is not None
        assert job.finished_at is None
        assert job.phase == "Waiting for guest IP"
        assert dep.status == "working"
        assert dep.vmid == 8610
        assert dep.mac == "52:54:00:37:00:10"
        assert (dep.cpu, dep.ram, dep.disk) == (1, 2, 20)
    _retire_waiting_jobs()


def test_waiting_job_resumes_only_captured_ansible_plan_when_ip_appears():
    """Resume must ignore mutable block rows and execute the admission-time plan."""
    job_id, dep_id, _conn_id, block_key = _mk_captured_waiting_job()
    with session_scope() as s:
        s.exec(select(Block).where(Block.key == block_key)).one().ansible_template = (
            "- debug: msg=mutated-command"
        )

    playbooks: list[str] = []

    class _Px:
        def __init__(self, _conn): self.node = "pve"
        def agent_ipv4(self, *_args, **_kwargs): return "10.37.1.42"

    def _run(playbook, _ip, _user, _key, **_kwargs):
        playbooks.append(playbook)
        return "successful", 0

    with _patched_worker(
        Proxmox=_Px,
        _managed_keypair=lambda: ("managed-private", "managed-public"),
        run_playbook=_run,
    ):
        assert worker._poll_waiting_jobs() is True

    assert len(playbooks) == 1
    assert "captured-command" in playbooks[0]
    assert "mutated-command" not in playbooks[0]
    with session_scope() as s:
        job = s.get(Job, job_id)
        dep = s.get(Deployment, dep_id)
        assert job.status == "succeeded"
        assert job.finished_at is not None
        assert dep.status == "running"
        assert dep.ip == "10.37.1.42"


def test_waiting_job_times_out_at_thirty_minutes_without_releasing_ownership():
    """The exact wait deadline fails visibly while retaining the VM and IP allocation."""
    job_id, dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
        age=timedelta(0), reserve_ip=True,
    )
    boundary = utcnow()
    with session_scope() as s:
        job = s.get(Job, job_id)
        job.waiting_since = boundary - timedelta(minutes=30)
        s.add(job)

    class _Px:
        def __init__(self, _conn): self.node = "pve"
        def agent_ipv4(self, *_args, **_kwargs):
            raise AssertionError("deadline must be checked before another external poll")

    with _patched_worker(Proxmox=_Px):
        assert worker._poll_waiting_jobs(now=boundary - timedelta(microseconds=1)) is True
        with session_scope() as s:
            assert s.get(Job, job_id).status == "waiting"
        assert worker._poll_waiting_jobs(now=boundary) is True

    with session_scope() as s:
        job = s.get(Job, job_id)
        dep = s.get(Deployment, dep_id)
        allocations = s.exec(select(IpAllocation).where(
            IpAllocation.deployment_id == dep_id,
        )).all()
        assert job.status == "failed"
        assert job.finished_at is not None
        assert "guest IP" in job.error
        assert dep is not None and dep.vmid is not None
        assert dep.status == "error"
        assert len(allocations) == 1


def test_timeout_rechecks_committed_cancellation_before_failing():
    """A cancel committed after poll selection must win over the timeout transition."""
    job_id, dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
        age=timedelta(minutes=31), reserve_ip=True,
    )
    destroyed: list[int] = []

    class _Px:
        def __init__(self, _conn): self.node = "pve"
        def destroy(self, vmid, **_kwargs):
            destroyed.append(vmid)
            return "UPID:destroy"
        def wait_task(self, *_args, **_kwargs): return None
        def list_qemu(self, _node=None, **_kwargs): return []

    original_timeout = worker._timeout_waiting_job

    def _cancel_then_timeout(selected_job_id: int) -> None:
        with session_scope() as s:
            job = s.get(Job, selected_job_id)
            job.cancel_requested = True
            s.add(job)
        original_timeout(selected_job_id)

    with _patched_worker(Proxmox=_Px, _timeout_waiting_job=_cancel_then_timeout):
        assert worker._poll_waiting_jobs() is True

    assert destroyed, "the cancellation reconciliation path must destroy the deploy VM"
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.status == "canceled"
        assert job.error == "canceled"
        assert s.get(Deployment, dep_id) is None
        assert s.exec(select(IpAllocation).where(
            IpAllocation.deployment_id == dep_id,
        )).all() == []


def test_waiting_cancellation_uses_deployment_reconciliation():
    """A canceled wait must take the same absence-confirmed cleanup path as running work."""
    job_id, dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
        cancel_requested=True, reserve_ip=True,
    )
    destroyed: list[int] = []

    class _Px:
        def __init__(self, _conn): self.node = "pve"
        def destroy(self, vmid, **_kwargs):
            destroyed.append(vmid)
            return "UPID:destroy"
        def wait_task(self, *_args, **_kwargs): return None
        def list_qemu(self, _node=None, **_kwargs): return []

    with _patched_worker(Proxmox=_Px):
        assert worker._poll_waiting_jobs() is True

    assert destroyed
    with session_scope() as s:
        job = s.get(Job, job_id)
        allocations = s.exec(select(IpAllocation).where(
            IpAllocation.deployment_id == dep_id,
        )).all()
        assert job.status == "canceled"
        assert job.finished_at is not None
        assert s.get(Deployment, dep_id) is None
        assert allocations == []


def test_waiting_poll_yields_to_queued_work():
    """The serial worker must claim queued work before touching an older IP wait."""
    waiting_id, _dep_id, conn_id, _block_key = _mk_captured_waiting_job()
    with session_scope() as s:
        queued = Job(type="image_sync", status="queued", connection_id=conn_id)
        s.add(queued)
        s.flush()
        queued_id = queued.id
    assert worker._poll_waiting_jobs() is False
    with session_scope() as s:
        assert s.get(Job, waiting_id).status == "waiting"
        assert s.get(Job, queued_id).status == "queued"
        s.get(Job, queued_id).status = "failed"


def test_restart_recovery_leaves_durable_wait_untouched():
    """Restart recovery must fail dead running work but preserve resumable waits."""
    waiting_id, dep_id, _conn_id, _block_key = _mk_captured_waiting_job(reserve_ip=True)
    with session_scope() as s:
        running = Job(type="image_sync", status="running", started_at=utcnow())
        s.add(running)
        s.flush()
        running_id = running.id
    worker._recover_orphans()
    with session_scope() as s:
        assert s.get(Job, waiting_id).status == "waiting"
        assert s.get(Job, waiting_id).finished_at is None
        assert s.get(Deployment, dep_id).status == "working"
        assert s.get(Job, running_id).status == "failed"
    _retire_waiting_jobs()


def test_waiting_is_active_in_state_widget_and_serialization():
    """Every read model must present a durable wait as active/working, never terminal."""
    job_id, dep_id, _conn_id, _block_key = _mk_captured_waiting_job()
    request = type("Request", (), {"session": {}})()
    saved_px_cache = api._px_cache
    api._px_cache = lambda _conns: {}
    try:
        with session_scope() as s:
            user = s.get(User, s.get(Job, job_id).created_by)
            state = api.state(request, user=user, session=s)
            widget = api.widget_summary(user=user, session=s)
            detail = S.job_detail(s, s.get(Job, job_id), viewer=user)
            brief = S.job_brief(s, s.get(Job, job_id))
    finally:
        api._px_cache = saved_px_cache
    vm = next(item for item in state["VMS"] if item["depId"] == dep_id)
    assert vm["status"] == "working"
    assert vm["job"]["jobId"] == job_id
    assert widget["jobs_active"] == 1
    assert detail["status"] == "working" and detail["rawStatus"] == "waiting"
    assert brief["status"] == "working"
    _retire_waiting_jobs()


def test_waiting_can_cancel_but_cannot_be_dismissed_purged_or_retained_as_terminal():
    """Job mutations must treat waiting as active and retention must leave it alone."""
    job_id, _dep_id, _conn_id, _block_key = _mk_captured_waiting_job()
    with session_scope() as s:
        user = s.get(User, s.get(Job, job_id).created_by)
        api.cancel_job(job_id, user=user, session=s)
    with session_scope() as s:
        assert s.get(Job, job_id).cancel_requested is True
        user = s.get(User, s.get(Job, job_id).created_by)
        for mutation in (api.delete_job, api.purge_job_permanently):
            try:
                mutation(job_id, user=user, session=s)
            except HTTPException as exc:
                assert getattr(exc, "status_code", None) == 409
            else:
                raise AssertionError("waiting jobs must reject terminal-only deletion")
        assert api.clear_jobs(user=user, session=s)["cleared"] == 0
        assert api.purge_all_jobs(user=user, session=s)["purged"] == 0
    saved_retention = appsettings.get_job_retention_days
    appsettings.get_job_retention_days = lambda: 1
    try:
        api.prune_old_jobs()
    finally:
        appsettings.get_job_retention_days = saved_retention
    with session_scope() as s:
        assert s.get(Job, job_id) is not None
        assert s.get(Job, job_id).dismissed is False
    _retire_waiting_jobs()


def test_waiting_blocks_connection_deletion_and_deduplicates_sync():
    """Connection guards and heavyweight sync admission must include durable waits."""
    _retire_waiting_jobs()
    suffix = os.urandom(3).hex()
    with session_scope() as s:
        admin = User(email=f"active-boundary-{suffix}@example.com", name="admin",
                     role="admin", password_hash=hash_password("StrongPass12!"))
        conn = Connection(name=f"active-boundary-{suffix}", host="pve",
                          token_id="u@pve!token", node="pve")
        image = Image(kind="base", name=f"active-image-{suffix}",
                      source_url="https://example.com/base.img", build_status="ready")
        s.add(admin); s.add(conn); s.add(image); s.flush()
        waiting = Job(type="image_sync", status="waiting", image_id=image.id,
                      connection_id=conn.id, created_by=admin.id, waiting_since=utcnow())
        s.add(waiting); s.flush()
        admin_id, conn_id, image_id, waiting_id = admin.id, conn.id, image.id, waiting.id
    with session_scope() as s:
        admin = s.get(User, admin_id)
        synced = api.sync_image(image_id, api.SyncBody(connectionId=conn_id),
                                user=admin, session=s)
        assert synced == {"ok": True, "jobId": waiting_id, "deduped": True}
        try:
            api.delete_connection(conn_id, user=admin, session=s)
        except HTTPException as exc:
            assert getattr(exc, "status_code", None) == 409
            assert "active job" in str(getattr(exc, "detail", ""))
        else:
            raise AssertionError("a waiting job must retain its connection")
    _retire_waiting_jobs()


if __name__ == "__main__":
    test_execution_plan_is_encrypted_and_immutable()
    test_execution_plan_rejects_malformed_ciphertext()
    test_job_detail_does_not_disclose_execution_plan_or_captured_command()
    test_cleanup_pending_serialization_preserves_owned_identity_and_error()
    test_cleanup_pending_serialization_uses_owned_static_allocation_when_ip_is_empty()
    test_cleanup_origin_migration_is_additive_and_idempotent()
    test_cleanup_retry_uses_persisted_origin_after_job_retention_prunes_history()
    test_legacy_queued_job_persists_execution_plan_once()
    test_recipe_without_ssh_key_fails_before_vm_creation()
    test_recipe_free_deployment_uses_native_ciuser_without_snippet()
    test_required_snippet_upload_validate_then_create()
    test_validate_snippet_volume_requires_visible_snippet_on_enabled_storage()
    test_validate_snippet_volume_rejects_inactive_storage()
    test_missing_guest_ip_defers_required_ansible_without_false_success()
    test_waiting_job_resumes_only_captured_ansible_plan_when_ip_appears()
    test_waiting_job_times_out_at_thirty_minutes_without_releasing_ownership()
    test_timeout_rechecks_committed_cancellation_before_failing()
    test_waiting_cancellation_uses_deployment_reconciliation()
    test_waiting_poll_yields_to_queued_work()
    test_restart_recovery_leaves_durable_wait_untouched()
    test_waiting_is_active_in_state_widget_and_serialization()
    test_waiting_can_cancel_but_cannot_be_dismissed_purged_or_retained_as_terminal()
    test_waiting_blocks_connection_deletion_and_deduplicates_sync()
    print("\\nALL WAVE 37 UNIT TESTS PASSED")
