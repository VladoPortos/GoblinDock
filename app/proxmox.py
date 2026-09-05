"""Thin, purpose-built wrapper over the Proxmox VE REST API (via proxmoxer).

Only the operations GoblinDock actually needs: list/inspect/lifecycle of VMs,
clone a template, build a template from a cloud image (download-url + import-from),
cloud-init configuration, guest-agent IP discovery, and (optionally, over SSH) a
cloud-init snippet for baking a recipe.
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import logging
import os
import re
import shlex
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from proxmoxer import ProxmoxAPI
from proxmoxer.core import ResourceException

from .config import settings
from .models import Connection
from .security import decrypt

log = logging.getLogger("goblindock")

# Hosts we've already warned about running with TLS verification off — so the warning
# fires once per host per process instead of on every Proxmox client construction.
_warned_insecure_tls: set = set()

# Docker's default bridge subnet — a guest running containers can report docker0's
# 172.17.x gateway, which must never be mistaken for the VM's real management IP.
_DOCKER_BRIDGE_NET = ipaddress.ip_network("172.17.0.0/16")
_ssh_tofu_lock = threading.Lock()


def _proxmox_port(value) -> int:
    """Normalize legacy database values before constructing network endpoints."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return 8006
    return port if 1 <= port <= 65535 else 8006


class ProxmoxError(RuntimeError):
    pass


class JobCancelled(Exception):
    """Raised to signal that a job was cancelled by the user (cancel_requested).

    A distinct TYPE so cancellation is never inferred from an error message — a
    genuine failure whose text happens to contain the word "cancel" (e.g. a VM named
    'cancel-svc' failing its ansible phase) must NOT be treated as a user cancel.
    """
    pass


def base_disk_filename(
    src_url: str, checksum: str = "", checksum_algorithm: str = "",
) -> str:
    """Safe cache identity for one source and, when declared, one digest.

    Checksum-less images retain the historical URL-only identity.  A declared
    checksum becomes part of the identity so adding or changing verification can
    never reuse bytes cached under a different integrity contract.  Only the URL
    path contributes human-readable text; credentials and query tokens never do.
    """
    raw_name = ((urlsplit(src_url or "").path.rsplit("/", 1)[-1]) or "image")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name).lstrip(".-") or "image"
    stem = safe.rsplit(".", 1)[0] or "image"
    digest = (checksum or "").strip().lower()
    algorithm = (checksum_algorithm or "").strip().lower()
    identity = src_url or ""
    if digest:
        identity = f"{identity}\0{algorithm}\0{digest}"
    url_tag = hashlib.sha256(identity.encode()).hexdigest()[:8]
    filename = f"{stem}-{url_tag}.qcow2"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", filename):
        raise RuntimeError(f"unsafe image filename derived from URL: {raw_name!r}")
    return filename


def _split_token(token_id: str) -> tuple[str, str]:
    # "goblindock@pve!app" -> ("goblindock@pve", "app")
    if "!" in token_id:
        user, name = token_id.split("!", 1)
        return user, name
    return token_id, ""


# Proxmox snapshot names are config-ids (letter first, then letters/digits/-/_).
# They flow into API URL paths, so enforce the shape at the client like guard_vmid.
_SNAPNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")


def guard_snapname(name) -> str:
    if not isinstance(name, str) or not _SNAPNAME_RE.fullmatch(name or ""):
        raise ProxmoxError(f"invalid snapshot name {name!r}")
    return name


def guard_vmid(vmid) -> int:
    """HARD guard rail: GoblinDock must only ever touch VMIDs in its own window.
    Enforced at the client so no code path (even a corrupt DB row) can stop or
    destroy a protected VM (e.g. 102, or the 9000-9099 dev range)."""
    try:
        v = int(vmid)
    except (TypeError, ValueError):
        raise ProxmoxError(f"refusing to act on non-numeric VMID {vmid!r}")
    if not (settings.vmid_min <= v <= settings.vmid_max):
        raise ProxmoxError(
            f"SAFETY: refusing to act on VMID {v} — outside GoblinDock's window "
            f"{settings.vmid_min}-{settings.vmid_max}"
        )
    return v


class Proxmox:
    def __init__(self, conn: Connection):
        self.conn = conn
        user, token_name = _split_token(conn.token_id)
        self.node = conn.node
        self.storage = conn.storage
        self.iso_storage = conn.iso_storage or "local"
        self.snippet_storage = conn.snippet_storage or "local"
        self.bridge = conn.bridge or "vmbr0"
        if not conn.verify_tls and conn.host not in _warned_insecure_tls:
            _warned_insecure_tls.add(conn.host)
            log.warning(
                "Proxmox TLS verification DISABLED for %s — the API token is sent over an "
                "unverified channel and an on-path attacker could capture it. Use a trusted "
                "certificate or accept this risk for a self-signed homelab node.", conn.host)
        self.api = ProxmoxAPI(
            conn.host,
            user=user,
            token_name=token_name,
            token_value=decrypt(conn.token_secret_enc),
            verify_ssl=conn.verify_tls,
            service="PVE",
            port=_proxmox_port(conn.port),
            timeout=30,
        )

    # ---- diagnostics ---------------------------------------------------
    def version(self) -> dict:
        try:
            return self.api.version.get()
        except ResourceException as e:  # noqa: PERF203
            raise ProxmoxError(str(e)) from e

    def nodes(self) -> list[dict]:
        return self.api.nodes.get()

    def pick_node(self) -> str:
        if self.node:
            return self.node
        nodes = [n for n in self.nodes() if n.get("status") == "online"]
        if not nodes:
            raise ProxmoxError("no online Proxmox node available")
        return nodes[0]["node"]

    # ---- capacity ------------------------------------------------------
    def node_status(self, node: Optional[str] = None) -> dict:
        """Node status: cpuinfo.cpus, cpu (0..1 load), memory.{total,used,free}."""
        return self.api.nodes(node or self.pick_node()).status.get()

    def storage_status(self, node: Optional[str] = None) -> list[dict]:
        """Per-store {storage, type, total, used, avail} on the node."""
        return self.api.nodes(node or self.pick_node()).storage.get()

    def bridges(self, node: Optional[str] = None) -> list[str]:
        """Bridge interface names configured on the node (for the network device).
        Best-effort: returns [] if the listing can't be read."""
        try:
            ifaces = self.api.nodes(node or self.pick_node()).network.get(type="any_bridge")
            return [it["iface"] for it in (ifaces or []) if (it or {}).get("iface")]
        except Exception:  # noqa: BLE001
            return []

    # ---- inventory -----------------------------------------------------
    def list_qemu(self, node: Optional[str] = None) -> list[dict]:
        return self.api.nodes(node or self.pick_node()).qemu.get()

    def list_cluster_guests(self) -> list[dict]:
        """Read validated cluster guest identity, including containers.

        The resources API filters guests by ACL. A propagated VM.Audit grant on
        /vms is required. Child ACLs may still hide individual guests, so negative
        decisions additionally require _assert_vmid_free's unfiltered registry.
        Never fall back to the configured node: a guest may have migrated.
        """
        try:
            permissions = self.api.access.permissions.get(path="/vms")
            if not isinstance(permissions, dict) or permissions.get("/vms", {}).get("VM.Audit") != 1:
                raise ProxmoxError("complete cluster inventory requires propagated VM.Audit on /vms")
            guests = self.api.cluster.resources.get(type="vm")
            if not isinstance(guests, list):
                raise ProxmoxError("cluster guest inventory is unavailable or incomplete")
            seen = set()
            for guest in guests:
                if (not isinstance(guest, dict) or guest.get("type") not in {"qemu", "lxc"}
                        or not isinstance(guest.get("node"), str) or not guest["node"].strip()
                        or not re.fullmatch(r"[0-9]+", str(guest.get("vmid")))):
                    raise ProxmoxError("cluster guest inventory has incomplete identity")
                vmid = int(guest["vmid"])
                if vmid <= 0 or vmid in seen:
                    raise ProxmoxError("cluster guest inventory has ambiguous identity")
                seen.add(vmid)
            return guests
        except ProxmoxError:
            raise
        except Exception as exc:
            raise ProxmoxError(f"complete cluster inventory unavailable: {exc}") from exc

    def find_vm_node(self, vmid: int, node: Optional[str] = None) -> Optional[str]:
        """Find current QEMU placement; None means proven cluster-wide absence."""
        guard_vmid(vmid)
        for guest in self.list_cluster_guests():
            if int(guest["vmid"]) == int(vmid):
                if guest["type"] != "qemu":
                    raise ProxmoxError(f"VM ID {vmid} belongs to a container")
                return guest["node"]
        self._assert_vmid_free(vmid)
        return None

    def _assert_vmid_free(self, vmid: int) -> None:
        """Corroborate negative ACL-filtered inventory with the unfiltered registry.

        /cluster/nextid?vmid= is a read-only assertion, available to every user.
        Unlike /cluster/resources it checks all QEMU/LXC identities even when a
        child NoAccess ACL hides one despite inherited VM.Audit on /vms.
        """
        try:
            free = self.api.cluster.nextid.get(vmid=int(vmid))
            if not re.fullmatch(r"[0-9]+", str(free)) or int(free) != int(vmid):
                raise ProxmoxError("cluster registry did not confirm the requested VM ID is free")
        except Exception as exc:
            raise ProxmoxError(f"cannot confirm VM {vmid} is absent: {exc}") from exc

    def _existing_vm_node(self, vmid: int) -> str:
        node = self.find_vm_node(vmid)
        if node is None:
            raise ProxmoxError(f"VM {vmid} is absent from the cluster")
        return node

    def vm_current(self, vmid: int, node: Optional[str] = None) -> dict:
        guard_vmid(vmid)
        return self.api.nodes(self._existing_vm_node(vmid)).qemu(vmid).status.current.get()

    def vm_config(self, vmid: int, node: Optional[str] = None) -> dict:
        guard_vmid(vmid)
        return self.api.nodes(self._existing_vm_node(vmid)).qemu(vmid).config.get()

    # ---- task polling --------------------------------------------------
    def task_status(self, upid: str, node: Optional[str] = None) -> dict:
        """Read a recorded task's status on its owning node without stopping it."""
        task_node = upid.split(":", 2)[1] if isinstance(upid, str) and upid.startswith("UPID:") else ""
        return self.api.nodes(task_node or node or self.pick_node()).tasks(upid).status.get()

    def wait_task(
        self,
        upid: str,
        node: Optional[str] = None,
        timeout: float = 900,
        on_poll: Optional[Callable[[dict], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Block until the task finishes OK; raise ProxmoxError on failure/timeout,
        or JobCancelled if the `cancelled` predicate fires."""
        task_node = upid.split(":", 2)[1] if isinstance(upid, str) and upid.startswith("UPID:") else ""
        node = task_node or node or self.pick_node()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cancelled and cancelled():
                try:
                    self.api.nodes(node).tasks(upid).delete()
                except Exception:  # transport failures must not mask requested cancellation
                    pass
                raise JobCancelled()
            st = self.api.nodes(node).tasks(upid).status.get()
            if on_poll:
                try:
                    on_poll(st)
                except Exception:  # noqa: BLE001 — a raising telemetry callback must never fail the task
                    pass
            if st.get("status") == "stopped":
                exit_status = st.get("exitstatus", "")
                if exit_status != "OK":
                    # A FAILED Proxmox task (clone/import/start/destroy that ends with a
                    # non-OK exit status) must never be mistaken for success. Raise so the
                    # caller's error handling runs instead of silently advancing — the
                    # job's _execute() try/except marks it failed. Best-effort callers
                    # (download, pre-rebuild cleanup) already wrap this in try/except.
                    raise ProxmoxError(f"task {upid} failed: {exit_status or 'unknown error'}")
                return
            time.sleep(1.5)
        # the node task keeps running after WE give up — stop it so a failed job
        # doesn't leave an orphaned download/clone chewing on the node
        try:
            self.api.nodes(node).tasks(upid).delete()
        except Exception:  # noqa: BLE001
            pass
        raise ProxmoxError(f"task {upid} timed out")

    # ---- lifecycle -----------------------------------------------------
    def start(self, vmid: int, node: Optional[str] = None) -> str:
        guard_vmid(vmid)
        return self.api.nodes(self._existing_vm_node(vmid)).qemu(vmid).status.start.post()

    def stop(self, vmid: int, node: Optional[str] = None) -> str:
        guard_vmid(vmid)
        return self.api.nodes(self._existing_vm_node(vmid)).qemu(vmid).status.stop.post()

    def reboot(self, vmid: int, node: Optional[str] = None) -> str:
        guard_vmid(vmid)
        return self.api.nodes(self._existing_vm_node(vmid)).qemu(vmid).status.reboot.post()

    def destroy(self, vmid: int, node: Optional[str] = None) -> str:
        guard_vmid(vmid)
        return (
            self.api.nodes(self._existing_vm_node(vmid))
            .qemu(vmid)
            .delete(purge=1, **{"destroy-unreferenced-disks": 1})
        )

    # ---- snapshots ------------------------------------------------------
    def list_snapshots(self, vmid: int, node: Optional[str] = None) -> list[dict]:
        """Raw snapshot list incl. the synthetic 'current' entry (its parent is the
        snapshot the VM currently sits on)."""
        guard_vmid(vmid)
        return self.api.nodes(self._existing_vm_node(vmid)).qemu(vmid).snapshot.get()

    def create_snapshot(self, vmid: int, name: str, description: str = "",
                        vmstate: bool = False, node: Optional[str] = None) -> str:
        guard_vmid(vmid)
        guard_snapname(name)
        params: dict[str, Any] = {"snapname": name, "vmstate": 1 if vmstate else 0}
        if description:
            params["description"] = description
        return self.api.nodes(self._existing_vm_node(vmid)).qemu(vmid).snapshot.post(**params)

    def delete_snapshot(self, vmid: int, name: str, node: Optional[str] = None) -> str:
        guard_vmid(vmid)
        guard_snapname(name)
        return self.api.nodes(self._existing_vm_node(vmid)).qemu(vmid).snapshot(name).delete()

    def rollback_snapshot(self, vmid: int, name: str, node: Optional[str] = None) -> str:
        guard_vmid(vmid)
        guard_snapname(name)
        return self.api.nodes(self._existing_vm_node(vmid)).qemu(vmid).snapshot(name).rollback.post()

    # ---- vmid allocation ----------------------------------------------
    def next_free_vmid(self, lo: int, hi: int, node: Optional[str] = None) -> int:
        used = {int(v["vmid"]) for v in self.list_cluster_guests()}
        for vmid in range(lo, hi + 1):
            if vmid not in used:
                self._assert_vmid_free(vmid)
                return vmid
        raise ProxmoxError(f"no free VMID in range {lo}-{hi}")

    def _wait_submitted_task(self, upid, node: str, on_task=None, cancelled=None) -> None:
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            raise ProxmoxError("Proxmox did not return an identifiable task; outcome is unknown")
        if on_task:
            on_task(upid)
        self.wait_task(upid, node=node, cancelled=cancelled)

    def set_config(self, vmid: int, node: Optional[str] = None, *,
                   on_task: Optional[Callable[[str], None]] = None,
                   cancelled: Optional[Callable[[], bool]] = None, **params) -> None:
        guard_vmid(vmid)
        if cancelled and cancelled():
            raise JobCancelled()
        node = self._existing_vm_node(vmid)
        upid = self.api.nodes(node).qemu(vmid).config.post(**params)
        # The API's explicit background_delay option returns null only after it
        # has verified success itself; without that option null is ambiguous.
        if upid is None and params.get("background_delay"):
            return
        self._wait_submitted_task(upid, node, on_task, cancelled)

    def resize_disk(self, vmid: int, disk: str, size: str, node: Optional[str] = None, *,
                    on_task: Optional[Callable[[str], None]] = None,
                    cancelled: Optional[Callable[[], bool]] = None) -> None:
        guard_vmid(vmid)
        if cancelled and cancelled():
            raise JobCancelled()
        node = self._existing_vm_node(vmid)
        upid = self.api.nodes(node).qemu(vmid).resize.put(disk=disk, size=size)
        self._wait_submitted_task(upid, node, on_task, cancelled)

    # ---- base image download / import helpers -------------------------
    def download_url(
        self, filename: str, url: str, node: Optional[str] = None,
        checksum: str = "", checksum_algorithm: str = "",
    ) -> str:
        node = node or self.pick_node()
        # 'import' content type (PVE 8.2+) so the downloaded VM image can be used
        # directly as scsi0 import-from. (iso content can't be import-from'd.)
        params: dict[str, Any] = {"content": "import", "filename": filename, "url": url}
        if checksum and checksum_algorithm:
            params["checksum"] = checksum
            params["checksum-algorithm"] = checksum_algorithm
        return self.api.nodes(node).storage(self.iso_storage)("download-url").post(**params)

    def iso_volume_path(self, filename: str) -> str:
        # Storage *volume id* (not an absolute path) so a non-root API token is
        # allowed to use it as import-from. e.g. local:import/noble.img
        return f"{self.iso_storage}:import/{filename}"

    def storage_volumes(self, node: Optional[str] = None, content: str = "import") -> set:
        """Volume ids present in the iso/import storage on `node`. RAISES on a
        listing failure — callers that need offline-detection (the cache-status
        endpoint) rely on the exception; tolerant callers use storage_has_volume."""
        node = node or self.pick_node()
        items = self.api.nodes(node).storage(self.iso_storage).content.get(content=content)
        return {(it or {}).get("volid") for it in (items or [])}

    def storage_has_volume(self, filename: str, node: Optional[str] = None,
                           content: str = "import") -> bool:
        """Is `filename` already present in the iso/import storage on `node`? Used to
        distinguish a benign 'file already exists' from a real download/checksum
        failure. Returns False if the listing itself can't be read."""
        node = node or self.pick_node()
        volid = self.iso_volume_path(filename)
        try:
            return volid in self.storage_volumes(node=node, content=content)
        except Exception:  # noqa: BLE001
            return False

    def delete_storage_volume(self, filename: str, node: Optional[str] = None):
        """Delete one safe import-cache volume, returning a task id when PVE does."""
        if not re.fullmatch(r"[A-Za-z0-9._-]+", filename or ""):
            raise ProxmoxError(f"invalid storage filename: {filename!r}")
        node = node or self.pick_node()
        volid = self.iso_volume_path(filename)
        return self.api.nodes(node).storage(self.iso_storage).content(volid).delete()

    def validate_snippet_volume(self, volid: str, node: Optional[str] = None) -> None:
        """Require a normalized, configured, and API-visible cloud-init snippet.

        SSH/SFTP success alone does not prove that Proxmox can attach the volume.
        Check the active snippet storage advertises ``snippets`` and that its content
        API lists this exact volume before accepting a VM-create request.
        """
        match = re.fullmatch(r"([A-Za-z0-9_.-]+):snippets/([A-Za-z0-9_.-]+)", volid or "")
        if not match:
            raise ProxmoxError(f"invalid snippet volume id: {volid!r}")
        storage = match.group(1)
        if storage != self.snippet_storage:
            raise ProxmoxError(
                f"snippet volume storage {storage!r} does not match configured storage "
                f"{self.snippet_storage!r}"
            )
        node = node or self.pick_node()
        stores = self.api.nodes(node).storage.get() or []
        active = next((item for item in stores if (item or {}).get("storage") == storage), None)
        contents = [part.strip() for part in str((active or {}).get("content", "")).split(",")]
        if not active or not active.get("active") or "snippets" not in contents:
            raise ProxmoxError(f"storage {storage!r} is not active for snippets on {node}")
        volumes = self.api.nodes(node).storage(storage).content.get(content="snippets") or []
        if volid not in {(item or {}).get("volid") for item in volumes}:
            raise ProxmoxError(f"snippet volume {volid!r} is not visible on {node}")

    def create_vm_import(
        self, vmid: int, name: str, import_path: str, cores: int, ram_mb: int,
        node: Optional[str] = None,
    ) -> str:
        node = node or self.pick_node()
        guard_vmid(vmid)
        params = {
            "vmid": vmid,
            "name": name,
            "cores": cores,
            "sockets": 1,
            "memory": ram_mb,
            "cpu": "host",
            "net0": f"virtio,bridge={self.bridge}",
            "scsihw": "virtio-scsi-single",
            "scsi0": f"{self.storage}:0,import-from={import_path},discard=on",
            "ide2": f"{self.storage}:cloudinit",
            "boot": "order=scsi0",
            # serial0 powers the in-app serial console; keep vga=std (real framebuffer)
            # so the GRAPHICAL console shows the VGA display (tty1 login), like Proxmox.
            "serial0": "socket",
            "vga": "std",
            "agent": "enabled=1",
            "ostype": "l26",
            "onboot": 0,
        }
        return self.api.nodes(node).qemu.post(**params)

    # ---- guest agent ---------------------------------------------------
    def wait_guest_ready(self, vmid: int, node: Optional[str] = None,
                         cancelled: Optional[Callable[[], bool]] = None,
                         timeout: float = 900, *, require_marker: bool = True) -> None:
        """Wait for cloud-init and the generated recipe through guest-agent exec.

        These Linux cloud images must provide cloud-init and /bin/sh; this uses no
        distro-specific package/status paths. Exec is asynchronous (PID/status),
        and a running agent alone is not readiness. Missing/failed recipe output,
        including cloud-init's recoverable-error exit code 2, fails deployment.
        """
        guard_vmid(vmid)
        if cancelled and cancelled():
            raise JobCancelled()
        node = self._existing_vm_node(vmid)
        script = ('state=$(cloud-init status --wait 2>&1) || exit 20; '
                  'case "$state" in *"status: done") ;; *) exit 20;; esac')
        if require_marker:
            script += '; [ "$(cat /var/lib/goblindock-recipe-result 2>/dev/null)" = 0 ] || exit 21'
        command = "/bin/sh -c " + shlex.quote(script)
        deadline = time.monotonic() + timeout
        pid = None
        last_error = "guest agent has not responded"
        while time.monotonic() < deadline:
            if cancelled and cancelled():
                raise JobCancelled()
            try:
                agent = self.api.nodes(node).qemu(vmid).agent
                if pid is None:
                    started = agent("exec").post(command=command)
                    pid = started.get("pid") if isinstance(started, dict) else None
                    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                        raise ProxmoxError("guest agent returned no valid readiness process ID")
                state = agent("exec-status").get(pid=pid)
            except ResourceException as exc:
                # Agent startup races are expected after boot. Keep an accepted PID
                # so transient polling errors never launch duplicate guest commands.
                last_error = str(exc)
                time.sleep(min(1.5, max(0, deadline - time.monotonic())))
                continue
            if not isinstance(state, dict):
                raise ProxmoxError("guest readiness returned malformed process status")
            if state.get("exited"):
                if state.get("exitcode") != 0:
                    raise ProxmoxError(
                        "guest initialization failed: cloud-init or recipe result was unsuccessful "
                        f"(exit {state.get('exitcode', 'unknown')})"
                    )
                return
            last_error = "cloud-init is still running"
            time.sleep(min(1.5, max(0, deadline - time.monotonic())))
        raise ProxmoxError(f"guest initialization timed out: {last_error}")

    def agent_ipv4(self, vmid: int, node: Optional[str] = None) -> Optional[str]:
        guard_vmid(vmid)
        node = self._existing_vm_node(vmid)
        try:
            res = self.api.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
        except ResourceException:
            return None
        candidates = []
        for iface in res.get("result", []):
            if iface.get("name") in ("lo", "lo0"):
                continue
            for addr in iface.get("ip-addresses", []) or []:
                if addr.get("ip-address-type") != "ipv4":
                    continue
                raw = (addr.get("ip-address") or "").strip()
                try:
                    ip = ipaddress.ip_address(raw)
                except ValueError:
                    continue
                # Skip non-routable noise so a wrong address can never latch as the
                # deployment IP (which _reconcile_ips only fills when empty): loopback,
                # link-local (169.254) and 0.0.0.0.
                if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
                    continue
                candidates.append(raw)
        if not candidates:
            return None
        # Prefer a globally-routable address, then a LAN address, and push a Docker
        # default-bridge address (172.17/16) last — a workload bridge must never shadow
        # the VM's real management lease.
        def _rank(r: str):
            a = ipaddress.ip_address(r)
            return (a in _DOCKER_BRIDGE_NET, a.is_private)
        candidates.sort(key=_rank)
        return candidates[0]

    def mac_of(self, vmid: int, node: Optional[str] = None) -> str:
        cfg = self.vm_config(vmid, node)
        net0 = cfg.get("net0", "")
        for part in net0.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.lower() in ("virtio", "e1000", "rtl8139", "vmxnet3", "macaddr"):
                    return v
        return ""

    # ---- detail view + console ----------------------------------------
    def agent_osinfo(self, vmid: int, node: Optional[str] = None) -> dict:
        guard_vmid(vmid)
        try:
            r = self.api.nodes(self._existing_vm_node(vmid)).qemu(vmid).agent("get-osinfo").get()
            return r.get("result", {}) if isinstance(r, dict) else {}
        except ResourceException:
            return {}

    def agent_interfaces(self, vmid: int, node: Optional[str] = None) -> list[dict]:
        guard_vmid(vmid)
        try:
            r = self.api.nodes(self._existing_vm_node(vmid)).qemu(vmid).agent("network-get-interfaces").get()
            return r.get("result", []) if isinstance(r, dict) else []
        except ResourceException:
            return []

    def ensure_serial(self, vmid: int, node: Optional[str] = None) -> bool:
        """Make sure the VM has serial0 (needed for the xterm console). Returns True
        if it was already present (console works now), False if just added (needs a
        reboot to take effect)."""
        guard_vmid(vmid)
        node = node or self.pick_node()
        if self.vm_config(vmid, node).get("serial0"):
            return True
        self.set_config(vmid, node=node, serial0="socket")
        return False

    def termproxy(self, vmid: int, node: Optional[str] = None) -> dict:
        """Open a serial term proxy — returns {ticket, port, user, ...}."""
        guard_vmid(vmid)
        return self.api.nodes(self._existing_vm_node(vmid)).qemu(vmid).termproxy.post()

    def vncproxy(self, vmid: int, node: Optional[str] = None) -> dict:
        """Open a VNC (graphical console) proxy — returns {ticket, port, user, ...}.
        The ticket doubles as the VNC password the client must send."""
        guard_vmid(vmid)
        return self.api.nodes(self._existing_vm_node(vmid)).qemu(vmid).vncproxy.post(websocket=1)

    def token_auth_header(self) -> str:
        return f"PVEAPIToken={self.conn.token_id}={decrypt(self.conn.token_secret_enc)}"

    def console_ws_url(self, vmid: int, node: str, port, ticket: str) -> str:
        from urllib.parse import quote
        node = self._existing_vm_node(vmid)
        host, pp = self.conn.host, _proxmox_port(self.conn.port)
        return (f"wss://{host}:{pp}/api2/json/nodes/{node}/qemu/{vmid}"
                f"/vncwebsocket?port={port}&vncticket={quote(str(ticket))}")


def _load_ssh_key(path: str):
    """Try the supported private-key types in turn; None if none load."""
    import paramiko

    for loader in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return loader.from_private_key_file(path)
        except Exception:  # noqa: BLE001
            continue
    return None


def _tofu_host_key_policy(paramiko, tofu_path: Path):
    """Persist the first key for a hostname, then reject every unknown key type.

    Paramiko's AutoAddPolicy works per hostname *and algorithm*, which would accept
    an ECDSA attacker key after an RSA key was pinned.  TOFU is hostname-wide here:
    once any algorithm is known, only an exact previously pinned key is acceptable.
    """
    class _HostnameTofuPolicy(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            with _ssh_tofu_lock:
                persisted = paramiko.HostKeys()
                persisted.load(str(tofu_path))
                pinned = []
                for host_keys in (
                    getattr(client, "_system_host_keys", None),
                    getattr(client, "_host_keys", None),
                    persisted,
                ):
                    known = host_keys.lookup(hostname) if host_keys is not None else None
                    if known:
                        pinned.extend(known.values())
                if any(existing == key for existing in pinned):
                    # A concurrent first connection may have persisted this exact key
                    # after this client loaded its snapshot but before its handshake.
                    client._host_keys.add(hostname, key.get_name(), key)
                    return
                if pinned:
                    raise paramiko.BadHostKeyException(hostname, key, pinned[0])
                client._host_keys.add(hostname, key.get_name(), key)
                client.save_host_keys(str(tofu_path))
                os.chmod(tofu_path, 0o600)

    return _HostnameTofuPolicy()


def _ssh_client(conn: Connection, key, timeout: int):
    """Connected paramiko SSHClient for the node — shared by snippet write/delete.
    Honours any known_hosts we have so a pinned node can't be MITM'd. Strict
    mode rejects unknown hosts; the homelab default trusts-on-first-use."""
    import paramiko

    client = paramiko.SSHClient()
    try:
        client.load_system_host_keys()
    except Exception:  # noqa: BLE001
        pass
    if settings.ssh_known_hosts and os.path.exists(settings.ssh_known_hosts):
        try:
            # Explicit pins are read-only and checked before the writable TOFU set.
            client.load_system_host_keys(settings.ssh_known_hosts)
        except Exception:  # noqa: BLE001
            pass
    if not settings.ssh_strict:
        tofu_path = Path(settings.data_dir) / "ssh_known_hosts"
        tofu_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(tofu_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        # Existing installations may have created the file under a permissive umask.
        # Tighten it before loading; AutoAddPolicy preserves the mode when truncating.
        os.chmod(tofu_path, 0o600)
        client.load_host_keys(str(tofu_path))
    client.set_missing_host_key_policy(
        paramiko.RejectPolicy()
        if settings.ssh_strict
        else _tofu_host_key_policy(paramiko, tofu_path)
    )
    client.connect(conn.ssh_host or conn.host, username=conn.ssh_user or "root", pkey=key, timeout=timeout)
    return client


def _safe_path_token(s: str) -> bool:
    """A single path segment safe to splice into a node-side SFTP path: a normal leading
    character (not a dot) then the allowed set, and never a '..' anywhere. The bare
    ``[A-Za-z0-9_.-]+`` allowlist matched '..' and '.'; requiring a non-dot lead and
    rejecting '..' closes traversal even if a future caller passes user input here."""
    return bool(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", s or "")) and ".." not in s


def write_snippet_over_ssh(conn: Connection, filename: str, content: str) -> str:
    """Drop a cloud-init snippet onto the node's snippet storage via SSH/SFTP.

    Returns the cicustom volume id (e.g. 'local:snippets/gd-8000.yml'). Requires
    conn.ssh_key_path to point at a usable private key inside the container.
    """
    if not conn.ssh_key_path:
        raise ProxmoxError("no SSH key configured for snippet baking")

    # snippet_storage and filename land in shell/sftp paths on the node — both are
    # constrained to a strict allowlist so neither can inject shell or traverse.
    store = conn.snippet_storage or "local"
    if not _safe_path_token(store):
        raise ProxmoxError(f"invalid snippet storage name: {store!r}")
    if not _safe_path_token(filename):
        raise ProxmoxError(f"invalid snippet filename: {filename!r}")

    base = "/var/lib/vz/snippets" if store == "local" else f"/mnt/pve/{store}/snippets"
    remote = f"{base}/{filename}"

    key = _load_ssh_key(conn.ssh_key_path)
    if key is None:
        raise ProxmoxError(f"could not load SSH key {conn.ssh_key_path}")

    client = _ssh_client(conn, key, timeout=20)
    try:
        # Pure SFTP — no shell exec, so nothing user-influenced reaches a shell.
        sftp = client.open_sftp()
        try:
            try:
                sftp.stat(base)
            except IOError:
                try:
                    sftp.mkdir(base)
                except IOError:
                    pass  # parent may be missing; putfo below will surface a clear error
            sftp.putfo(io.BytesIO(content.encode("utf-8")), remote)
            # The cloud-config may contain resolved password/secret inputs.  Fail
            # closed unless the node-side copy is readable only by its SSH owner.
            try:
                sftp.chmod(remote, 0o600)
            except Exception:
                try:
                    sftp.remove(remote)
                except Exception:  # noqa: BLE001
                    pass
                raise
        finally:
            sftp.close()
    finally:
        client.close()

    return f"{store}:snippets/{filename}"


def delete_snippet_over_ssh(conn: Connection, filename: str) -> None:
    """Best-effort removal of a cloud-init snippet from the node (cleanup)."""
    if not conn.ssh_key_path or not _safe_path_token(filename or ""):
        return
    store = conn.snippet_storage or "local"
    if not _safe_path_token(store):
        return
    base = "/var/lib/vz/snippets" if store == "local" else f"/mnt/pve/{store}/snippets"
    key = _load_ssh_key(conn.ssh_key_path)
    if key is None:
        return
    try:
        client = _ssh_client(conn, key, timeout=15)
    except Exception:  # noqa: BLE001
        return
    try:
        sftp = client.open_sftp()
        try:
            sftp.remove(f"{base}/{filename}")
        except IOError:
            pass
        sftp.close()
    except Exception:  # noqa: BLE001
        pass
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# read-only inventory truth                                                    #
# --------------------------------------------------------------------------- #
VM_PRESENT = "present"
VM_ABSENT = "absent"
VM_UNKNOWN = "unknown"


def probe_vm_presence(
    px: Optional["Proxmox"], vmid: Optional[int], node: Optional[str]
) -> tuple[str, str]:
    """Return tri-state Proxmox inventory truth without collapsing errors to absence."""
    if vmid is None:
        return VM_ABSENT, "VM ID was never assigned"
    if px is None:
        return VM_UNKNOWN, "Proxmox client unavailable"
    try:
        present = vmid in {int(v["vmid"]) for v in px.list_cluster_guests()}
        if not present:
            px._assert_vmid_free(vmid)
    except Exception as exc:  # noqa: BLE001
        return VM_UNKNOWN, f"Proxmox inventory probe failed: {exc}"
    if present:
        return VM_PRESENT, f"VM {vmid} is present in Proxmox inventory"
    return VM_ABSENT, f"VM {vmid} is absent from Proxmox inventory"
