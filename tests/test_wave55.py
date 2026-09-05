"""Remote contracts: real wrappers over a deterministic Proxmox transport, no VMs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GOBLINDOCK_DEV", "1")
os.environ.setdefault("GOBLINDOCK_DATA_DIR", tempfile.mkdtemp(prefix="gd-wave55-"))

import yaml
from app.proxmox import Proxmox, ProxmoxError, JobCancelled, probe_vm_presence
from app.models import Block
from app.recipes import compile_playbook
from app.seed import BUILTIN_BLOCKS


class Endpoint:
    def __init__(self, handler, path=()):
        self.handler, self.path = handler, path

    def __getattr__(self, name):
        if name in {"get", "post", "put", "delete"}:
            return lambda **params: self.handler(name, self.path, params)
        return Endpoint(self.handler, self.path + (name,))

    def __call__(self, name):
        return Endpoint(self.handler, self.path + (str(name),))


def client(handler, inventory=True, guest_node="pve1"):
    px = object.__new__(Proxmox)
    px.node = "pve1"
    def transport(method, path, params):
        if inventory and path == ("access", "permissions"):
            return {"/vms": {"VM.Audit": 1}}
        if inventory and path == ("cluster", "resources"):
            return [{"vmid": 8001, "type": "qemu", "node": guest_node}]
        return handler(method, path, params)
    px.api = Endpoint(transport)
    return px


class RemoteContracts(unittest.TestCase):
    def test_config_and_resize_wait_for_the_returned_task_before_success(self):
        for operation in ("config", "resize"):
            for terminal in ("OK", "disk locked"):
                with self.subTest(operation=operation, terminal=terminal):
                    events = []
                    def transport(method, path, params):
                        events.append((method, path, params))
                        if path[-1] == operation:
                            return "UPID:pve1:000001:000001:000001:qmconfig:8001:root@pam:"
                        if path[-1] == "status":
                            return {"status": "stopped", "exitstatus": terminal}
                        self.fail(f"unexpected request {method} {path}")
                    px = client(transport)
                    call = (lambda: px.set_config(8001, memory=2048)) if operation == "config" else (
                        lambda: px.resize_disk(8001, "scsi0", "20G"))
                    if terminal == "OK":
                        self.assertIsNone(call())
                    else:
                        with self.assertRaisesRegex(ProxmoxError, "disk locked"):
                            call()
                    self.assertEqual(events[-1][1][-3:], (
                        "tasks", "UPID:pve1:000001:000001:000001:qmconfig:8001:root@pam:", "status"))

    def test_task_identity_callback_runs_before_polling(self):
        events = []
        def transport(method, path, params):
            if method == "post":
                self.assertNotIn("on_task", params)
                return "UPID:pve1:config"
            events.append("poll")
            return {"status": "stopped", "exitstatus": "OK"}
        client(transport).set_config(8001, on_task=lambda upid: events.append(upid), memory=2048)
        self.assertEqual(events, ["UPID:pve1:config", "poll"])

    def test_cancellation_stop_transport_failure_does_not_mask_jobcancelled(self):
        def transport(method, path, params):
            self.assertEqual(method, "delete")
            self.assertEqual(path[:2], ("nodes", "pve2"))
            raise OSError("connection reset while stopping task")
        with self.assertRaises(JobCancelled):
            client(transport).wait_task("UPID:pve2:000001:000001:000001:qmconfig:8001:root@pam:",
                                        node="pve1", cancelled=lambda: True)

    def test_already_cancelled_config_and_resize_do_not_submit_new_tasks(self):
        for operation in ("config", "resize"):
            events = []
            def transport(method, path, params):
                events.append(method)
                return "UPID:pve1:000001:000001:000001:qmconfig:8001:root@pam:"
            with self.subTest(operation=operation), self.assertRaises(JobCancelled):
                px = client(transport)
                if operation == "config":
                    px.set_config(8001, cancelled=lambda: True, memory=2048)
                else:
                    px.resize_disk(8001, "scsi0", "20G", cancelled=lambda: True)
            self.assertEqual(events, [])

    def inventory_client(self, guests, audit=1):
        def transport(method, path, params):
            if path == ("access", "permissions"):
                return {"/vms": {"VM.Audit": audit}}
            if path == ("cluster", "resources"):
                self.assertEqual(params, {"type": "vm"})
                return guests
            if path == ("cluster", "nextid"):
                return params["vmid"]
            if path[-1] == "qemu":
                return []  # old node genuinely empty after migration
            self.fail(f"unexpected inventory request {path}")
        return client(transport, inventory=False)

    def test_cluster_allocation_reserves_lxc_and_remote_node_vmid(self):
        px = self.inventory_client([
            {"vmid": 8000, "node": "pve2", "type": "qemu"},
            {"vmid": 8001, "node": "pve1", "type": "lxc"},
        ])
        self.assertEqual(px.next_free_vmid(8000, 8003), 8002)

    def test_migrated_vm_is_present_and_its_current_node_is_found(self):
        px = self.inventory_client([{"vmid": 8001, "node": "pve2", "type": "qemu"}])
        self.assertEqual(probe_vm_presence(px, 8001, "pve1")[0], "present")
        self.assertEqual(px.find_vm_node(8001, "pve1"), "pve2")

    def test_cluster_absence_and_allocation_fail_closed_on_partial_inventory(self):
        for guests, audit in [(None, 1), ([], 0), ([{"vmid": 8001}], 1),
                              ([{"vmid": "bad", "node": "pve1", "type": "qemu"}], 1),
                              ([{"vmid": 8001.5, "node": "pve1", "type": "qemu"}], 1)]:
            with self.subTest(guests=guests, audit=audit):
                px = self.inventory_client(guests, audit)
                self.assertEqual(probe_vm_presence(px, 8001, "pve1")[0], "unknown")
                with self.assertRaises(ProxmoxError):
                    px.next_free_vmid(8000, 8003)

    def test_complete_empty_inventory_proves_absence(self):
        px = self.inventory_client([])
        self.assertEqual(probe_vm_presence(px, 8001, "pve1")[0], "absent")

    def test_negative_inventory_is_corroborated_by_unfiltered_cluster_registry(self):
        def transport(method, path, params):
            if path == ("access", "permissions"):
                return {"/vms": {"VM.Audit": 1}}
            if path == ("cluster", "resources"):
                return []  # child NoAccess ACL can hide an existing VM
            if path == ("cluster", "nextid"):
                raise ProxmoxError("VM 8001 already exists")
            self.fail(f"unexpected request {path}")
        px = client(transport, inventory=False)
        self.assertEqual(probe_vm_presence(px, 8001, "pve1")[0], "unknown")
        with self.assertRaises(ProxmoxError):
            px.next_free_vmid(8001, 8002)

    def test_malformed_registry_confirmation_cannot_prove_absence(self):
        def transport(method, path, params):
            if path == ("access", "permissions"):
                return {"/vms": {"VM.Audit": 1}}
            if path == ("cluster", "resources"):
                return []
            return 8001.5
        self.assertEqual(probe_vm_presence(client(transport, inventory=False), 8001, "pve1")[0], "unknown")

    def test_existing_vm_operations_route_to_current_node_after_migration(self):
        actions = [lambda p: p.vm_current(8001, "pve1"), lambda p: p.vm_config(8001, "pve1"),
                   lambda p: p.start(8001, "pve1"), lambda p: p.stop(8001, "pve1"),
                   lambda p: p.reboot(8001, "pve1"), lambda p: p.destroy(8001, "pve1"),
                   lambda p: p.list_snapshots(8001, "pve1"),
                   lambda p: p.create_snapshot(8001, "safe", node="pve1"),
                   lambda p: p.delete_snapshot(8001, "safe", node="pve1"),
                   lambda p: p.rollback_snapshot(8001, "safe", node="pve1"),
                   lambda p: p.termproxy(8001, "pve1"), lambda p: p.vncproxy(8001, "pve1")]
        for action in actions:
            requests = []
            def transport(method, path, params):
                requests.append(path)
                return {}
            action(client(transport, guest_node="pve2"))
            self.assertEqual(requests[-1][:2], ("nodes", "pve2"))

    def test_wait_task_uses_owning_node_from_upid(self):
        def transport(method, path, params):
            self.assertEqual(path[:2], ("nodes", "pve2"))
            return {"status": "stopped", "exitstatus": "OK"}
        client(transport).wait_task("UPID:pve2:000001:000001:000001:qmstart:8001:root@pam:", node="pve1")

    def test_readonly_task_status_uses_the_upid_node_for_recovery(self):
        def transport(method, path, params):
            self.assertEqual(method, "get")
            self.assertEqual(path, ("nodes", "pve2", "tasks", "UPID:pve2:000001:000001:000001:qmstart:8001:root@pam:", "status"))
            return {"status": "stopped", "exitstatus": "OK"}
        result = client(transport).task_status(
            "UPID:pve2:000001:000001:000001:qmstart:8001:root@pam:", node="pve1")
        self.assertEqual(result, {"status": "stopped", "exitstatus": "OK"})

    def test_console_url_uses_the_current_node_after_migration(self):
        px = client(lambda *_: None, guest_node="pve2")
        px.conn = SimpleNamespace(host="pve.invalid", port=8006)
        self.assertIn("/nodes/pve2/qemu/8001/", px.console_ws_url(8001, "pve1", 5900, "ticket"))

    def test_readiness_polls_guest_exec_and_propagates_failure(self):
        for exitcode in (0, 21):
            states = iter([{"exited": 0}, {"exited": 1, "exitcode": exitcode, "out-data": "", "err-data": ""}])
            def transport(method, path, params):
                if path[-1] == "exec":
                    return {"pid": 71}
                self.assertEqual(path[-1], "exec-status")
                self.assertEqual(params, {"pid": 71})
                return next(states)
            with patch("app.proxmox.time.sleep"):
                if exitcode:
                    with self.assertRaises(ProxmoxError):
                        client(transport).wait_guest_ready(8001)
                else:
                    self.assertIsNone(client(transport).wait_guest_ready(8001))

    def test_readiness_cancellation_does_not_execute_guest_commands(self):
        def transport(*_):
            self.fail("cancelled readiness must not send a guest command")
        with self.assertRaises(JobCancelled):
            client(transport).wait_guest_ready(8001, cancelled=lambda: True)

    def test_readiness_command_requires_cloudinit_success_and_recipe_marker(self):
        # Execute the actual command produced by the wrapper under a fake guest PATH.
        for cloud_exit, marker, status in [(0, "0", "done"), (1, "0", "error"), (2, "0", "done"),
                                           (0, "1", "done"), (0, None, "done"), (0, "0", "disabled")]:
            with self.subTest(cloud_exit=cloud_exit, marker=marker, status=status), tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp, "cloud-init")
                script.write_text(f"#!/bin/sh\necho 'status: {status}'\nexit {cloud_exit}\n")
                script.chmod(0o755)
                marker_file = Path(tmp, "recipe-result")
                if marker is not None:
                    marker_file.write_text(marker + "\n")
                result = None
                def transport(method, path, params):
                    nonlocal result
                    if path[-1] == "exec":
                        command = params["command"]
                        self.assertIsInstance(command, str)
                        command = command.replace("/var/lib/goblindock-recipe-result", str(marker_file))
                        result = subprocess.run(command, shell=True, env={**os.environ, "PATH": tmp + ":/usr/bin:/bin"}, capture_output=True)
                        return {"pid": 72}
                    return {"exited": 1, "exitcode": result.returncode}
                if cloud_exit == 0 and marker == "0" and status == "done":
                    client(transport).wait_guest_ready(8001)
                else:
                    with self.assertRaises(ProxmoxError):
                        client(transport).wait_guest_ready(8001)

    def test_native_cloudinit_checks_completion_without_requiring_recipe_marker(self):
        def transport(method, path, params):
            if path[-1] == "exec":
                self.assertNotIn("goblindock-recipe-result", params["command"])
                return {"pid": 73}
            return {"exited": 1, "exitcode": 0}
        client(transport).wait_guest_ready(8001, require_marker=False)

    def test_readiness_running_agent_cannot_satisfy_initialization_deadline(self):
        def transport(method, path, params):
            return {"pid": 71} if path[-1] == "exec" else {"exited": 0}
        ticks = iter([0, 0, 1, 2])
        with patch("app.proxmox.time.monotonic", side_effect=lambda: next(ticks)), patch("app.proxmox.time.sleep"):
            with self.assertRaisesRegex(ProxmoxError, "timed out"):
                client(transport).wait_guest_ready(8001, timeout=1)

    def test_missing_async_task_identity_cannot_be_reported_as_success(self):
        for result in (None, {}, "", 0):
            with self.subTest(result=result), self.assertRaises(ProxmoxError):
                client(lambda *_: result).set_config(8001, memory=2048)

    def test_explicit_background_delay_accepts_api_confirmed_completed_task(self):
        def transport(method, path, params):
            self.assertEqual(method, "post")
            self.assertEqual(params, {"memory": 2048, "background_delay": 5})
            return None  # Proxmox returns null only when this opted-in wait succeeded.
        self.assertIsNone(client(transport).set_config(8001, memory=2048, background_delay=5))


def rendered_tasks(key, inputs=None):
    item = next(b for b in BUILTIN_BLOCKS if b["key"] == key)
    values = {f["name"]: f.get("default", "") for f in item["input_schema"]}
    values.update(inputs or {})
    block = Block(key=key, name=item["name"], phase="ansible",
                  input_schema_json=json.dumps(item["input_schema"]), ansible_template=item["ansible"])
    book = compile_playbook([{"blocks": [{"ref": key, "inputs": values}]}], {key: block})
    return yaml.safe_load(book)[0]["tasks"]


class BuiltinShellContracts(unittest.TestCase):
    def test_download_failure_is_not_hidden_by_a_successful_pipeline_consumer(self):
        for key in ("b-docker", "b-nodejs", "b-tailscale", "b-k3s", "b-claudecode"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "curl").write_text("#!/bin/sh\nexit 22\n")
                Path(tmp, "sudo").write_text("#!/bin/sh\nshift 3\nexec \"$@\"\n")
                Path(tmp, "apt-get").write_text("#!/bin/sh\nexit 0\n")
                for p in Path(tmp).iterdir():
                    p.chmod(0o755)
                task = next(t for t in rendered_tasks(key) if "ansible.builtin.shell" in t)
                result = subprocess.run([task.get("args", {}).get("executable", "/bin/sh"), "-c", task["ansible.builtin.shell"]],
                                        env={**os.environ, "PATH": tmp + ":/usr/bin:/bin", "HOME": tmp}, capture_output=True)
                self.assertNotEqual(result.returncode, 0, key)

    def test_failed_node_bootstrap_does_not_continue_to_install_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "curl").write_text("#!/bin/sh\nexit 22\n")
            # A successful consumer creates npm, modeling a bootstrap that consumes
            # a failed/empty download without producing an error itself.
            Path(tmp, "bash").write_text(
                '#!/bin/sh\nprintf "#!/bin/sh\\necho FALSE_SUCCESS\\n" > "$FAKE_BIN/npm"\n'
                '/bin/chmod +x "$FAKE_BIN/npm"\nexit 0\n')
            Path(tmp, "apt-get").write_text("#!/bin/sh\nexit 0\n")
            for p in Path(tmp).iterdir():
                p.chmod(0o755)
            task = rendered_tasks("b-codex")[0]
            result = subprocess.run([task.get("args", {}).get("executable", "/bin/sh"), "-c", task["ansible.builtin.shell"]],
                                    env={**os.environ, "PATH": tmp, "FAKE_BIN": tmp}, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b"FALSE_SUCCESS", result.stdout)

    def test_failed_package_metadata_refresh_stops_before_repository_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp, "unexpected-download")
            Path(tmp, "apt-get").write_text("#!/bin/sh\nexit 41\n")
            Path(tmp, "curl").write_text(f"#!/bin/sh\necho attempted > '{marker}'\nexit 22\n")
            Path(tmp, "gpg").write_text("#!/bin/sh\nexit 0\n")
            for p in Path(tmp).iterdir():
                p.chmod(0o755)
            task = rendered_tasks("b-caddy")[0]
            result = subprocess.run([task.get("args", {}).get("executable", "/bin/sh"), "-c", task["ansible.builtin.shell"]],
                                    env={**os.environ, "PATH": tmp}, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists(), "installer continued after apt-get update failed")

    def test_download_failure_stops_multi_command_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "curl").write_text("#!/bin/sh\nexit 22\n")
            Path(tmp, "curl").chmod(0o755)
            task = rendered_tasks("b-netdata")[0]
            command = task["ansible.builtin.shell"].replace("/tmp/netdata-kickstart.sh", str(Path(tmp, "install.sh")))
            result = subprocess.run([task.get("args", {}).get("executable", "/bin/sh"), "-c", command],
                                    env={**os.environ, "PATH": tmp + ":/usr/bin:/bin"}, capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_catalog_shells_stop_after_an_unhandled_failed_command(self):
        # Prepend a failing executable after the options line to exercise the actual
        # shell selected by each rendered builtin, without touching host services.
        for item in BUILTIN_BLOCKS:
            if item["key"] == "b-script":
                continue
            for task in rendered_tasks(item["key"]):
                command = task.get("ansible.builtin.shell")
                if not command:
                    continue
                with self.subTest(key=item["key"], task=task["name"]):
                    lines = command.splitlines()
                    options = lines[:1] if lines[0].startswith("set ") else []
                    command = "\n".join(options + ["false", "echo FALSE_SUCCESS", "exit 0"])
                    result = subprocess.run([task.get("args", {}).get("executable", "/bin/sh"), "-c", command], capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(b"FALSE_SUCCESS", result.stdout)

    def test_user_script_keeps_its_explicit_exit_semantics(self):
        task = rendered_tasks("b-script", {"script": "false; echo USER_SUCCESS"})[0]
        result = subprocess.run([task.get("args", {}).get("executable", "/bin/sh"), "-c", task["ansible.builtin.shell"]], capture_output=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"USER_SUCCESS", result.stdout)


if __name__ == "__main__":
    unittest.main()
