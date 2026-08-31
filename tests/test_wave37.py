"""Wave 37 — immutable encrypted deployment execution plans.

Run (Windows): $env:GOBLINDOCK_DEV='1'; .venv\\Scripts\\python.exe tests\\test_wave37.py
"""
import json
import os
import sys
import tempfile

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

from app import api, execution_plan, worker  # noqa: E402
from app.db import engine, init_db, session_scope  # noqa: E402
from app.models import Block, Connection, Deployment, Image, Job, Network, Template, User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app import serialize as S  # noqa: E402

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


if __name__ == "__main__":
    test_execution_plan_is_encrypted_and_immutable()
    test_execution_plan_rejects_malformed_ciphertext()
    test_job_detail_does_not_disclose_execution_plan_or_captured_command()
    test_legacy_queued_job_persists_execution_plan_once()
    print("\\nALL WAVE 37 UNIT TESTS PASSED")
