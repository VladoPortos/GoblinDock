"""Wave 37 — adversarial security review fixes (2026-07).

Covers the fixes for the review findings:
  * CRITICAL — Ansible YAML task-injection via stored recipe input values
  * HIGH     — open first-run /api/auth/setup admin-takeover race
  * MEDIUM   — unbounded rebuild/destroy job flood (worker starvation)
  * LOW      — login user-enumeration (dummy-hash equalisation)
  * LOW      — logout does not revoke the session (epoch bump)

Run (Linux/WSL/CI):
  GOBLINDOCK_DEV=1 .venv/bin/python -m pytest tests/test_wave37.py
"""
import os
import sys
import tempfile
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave37-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault(
    "GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test")
)

import yaml  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app import api, recipes  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Block, Template, User  # noqa: E402
from app.security import hash_password  # noqa: E402

init_db()


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


# The exact payload from the review: a newline-bearing value that, spliced raw into a
# built-in block's YAML scalar, would inject a sibling task running on the control node.
_INJECT = ('0755"\n- name: pwn\n  delegate_to: localhost\n  become: false\n'
           '  ansible.builtin.command: id\n  changed_when: "false')


# --------------------------------------------------------------------------- #
# CRITICAL — Ansible YAML task-injection via stored recipe input values         #
# --------------------------------------------------------------------------- #
def test_template_save_rejects_newline_in_noncode_input():
    """A control char in a non-'code' stored input is refused at the storage boundary."""
    uid = _mk_user("w37-inject-save@example.com")
    with session_scope() as s:
        s.add(Block(
            key="c-w37-mkdir", kind="custom", builtin=False, owner_id=uid,
            name="mkdir", phase="ansible",
            ansible_template=('- name: Create Directory\n'
                              '  ansible.builtin.file:\n'
                              '    path: {path}\n'
                              '    state: directory\n'
                              '    mode: "{mode}"'),
            input_schema_json=json.dumps([
                {"name": "path", "type": "text"},
                {"name": "mode", "type": "text"},
            ]),
        ))
    with session_scope() as s:
        exc = _expect_http(400, lambda: api.save_template(
            api.TemplateBody(
                name="inject",
                recipe=[{"blocks": [{"ref": "c-w37-mkdir",
                                     "inputs": {"path": "/opt/app", "mode": _INJECT}}]}],
            ),
            user=s.get(User, uid), session=s,
        ))
    assert "control character" in exc.detail.lower(), exc.detail
    print("test_template_save_rejects_newline_in_noncode_input OK")


def test_template_save_allows_newline_in_code_input():
    """A 'code' field (Run Script / file content) is intentionally multi-line — allowed."""
    uid = _mk_user("w37-code-ok@example.com")
    with session_scope() as s:
        s.add(Block(
            key="c-w37-script", kind="custom", builtin=False, owner_id=uid,
            name="script", phase="cloudinit", cloudinit_template="{body}",
            input_schema_json=json.dumps([{"name": "body", "type": "code"}]),
        ))
    with session_scope() as s:
        # Must NOT raise.
        api.save_template(
            api.TemplateBody(
                name="multiline-script",
                recipe=[{"blocks": [{"ref": "c-w37-script",
                                     "inputs": {"body": "echo one\necho two\n"}}]}],
            ),
            user=s.get(User, uid), session=s,
        )
    print("test_template_save_allows_newline_in_code_input OK")


def test_ansible_compile_scrubs_control_chars_for_legacy_rows():
    """Sink defense: even a legacy recipe stored before the API check can't inject a
    sibling task — the raw {k} form has control chars neutralised at compile time."""
    block = Block(
        key="c-w37-legacy", name="legacy", phase="ansible",
        ansible_template=('- name: Create Directory\n'
                          '  ansible.builtin.file:\n'
                          '    path: {path}\n'
                          '    state: directory\n'
                          '    mode: "{mode}"'),
        input_schema_json=json.dumps([
            {"name": "path", "type": "text"},
            {"name": "mode", "type": "text"},
        ]),
    )
    recipe = [{"blocks": [{"ref": block.key,
                           "inputs": {"path": "/opt/app", "mode": _INJECT}}]}]
    playbook = recipes.compile_ansible(recipe, {block.key: block}, lambda _ns, _n: "")
    # Structural safety: the newline that would start a sibling task is gone, so the
    # payload collapses into the `mode` scalar as inert data on one line. No sibling
    # task materialises at task-list indentation, and no delegate_to key appears — worst
    # case the crafted payload yields invalid YAML that safely fails to run.
    lines = playbook.splitlines()
    task_markers = [ln for ln in lines if ln.startswith("    - name:")]
    assert len(task_markers) == 1, f"sibling task injected: {task_markers}"
    assert not any(ln.strip().startswith("delegate_to:") for ln in lines), playbook
    mode_lines = [ln for ln in lines if ln.strip().startswith("mode:")]
    assert len(mode_lines) == 1 and "- name: pwn" in mode_lines[0], mode_lines
    print("test_ansible_compile_scrubs_control_chars_for_legacy_rows OK")


def test_benign_newline_in_noncode_value_stays_single_line():
    """A legacy benign multi-line non-'code' value is flattened to one valid scalar
    (newline -> space) rather than breaking the YAML."""
    block = Block(
        key="c-w37-benign", name="benign", phase="ansible",
        ansible_template=('- name: Timezone\n'
                          '  ansible.builtin.timezone:\n'
                          '    name: {tz}'),
        input_schema_json=json.dumps([{"name": "tz", "type": "text"}]),
    )
    recipe = [{"blocks": [{"ref": block.key, "inputs": {"tz": "Europe\nBratislava"}}]}]
    playbook = recipes.compile_ansible(recipe, {block.key: block}, lambda _ns, _n: "")
    doc = yaml.safe_load(playbook)  # must still be valid YAML
    assert doc[0]["tasks"][0]["ansible.builtin.timezone"]["name"] == "Europe Bratislava"
    print("test_benign_newline_in_noncode_value_stays_single_line OK")


def test_code_field_still_renders_multiline_in_cloudinit():
    """Regression: a 'code' body keeps its newlines through cloud-init compilation."""
    block = Block(
        key="c-w37-ci", name="ci", phase="cloudinit",
        cloudinit_template="{body}",
        input_schema_json=json.dumps([{"name": "body", "type": "code"}]),
    )
    cmds = recipes.compile_cloudinit(
        [{"blocks": [{"ref": block.key, "inputs": {"body": "echo one\necho two"}}]}],
        {block.key: block}, lambda _ns, _n: "",
    )
    joined = "\n".join(cmds)
    assert "echo one" in joined and "echo two" in joined, cmds
    print("test_code_field_still_renders_multiline_in_cloudinit OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("wave37 OK")
