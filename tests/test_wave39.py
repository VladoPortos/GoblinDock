"""Wave 39 — custom Ansible blocks are admin-only (control-node RCE closure).

An Ansible task runs on the shared GoblinDock control node (delegate_to / lookups /
templating), so a non-admin authoring or deploying custom Ansible = control-plane RCE.
Custom cloud-init blocks stay open to everyone (they run inside the deployer's own VM).

Run (Linux/WSL/CI):
  GOBLINDOCK_DEV=1 .venv/bin/python -m pytest tests/test_wave39.py
"""
import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave39-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault(
    "GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test")
)

from fastapi import HTTPException  # noqa: E402

from app import api  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Block, User  # noqa: E402
from app.security import hash_password  # noqa: E402

init_db()

_GOOD_ANSIBLE = "- name: x\n  ansible.builtin.debug: { msg: hi }"


def _mk_user(email: str, role: str = "user") -> int:
    with session_scope() as s:
        user = User(email=email, name=email.split("@", 1)[0],
                    password_hash=hash_password("StrongPass12!"), role=role)
        s.add(user)
        s.flush()
        return user.id


def _expect_http(code: int, fn):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == code, (exc.status_code, exc.detail)
        return exc
    raise AssertionError(f"expected HTTPException {code}")


def _body(phase: str):
    return api.BlockBody(name="blk", phase=phase,
                         ansible_template=_GOOD_ANSIBLE if phase == "ansible" else "",
                         cloudinit_template="echo hi" if phase == "cloudinit" else "")


# --------------------------------------------------------------------------- #
# authoring gates                                                              #
# --------------------------------------------------------------------------- #
def test_nonadmin_cannot_create_ansible_block():
    uid = _mk_user("w39-na-create@example.com")
    with session_scope() as s:
        exc = _expect_http(403, lambda: api.create_block(_body("ansible"),
                                                         user=s.get(User, uid), session=s))
    assert "admin" in exc.detail.lower(), exc.detail
    print("test_nonadmin_cannot_create_ansible_block OK")


def test_nonadmin_can_create_cloudinit_block():
    uid = _mk_user("w39-na-ci@example.com")
    with session_scope() as s:
        res = api.create_block(_body("cloudinit"), user=s.get(User, uid), session=s)
    assert res["ok"] is True
    print("test_nonadmin_can_create_cloudinit_block OK")


def test_admin_can_create_ansible_block():
    aid = _mk_user("w39-admin-create@example.com", role="admin")
    with session_scope() as s:
        res = api.create_block(_body("ansible"), user=s.get(User, aid), session=s)
    assert res["ok"] is True
    print("test_admin_can_create_ansible_block OK")


def test_nonadmin_cannot_fork_ansible_block():
    uid = _mk_user("w39-na-fork@example.com")
    with session_scope() as s:
        s.add(Block(key="b-w39-builtin-ans", kind="builtin", builtin=True, phase="ansible",
                    name="builtin ansible", ansible_template=_GOOD_ANSIBLE))
    with session_scope() as s:
        _expect_http(403, lambda: api.fork_block("b-w39-builtin-ans",
                                                 user=s.get(User, uid), session=s))
    print("test_nonadmin_cannot_fork_ansible_block OK")


# --------------------------------------------------------------------------- #
# template save / deploy trust check (also covers pre-existing legacy blocks)  #
# --------------------------------------------------------------------------- #
def test_template_save_rejects_nonadmin_custom_ansible_block():
    uid = _mk_user("w39-tpl-untrusted@example.com")
    with session_scope() as s:
        # A "legacy" non-admin custom ansible block (created directly, bypassing the gate).
        s.add(Block(key="c-w39-legacy-ans", kind="custom", builtin=False, owner_id=uid,
                    phase="ansible", name="legacy ansible", ansible_template=_GOOD_ANSIBLE))
    with session_scope() as s:
        exc = _expect_http(403, lambda: api.save_template(
            api.TemplateBody(name="t",
                             recipe=[{"blocks": [{"ref": "c-w39-legacy-ans", "inputs": {}}]}]),
            user=s.get(User, uid), session=s))
    assert "admin" in exc.detail.lower(), exc.detail
    print("test_template_save_rejects_nonadmin_custom_ansible_block OK")


def test_template_save_allows_admin_custom_ansible_block():
    aid = _mk_user("w39-admin-tpl@example.com", role="admin")
    with session_scope() as s:
        s.add(Block(key="c-w39-admin-ans", kind="custom", builtin=False, owner_id=aid,
                    phase="ansible", name="admin ansible", ansible_template=_GOOD_ANSIBLE))
    with session_scope() as s:
        res = api.save_template(
            api.TemplateBody(name="t-admin",
                             recipe=[{"blocks": [{"ref": "c-w39-admin-ans", "inputs": {}}]}]),
            user=s.get(User, aid), session=s)
    assert res["ok"] is True
    print("test_template_save_allows_admin_custom_ansible_block OK")


def test_assert_trusted_allows_builtin_and_cloudinit():
    uid = _mk_user("w39-assert@example.com")
    with session_scope() as s:
        s.add(Block(key="b-w39-ans", kind="builtin", builtin=True, phase="ansible",
                    name="ba", ansible_template=_GOOD_ANSIBLE))
        s.add(Block(key="c-w39-ci", kind="custom", builtin=False, owner_id=uid,
                    phase="cloudinit", name="cc", cloudinit_template="echo hi"))
    recipe = [{"blocks": [{"ref": "b-w39-ans"}, {"ref": "c-w39-ci"}]}]
    with session_scope() as s:
        api._assert_trusted_ansible_blocks(s, recipe)  # must NOT raise
    print("test_assert_trusted_allows_builtin_and_cloudinit OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("wave39 OK")
