"""Wave 19 — sink hardening of the 'user' input.

A block 'user' value becomes a Linux/PostgreSQL username that lands RAW in ansible
YAML across several blocks (become_user, owner/group, /home/<user> paths, module
name args). _merged_inputs now restricts it to a safe username charset, so a crafted
value cannot inject sibling YAML keys, traverse paths, or break a scalar. Cloud-init
already shell-quoted every value; this closes the ansible side (and path traversal).

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave19.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave19-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

import yaml  # noqa: E402

from app.db import init_db, session_scope  # noqa: E402
from app.models import Block  # noqa: E402
from app.recipes import compile_ansible, compile_cloudinit  # noqa: E402
from app.seed import seed_blocks  # noqa: E402
from sqlmodel import select  # noqa: E402

init_db()
seed_blocks()

_NO_SECRETS = lambda kind, name: ""  # noqa: E731


def _blocks_by_key() -> dict:
    with session_scope() as s:
        return {b.key: Block(**b.model_dump()) for b in s.exec(select(Block)).all()}


def _recipe(ref: str, inputs: dict) -> list:
    return [{"id": "s1", "name": "S", "blocks": [{"ref": ref, "name": ref, "inputs": inputs}]}]


# Newline + YAML key injection + a shell payload, all in the username field.
EVIL = "goblin\n  ansible.builtin.shell: touch /tmp/pwned\n#"
TRAVERSAL = "../../etc/cron.d/x"


def test_ansible_user_injection_neutralised():
    bbk = _blocks_by_key()
    # b-claudemd is an ansible-phase block that puts {user} in paths + owner/group.
    recipe = _recipe("b-claudemd", {"user": EVIL, "content": "hello"})
    pb = compile_ansible(recipe, bbk, _NO_SECRETS, name="t")
    # 1) the shell payload never reaches the output
    assert "touch /tmp/pwned" not in pb, pb
    # 2) the playbook is still valid YAML (no injected sibling keys / broken scalar)
    plays = yaml.safe_load(pb)
    assert isinstance(plays, list) and len(plays) == 1, plays
    tasks = plays[0]["tasks"]
    # 3) exactly the two b-claudemd tasks — nothing injected
    assert len(tasks) == 2, [t.get("name") for t in tasks]
    # 4) the username was reduced to its safe charset (and capped at 32 chars)
    assert "goblinansiblebuiltinshelltouchtm" in pb, pb
    print("test_ansible_user_injection_neutralised OK")


def test_ansible_user_path_traversal_blocked():
    bbk = _blocks_by_key()
    recipe = _recipe("b-claudemd", {"user": TRAVERSAL, "content": "x"})
    pb = compile_ansible(recipe, bbk, _NO_SECRETS, name="t")
    assert ".." not in pb and "/home/etccrondx/.claude" in pb, pb
    assert yaml.safe_load(pb)  # valid YAML
    print("test_ansible_user_path_traversal_blocked OK")


def test_cloudinit_user_sanitised():
    bbk = _blocks_by_key()
    # b-conpw is a cloud-init-phase block that uses {user}.
    recipe = _recipe("b-conpw", {"user": TRAVERSAL, "password": "Abc12345"})
    cmds = "\n".join(compile_cloudinit(recipe, bbk, _NO_SECRETS))
    assert ".." not in cmds, cmds
    assert "etccrondx" in cmds, cmds
    print("test_cloudinit_user_sanitised OK")


def test_normal_username_unchanged():
    bbk = _blocks_by_key()
    pb = compile_ansible(_recipe("b-claudemd", {"user": "goblin", "content": "x"}),
                         bbk, _NO_SECRETS, name="t")
    assert "/home/goblin/.claude" in pb and "owner: goblin" in pb, pb
    print("test_normal_username_unchanged OK")


if __name__ == "__main__":
    test_ansible_user_injection_neutralised()
    test_ansible_user_path_traversal_blocked()
    test_cloudinit_user_sanitised()
    test_normal_username_unchanged()
    print("\nALL WAVE 19 UNIT TESTS PASSED")
