"""Wave 20 — account-creation blocks moved to the new 'Accounts' step.

Account creation is a prerequisite, not a 'Configure' step: the builder gained an
'Accounts' group between 'OS Setup' and 'Install', and the account blocks (User & SSH
Key, Create User, Console Password) now live there. So a post-boot 'Create User' block
runs BEFORE a user-scoped 'Install' block like Claude Code, instead of after it.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave20.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave20-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from app.db import init_db, session_scope  # noqa: E402
from app.models import Block  # noqa: E402
from app.recipes import compile_ansible  # noqa: E402
from app.seed import seed_blocks  # noqa: E402
from sqlmodel import select  # noqa: E402

init_db()
seed_blocks()

_NO_SECRETS = lambda kind, name: ""  # noqa: E731


def _blocks_by_key() -> dict:
    with session_scope() as s:
        return {b.key: Block(**b.model_dump()) for b in s.exec(select(Block)).all()}


def test_account_blocks_in_accounts_section():
    bbk = _blocks_by_key()
    for key in ("b-ssh", "b-user", "b-conpw"):
        assert bbk[key].section == "Accounts", f"{key} section is {bbk[key].section!r}"
    print("test_account_blocks_in_accounts_section OK")


def test_create_user_compiles_before_claude_code():
    bbk = _blocks_by_key()
    # Mirror what the builder serialises: Accounts step before Install step.
    recipe = [
        {"id": "s-acc", "name": "Accounts", "blocks": [
            {"ref": "b-user", "name": "Create User", "inputs": {"user": "cloudauto"}}]},
        {"id": "s-inst", "name": "Install", "blocks": [
            {"ref": "b-claudecode", "name": "Claude Code", "inputs": {"user": "cloudauto"}}]},
    ]
    pb = compile_ansible(recipe, bbk, _NO_SECRETS, name="t")
    i_user = pb.find("name: Create User")
    i_claude = pb.find("name: Install Claude Code")
    assert i_user != -1 and i_claude != -1, pb
    assert i_user < i_claude, f"Create User must compile before Claude Code\n{pb}"
    print("test_create_user_compiles_before_claude_code OK")


if __name__ == "__main__":
    test_account_blocks_in_accounts_section()
    test_create_user_compiles_before_claude_code()
    print("\nALL WAVE 20 UNIT TESTS PASSED")
