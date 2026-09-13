"""Wave 61 — selected 2026-09 security review remediations.

Run: GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave61.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import tempfile
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
for _name in (
    "GOBLINDOCK_MAX_CORES", "GOBLINDOCK_MAX_RAM_MB", "GOBLINDOCK_MAX_DISK_GB",
    "GOBLINDOCK_MAX_VMS_PER_USER", "GOBLINDOCK_MAX_ACTIVE_JOBS_PER_USER",
):
    os.environ.pop(_name, None)
_DB = os.path.join(tempfile.gettempdir(), "gd-wave61-test.sqlite3")
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

import yaml  # noqa: E402
import ansible_runner  # noqa: E402
from sqlmodel import select  # noqa: E402

from app.db import init_db, session_scope  # noqa: E402
from app.models import Block, Connection, Deployment, Job, User  # noqa: E402
from app.recipes import compile_ansible  # noqa: E402
from app.seed import seed_blocks  # noqa: E402
from app import api, appsettings  # noqa: E402
from app import main as app_main  # noqa: E402
from app.security import hash_password  # noqa: E402
from fastapi import HTTPException  # noqa: E402

init_db()
seed_blocks()


def _blocks() -> dict[str, Block]:
    with session_scope() as session:
        return {
            block.key: Block(**block.model_dump())
            for block in session.exec(select(Block)).all()
        }


def test_run_script_is_literal_unsafe_ansible_data():
    """Removing the unsafe tag would make Ansible evaluate the pipe lookup on the
    GoblinDock controller instead of sending the literal text to the tenant VM."""
    script = "printf '%s' '{{ lookup(\"pipe\", \"printf CONTROLLER_EVALUATED\") }}'"
    blocks = _blocks()
    playbook = compile_ansible(
        [{"blocks": [{"ref": "b-script", "inputs": {"script": script}}]}],
        blocks,
        lambda _namespace, _name: "",
        name="unsafe-input-regression",
    )
    # The production play runs with privilege escalation on the VM. The local
    # integration target only exercises Ansible's controller templating boundary.
    playbook = playbook.replace("  become: true\n", "  become: false\n", 1)

    with tempfile.TemporaryDirectory(prefix="gd-wave61-ansible-") as private_data_dir:
        project_dir = os.path.join(private_data_dir, "project")
        os.makedirs(project_dir)
        with open(os.path.join(project_dir, "playbook.yml"), "w", encoding="utf-8") as handle:
            handle.write(playbook)
        result = ansible_runner.run(
            private_data_dir=private_data_dir,
            playbook="playbook.yml",
            inventory={"all": {"hosts": {"localhost": {
                "ansible_connection": "local",
                "ansible_python_interpreter": sys.executable,
            }}}},
            quiet=True,
        )
        events = list(result.events)

    failures = [
        event.get("event_data", {}).get("res")
        for event in events
        if event.get("event") in ("runner_on_failed", "runner_on_unreachable")
    ]
    assert result.status == "successful" and result.rc == 0, failures
    task_result = next(
        event["event_data"]["res"]
        for event in events
        if event.get("event") == "runner_on_ok"
        and event.get("event_data", {}).get("task") == "Run Script"
    )
    assert task_result["stdout"] == '{{ lookup("pipe", "printf CONTROLLER_EVALUATED") }}'


def test_runtime_playbook_keeps_seeded_script_and_yaml_shapes():
    """The hardening must not turn lists/bools into strings or alter multiline
    scripts that existing seeded blocks already compile."""
    blocks = _blocks()
    recipe = [{"blocks": [
        {"ref": "b-apt", "inputs": {"packages": ["git", "curl"]}},
        {"ref": "b-user", "inputs": {
            "user": "deploy", "groups": ["sudo", "docker"],
            "password": "safe secret", "ssh_password_login": False,
        }},
        {"ref": "b-script", "inputs": {"script": "echo one\necho two"}},
    ]}]
    playbook = compile_ansible(recipe, blocks, lambda _namespace, _name: "", name="compat")
    document = yaml.safe_load(playbook)
    tasks = document[0]["tasks"]

    apt = next(task for task in tasks if task.get("name") == "Install Packages (apt)")
    assert apt["ansible.builtin.apt"]["name"] == ["git", "curl"]
    password = next(task for task in tasks if task.get("name") == "Set login password")
    assert password["when"] is True
    ssh_login = next(task for task in tasks if task.get("name") == "Allow SSH password login")
    assert ssh_login["when"] is False
    run_script = next(task for task in tasks if task.get("name") == "Run Script")
    assert run_script["ansible.builtin.shell"] == "echo one\necho two\n"


def test_default_vm_and_active_job_quotas_bound_non_admins():
    with session_scope() as session:
        user = User(email="quota-user@example.com", name="Quota", password_hash="x")
        admin = User(email="quota-admin@example.com", name="Admin", password_hash="x", role="admin")
        session.add(user)
        session.add(admin)
        session.flush()
        for index in range(10):
            session.add(Deployment(name=f"quota-vm-{index}", owner_id=user.id))
            session.add(Job(title=f"quota-job-{index}", created_by=user.id, status="queued"))
        user_id, admin_id = user.id, admin.id

    with session_scope() as session:
        user = session.get(User, user_id)
        try:
            api._enforce_quota(session, user, "vm")
        except HTTPException as exc:
            assert exc.status_code == 429
        else:
            raise AssertionError("the default VM quota must reject an eleventh VM")

        enforce_jobs = getattr(api, "_enforce_active_job_quota", lambda *_args: None)
        try:
            enforce_jobs(session, user)
        except HTTPException as exc:
            assert exc.status_code == 429
        else:
            raise AssertionError("the default active-job quota must reject an eleventh job")

        api._enforce_quota(session, session.get(User, admin_id), "vm")
        enforce_jobs(session, session.get(User, admin_id))


def test_job_retention_defaults_to_ninety_days():
    assert appsettings.get_job_retention_days() == 90


def test_login_password_and_ip_hash_work_are_bounded():
    try:
        api.LoginBody(email="oversize@example.com", password="x" * 1025)
    except Exception as exc:
        assert "password" in str(exc)
    else:
        raise AssertionError("a 1,025-character login password must be rejected")

    ip = "192.0.2.161"
    ip_key = f"ip:{ip}"
    with api._login_attempts_lock:
        api._login_attempts.clear()
    for _index in range(40):
        api._record_attempt(ip_key)
    request = SimpleNamespace(session={}, client=SimpleNamespace(host=ip), headers={})
    with session_scope() as session:
        try:
            api.login(api.LoginBody(
                email="different-address@example.com", password="WrongPass12!",
            ), request, session)
        except HTTPException as exc:
            assert exc.status_code == 429
        else:
            raise AssertionError("IP-wide failed-login pressure must throttle before hashing")


def test_successful_login_does_not_clear_ip_failure_pressure():
    ip = "192.0.2.162"
    ip_key = f"ip:{ip}"
    with api._login_attempts_lock:
        api._login_attempts.clear()
    with session_scope() as session:
        user = User(
            email="valid-login@example.com", name="Valid",
            password_hash=hash_password("StrongPass12!"),
        )
        session.add(user)
    for _index in range(39):
        api._record_attempt(ip_key)
    request = SimpleNamespace(session={}, client=SimpleNamespace(host=ip), headers={})
    with session_scope() as session:
        assert api.login(api.LoginBody(
            email="valid-login@example.com", password="StrongPass12!",
        ), request, session)["ok"] is True
    api._record_attempt(ip_key)
    with session_scope() as session:
        try:
            api.login(api.LoginBody(
                email="another-address@example.com", password="WrongPass12!",
            ), SimpleNamespace(session={}, client=SimpleNamespace(host=ip), headers={}), session)
        except HTTPException as exc:
            assert exc.status_code == 429
        else:
            raise AssertionError("a valid login must not reset IP-wide failure pressure")


def test_request_body_limit_rejects_content_length_and_chunked_overflow():
    middleware_class = getattr(app_main, "RequestBodyLimitMiddleware", None)
    assert middleware_class is not None, "request body limiting middleware is missing"

    async def exercise(scope_headers, chunks):
        downstream_called = False
        sent = []
        pending = list(chunks)

        async def downstream(_scope, receive, send):
            nonlocal downstream_called
            downstream_called = True
            while True:
                message = await receive()
                if not message.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def receive():
            return pending.pop(0)

        async def send(message):
            sent.append(message)

        middleware = middleware_class(downstream, max_bytes=4)
        await middleware({"type": "http", "method": "POST", "path": "/api/auth/login",
                          "headers": scope_headers}, receive, send)
        return downstream_called, sent

    declared_called, declared_sent = asyncio.run(exercise(
        [(b"content-length", b"5")],
        [{"type": "http.request", "body": b"abcde", "more_body": False}],
    ))
    assert declared_called is False
    assert declared_sent[0]["status"] == 413

    chunked_called, chunked_sent = asyncio.run(exercise(
        [],
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        ],
    ))
    assert chunked_called is False
    assert chunked_sent[0]["status"] == 413


def test_concurrent_admin_demotions_cannot_remove_every_active_admin():
    """Both guard reads must not succeed against the same two-admin snapshot."""
    with session_scope() as session:
        for existing in session.exec(select(User)).all():
            existing.role = "user"
            session.add(existing)
        first = User(email="race-admin-a@example.com", name="A", password_hash="x", role="admin")
        second = User(email="race-admin-b@example.com", name="B", password_hash="x", role="admin")
        session.add(first)
        session.add(second)
        session.flush()
        user_ids = (first.id, second.id)

    before_guard = threading.Barrier(2)
    after_guard = threading.Barrier(2)
    original_guard = api._last_admin_guard

    def synchronized_guard(session, target):
        try:
            before_guard.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        result = original_guard(session, target)
        try:
            after_guard.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return result

    failures = []

    def demote(user_id):
        try:
            with session_scope() as session:
                actor = session.get(User, user_id)
                api.edit_user(
                    user_id, api.UserEditBody(role="user"),
                    user=actor, session=session,
                )
        except Exception as exc:  # noqa: BLE001 -- asserted below
            failures.append(exc)

    api._last_admin_guard = synchronized_guard
    try:
        threads = [threading.Thread(target=demote, args=(user_id,)) for user_id in user_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert all(not thread.is_alive() for thread in threads)
    finally:
        api._last_admin_guard = original_guard

    with session_scope() as session:
        active_admins = session.exec(select(User).where(
            User.role == "admin", User.disabled == False, User.deleted_at.is_(None),  # noqa: E712
        )).all()
    assert len(active_admins) == 1, failures
    assert any(isinstance(exc, HTTPException) and exc.status_code == 409 for exc in failures)


def test_live_console_grant_tracks_connection_disable_and_reassignment():
    with session_scope() as session:
        user = User(email="console-grant@example.com", name="Console", password_hash="x")
        first = Connection(name="console-first", host="pve-1", token_id="u@pve!t")
        second = Connection(name="console-second", host="pve-2", token_id="u@pve!t")
        session.add(user)
        session.add(first)
        session.add(second)
        session.flush()
        dep = Deployment(
            name="console-vm", owner_id=user.id, connection_id=first.id, vmid=6101,
        )
        session.add(dep)
        session.flush()
        user_id, session_epoch = user.id, user.session_epoch
        first_id, second_id, dep_id = first.id, second.id, dep.id
        grant = api._ConsoleGrant(
            conn=Connection(**first.model_dump()),
            deployment=Deployment(**dep.model_dump()),
            user_id=user.id,
            session_epoch=user.session_epoch,
        )

    assert api._console_grant_still_valid(grant) is True
    with session_scope() as session:
        conn = session.get(Connection, first_id)
        conn.host = "replacement-pve"
        session.add(conn)
    assert api._console_grant_still_valid(grant) is False

    with session_scope() as session:
        conn = session.get(Connection, first_id)
        conn.host = "pve-1"
        conn.disabled = True
        session.add(conn)
    assert api._console_grant_still_valid(grant) is False

    with session_scope() as session:
        conn = session.get(Connection, first_id)
        conn.disabled = False
        dep = session.get(Deployment, dep_id)
        dep.connection_id = second_id
        session.add(conn)
        session.add(dep)
    assert api._console_grant_still_valid(grant) is False

    with session_scope() as session:
        dep = session.get(Deployment, dep_id)
        dep.connection_id = first_id
        session.add(dep)
    assert api._console_grant_still_valid(grant) is True
    with session_scope() as session:
        session.delete(session.get(Connection, first_id))
    assert api._console_grant_still_valid(grant) is False

    with session_scope() as session:
        dep = session.get(Deployment, dep_id)
        dep.connection_id = second_id
        second = session.get(Connection, second_id)
        second.disabled = True
        session.add(dep)
        session.add(second)

    class HandshakeSocket:
        def __init__(self):
            self.session = {"uid": user_id, "sv": session_epoch}
            self.headers = {}
            self.scope = {}
            self.close_codes = []

        async def close(self, code=1000):
            self.close_codes.append(code)

    socket = HandshakeSocket()
    assert asyncio.run(api._ws_authorized_dep(socket, dep_id)) is None
    assert socket.close_codes == [4403]


def test_proxmox_failures_expose_only_a_log_reference():
    helper = getattr(api, "_proxmox_http_failure", None)
    assert helper is not None, "centralized Proxmox error redaction is missing"

    rendered = []

    class Capture(logging.Handler):
        def emit(self, record):
            rendered.append(self.format(record))

    handler = Capture()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    previous_level = api.log.level
    previous_propagate = api.log.propagate
    api.log.addHandler(handler)
    api.log.setLevel(logging.ERROR)
    api.log.propagate = False
    sensitive = "https://root:super-secret@pve.internal:8006/api2/json"
    try:
        response_error = helper("power action", RuntimeError(sensitive))
    finally:
        api.log.removeHandler(handler)
        api.log.setLevel(previous_level)
        api.log.propagate = previous_propagate

    assert isinstance(response_error, HTTPException)
    assert response_error.status_code == 502
    assert "super-secret" not in response_error.detail
    assert "pve.internal" not in response_error.detail
    reference = re.search(r"reference ([0-9a-f]{12})", response_error.detail)
    assert reference, response_error.detail
    assert rendered and sensitive in rendered[0]
    assert reference.group(1) in rendered[0]


if __name__ == "__main__":
    test_run_script_is_literal_unsafe_ansible_data()
    test_runtime_playbook_keeps_seeded_script_and_yaml_shapes()
    test_default_vm_and_active_job_quotas_bound_non_admins()
    test_job_retention_defaults_to_ninety_days()
    test_login_password_and_ip_hash_work_are_bounded()
    test_successful_login_does_not_clear_ip_failure_pressure()
    test_request_body_limit_rejects_content_length_and_chunked_overflow()
    test_concurrent_admin_demotions_cannot_remove_every_active_admin()
    test_live_console_grant_tracks_connection_disable_and_reassignment()
    test_proxmox_failures_expose_only_a_log_reference()
    print("\nALL WAVE 61 UNIT TESTS PASSED")
