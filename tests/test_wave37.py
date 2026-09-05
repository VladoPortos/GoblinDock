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
        # custom Ansible blocks are admin-only (control-node RCE closure) — this
        # fixture is about plan immutability, so the block simply gets an admin author
        author = User(
            email=f"wave37-author-{suffix}@example.com", name="wave37 author",
            password_hash=hash_password("StrongPass12!"), role="admin",
        )
        conn = Connection(name=f"w37-conn-{suffix}", host="pve", token_id="u@pve!token")
        image = Image(
            kind="base", name=f"w37-image-{suffix}", source_url="https://example.com/base.img",
            build_status="ready",
        )
        s.add(user)
        s.add(author)
        s.add(conn)
        s.add(image)
        s.flush()
        net = Network(connection_id=conn.id, name=f"w37-net-{suffix}")
        s.add(net)
        block_key = f"c-wave37-plan-{suffix}"
        s.add(Block(
            key=block_key, kind="custom", builtin=False, owner_id=author.id,
            name="mutable block", phase="ansible",
            input_schema_json=(
                '[{"name":"command","type":"text"},'
                '{"name":"hostname","type":"text"}]'
            ),
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


def _mk_detail_fixture(status: str, *, error: str = "") -> tuple[int, int]:
    suffix = os.urandom(3).hex()
    with session_scope() as s:
        user = User(
            email=f"detail-{status}-{suffix}@example.com", name="detail",
            password_hash=hash_password("StrongPass12!"),
        )
        conn = Connection(
            name=f"detail-{suffix}", host="pve", token_id="u@pve!token", node="pve",
        )
        s.add(user)
        s.add(conn)
        s.flush()
        dep = Deployment(
            name=f"detail-{suffix}", owner_id=user.id, connection_id=conn.id,
            node="pve", vmid=8374, status=status, error=error,
        )
        s.add(dep)
        s.flush()
        return user.id, dep.id


def test_vm_detail_cleanup_pending_preserves_exact_error_without_live_probe():
    """Ambiguous cleanup ownership must remain visible without consulting Proxmox."""
    uid, dep_id = _mk_detail_fixture(
        "cleanup_pending", error="cleanup ownership could not be confirmed exactly",
    )
    probes = []

    class _Px:
        def __init__(self, _conn): probes.append("construct")
        def vm_current(self, *_args, **_kwargs): probes.append("current"); return {}
        def vm_config(self, *_args, **_kwargs): probes.append("config"); return {}

    saved_px = api.Proxmox
    api.Proxmox = _Px
    try:
        with Session(engine) as s:
            out = api.vm_detail(dep_id, user=s.get(User, uid), session=s)
    finally:
        api.Proxmox = saved_px

    assert probes == [], probes
    assert out["status"] == "cleanup_pending"
    assert out["err"] == "cleanup ownership could not be confirmed exactly"
    assert out["live"] is None and out["config"] is None and out["agent"] is None


def test_vm_detail_error_preserves_exact_error_without_live_probe():
    """A persisted failure must be returned verbatim and remain probe-free."""
    uid, dep_id = _mk_detail_fixture("error", error="provisioning failed exactly here")
    probes = []

    class _Px:
        def __init__(self, _conn): probes.append("construct")

    saved_px = api.Proxmox
    api.Proxmox = _Px
    try:
        with Session(engine) as s:
            out = api.vm_detail(dep_id, user=s.get(User, uid), session=s)
    finally:
        api.Proxmox = saved_px

    assert probes == [], probes
    assert out["status"] == "error"
    assert out["err"] == "provisioning failed exactly here"
    assert out["live"] is None and out["config"] is None and out["agent"] is None


def test_vm_detail_prefers_active_lifecycle_job_and_effective_working_state():
    """Active lifecycle work, not a newer terminal row or stale deployment state, owns detail."""
    uid, dep_id = _mk_detail_fixture("running")
    with session_scope() as s:
        dep = s.get(Deployment, dep_id)
        active = Job(
            type="rebuild", status="waiting", deployment_id=dep_id,
            connection_id=dep.connection_id, created_by=uid,
        )
        s.add(active)
        s.flush()
        active_id = active.id
        terminal = Job(
            type="deploy", status="succeeded", deployment_id=dep_id,
            connection_id=dep.connection_id, created_by=uid,
        )
        s.add(terminal)
        s.flush()
        assert terminal.id > active_id

    probes = []

    class _Px:
        def __init__(self, _conn): probes.append("construct")

    saved_px = api.Proxmox
    api.Proxmox = _Px
    try:
        with Session(engine) as s:
            out = api.vm_detail(dep_id, user=s.get(User, uid), session=s)
    finally:
        api.Proxmox = saved_px

    assert probes == [], probes
    assert out["status"] == "working"
    assert out["jobId"] == active_id
    assert out["live"] is None and out["config"] is None and out["agent"] is None


def test_vm_detail_normal_running_and_stopped_states_still_live_probe():
    """The lifecycle lock must not remove ordinary running/stopped detail probing."""
    for persisted_status, live_status in (("running", "running"), ("stopped", "stopped")):
        uid, dep_id = _mk_detail_fixture(persisted_status)
        probes = []

        class _Px:
            def __init__(self, _conn): probes.append("construct")
            def vm_current(self, vmid, node):
                probes.append(("current", vmid, node))
                return {"status": live_status, "uptime": 61, "cpu": 0.25, "agent": 0}
            def vm_config(self, vmid, node):
                probes.append(("config", vmid, node))
                return {"cores": 2, "memory": 2048}

        saved_px = api.Proxmox
        api.Proxmox = _Px
        try:
            with Session(engine) as s:
                out = api.vm_detail(dep_id, user=s.get(User, uid), session=s)
        finally:
            api.Proxmox = saved_px

        assert probes == [
            "construct", ("current", 8374, "pve"), ("config", 8374, "pve"),
        ]
        assert out["status"] == persisted_status
        assert out["live"]["status"] == live_status
        assert out["config"]["cores"] == 2
        assert out["consoleReady"] is (live_status == "running")


def test_cleanup_pending_vm_dict_wins_over_stale_active_job_overlay():
    """A stale active map must not turn cleanup ownership into a working job chip."""
    uid, dep_id = _mk_detail_fixture(
        "cleanup_pending", error="cleanup remains ambiguous",
    )
    with session_scope() as s:
        dep = s.get(Deployment, dep_id)
        active = Job(
            type="destroy", status="running", deployment_id=dep_id,
            connection_id=dep.connection_id, created_by=uid, phase="Destroying",
        )
        s.add(active)
        s.flush()
        out = S.vm_dict(
            s, dep, s.get(User, uid), {}, {uid: s.get(User, uid)},
            {dep.connection_id: s.get(Connection, dep.connection_id)},
            active_jobs={dep_id: active},
        )

    assert out["status"] == "cleanup_pending"
    assert out["err"] == "cleanup remains ambiguous"
    assert "job" not in out


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
        def find_vm_node(self, vmid, node=None): return "pve"
        def vm_current(self, vmid, node=None): return {"status": "stopped"}
        def destroy(self, vmid, node=None):
            destroys.append(vmid)
            return "UPID:destroy"
        def wait_task(self, *args, **kwargs): return None
        def list_cluster_guests(self):
            self._cluster_fixture = self.list_qemu(node=getattr(self, "node", None))
            return self._cluster_fixture
        def _assert_vmid_free(self, vmid):
            assert vmid not in {int(v["vmid"]) for v in self._cluster_fixture}
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
            deploy_inputs_enc=execution_plan.encrypt_deploy_inputs(
                '{"0.0":{"hostname":"legacy-host"}}'),
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
                             reserve_ip: bool = False,
                             retire_existing: bool = True) -> tuple[int, int, int, str]:
    if retire_existing:
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


def _waiting_vmids(*job_ids: int) -> dict[int, int]:
    with session_scope() as s:
        return {
            job_id: s.get(Deployment, s.get(Job, job_id).deployment_id).vmid
            for job_id in job_ids
        }


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
        def wait_guest_ready(self, *_args, **_kwargs): return None
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


def test_waiting_poll_does_not_starve_ready_job_behind_missing_ip():
    """A missing IP on the oldest wait must not block a later ready deployment."""
    oldest_id, _oldest_dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
        age=timedelta(minutes=2),
    )
    ready_id, ready_dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
        age=timedelta(minutes=1), retire_existing=False,
    )
    vmids = _waiting_vmids(oldest_id, ready_id)
    probed: list[int] = []
    ansible_ips: list[str] = []

    class _Px:
        def __init__(self, _conn): self.node = "pve"
        def agent_ipv4(self, vmid, *_args, **_kwargs):
            probed.append(vmid)
            return "10.37.2.42" if vmid == vmids[ready_id] else None

    def _run(_playbook, ip, _user, _key, **_kwargs):
        ansible_ips.append(ip)
        return "successful", 0

    with _patched_worker(
        Proxmox=_Px,
        _managed_keypair=lambda: ("managed-private", "managed-public"),
        run_playbook=_run,
    ):
        assert worker._poll_waiting_jobs() is True

    assert probed == [vmids[oldest_id], vmids[ready_id]]
    assert ansible_ips == ["10.37.2.42"]
    with session_scope() as s:
        assert s.get(Job, oldest_id).status == "waiting"
        assert s.get(Job, ready_id).status == "succeeded"
        assert s.get(Deployment, ready_dep_id).status == "running"


def test_waiting_poll_times_out_oldest_and_resumes_later_ready_job():
    """An exact-deadline wait gets one last probe before failing."""
    oldest_id, oldest_dep_id, _conn_id, _block_key = _mk_captured_waiting_job()
    ready_id, ready_dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
        retire_existing=False,
    )
    boundary = utcnow()
    with session_scope() as s:
        s.get(Job, oldest_id).waiting_since = boundary - timedelta(minutes=30)
        s.get(Job, ready_id).waiting_since = boundary - timedelta(minutes=1)
    vmids = _waiting_vmids(oldest_id, ready_id)
    probed: list[int] = []

    class _Px:
        def __init__(self, _conn): self.node = "pve"
        def agent_ipv4(self, vmid, *_args, **_kwargs):
            probed.append(vmid)
            return "10.37.2.43" if vmid == vmids[ready_id] else None

    with _patched_worker(
        Proxmox=_Px,
        _managed_keypair=lambda: ("managed-private", "managed-public"),
        run_playbook=lambda *_args, **_kwargs: ("successful", 0),
    ):
        assert worker._poll_waiting_jobs(now=boundary) is True

    assert probed == [vmids[oldest_id], vmids[ready_id]]
    with session_scope() as s:
        assert s.get(Job, oldest_id).status == "failed"
        assert s.get(Deployment, oldest_dep_id).status == "error"
        assert s.get(Job, ready_id).status == "succeeded"
        assert s.get(Deployment, ready_dep_id).status == "running"


def test_waiting_poll_probes_missing_ip_jobs_once_in_stable_order():
    """Equal-age waits are probed once per poll in deterministic ID order."""
    job_ids = []
    for index in range(3):
        job_id, _dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
            retire_existing=index == 0,
        )
        job_ids.append(job_id)
    same_waiting_since = utcnow() - timedelta(minutes=1)
    with session_scope() as s:
        for job_id in job_ids:
            s.get(Job, job_id).waiting_since = same_waiting_since
    vmids = _waiting_vmids(*job_ids)
    expected_order = [vmids[job_id] for job_id in sorted(job_ids)]
    probed: list[int] = []

    class _Px:
        def __init__(self, _conn): self.node = "pve"
        def agent_ipv4(self, vmid, *_args, **_kwargs):
            probed.append(vmid)
            return None

    with _patched_worker(Proxmox=_Px):
        assert worker._poll_waiting_jobs() is True
        assert worker._poll_waiting_jobs() is True

    assert probed == expected_order + expected_order
    with session_scope() as s:
        assert [s.get(Job, job_id).status for job_id in job_ids] == ["waiting"] * 3


def test_waiting_poll_completes_ready_jobs_once_without_duplicate_resume():
    """Successful waits leave the snapshot and cannot execute Ansible twice."""
    first_id, _first_dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
        age=timedelta(minutes=2),
    )
    second_id, _second_dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
        age=timedelta(minutes=1), retire_existing=False,
    )
    job_ids = [first_id, second_id]
    vmids = _waiting_vmids(*job_ids)
    ips = {vmids[first_id]: "10.37.2.44", vmids[second_id]: "10.37.2.45"}
    probed: list[int] = []
    ansible_ips: list[str] = []

    class _Px:
        def __init__(self, _conn): self.node = "pve"
        def agent_ipv4(self, vmid, *_args, **_kwargs):
            probed.append(vmid)
            return ips[vmid]

    def _run(_playbook, ip, _user, _key, **_kwargs):
        ansible_ips.append(ip)
        return "successful", 0

    with _patched_worker(
        Proxmox=_Px,
        _managed_keypair=lambda: ("managed-private", "managed-public"),
        run_playbook=_run,
    ):
        assert worker._poll_waiting_jobs() is True
        assert worker._poll_waiting_jobs() is False

    assert probed == [vmids[first_id], vmids[second_id]]
    assert ansible_ips == ["10.37.2.44", "10.37.2.45"]
    with session_scope() as s:
        assert [s.get(Job, job_id).status for job_id in job_ids] == ["succeeded"] * 2


def test_waiting_poll_isolates_resume_failure_from_later_ready_job():
    """A failed Ansible resume must be terminal for A and must not block B."""
    first_id, first_dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
        age=timedelta(minutes=2),
    )
    second_id, second_dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
        age=timedelta(minutes=1), retire_existing=False,
    )
    vmids = _waiting_vmids(first_id, second_id)
    ips = {vmids[first_id]: "10.37.2.46", vmids[second_id]: "10.37.2.47"}
    ansible_ips: list[str] = []

    class _Px:
        def __init__(self, _conn): self.node = "pve"
        def agent_ipv4(self, vmid, *_args, **_kwargs): return ips[vmid]
        def list_cluster_guests(self):
            self._cluster_fixture = self.list_qemu(node=getattr(self, "node", None))
            return self._cluster_fixture
        def _assert_vmid_free(self, vmid):
            assert vmid not in {int(v["vmid"]) for v in self._cluster_fixture}
        def list_qemu(self, *_args, **_kwargs): return [{"vmid": vmids[first_id]}]

    def _run(_playbook, ip, _user, _key, **_kwargs):
        ansible_ips.append(ip)
        if ip == "10.37.2.46":
            raise RuntimeError("resume A failed")
        return "successful", 0

    with _patched_worker(
        Proxmox=_Px,
        _managed_keypair=lambda: ("managed-private", "managed-public"),
        run_playbook=_run,
    ):
        assert worker._poll_waiting_jobs() is True

    assert ansible_ips == ["10.37.2.46", "10.37.2.47"]
    with session_scope() as s:
        assert s.get(Job, first_id).status == "failed"
        assert "resume A failed" in s.get(Job, first_id).error
        assert s.get(Deployment, first_dep_id).status == "error"
        assert s.get(Job, second_id).status == "succeeded"
        assert s.get(Deployment, second_dep_id).status == "running"


def test_waiting_poll_isolates_ip_probe_exception_from_later_ready_job():
    """A failed IP probe is one no-IP result and must not block the next wait."""
    first_id, _first_dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
        age=timedelta(minutes=2),
    )
    second_id, second_dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
        age=timedelta(minutes=1), retire_existing=False,
    )
    vmids = _waiting_vmids(first_id, second_id)
    probed: list[int] = []

    class _Px:
        def __init__(self, _conn): self.node = "pve"
        def agent_ipv4(self, vmid, *_args, **_kwargs):
            probed.append(vmid)
            if vmid == vmids[first_id]:
                raise RuntimeError("probe A failed")
            return "10.37.2.48"

    with _patched_worker(
        Proxmox=_Px,
        _managed_keypair=lambda: ("managed-private", "managed-public"),
        run_playbook=lambda *_args, **_kwargs: ("successful", 0),
    ):
        assert worker._poll_waiting_jobs() is True

    assert probed == [vmids[first_id], vmids[second_id]]
    with session_scope() as s:
        assert s.get(Job, first_id).status == "waiting"
        assert s.get(Job, second_id).status == "succeeded"
        assert s.get(Deployment, second_dep_id).status == "running"


def test_waiting_poll_isolates_timeout_transition_exception_from_later_job():
    """An unexpected timeout-row exception must not prevent processing later IDs."""
    first_id, _first_dep_id, _conn_id, _block_key = _mk_captured_waiting_job()
    second_id, second_dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
        retire_existing=False,
    )
    boundary = utcnow()
    with session_scope() as s:
        s.get(Job, first_id).waiting_since = boundary - timedelta(minutes=30)
        s.get(Job, second_id).waiting_since = boundary - timedelta(minutes=1)
    vmids = _waiting_vmids(first_id, second_id)
    original_timeout = worker._timeout_waiting_job

    def _timeout_then_raise(job_id: int) -> None:
        original_timeout(job_id)
        raise RuntimeError("timeout transition follow-up failed")

    class _Px:
        def __init__(self, _conn): self.node = "pve"
        def agent_ipv4(self, vmid, *_args, **_kwargs):
            return "10.37.2.49" if vmid == vmids[second_id] else None

    with _patched_worker(
        Proxmox=_Px,
        _timeout_waiting_job=_timeout_then_raise,
        _managed_keypair=lambda: ("managed-private", "managed-public"),
        run_playbook=lambda *_args, **_kwargs: ("successful", 0),
        traceback=type("Traceback", (), {"print_exc": staticmethod(lambda: None)}),
    ):
        try:
            assert worker._poll_waiting_jobs(now=boundary) is True
        except RuntimeError as exc:
            raise AssertionError("one timeout row must not abort the waiting snapshot") from exc

    with session_scope() as s:
        assert s.get(Job, first_id).status == "failed"
        assert s.get(Job, second_id).status == "succeeded"
        assert s.get(Deployment, second_dep_id).status == "running"


def test_waiting_poll_continues_between_rows_when_new_work_is_queued():
    """The dedicated waiting poller must not stall behind newly queued work."""
    first_id, _first_dep_id, conn_id, _block_key = _mk_captured_waiting_job(
        age=timedelta(minutes=2),
    )
    second_id, _second_dep_id, _conn_id, _block_key = _mk_captured_waiting_job(
        age=timedelta(minutes=1), retire_existing=False,
    )
    vmids = _waiting_vmids(first_id, second_id)
    probed: list[int] = []
    queued_ids: list[int] = []

    class _Px:
        def __init__(self, _conn): self.node = "pve"
        def agent_ipv4(self, vmid, *_args, **_kwargs):
            probed.append(vmid)
            if vmid == vmids[first_id]:
                with session_scope() as s:
                    queued = Job(type="image_sync", status="queued", connection_id=conn_id)
                    s.add(queued)
                    s.flush()
                    queued_ids.append(queued.id)
                return None
            return "10.37.2.50"

    with _patched_worker(
        Proxmox=_Px,
        _managed_keypair=lambda: ("managed-private", "managed-public"),
        run_playbook=lambda *_args, **_kwargs: ("successful", 0),
    ):
        assert worker._poll_waiting_jobs() is True

    assert probed == [vmids[first_id], vmids[second_id]]
    assert len(queued_ids) == 1
    with session_scope() as s:
        assert s.get(Job, queued_ids[0]).status == "queued"
        assert s.get(Job, second_id).status == "succeeded"
    _retire_waiting_jobs()


def test_waiting_job_times_out_at_thirty_minutes_without_releasing_ownership():
    """The exact wait deadline fails after one last probe and retains ownership."""
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
            return None

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
        def find_vm_node(self, vmid, node=None): return "pve"
        def vm_current(self, vmid, node=None): return {"status": "stopped"}
        def destroy(self, vmid, **_kwargs):
            destroyed.append(vmid)
            return "UPID:destroy"
        def wait_task(self, *_args, **_kwargs): return None
        def list_cluster_guests(self):
            self._cluster_fixture = self.list_qemu(node=getattr(self, "node", None))
            return self._cluster_fixture
        def _assert_vmid_free(self, vmid):
            assert vmid not in {int(v["vmid"]) for v in self._cluster_fixture}
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
        def find_vm_node(self, vmid, node=None): return "pve"
        def vm_current(self, vmid, node=None): return {"status": "stopped"}
        def destroy(self, vmid, **_kwargs):
            destroyed.append(vmid)
            return "UPID:destroy"
        def wait_task(self, *_args, **_kwargs): return None
        def list_cluster_guests(self):
            self._cluster_fixture = self.list_qemu(node=getattr(self, "node", None))
            return self._cluster_fixture
        def _assert_vmid_free(self, vmid):
            assert vmid not in {int(v["vmid"]) for v in self._cluster_fixture}
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


def test_waiting_poll_is_independent_of_queued_work():
    """Queued work must not prevent the dedicated poller visiting a durable wait."""
    waiting_id, _dep_id, conn_id, _block_key = _mk_captured_waiting_job()
    with session_scope() as s:
        queued = Job(type="image_sync", status="queued", connection_id=conn_id)
        s.add(queued)
        s.flush()
        queued_id = queued.id
    visited: list[int] = []
    with _patched_worker(
        _poll_waiting_job=lambda job_id, _poll_at: visited.append(job_id),
    ):
        assert worker._poll_waiting_jobs() is True
    assert visited == [waiting_id]
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


# --------------------------------------------------------------------------- #
# Lifecycle task completion                                                    #
# --------------------------------------------------------------------------- #
def _mk_lifecycle_task_fixture(*, job_type: str | None = None) -> tuple[int, int, int | None]:
    suffix = os.urandom(3).hex()
    with session_scope() as s:
        user = User(
            email=f"wave37-lifecycle-{suffix}@example.com", name="wave37-lifecycle",
            password_hash=hash_password("StrongPass12!"),
        )
        conn = Connection(
            name=f"w37-lifecycle-conn-{suffix}", host="pve",
            token_id="u@pve!token", node="pve",
        )
        s.add(user)
        s.add(conn)
        s.flush()
        dep = Deployment(
            name=f"w37-lifecycle-vm-{suffix}", owner_id=user.id,
            connection_id=conn.id, node="pve", vmid=8737, status="running",
        )
        s.add(dep)
        s.flush()
        job_id = None
        if job_type:
            job = Job(
                type=job_type, status="running", deployment_id=dep.id,
                connection_id=conn.id, created_by=user.id, context_json="{}",
            )
            s.add(job)
            s.flush()
            job_id = job.id
        return user.id, dep.id, job_id


def test_direct_lifecycle_actions_wait_for_their_submitted_upids():
    """Start, stop, and restart must report success only after their task completes."""
    calls = []

    class _Px:
        def __init__(self, _conn): pass
        def start(self, vmid, node=None):
            calls.append(("submit", "start", vmid, node))
            return "UPID:start"
        def stop(self, vmid, node=None):
            calls.append(("submit", "stop", vmid, node))
            return "UPID:stop"
        def reboot(self, vmid, node=None):
            calls.append(("submit", "restart", vmid, node))
            return "UPID:restart"
        def wait_task(self, upid, node=None, timeout=None):
            calls.append(("wait", upid, node, timeout))

    saved = api.Proxmox
    api.Proxmox = _Px
    try:
        for action in ("start", "stop", "restart"):
            uid, dep_id, _job_id = _mk_lifecycle_task_fixture()
            calls.clear()
            with Session(engine) as s:
                assert api.vm_action(
                    dep_id, api.ActionBody(action=action),
                    user=s.get(User, uid), session=s,
                ) == {"ok": True}
            assert calls == [
                ("submit", action, 8737, "pve"),
                ("wait", f"UPID:{action}", "pve", 120),
            ], (action, calls)
    finally:
        api.Proxmox = saved


def test_direct_lifecycle_task_failure_and_timeout_return_502():
    """A failed or timed-out Proxmox task must never be acknowledged as a VM action success."""
    calls = []
    failure = None

    class _Px:
        def __init__(self, _conn): pass
        def start(self, *_args, **_kwargs): return "UPID:start"
        def stop(self, *_args, **_kwargs): return "UPID:stop"
        def reboot(self, *_args, **_kwargs): return "UPID:restart"
        def wait_task(self, upid, node=None, timeout=None):
            calls.append((upid, node, timeout))
            raise failure

    saved = api.Proxmox
    api.Proxmox = _Px
    try:
        for action in ("start", "stop", "restart"):
            for message in ("task finished with ERROR", "task timed out"):
                uid, dep_id, _job_id = _mk_lifecycle_task_fixture()
                calls.clear()
                failure = ProxmoxError(message)
                with Session(engine) as s:
                    try:
                        api.vm_action(
                            dep_id, api.ActionBody(action=action),
                            user=s.get(User, uid), session=s,
                        )
                    except HTTPException as exc:
                        assert exc.status_code == 502, (action, message, exc.status_code)
                        assert message in str(exc.detail), (action, message, exc.detail)
                    else:
                        raise AssertionError((action, message, "task failure was acknowledged"))
                assert calls == [(f"UPID:{action}", "pve", 120)], (action, message, calls)
    finally:
        api.Proxmox = saved


def _run_rebuild_post_destroy_presence_case(inventory_state: str):
    job_id, original_plan_loader = _mk_worker_job(recipe=[], blocks={}, ssh_key_path="")
    with session_scope() as s:
        job = s.get(Job, job_id)
        dep = s.get(Deployment, job.deployment_id)
        network = Network(connection_id=dep.connection_id, name=f"rebuild-guard-{job_id}")
        s.add(network)
        s.flush()
        vmid = 8800 + (job_id % 100)
        dep.vmid = vmid
        dep.network_id = network.id
        job.type = "rebuild"
        s.add(dep)
        s.add(job)
        s.add(IpAllocation(
            network_id=network.id, ip=f"10.37.8.{job_id % 200 + 10}",
            deployment_id=dep.id, state="reserved",
        ))
        s.flush()
        dep_id = dep.id

    create_calls = []

    class _Px:
        def __init__(self, _conn):
            self.node = "pve"

        def vm_current(self, _vmid, node=None):
            return {"status": "stopped"}

        def destroy(self, _vmid, node=None):
            return "UPID:destroy"

        def wait_task(self, *_args, **_kwargs):
            return None

        def list_cluster_guests(self):
            self._cluster_fixture = self.list_qemu(node=getattr(self, "node", None))
            return self._cluster_fixture
        def _assert_vmid_free(self, vmid):
            assert vmid not in {int(v["vmid"]) for v in self._cluster_fixture}
        def list_qemu(self, node=None):
            if inventory_state == "unknown":
                raise RuntimeError("inventory unavailable after destroy")
            return [{"vmid": vmid}]

        def storage_has_volume(self, *_args, **_kwargs):
            return True

        def iso_volume_path(self, filename):
            return f"local:import/{filename}"

        def create_vm_import(self, *_args, **_kwargs):
            create_calls.append(vmid)
            raise RuntimeError("create rejected before UPID")

    try:
        with _patched_worker(
            Proxmox=_Px,
            auto_root_password_enabled=lambda: False,
            _managed_keypair=lambda: ("managed-private", "ssh-ed25519 managed"),
            _ssh_pubkey=lambda *_args, **_kwargs: "",
        ):
            worker._execute(job_id)
    finally:
        worker._load_materialized_job_plan = original_plan_loader

    with session_scope() as s:
        job = s.get(Job, job_id)
        dep = s.get(Deployment, dep_id)
        allocations = s.exec(select(IpAllocation).where(
            IpAllocation.deployment_id == dep_id,
        )).all()
        return job.status, dep.status, dep.vmid, len(allocations), create_calls, vmid


def test_rebuild_requires_confirmed_absence_after_successful_destroy_wait_when_vm_present():
    """Missing the post-destroy absence gate can recreate over a surviving old VM."""
    job_status, dep_status, dep_vmid, allocations, create_calls, vmid = (
        _run_rebuild_post_destroy_presence_case("present")
    )
    assert job_status == "failed"
    assert dep_status == "error"
    assert dep_vmid == vmid
    assert allocations == 1
    assert create_calls == []


def test_rebuild_requires_confirmed_absence_after_successful_destroy_wait_when_inventory_unknown():
    """Missing the post-destroy absence gate can clear ownership during an inventory outage."""
    job_status, dep_status, dep_vmid, allocations, create_calls, vmid = (
        _run_rebuild_post_destroy_presence_case("unknown")
    )
    assert job_status == "failed"
    assert dep_status == "error"
    assert dep_vmid == vmid
    assert allocations == 1
    assert create_calls == []


def _run_worker_lifecycle_case(job_type: str, vm_status: str, *, stop_error: bool = False):
    _uid, _dep_id, job_id = _mk_lifecycle_task_fixture(job_type=job_type)
    calls = []
    sleeps = []

    class _Px:
        def __init__(self, _conn): pass
        def vm_current(self, vmid, node=None):
            calls.append(("inspect", vmid, node))
            return {"status": vm_status}
        def stop(self, vmid, node=None):
            calls.append(("stop", vmid, node))
            return "UPID:stop"
        def destroy(self, vmid, node=None):
            calls.append(("destroy", vmid, node))
            return "UPID:destroy"
        def wait_task(self, upid, node=None, cancelled=None, timeout=None):
            calls.append(("wait", upid, node, timeout, callable(cancelled)))
            if stop_error and upid == "UPID:stop":
                raise ProxmoxError("stop task failed")
        def list_cluster_guests(self):
            self._cluster_fixture = self.list_qemu(node=getattr(self, "node", None))
            return self._cluster_fixture
        def _assert_vmid_free(self, vmid):
            assert vmid not in {int(v["vmid"]) for v in self._cluster_fixture}
        def list_qemu(self, node=None):
            calls.append(("inventory", node))
            return []

    def forbidden_sleep(seconds):
        sleeps.append(seconds)
        raise AssertionError(f"fixed lifecycle sleep used: {seconds}")

    deploy_calls = []
    with session_scope() as s:
        job = Job(**s.get(Job, job_id).model_dump())
    error = None
    with _patched_worker(
        Proxmox=_Px,
        _run_deploy=lambda *_args, **_kwargs: deploy_calls.append("deploy"),
        _ensure_base_disk=lambda *_args, **_kwargs: "base.img",
        _preflight_deploy_cloud_init=lambda *_args, **_kwargs: worker.DeployPreflight({}, '', '', ''),
    ):
        saved_sleep = worker.time.sleep
        worker.time.sleep = forbidden_sleep
        try:
            if job_type == "rebuild":
                worker._run_rebuild(worker.JobCtx(job_id), job)
            else:
                worker._run_destroy(worker.JobCtx(job_id), job)
        except Exception as exc:  # returned for exact behavior assertions below
            error = exc
        finally:
            worker.time.sleep = saved_sleep
    return calls, sleeps, deploy_calls, error


def test_worker_rebuild_and_destroy_await_stop_without_fixed_sleep():
    """A running VM's stop task must finish before either destructive worker path advances."""
    for job_type in ("rebuild", "destroy"):
        calls, sleeps, deploy_calls, error = _run_worker_lifecycle_case(job_type, "running")
        assert error is None, (job_type, error)
        assert sleeps == [], (job_type, sleeps)
        stop_index = calls.index(("stop", 8737, "pve"))
        stop_wait_index = calls.index(("wait", "UPID:stop", "pve", 300, True))
        destroy_index = calls.index(("destroy", 8737, "pve"))
        assert stop_index < stop_wait_index < destroy_index, (job_type, calls)
        assert ("wait", "UPID:destroy", "pve", 300, True) in calls, (job_type, calls)
        assert deploy_calls == (["deploy"] if job_type == "rebuild" else []), deploy_calls


def test_worker_rebuild_and_destroy_skip_stop_when_vm_is_stopped():
    """A stopped VM must proceed directly to destroy without submitting a redundant stop task."""
    for job_type in ("rebuild", "destroy"):
        calls, sleeps, _deploy_calls, error = _run_worker_lifecycle_case(job_type, "stopped")
        assert error is None, (job_type, error)
        assert sleeps == [], (job_type, sleeps)
        assert ("inspect", 8737, "pve") in calls, (job_type, calls)
        assert not any(call[0] == "stop" for call in calls), (job_type, calls)
        assert not any(call[:2] == ("wait", "UPID:stop") for call in calls), (job_type, calls)


def test_worker_stop_task_failure_aborts_before_destroy():
    """A non-OK stop task must fail the job path instead of racing ahead to destroy."""
    for job_type in ("rebuild", "destroy"):
        calls, sleeps, deploy_calls, error = _run_worker_lifecycle_case(
            job_type, "running", stop_error=True,
        )
        assert isinstance(error, ProxmoxError), (job_type, error)
        assert "stop task failed" in str(error), (job_type, error)
        assert sleeps == [], (job_type, sleeps)
        assert ("wait", "UPID:stop", "pve", 300, True) in calls, (job_type, calls)
        assert not any(call[0] == "destroy" for call in calls), (job_type, calls)
        assert deploy_calls == [], (job_type, deploy_calls)


if __name__ == "__main__":
    test_execution_plan_is_encrypted_and_immutable()
    test_execution_plan_rejects_malformed_ciphertext()
    test_job_detail_does_not_disclose_execution_plan_or_captured_command()
    test_cleanup_pending_serialization_preserves_owned_identity_and_error()
    test_cleanup_pending_serialization_uses_owned_static_allocation_when_ip_is_empty()
    test_vm_detail_cleanup_pending_preserves_exact_error_without_live_probe()
    test_vm_detail_error_preserves_exact_error_without_live_probe()
    test_vm_detail_prefers_active_lifecycle_job_and_effective_working_state()
    test_vm_detail_normal_running_and_stopped_states_still_live_probe()
    test_cleanup_pending_vm_dict_wins_over_stale_active_job_overlay()
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
    test_waiting_poll_does_not_starve_ready_job_behind_missing_ip()
    test_waiting_poll_times_out_oldest_and_resumes_later_ready_job()
    test_waiting_poll_probes_missing_ip_jobs_once_in_stable_order()
    test_waiting_poll_completes_ready_jobs_once_without_duplicate_resume()
    test_waiting_poll_isolates_resume_failure_from_later_ready_job()
    test_waiting_poll_isolates_ip_probe_exception_from_later_ready_job()
    test_waiting_poll_isolates_timeout_transition_exception_from_later_job()
    test_waiting_poll_continues_between_rows_when_new_work_is_queued()
    test_waiting_job_times_out_at_thirty_minutes_without_releasing_ownership()
    test_timeout_rechecks_committed_cancellation_before_failing()
    test_waiting_cancellation_uses_deployment_reconciliation()
    test_waiting_poll_is_independent_of_queued_work()
    test_restart_recovery_leaves_durable_wait_untouched()
    test_waiting_is_active_in_state_widget_and_serialization()
    test_waiting_can_cancel_but_cannot_be_dismissed_purged_or_retained_as_terminal()
    test_waiting_blocks_connection_deletion_and_deduplicates_sync()
    test_direct_lifecycle_actions_wait_for_their_submitted_upids()
    test_direct_lifecycle_task_failure_and_timeout_return_502()
    test_rebuild_requires_confirmed_absence_after_successful_destroy_wait_when_vm_present()
    test_rebuild_requires_confirmed_absence_after_successful_destroy_wait_when_inventory_unknown()
    test_worker_rebuild_and_destroy_await_stop_without_fixed_sleep()
    test_worker_rebuild_and_destroy_skip_stop_when_vm_is_stopped()
    test_worker_stop_task_failure_aborts_before_destroy()
    print("\\nALL WAVE 37 UNIT TESTS PASSED")
