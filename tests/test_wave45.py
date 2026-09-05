"""Wave 45 — image-cache, SSH TOFU, cloud-init, and lifecycle regressions."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

import paramiko
import yaml
from sqlmodel import Session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave45-test.sqlite3")
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault(
    "GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-wave45-data")
)

from app import api, worker  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import engine, init_db, session_scope  # noqa: E402
from app.models import Connection, Deployment, Image, Job, User  # noqa: E402
from app.proxmox import (  # noqa: E402
    JobCancelled,
    Proxmox,
    ProxmoxError,
    _ssh_client,
    base_disk_filename,
    write_snippet_over_ssh,
)

init_db()


class _Ctx:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, line: str, _cls: str = "") -> None:
        self.lines.append(line)

    def phase_note(self, _note: str) -> None:
        pass

    def cancelled(self) -> bool:
        return False


def test_legacy_invalid_proxmox_ports_fall_back_for_api_and_console_urls():
    """Corrupt legacy port rows must not drive an invalid API or websocket endpoint."""
    import app.proxmox as proxmox_module

    captured: list[int] = []

    class _Api:
        def __init__(self, _host, **kwargs):
            captured.append(kwargs["port"])

    old_api = proxmox_module.ProxmoxAPI
    old_decrypt = proxmox_module.decrypt
    proxmox_module.ProxmoxAPI = _Api
    proxmox_module.decrypt = lambda _value: "token"
    try:
        for stored_port, expected in ((0, 8006), (-1, 8006), (65536, 8006), (9443, 9443)):
            conn = Connection(
                name=f"wave45-port-{stored_port}", host="pve.example.test",
                port=stored_port, token_id="u@p!t",
            )
            px = Proxmox(conn)
            px.list_cluster_guests = lambda: [{"vmid":8045,"node":"pve","type":"qemu"}]
            assert captured[-1] == expected, (stored_port, captured[-1])
            assert px.console_ws_url(8045, "pve", 5900, "ticket").startswith(
                f"wss://pve.example.test:{expected}/"
            )
    finally:
        proxmox_module.ProxmoxAPI = old_api
        proxmox_module.decrypt = old_decrypt


def test_checksum_aware_cache_names_keep_legacy_identity_and_hide_url_credentials():
    """Changing a declared digest must not reuse bytes validated for an older digest."""
    plain = "https://images.example.test/releases/noble-server-cloudimg-amd64.img"
    plain_tag = __import__("hashlib").sha256(plain.encode()).hexdigest()[:8]
    assert base_disk_filename(plain) == f"noble-server-cloudimg-amd64-{plain_tag}.qcow2"

    secret_url = (
        "https://download-user:download-password@images.example.test/releases/"
        "noble-server-cloudimg-amd64.img?token=super-secret-query"
    )
    digest_a = "a" * 64
    digest_b = "b" * 64
    name_a = base_disk_filename(secret_url, digest_a, "sha256")
    assert name_a == base_disk_filename(secret_url, digest_a, "SHA256")
    assert name_a != base_disk_filename(secret_url, digest_b, "sha256")
    assert name_a != base_disk_filename(secret_url), "adding a checksum must force validation"
    assert name_a.startswith("noble-server-cloudimg-amd64-")
    for secret in ("download-user", "download-password", "token", "super-secret-query"):
        assert secret not in name_a


def test_matching_checksum_cache_is_reused_but_checksum_change_redownloads():
    """A cache entry is reusable only for the exact URL/checksum identity it represents."""
    source = "https://images.example.test/cloud/base.img"
    old_checksum = "1" * 64
    new_checksum = "2" * 64
    old_name = base_disk_filename(source, old_checksum, "sha256")
    new_name = base_disk_filename(source, new_checksum, "sha256")

    class _Px:
        def __init__(self, present: set[str]) -> None:
            self.conn = Connection(id=452, name="wave45-matching", host="pve", token_id="u@p!t")
            self.iso_storage = "local"
            self.present = present
            self.downloads: list[tuple] = []

        def storage_has_volume(self, filename, node=None):
            return filename in self.present

        def download_url(self, filename, url, node=None, checksum="", checksum_algorithm=""):
            self.downloads.append((filename, url, node, checksum, checksum_algorithm))
            return "UPID:download"

        def wait_task(self, *_args, **_kwargs):
            if self.downloads:
                self.present.add(self.downloads[-1][0])
            return None

        def delete_storage_volume(self, filename, node=None):
            self.present.discard(filename)
            return None

    cached = _Px(set())
    assert worker._ensure_base_disk(
        _Ctx(), cached, "pve", {
            "src_url": source, "checksum": new_checksum, "checksum_algorithm": "sha256",
        },
    ) == new_name
    assert len(cached.downloads) == 1
    cached.downloads.clear()
    assert worker._ensure_base_disk(
        _Ctx(), cached, "pve", {
            "src_url": source, "checksum": new_checksum, "checksum_algorithm": "sha256",
        },
    ) == new_name
    assert cached.downloads == []

    changed = _Px({old_name})
    assert worker._ensure_base_disk(
        _Ctx(), changed, "pve", {
            "src_url": source, "checksum": new_checksum, "checksum_algorithm": "sha256",
        },
    ) == new_name
    assert changed.downloads == [
        (new_name, source, "pve", new_checksum, "sha256")
    ]


def test_download_failure_fails_closed_even_when_target_file_appears():
    """A partial or checksum-failed file appearing on storage is not proof of success."""
    source = "https://images.example.test/cloud/base.img"
    checksum = "3" * 64

    class _Px:
        def __init__(self) -> None:
            self.lookups = 0

        def storage_has_volume(self, _filename, node=None):
            self.lookups += 1
            return self.lookups > 1

        def download_url(self, *_args, **_kwargs):
            return "UPID:download"

        def wait_task(self, *_args, **_kwargs):
            raise ProxmoxError("checksum mismatch")

    try:
        worker._ensure_base_disk(
            _Ctx(), _Px(), "pve", {
                "src_url": source, "checksum": checksum, "checksum_algorithm": "sha256",
            },
        )
    except RuntimeError as exc:
        assert "checksum mismatch" in str(exc)
    else:
        raise AssertionError("a failed checksum was accepted because a file appeared")


def test_download_cancellation_remains_a_cancellation_when_file_appears():
    """Fail-closed wrapping must not turn a user cancellation into a generic failure."""
    class _Px:
        def __init__(self) -> None:
            self.lookups = 0

        def storage_has_volume(self, _filename, node=None):
            self.lookups += 1
            return self.lookups > 1

        def download_url(self, *_args, **_kwargs):
            return "UPID:download"

        def wait_task(self, *_args, **_kwargs):
            raise JobCancelled()

    try:
        worker._ensure_base_disk(
            _Ctx(), _Px(), "pve", {"src_url": "https://images.example.test/base.img"},
        )
    except JobCancelled:
        pass
    else:
        raise AssertionError("download cancellation was swallowed")


def test_retry_never_reuses_leftover_from_failed_checksum_download():
    """A failed target must stay distrusted across retries until validation succeeds."""
    source = "https://images.example.test/cloud/retry-base.img"
    checksum = "5" * 64

    class _Px:
        def __init__(self) -> None:
            self.conn = Connection(id=45, name="wave45-retry", host="pve", token_id="u@p!t")
            self.iso_storage = "local"
            self.present: set[str] = set()
            self.downloads: list[str] = []
            self.waits = 0
            self.deletes = 0

        def storage_has_volume(self, filename, node=None):
            return filename in self.present

        def download_url(self, filename, *_args, **_kwargs):
            self.downloads.append(filename)
            return "UPID:download"

        def wait_task(self, *_args, **_kwargs):
            self.waits += 1
            self.present.add(self.downloads[-1])
            if self.waits == 1:
                raise ProxmoxError("checksum mismatch")

        def delete_storage_volume(self, filename, node=None):
            self.deletes += 1
            if self.deletes == 1:
                raise ProxmoxError("temporary cleanup failure")
            self.present.discard(filename)
            return None

    px = _Px()
    cfg = {"src_url": source, "checksum": checksum, "checksum_algorithm": "sha256"}
    try:
        worker._ensure_base_disk(_Ctx(), px, "pve", cfg)
    except RuntimeError as exc:
        assert "checksum mismatch" in str(exc)
    else:
        raise AssertionError("failed checksum download was accepted")
    assert px.present, "fixture must retain the failed target after cleanup fails"
    failed_filename = px.downloads[0]

    filename = worker._ensure_base_disk(_Ctx(), px, "pve", cfg)
    assert px.downloads == [failed_filename, filename] and failed_filename != filename, "retry reused the failed leftover"
    assert failed_filename in px.present, "replacement must not move a pre-existing volume"

    # A subsequent call may reuse only the target whose successful validation was
    # durably recorded by the second attempt.
    assert worker._ensure_base_disk(_Ctx(), px, "pve", cfg) == filename
    assert px.downloads == [failed_filename, filename]


def test_cached_images_uses_the_checksum_specific_volume_identity():
    """The cache-status endpoint must agree with the worker about the selected bytes."""
    suffix = os.urandom(3).hex()
    source = "https://images.example.test/cache/base.img"
    checksum = "4" * 64
    with session_scope() as session:
        user = User(email=f"wave45-cache-{suffix}@example.com", name="cache", password_hash="x")
        conn = Connection(name=f"wave45-cache-{suffix}", host="pve", token_id="u@p!t")
        image = Image(kind="base", name=f"wave45-cache-{suffix}", source_url=source,
                      checksum=checksum, build_status="ready")
        session.add(user); session.add(conn); session.add(image); session.flush()
        user_id, conn_id, image_id = user.id, conn.id, image.id
    expected = base_disk_filename(source, checksum, "sha256")

    from app import inventory
    saved = inventory.get_snapshot
    inventory.get_snapshot = lambda cid: {"status":"online", "volumes":{f"local:import/{expected}"}, "stale":False}
    try:
        with session_scope() as session:
            result = api.cached_images(
                conn_id, user=session.get(User, user_id), session=session,
            )
    finally:
        inventory.get_snapshot = saved
    assert result["cached"][str(image_id)] is True


class _HandshakeSSHClient:
    """Small transport boundary fake: Paramiko still owns key parsing/comparison."""

    presented_key = None
    missing_key_calls = 0

    def __init__(self) -> None:
        self._host_keys = paramiko.HostKeys()
        self._system_host_keys = paramiko.HostKeys()
        self._host_keys_filename = None
        self._policy = None

    def load_system_host_keys(self, _filename=None) -> None:
        pass

    def load_host_keys(self, filename: str) -> None:
        self._host_keys.load(filename)
        self._host_keys_filename = filename

    def set_missing_host_key_policy(self, policy) -> None:
        self._policy = policy

    def save_host_keys(self, filename: str) -> None:
        self._host_keys.save(filename)

    def _log(self, *_args, **_kwargs) -> None:
        pass

    def connect(self, hostname, **_kwargs) -> None:
        key = type(self).presented_key
        known = self._host_keys.lookup(hostname) or {}
        expected = known.get(key.get_name())
        if expected is None:
            type(self).missing_key_calls += 1
            self._policy.missing_host_key(self, hostname, key)
        elif expected != key:
            raise paramiko.BadHostKeyException(hostname, key, expected)

    def close(self) -> None:
        pass


def test_non_strict_ssh_persists_first_key_accepts_it_again_and_rejects_change():
    """TOFU means trust once and pin, not trust every connection forever."""
    old_client = paramiko.SSHClient
    old_dir = settings.data_dir
    old_explicit = settings.ssh_known_hosts
    old_strict = settings.ssh_strict
    with tempfile.TemporaryDirectory(prefix="gd-wave45-tofu-") as tmp:
        known_hosts = Path(tmp) / "ssh_known_hosts"
        first_key = paramiko.RSAKey.generate(1024)
        changed_key = paramiko.RSAKey.generate(1024)
        changed_algorithm_key = paramiko.ECDSAKey.generate()
        conn = Connection(name="wave45-tofu", host="pve.example.test", token_id="u@p!t")
        try:
            paramiko.SSHClient = _HandshakeSSHClient
            settings.data_dir = Path(tmp)
            settings.ssh_known_hosts = ""
            settings.ssh_strict = False
            _HandshakeSSHClient.missing_key_calls = 0
            _HandshakeSSHClient.presented_key = first_key

            _ssh_client(conn, key=None, timeout=1).close()
            assert known_hosts.exists(), "first-seen key was not persisted"
            if os.name != "nt":
                assert stat.S_IMODE(known_hosts.stat().st_mode) == 0o600
            assert _HandshakeSSHClient.missing_key_calls == 1

            _ssh_client(conn, key=None, timeout=1).close()
            assert _HandshakeSSHClient.missing_key_calls == 1, "same key was treated as unknown"

            _HandshakeSSHClient.presented_key = changed_key
            try:
                _ssh_client(conn, key=None, timeout=1)
            except paramiko.BadHostKeyException:
                pass
            else:
                raise AssertionError("changed SSH host key was trusted")

            _HandshakeSSHClient.presented_key = changed_algorithm_key
            try:
                _ssh_client(conn, key=None, timeout=1)
            except paramiko.BadHostKeyException:
                pass
            else:
                raise AssertionError("changed SSH host-key algorithm was trusted")
        finally:
            paramiko.SSHClient = old_client
            settings.data_dir = old_dir
            settings.ssh_known_hosts = old_explicit
            settings.ssh_strict = old_strict


def test_snippet_upload_marks_resolved_cloud_config_owner_only():
    """The node-side copy can contain resolved secrets and must never be world-readable."""
    calls: list[tuple] = []

    class _Sftp:
        def stat(self, _path): return object()
        def putfo(self, stream, remote): calls.append(("put", remote, stream.read()))
        def chmod(self, remote, mode): calls.append(("chmod", remote, mode))
        def close(self): calls.append(("close",))

    class _Client:
        def open_sftp(self): return _Sftp()
        def close(self): calls.append(("client-close",))

    conn = Connection(
        name="wave45-snippet", host="pve", token_id="u@p!t",
        ssh_key_path="/keys/managed", snippet_storage="local",
    )
    import app.proxmox as proxmox_module
    old_load = proxmox_module._load_ssh_key
    old_client = proxmox_module._ssh_client
    try:
        proxmox_module._load_ssh_key = lambda _path: object()
        proxmox_module._ssh_client = lambda *_args, **_kwargs: _Client()
        result = write_snippet_over_ssh(conn, "gd-deploy-8045.yml", "password: resolved-secret")
    finally:
        proxmox_module._load_ssh_key = old_load
        proxmox_module._ssh_client = old_client
    assert result == "local:snippets/gd-deploy-8045.yml"
    assert ("chmod", "/var/lib/vz/snippets/gd-deploy-8045.yml", 0o600) in calls


def test_resolved_first_boot_recipe_is_root_only_and_self_removing_on_failure():
    """A failed first-boot command must not leave its resolved secret script behind."""
    config = worker._deploy_cloud_config(
        "wave45-vm", [], ["set -e", "echo resolved-password", "exit 45"],
    )
    document = yaml.safe_load(config)
    recipe_file = next(
        item for item in document["write_files"]
        if item["path"] == "/opt/goblindock-recipe.sh"
    )
    assert recipe_file["permissions"] == "0700"
    lines = recipe_file["content"].splitlines()
    cleanup_index = next(i for i, line in enumerate(lines) if "trap " in line and " EXIT" in line)
    secret_index = next(i for i, line in enumerate(lines) if "resolved-password" in line)
    assert cleanup_index < secret_index, "cleanup was not armed before secret-bearing commands"
    assert any("$0" in line and "rm -f" in line for line in lines[:secret_index])
    launcher = next(
        command for command in document["runcmd"]
        if isinstance(command, list) and "/opt/goblindock-recipe.sh" in " ".join(command)
    )
    assert launcher[:2] == ["/bin/bash", "-c"]
    assert "trap " in launcher[2] and "rm -f -- /opt/goblindock-recipe.sh" in launcher[2]


def _lifecycle_fixture(action: str) -> tuple[int, int]:
    suffix = os.urandom(3).hex()
    with session_scope() as session:
        user = User(
            email=f"wave45-{action}-{suffix}@example.com", name="lifecycle", password_hash="x",
        )
        conn = Connection(name=f"wave45-{action}-{suffix}", host="pve", token_id="u@p!t")
        session.add(user); session.add(conn); session.flush()
        dep = Deployment(
            name=f"wave45-{action}-{suffix}", owner_id=user.id, connection_id=conn.id,
            node="pve", vmid=8045, status="stopped" if action == "start" else "running",
            error="old error",
        )
        session.add(dep); session.flush()
        return user.id, dep.id


def test_successful_power_actions_persist_the_observed_target_state():
    """The stored status must agree with a power task that Proxmox completed successfully."""
    class _Px:
        def __init__(self, _conn): pass
        def start(self, *_args, **_kwargs): return "UPID:start"
        def stop(self, *_args, **_kwargs): return "UPID:stop"
        def reboot(self, *_args, **_kwargs): return "UPID:restart"
        def wait_task(self, *_args, **_kwargs): return None

    saved = api.Proxmox
    api.Proxmox = _Px
    try:
        for action, expected in (("start", "running"), ("restart", "running"), ("stop", "stopped")):
            user_id, dep_id = _lifecycle_fixture(action)
            with Session(engine) as session:
                assert api.vm_action(
                    dep_id, api.ActionBody(action=action),
                    user=session.get(User, user_id), session=session,
                ) == {"ok": True}
            with session_scope() as session:
                dep = session.get(Deployment, dep_id)
                assert dep.status == expected, (action, dep.status)
                assert dep.error == ""
    finally:
        api.Proxmox = saved


def _queued_cancel_fixture(job_type: str) -> tuple[int, int]:
    suffix = os.urandom(3).hex()
    with session_scope() as session:
        conn = Connection(name=f"wave45-cancel-{job_type}-{suffix}", host="pve", token_id="u@p!t")
        session.add(conn); session.flush()
        dep = Deployment(
            name=f"wave45-cancel-{job_type}-{suffix}", connection_id=conn.id,
            node="pve", vmid=8046, status="working",
        )
        session.add(dep); session.flush()
        job = Job(
            type=job_type, status="queued", cancel_requested=True,
            deployment_id=dep.id, connection_id=conn.id,
        )
        session.add(job); session.flush()
        return dep.id, job.id


def test_queued_rebuild_and_destroy_cancellation_restore_live_runtime_state():
    """A spared running VM must be recorded running, never blindly stamped stopped."""
    class _Px:
        def __init__(self, _conn): pass
        def list_cluster_guests(self): return [{"vmid":8046,"node":"pve","type":"qemu"}]
        def vm_current(self, vmid, node=None): return {"status": "running"}

    saved = worker.Proxmox
    worker.Proxmox = _Px
    try:
        for job_type in ("rebuild", "destroy"):
            dep_id, job_id = _queued_cancel_fixture(job_type)
            assert worker._claim_next_job() is None
            with session_scope() as session:
                assert session.get(Job, job_id).status == "canceled"
                dep = session.get(Deployment, dep_id)
                assert dep.status == "running", (job_type, dep.status)
                assert dep.error == ""
    finally:
        worker.Proxmox = saved


def test_canceled_lifecycle_with_unknown_runtime_state_fails_safe():
    """Presence without a readable power state must be visible as uncertain, not stopped."""
    class _Px:
        def __init__(self, _conn): pass
        def list_cluster_guests(self): return [{"vmid":8046,"node":"pve","type":"qemu"}]
        def vm_current(self, vmid, node=None): raise RuntimeError("status endpoint unavailable")

    dep_id, job_id = _queued_cancel_fixture("destroy")
    saved = worker.Proxmox
    worker.Proxmox = _Px
    try:
        assert worker._claim_next_job() is None
    finally:
        worker.Proxmox = saved
    with session_scope() as session:
        assert session.get(Job, job_id).status == "canceled"
        dep = session.get(Deployment, dep_id)
        assert dep.status == "error"
        assert "status" in dep.error.lower() and "unavailable" in dep.error.lower()


_TESTS = [
    test_legacy_invalid_proxmox_ports_fall_back_for_api_and_console_urls,
    test_checksum_aware_cache_names_keep_legacy_identity_and_hide_url_credentials,
    test_matching_checksum_cache_is_reused_but_checksum_change_redownloads,
    test_download_failure_fails_closed_even_when_target_file_appears,
    test_download_cancellation_remains_a_cancellation_when_file_appears,
    test_retry_never_reuses_leftover_from_failed_checksum_download,
    test_cached_images_uses_the_checksum_specific_volume_identity,
    test_non_strict_ssh_persists_first_key_accepts_it_again_and_rejects_change,
    test_snippet_upload_marks_resolved_cloud_config_owner_only,
    test_resolved_first_boot_recipe_is_root_only_and_self_removing_on_failure,
    test_successful_power_actions_persist_the_observed_target_state,
    test_queued_rebuild_and_destroy_cancellation_restore_live_runtime_state,
    test_canceled_lifecycle_with_unknown_runtime_state_fails_safe,
]


if __name__ == "__main__":
    selected = set(sys.argv[1:])
    for _test in _TESTS:
        if selected and _test.__name__ not in selected:
            continue
        _test()
        print(f"{_test.__name__} OK")
    print("Wave 45 tests passed.")
