"""Thin, purpose-built wrapper over the Proxmox VE REST API (via proxmoxer).

Only the operations GoblinDock actually needs: list/inspect/lifecycle of VMs,
clone a template, build a template from a cloud image (download-url + import-from),
cloud-init configuration, guest-agent IP discovery, and (optionally, over SSH) a
cloud-init snippet for baking a recipe.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import time
from typing import Any, Callable, Optional

from proxmoxer import ProxmoxAPI
from proxmoxer.core import ResourceException

from .config import settings
from .models import Connection
from .security import decrypt


class ProxmoxError(RuntimeError):
    pass


def base_disk_filename(src_url: str) -> str:
    """Cached per-URL qcow2 name on node storage. 'import' content needs a
    recognised extension (cloud .img files are qcow2), and the name flows into
    the comma-delimited import-from config — strict allowlist, URL-hash namespaced."""
    raw_name = (src_url.rsplit("/", 1)[-1] if src_url else "image") or "image"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name).lstrip(".-") or "image"
    stem = safe.rsplit(".", 1)[0] or "image"
    url_tag = hashlib.sha256((src_url or "").encode()).hexdigest()[:8]
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
        self.api = ProxmoxAPI(
            conn.host,
            user=user,
            token_name=token_name,
            token_value=decrypt(conn.token_secret_enc),
            verify_ssl=conn.verify_tls,
            service="PVE",
            port=conn.port or 8006,
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

    # ---- inventory -----------------------------------------------------
    def list_qemu(self, node: Optional[str] = None) -> list[dict]:
        return self.api.nodes(node or self.pick_node()).qemu.get()

    def vm_current(self, vmid: int, node: Optional[str] = None) -> dict:
        guard_vmid(vmid)
        return self.api.nodes(node or self.pick_node()).qemu(vmid).status.current.get()

    def vm_config(self, vmid: int, node: Optional[str] = None) -> dict:
        guard_vmid(vmid)
        return self.api.nodes(node or self.pick_node()).qemu(vmid).config.get()

    # ---- task polling --------------------------------------------------
    def wait_task(
        self,
        upid: str,
        node: Optional[str] = None,
        timeout: float = 900,
        on_poll: Optional[Callable[[dict], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> None:
        """Block until the task finishes OK; raise ProxmoxError on failure/timeout/cancel."""
        node = node or self.pick_node()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cancelled and cancelled():
                try:
                    self.api.nodes(node).tasks(upid).delete()
                except ResourceException:
                    pass
                raise ProxmoxError("cancelled")
            st = self.api.nodes(node).tasks(upid).status.get()
            if on_poll:
                on_poll(st)
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
        return self.api.nodes(node or self.pick_node()).qemu(vmid).status.start.post()

    def stop(self, vmid: int, node: Optional[str] = None) -> str:
        guard_vmid(vmid)
        return self.api.nodes(node or self.pick_node()).qemu(vmid).status.stop.post()

    def shutdown(self, vmid: int, node: Optional[str] = None, timeout: int = 60) -> str:
        guard_vmid(vmid)
        return (
            self.api.nodes(node or self.pick_node())
            .qemu(vmid)
            .status.shutdown.post(timeout=timeout, forceStop=1)
        )

    def reboot(self, vmid: int, node: Optional[str] = None) -> str:
        guard_vmid(vmid)
        return self.api.nodes(node or self.pick_node()).qemu(vmid).status.reboot.post()

    def destroy(self, vmid: int, node: Optional[str] = None) -> str:
        guard_vmid(vmid)
        return (
            self.api.nodes(node or self.pick_node())
            .qemu(vmid)
            .delete(purge=1, **{"destroy-unreferenced-disks": 1})
        )

    # ---- vmid allocation ----------------------------------------------
    def next_free_vmid(self, lo: int, hi: int, node: Optional[str] = None) -> int:
        used = {int(v["vmid"]) for v in self.list_qemu(node)}
        for vmid in range(lo, hi + 1):
            if vmid not in used:
                return vmid
        raise ProxmoxError(f"no free VMID in range {lo}-{hi}")

    def set_config(self, vmid: int, node: Optional[str] = None, **params) -> None:
        guard_vmid(vmid)
        self.api.nodes(node or self.pick_node()).qemu(vmid).config.post(**params)

    def resize_disk(self, vmid: int, disk: str, size: str, node: Optional[str] = None) -> None:
        guard_vmid(vmid)
        self.api.nodes(node or self.pick_node()).qemu(vmid).resize.put(disk=disk, size=size)

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
    def agent_ipv4(self, vmid: int, node: Optional[str] = None) -> Optional[str]:
        guard_vmid(vmid)
        node = node or self.pick_node()
        try:
            res = self.api.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
        except ResourceException:
            return None
        for iface in res.get("result", []):
            if iface.get("name") in ("lo", "lo0"):
                continue
            for addr in iface.get("ip-addresses", []) or []:
                if addr.get("ip-address-type") == "ipv4":
                    ip = addr.get("ip-address", "")
                    if ip and not ip.startswith("127."):
                        return ip
        return None

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
            r = self.api.nodes(node or self.pick_node()).qemu(vmid).agent("get-osinfo").get()
            return r.get("result", {}) if isinstance(r, dict) else {}
        except ResourceException:
            return {}

    def agent_interfaces(self, vmid: int, node: Optional[str] = None) -> list[dict]:
        guard_vmid(vmid)
        try:
            r = self.api.nodes(node or self.pick_node()).qemu(vmid).agent("network-get-interfaces").get()
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
        return self.api.nodes(node or self.pick_node()).qemu(vmid).termproxy.post()

    def vncproxy(self, vmid: int, node: Optional[str] = None) -> dict:
        """Open a VNC (graphical console) proxy — returns {ticket, port, user, ...}.
        The ticket doubles as the VNC password the client must send."""
        guard_vmid(vmid)
        return self.api.nodes(node or self.pick_node()).qemu(vmid).vncproxy.post(websocket=1)

    def token_auth_header(self) -> str:
        return f"PVEAPIToken={self.conn.token_id}={decrypt(self.conn.token_secret_enc)}"

    def console_ws_url(self, vmid: int, node: str, port, ticket: str) -> str:
        from urllib.parse import quote
        host, pp = self.conn.host, self.conn.port or 8006
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
            client.load_host_keys(settings.ssh_known_hosts)
        except Exception:  # noqa: BLE001
            pass
    client.set_missing_host_key_policy(
        paramiko.RejectPolicy() if settings.ssh_strict else paramiko.AutoAddPolicy()
    )
    client.connect(conn.ssh_host or conn.host, username=conn.ssh_user or "root", pkey=key, timeout=timeout)
    return client


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
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", store):
        raise ProxmoxError(f"invalid snippet storage name: {store!r}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
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
            sftp.stat(base)
        except IOError:
            try:
                sftp.mkdir(base)
            except IOError:
                pass  # parent may be missing; putfo below will surface a clear error
        sftp.putfo(io.BytesIO(content.encode("utf-8")), remote)
        sftp.close()
    finally:
        client.close()

    return f"{conn.snippet_storage}:snippets/{filename}"


def delete_snippet_over_ssh(conn: Connection, filename: str) -> None:
    """Best-effort removal of a cloud-init snippet from the node (cleanup)."""
    if not conn.ssh_key_path or not re.fullmatch(r"[A-Za-z0-9_.-]+", filename or ""):
        return
    store = conn.snippet_storage or "local"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", store):
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
