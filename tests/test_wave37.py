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
from datetime import timedelta
from types import SimpleNamespace

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
from app.models import Block, Deployment, Job, Template, User, utcnow  # noqa: E402
from app.security import hash_password  # noqa: E402

init_db()


def _login_req():
    # Minimal stand-in for a Starlette Request: login only touches .session and the
    # client-IP resolution (.client.host + .headers).
    return SimpleNamespace(session={}, client=SimpleNamespace(host="203.0.113.7"), headers={})


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


# --------------------------------------------------------------------------- #
# HIGH — open first-run /api/auth/setup admin-takeover race                      #
# --------------------------------------------------------------------------- #
def test_setup_refused_once_a_user_exists():
    """Setup can only ever mint the very first account; after that it is closed even to
    an unauthenticated caller (the race window is the concern, not a second admin)."""
    _mk_user("w37-setup-guard@example.com")  # a user now exists
    req = _login_req()
    with session_scope() as s:
        _expect_http(400, lambda: api.auth_setup(
            api.SetupBody(email="attacker@evil.example", name="x", password="StrongPass12!"),
            req, session=s))
    print("test_setup_refused_once_a_user_exists OK")


# --------------------------------------------------------------------------- #
# MEDIUM — unbounded rebuild/destroy job flood (worker starvation)              #
# --------------------------------------------------------------------------- #
def test_rebuild_rejected_when_a_job_is_active():
    uid = _mk_user("w37-rebuild-flood@example.com")
    with session_scope() as s:
        dep = Deployment(name="gd-w37-a", owner_id=uid, status="running")
        s.add(dep)
        s.flush()
        s.add(Job(type="deploy", title="x", deployment_id=dep.id, created_by=uid,
                  status="running"))
        s.flush()
        did = dep.id
    with session_scope() as s:
        exc = _expect_http(409, lambda: api._vm_rebuild_transaction(did, s.get(User, uid), s))
    assert "in progress" in exc.detail, exc.detail
    print("test_rebuild_rejected_when_a_job_is_active OK")


def test_destroy_dedups_an_active_destroy():
    uid = _mk_user("w37-destroy-dedup@example.com")
    with session_scope() as s:
        dep = Deployment(name="gd-w37-b", owner_id=uid, status="working")
        s.add(dep)
        s.flush()
        job = Job(type="destroy", title="x", deployment_id=dep.id, created_by=uid,
                  status="queued")
        s.add(job)
        s.flush()
        did, jid = dep.id, job.id
    with session_scope() as s:
        res = api.vm_destroy(did, user=s.get(User, uid), session=s)
    assert res.get("deduped") is True and res["jobId"] == jid, res
    print("test_destroy_dedups_an_active_destroy OK")


def test_destroy_rejected_while_rebuild_active():
    uid = _mk_user("w37-destroy-block@example.com")
    with session_scope() as s:
        dep = Deployment(name="gd-w37-c", owner_id=uid, status="working")
        s.add(dep)
        s.flush()
        s.add(Job(type="rebuild", title="x", deployment_id=dep.id, created_by=uid,
                  status="running"))
        s.flush()
        did = dep.id
    with session_scope() as s:
        _expect_http(409, lambda: api.vm_destroy(did, user=s.get(User, uid), session=s))
    print("test_destroy_rejected_while_rebuild_active OK")


# --------------------------------------------------------------------------- #
# LOW — login user-enumeration (uniform failure, no lockout oracle)             #
# --------------------------------------------------------------------------- #
def test_login_unknown_email_is_generic_401():
    with session_scope() as s:
        exc = _expect_http(401, lambda: api.login(
            api.LoginBody(email="w37-nobody@nowhere.example", password="whatever123!"),
            _login_req(), session=s))
    assert exc.detail == "invalid email or password", exc.detail
    print("test_login_unknown_email_is_generic_401 OK")


def test_login_locked_account_is_generic_401_not_429():
    """A locked account (even with the correct password) returns the same 401 as a wrong
    password — no 429 'account locked' oracle to confirm an email exists."""
    uid = _mk_user("w37-locked@example.com")
    with session_scope() as s:
        u = s.get(User, uid)
        u.locked_until = utcnow() + timedelta(minutes=15)
        s.add(u)
    with session_scope() as s:
        exc = _expect_http(401, lambda: api.login(
            api.LoginBody(email="w37-locked@example.com", password="StrongPass12!"),
            _login_req(), session=s))
    assert exc.detail == "invalid email or password", exc.detail
    print("test_login_locked_account_is_generic_401_not_429 OK")


# --------------------------------------------------------------------------- #
# LOW — logout must revoke the session (epoch bump), not just drop the cookie    #
# --------------------------------------------------------------------------- #
def test_logout_bumps_session_epoch():
    uid = _mk_user("w37-logout@example.com")
    with session_scope() as s:
        before = s.get(User, uid).session_epoch or 0
    req = SimpleNamespace(session={"uid": uid, "sv": before})
    with session_scope() as s:
        api.logout(req, session=s)
        after = s.get(User, uid).session_epoch or 0
    assert after == before + 1, (before, after)
    assert req.session == {}, req.session  # this browser's cookie is cleared too
    print("test_logout_bumps_session_epoch OK")


# --------------------------------------------------------------------------- #
# LOW — snippet path allowlist must reject '.' / '..' traversal tokens          #
# --------------------------------------------------------------------------- #
def test_snippet_path_token_rejects_traversal():
    from app.proxmox import _safe_path_token
    for good in ("gd-deploy-8000.yml", "local", "local.test", "cephfs"):
        assert _safe_path_token(good), good
    for bad in ("", ".", "..", "../etc/passwd", ".ssh", "a/b", "a b", "..\\x"):
        assert not _safe_path_token(bad), bad
    print("test_snippet_path_token_rejects_traversal OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("wave37 OK")
