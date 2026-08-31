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
    print("\\nALL WAVE 37 UNIT TESTS PASSED")
