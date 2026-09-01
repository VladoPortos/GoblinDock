"""Wave 46 — public-template prompt metadata and resource capabilities.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave46.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave46-test.sqlite3")
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

from app import api  # noqa: E402
from app import serialize as S  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Block, Connection, Deployment, Image, Job, Template, User  # noqa: E402
from app.recipes import input_schema_problems, lint_block  # noqa: E402

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


def _public_custom_template_fixture():
    suffix = os.urandom(4).hex()
    script_sentinel = f"PRIVATE-SCRIPT-{suffix}"
    secret_sentinel = f"PRIVATE-SECRET-{suffix}"
    with session_scope() as session:
        owner = User(
            email=f"wave46-owner-{suffix}@example.com", name="Template author",
            password_hash="unused",
        )
        viewer = User(
            email=f"wave46-viewer-{suffix}@example.com", name="Template viewer",
            password_hash="unused",
        )
        session.add(owner)
        session.add(viewer)
        session.flush()
        block = Block(
            key=f"c-wave46-{suffix}", kind="custom", builtin=False,
            owner_id=owner.id, name="Private author block", phase="cloudinit",
            input_schema_json=json.dumps([
                {"name": "hostname", "type": "text", "label": "Host name", "default": "author-host"},
                {"name": "api_token", "type": "secret", "label": "API token", "default": secret_sentinel},
                {"name": "mode", "type": "select", "label": "Mode", "options": ["safe", "fast"], "default": "safe"},
            ]),
            cloudinit_template=f"echo {script_sentinel} {{hostname}} {{api_token}}",
        )
        image = Image(
            kind="base", name=f"wave46-image-{suffix}",
            source_url="https://example.com/wave46.img", build_status="ready",
        )
        connection = Connection(
            name=f"wave46-connection-{suffix}", host="pve.example",
            token_id="automation@pve!goblindock", node="pve",
        )
        session.add(block)
        session.add(image)
        session.add(connection)
        session.flush()
        template = Template(
            name=f"wave46-public-{suffix}", owner_id=owner.id, public=True,
            recipe_json=json.dumps([{"blocks": [{
                "ref": block.key,
                "name": block.name,
                "inputs": {
                    "hostname": "author-host",
                    "api_token": secret_sentinel,
                    "mode": "safe",
                },
                "ask": ["hostname", "api_token", "mode"],
            }]}]),
            base_image_id=image.id, connection_id=connection.id,
        )
        session.add(template)
        session.flush()
        return {
            "owner": owner.id,
            "viewer": viewer.id,
            "block": block.key,
            "template": template.id,
            "image": image.id,
            "script_sentinel": script_sentinel,
            "secret_sentinel": secret_sentinel,
        }


def test_non_owner_gets_only_sanitized_ask_descriptors_and_can_deploy():
    fixture = _public_custom_template_fixture()
    with session_scope() as session:
        viewer = session.get(User, fixture["viewer"])
        template = session.get(Template, fixture["template"])
        payload = S.template_dict(session, template, viewer=viewer)

    placed = payload["recipe"][0]["blocks"][0]
    assert payload["canEdit"] is False
    assert placed["inputs"] == {
        "hostname": "author-host",
        "api_token": "",
        "mode": "safe",
    }
    assert placed["askSchema"] == [
        {"name": "hostname", "type": "text", "label": "Host name"},
        {"name": "api_token", "type": "secret", "label": "API token"},
        {"name": "mode", "type": "select", "label": "Mode", "options": ["safe", "fast"]},
    ]
    serialized = json.dumps(payload)
    assert fixture["script_sentinel"] not in serialized
    assert fixture["secret_sentinel"] not in serialized
    assert "ansible_template" not in serialized
    assert "cloudinit_template" not in serialized

    with session_scope() as session:
        viewer = session.get(User, fixture["viewer"])
        result = api.deploy(api.DeployBody(
            templateId=fixture["template"], name="wave46-cross-owner",
            deployInputs={"0.0": {
                "hostname": "viewer-host",
                "api_token": "viewer-provided-token",
            }},
        ), user=viewer, session=session)
        job = session.get(Job, result["jobId"])
        assert job and job.created_by == viewer.id and job.status == "queued"

    with session_scope() as session:
        template = session.get(Template, fixture["template"])
        invalid = _expect_http(400, lambda: api._validate_deploy_inputs(
            session, template, {"0.0": {"mode": "unsafe"}},
        ))
        assert "configured options" in invalid.detail
        accepted = json.loads(api._validate_deploy_inputs(
            session, template, {"0.0": {"mode": "fast"}},
        ))
        assert accepted == {"0.0": {"mode": "fast"}}


def test_state_hides_custom_block_but_keeps_its_public_prompt_contract():
    fixture = _public_custom_template_fixture()
    original_px_cache = api._px_cache
    api._px_cache = lambda _connections: {}
    try:
        with session_scope() as session:
            viewer = session.get(User, fixture["viewer"])
            state = api.state(_request(), user=viewer, session=session)
    finally:
        api._px_cache = original_px_cache

    assert fixture["block"] not in {row["key"] for row in state["PALETTE"]}
    template = next(row for row in state["TEMPLATES"]
                    if row["templateId"] == fixture["template"])
    assert [field["name"] for field in template["recipe"][0]["blocks"][0]["askSchema"]] == [
        "hostname", "api_token", "mode",
    ]


def test_select_schema_requires_usable_options_and_matching_default():
    invalid = [
        ({"name": "mode", "type": "select"}, "option"),
        ({"name": "mode", "type": "select", "options": []}, "option"),
        ({"name": "mode", "type": "select", "options": ["safe", ""]}, "non-empty"),
        ({"name": "mode", "type": "select", "options": ["safe", "safe"]}, "duplicate"),
        ({"name": "mode", "type": "select", "options": ["safe"], "default": "fast"}, "default"),
    ]
    for field, expected in invalid:
        problems = input_schema_problems([field])
        assert any(expected in problem.lower() for problem in problems), (field, problems)
        assert lint_block("cloudinit", [field], "", "echo {mode}"), field

    valid = {"name": "mode", "type": "select", "options": ["safe", "fast"], "default": "safe"}
    assert input_schema_problems([valid]) == []
    assert lint_block("cloudinit", [valid], "", "echo {mode}") == []


def test_state_resource_delete_capabilities_match_reference_rules():
    suffix = os.urandom(4).hex()
    with session_scope() as session:
        admin = User(
            email=f"wave46-admin-{suffix}@example.com", name="Admin",
            password_hash="unused", role="admin",
        )
        referenced_block = Block(
            key=f"c-wave46-referenced-{suffix}", kind="custom", builtin=False,
            owner_id=None, name="Referenced block", phase="cloudinit",
            cloudinit_template="echo referenced",
        )
        free_block = Block(
            key=f"c-wave46-free-{suffix}", kind="custom", builtin=False,
            owner_id=None, name="Free block", phase="cloudinit",
            cloudinit_template="echo free",
        )
        referenced_image = Image(
            kind="base", name=f"wave46-referenced-{suffix}",
            source_url="https://example.com/referenced.img", build_status="ready",
        )
        free_image = Image(
            kind="base", name=f"wave46-free-{suffix}",
            source_url="https://example.com/free.img", build_status="ready",
        )
        deployed_image = Image(
            kind="base", name=f"wave46-deployed-{suffix}",
            source_url="https://example.com/deployed.img", build_status="ready",
        )
        session.add(admin)
        session.add(referenced_block)
        session.add(free_block)
        session.add(referenced_image)
        session.add(free_image)
        session.add(deployed_image)
        session.flush()
        template = Template(
            name=f"wave46-reference-{suffix}", owner_id=admin.id, public=False,
            recipe_json=json.dumps([{"blocks": [{"ref": referenced_block.key}]}]),
            base_image_id=referenced_image.id,
        )
        session.add(template)
        session.add(Deployment(
            name=f"wave46-deployment-{suffix}", owner_id=admin.id,
            image_id=deployed_image.id, status="stopped",
        ))
        session.flush()
        ids = {
            "admin": admin.id,
            "referenced_block": referenced_block.key,
            "free_block": free_block.key,
            "referenced_image": referenced_image.id,
            "free_image": free_image.id,
            "deployed_image": deployed_image.id,
        }

    original_px_cache = api._px_cache
    api._px_cache = lambda _connections: {}
    try:
        with session_scope() as session:
            state = api.state(_request(), user=session.get(User, ids["admin"]), session=session)
    finally:
        api._px_cache = original_px_cache

    blocks = {row["key"]: row for row in state["PALETTE"]}
    images = {row["imgId"]: row for row in state["BASE_IMAGES"]}
    assert blocks[ids["referenced_block"]]["canDelete"] is False
    assert blocks[ids["free_block"]]["canDelete"] is True
    assert images[ids["referenced_image"]]["canDelete"] is False
    assert images[ids["deployed_image"]]["canDelete"] is False
    assert images[ids["free_image"]]["canDelete"] is True

    with session_scope() as session:
        admin = session.get(User, ids["admin"])
        _expect_http(409, lambda: api.delete_block(
            ids["referenced_block"], user=admin, session=session,
        ))
        _expect_http(409, lambda: api.delete_image(
            ids["referenced_image"], user=admin, session=session,
        ))


if __name__ == "__main__":
    test_non_owner_gets_only_sanitized_ask_descriptors_and_can_deploy()
    test_state_hides_custom_block_but_keeps_its_public_prompt_contract()
    test_select_schema_requires_usable_options_and_matching_default()
    test_state_resource_delete_capabilities_match_reference_rules()
    print("\nALL WAVE 46 UNIT TESTS PASSED")
