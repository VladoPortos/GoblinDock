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

from sqlmodel import select                       # noqa: E402
from app.db import init_db, session_scope         # noqa: E402
from app import seed                               # noqa: E402
from app.models import Block, Template             # noqa: E402

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


if __name__ == "__main__":
    test_seed_migrates_b_ssh_before_pruning()
    print("\nALL WAVE 38 UNIT TESTS PASSED")
