"""Wave 38 — migrate persisted legacy b-ssh placements before block pruning."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave38-test.sqlite3")
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from fastapi import HTTPException                 # noqa: E402
from sqlmodel import Session, select              # noqa: E402
from app.db import engine, init_db, session_scope  # noqa: E402
from app import api, recipes, seed                 # noqa: E402
from app import serialize as S                     # noqa: E402
from app.models import (                           # noqa: E402
    Block, Connection, Deployment, Image, IpAllocation, Job, Network, Template,
    User,
)
from app.security import hash_password             # noqa: E402

init_db()


def _legacy_template(inputs, ask, *, legacy_block=False):
    recipe = [{
        "id": "s-legacy",
        "name": "Legacy Accounts",
        "metadata": {"color": "rose", "collapsed": False},
        "blocks": [
            {
                "id": "placed-ssh-1",
                "ref": "b-ssh",
                "name": "Original SSH User",
                "inputs": inputs,
                "ask": ask,
                "metadata": {"x": 17, "locked": True},
            },
            {
                "id": "placed-other-1",
                "ref": "c-unrelated",
                "name": "Unrelated placement",
                "inputs": {"value": "keep"},
                "metadata": {"x": 23},
            },
        ],
    }]
    with session_scope() as s:
        template = Template(
            name="legacy-ssh-" + os.urandom(3).hex(),
            recipe_json=json.dumps(recipe),
        )
        s.add(template)
        if legacy_block:
            s.add(Block(
                key="b-ssh", name="Legacy SSH", kind="builtin", builtin=True,
                phase="cloudinit", input_schema_json="[]",
            ))
        s.commit()
        s.refresh(template)
        return template.id


def _placed(template_id):
    with session_scope() as s:
        recipe = json.loads(s.get(Template, template_id).recipe_json)
    return recipe[0]["blocks"][0]


def _template_json(template_id):
    with session_scope() as s:
        return s.get(Template, template_id).recipe_json


def test_seed_migrates_b_ssh_before_pruning():
    # This template has no legacy Block row: migration must use persisted recipes,
    # not the catalog row that seed_blocks() is about to prune.
    tid_absent = _legacy_template({
        "user": "alice", "password": "pw", "public_key": "ssh-ed25519 AAAA",
        "sudo": False, "ssh_password_login": True,
    }, ask=["password", "sudo"])
    # This one still has the old built-in row at migration time; both paths must
    # produce the same placement and the row must then be removed.
    tid_present = _legacy_template({
        "user": "bob", "password": "pw2", "public_key": "ssh-ed25519 BBBB",
        "sudo": True, "ssh_password_login": False,
    }, ask=["user", "sudo", "not-an-input"], legacy_block=True)

    seed.seed_blocks()

    placed = _placed(tid_absent)
    assert placed["ref"] == "b-user"
    assert placed["inputs"] == {
        "user": "alice", "password": "pw", "public_key": "ssh-ed25519 AAAA",
        "ssh_password_login": True, "shell": "/bin/bash", "home": "",
        "groups": ["sudo"], "sudoers": True, "nopasswd": False,
    }
    assert placed["ask"] == ["password", "nopasswd"]
    assert placed["id"] == "placed-ssh-1"
    assert placed["name"] == "Original SSH User"
    assert placed["metadata"] == {"x": 17, "locked": True}

    placed_present = _placed(tid_present)
    assert placed_present["ref"] == "b-user"
    assert placed_present["inputs"]["nopasswd"] is True
    assert placed_present["ask"] == ["user", "nopasswd"]
    with session_scope() as s:
        assert s.exec(select(Block).where(Block.key == "b-ssh")).first() is None
        recipe = json.loads(s.get(Template, tid_absent).recipe_json)
        assert recipe[0]["id"] == "s-legacy"
        assert recipe[0]["metadata"] == {"color": "rose", "collapsed": False}
        assert recipe[0]["blocks"][1]["ref"] == "c-unrelated"

    first = _template_json(tid_absent)
    first_present = _template_json(tid_present)
    seed.seed_blocks()
    assert _template_json(tid_absent) == first
    assert _template_json(tid_present) == first_present


def _expect_http(code, fn):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == code, (exc.status_code, exc.detail)
        return exc
    raise AssertionError(f"expected HTTPException {code}")


def _mk_user(email):
    with session_scope() as s:
        user = User(
            email=email, name=email.split("@", 1)[0],
            password_hash=hash_password("StrongPass12!"),
        )
        s.add(user)
        s.flush()
        return user.id


def _sensitive_fixture():
    suffix = os.urandom(3).hex()
    author_id = _mk_user(f"w38-author-{suffix}@example.com")
    deployer_id = _mk_user(f"w38-deployer-{suffix}@example.com")
    with session_scope() as s:
        connection = Connection(
            name=f"w38-conn-{suffix}", host="pve", token_id="u@pve!token", node="pve",
        )
        image = Image(
            kind="base", name=f"w38-image-{suffix}",
            source_url="https://example.com/base.img", build_status="ready",
        )
        s.add(connection)
        s.add(image)
        s.flush()
        network = Network(
            connection_id=connection.id, name=f"w38-net-{suffix}", mode="static",
            subnet_cidr="10.38.0.0/24", range_start="10.38.0.20",
            range_end="10.38.0.30", gateway="10.38.0.1",
        )
        block = Block(
            key=f"c-w38-sensitive-{suffix}", kind="custom", builtin=False,
            owner_id=author_id, name="Sensitive setup", phase="cloudinit",
            input_schema_json=json.dumps([
                {"name": "password", "type": "password"},
                {"name": "token", "type": "secret"},
                {"name": "note", "type": "text"},
            ]),
            cloudinit_template="echo configured",
        )
        s.add(network)
        s.add(block)
        s.flush()
        return {
            "author": author_id,
            "deployer": deployer_id,
            "connection": connection.id,
            "image": image.id,
            "network": network.id,
            "block": block.key,
        }


def _recipe(ref, *, password="", token="", note="", ask=None):
    placed = {
        "ref": ref,
        "inputs": {"password": password, "token": token, "note": note},
    }
    if ask is not None:
        placed["ask"] = ask
    return [{"blocks": [placed]}]


def _body(fixture, *, public, recipe):
    return api.TemplateBody(
        name="w38-sensitive-template", public=public, recipe=recipe,
        baseImageId=fixture["image"], connectionId=fixture["connection"],
        networkId=fixture["network"],
    )


def _save(fixture, *, public, recipe):
    with session_scope() as s:
        return api.save_template(
            _body(fixture, public=public, recipe=recipe),
            user=s.get(User, fixture["author"]), session=s,
        )


def _insert_template(fixture, recipe, *, public=True):
    with session_scope() as s:
        template = Template(
            name="w38-imported-" + os.urandom(3).hex(),
            owner_id=fixture["author"], public=public,
            base_image_id=fixture["image"], connection_id=fixture["connection"],
            network_id=fixture["network"], recipe_json=json.dumps(recipe),
        )
        s.add(template)
        s.flush()
        return template.id


def _row_counts(session):
    return tuple(len(session.exec(select(model)).all()) for model in (
        Deployment, Job, IpAllocation,
    ))


def test_public_literal_is_rejected_without_echo():
    fixture = _sensitive_fixture()
    for field, value in (("password", "DO-NOT-ECHO"), ("token", "TOKEN-NOT-ECHO")):
        inputs = {"password": "", "token": "", "note": "public"}
        inputs[field] = value
        exc = _expect_http(400, lambda inputs=inputs: _save(
            fixture, public=True,
            recipe=_recipe(fixture["block"], **inputs),
        ))
        assert field in str(exc.detail)
        assert value not in str(exc.detail)


def test_public_ask_and_exact_deployer_secret_references_are_allowed():
    fixture = _sensitive_fixture()
    assert recipes.is_deployer_secret_ref("{{ secrets.DEPLOY_KEY }}") is True
    assert recipes.is_deployer_secret_ref("{{  secrets.DEPLOY_KEY  }}") is True
    assert recipes.is_deployer_secret_ref("prefix {{ secrets.DEPLOY_KEY }}") is False
    assert recipes.is_deployer_secret_ref("{{ secrets.DEPLOY-KEY }}") is False

    assert _save(
        fixture, public=True,
        recipe=_recipe(
            fixture["block"], password="", token="{{ secrets.DEPLOY_KEY }}",
            ask=["password"],
        ),
    )["ok"]
    exc = _expect_http(400, lambda: _save(
        fixture, public=True,
        recipe=_recipe(
            fixture["block"], password="prefix {{ secrets.DEPLOY_KEY }}",
            ask=["password"],
        ),
    ))
    assert "password" in str(exc.detail)
    assert "DEPLOY_KEY" not in str(exc.detail)


def test_public_edit_rejects_literal_but_private_and_owner_paths_remain_allowed():
    fixture = _sensitive_fixture()
    private_recipe = _recipe(
        fixture["block"], password="PRIVATE-AUTHOR-VALUE", token="PRIVATE-TOKEN",
    )
    assert _save(fixture, public=False, recipe=private_recipe)["ok"]
    with session_scope() as s:
        template = s.exec(select(Template).where(
            Template.owner_id == fixture["author"],
            Template.name == "w38-sensitive-template",
        )).first()
        template_id = template.id
        exc = _expect_http(400, lambda: api.edit_template_ep(
            template_id, _body(fixture, public=True, recipe=private_recipe),
            user=s.get(User, fixture["author"]), session=s,
        ))
        assert "password" in str(exc.detail)
        assert "PRIVATE-AUTHOR-VALUE" not in str(exc.detail)

    imported_id = _insert_template(fixture, private_recipe, public=True)
    with Session(engine) as s:
        result = api.deploy(
            api.DeployBody(templateId=imported_id, name="w38-owner-deploy"),
            user=s.get(User, fixture["author"]), session=s,
        )
        assert result["ok"]


def test_cross_owner_missing_sensitive_ask_answer_cannot_fallback_to_author_value():
    fixture = _sensitive_fixture()
    author_value = "AUTHOR-FALLBACK-MUST-NOT-RUN"
    template_id = _insert_template(
        fixture,
        _recipe(fixture["block"], password=author_value, ask=["password"]),
    )
    with Session(engine) as s:
        before = _row_counts(s)
        exc = _expect_http(409, lambda: api.deploy(
            api.DeployBody(templateId=template_id, name="w38-missing-answer"),
            user=s.get(User, fixture["deployer"]), session=s,
        ))
        assert "password" in str(exc.detail)
        assert author_value not in str(exc.detail)
        assert _row_counts(s) == before


def test_cross_owner_imported_literal_is_rejected_before_any_rows_are_inserted():
    fixture = _sensitive_fixture()
    author_value = "IMPORTED-AUTHOR-SECRET"
    template_id = _insert_template(
        fixture, _recipe(fixture["block"], password=author_value),
    )
    with Session(engine) as s:
        before = _row_counts(s)
        exc = _expect_http(409, lambda: api.deploy(
            api.DeployBody(templateId=template_id, name="w38-unsafe-import"),
            user=s.get(User, fixture["deployer"]), session=s,
        ))
        assert "password" in str(exc.detail)
        assert author_value not in str(exc.detail)
        assert _row_counts(s) == before


def test_cross_owner_unknown_block_is_rejected_before_any_rows_are_inserted():
    fixture = _sensitive_fixture()
    unknown_value = "UNKNOWN-BLOCK-PRIVATE-VALUE"
    template_id = _insert_template(
        fixture,
        [{"blocks": [{"ref": "b-pruned", "inputs": {"note": unknown_value}}]}],
    )
    with Session(engine) as s:
        before = _row_counts(s)
        exc = _expect_http(409, lambda: api.deploy(
            api.DeployBody(templateId=template_id, name="w38-unknown-import"),
            user=s.get(User, fixture["deployer"]), session=s,
        ))
        assert "b-pruned" in str(exc.detail)
        assert unknown_value not in str(exc.detail)
        assert _row_counts(s) == before


def test_cross_owner_sensitive_ask_answer_and_exact_ref_deploy():
    fixture = _sensitive_fixture()
    template_id = _insert_template(
        fixture,
        _recipe(
            fixture["block"], password="", token="{{ secrets.DEPLOY_TOKEN }}",
            ask=["password"],
        ),
    )
    with Session(engine) as s:
        result = api.deploy(
            api.DeployBody(
                templateId=template_id, name="w38-safe-cross-owner",
                deployInputs={"0.0": {"password": "DEPLOYER-PROVIDED"}},
            ),
            user=s.get(User, fixture["deployer"]), session=s,
        )
        assert result["ok"]


def test_unknown_legacy_block_masks_all_nonempty_inputs():
    recipe = [{"blocks": [{"ref": "b-pruned", "inputs": {
        "password": "p", "note": "private", "empty": "",
    }}]}]
    with Session(engine) as s:
        assert S._mask_recipe_passwords(s, recipe)[0]["blocks"][0]["inputs"] == {
            "password": "********", "note": "********", "empty": "",
        }


if __name__ == "__main__":
    test_seed_migrates_b_ssh_before_pruning()
    test_public_literal_is_rejected_without_echo()
    test_public_ask_and_exact_deployer_secret_references_are_allowed()
    test_public_edit_rejects_literal_but_private_and_owner_paths_remain_allowed()
    test_cross_owner_missing_sensitive_ask_answer_cannot_fallback_to_author_value()
    test_cross_owner_imported_literal_is_rejected_before_any_rows_are_inserted()
    test_cross_owner_unknown_block_is_rejected_before_any_rows_are_inserted()
    test_cross_owner_sensitive_ask_answer_and_exact_ref_deploy()
    test_unknown_legacy_block_masks_all_nonempty_inputs()
    print("\nALL WAVE 38 UNIT TESTS PASSED")
