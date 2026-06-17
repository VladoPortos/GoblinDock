"""Background job runner.

A single daemon thread claims queued jobs from SQLite and executes them, writing
JobStep / JobEvent rows as it goes so the SSE endpoint can stream live progress.
This is the "worker" of the design's web+worker split, collapsed into one process
(a daemon thread) — appropriate for a single-container homelab tool.
"""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import select

from .config import settings
from .db import session_scope
from .ansible_exec import run_playbook
from .models import (
    Block,
    Connection,
    Deployment,
    Image,
    IpAllocation,
    Job,
    JobEvent,
    JobStep,
    Template,
    Secret,
    Variable,
    utcnow,
)
from .proxmox import (
    JobCancelled,
    Proxmox,
    ProxmoxError,
    base_disk_filename,
    delete_snippet_over_ssh,
    write_snippet_over_ssh,
)
from .recipes import (
    collect_sensitive_inputs,
    compile_ansible,
    compile_cloudinit,
    has_ansible_blocks,
    load_recipe,
    merge_deploy_inputs,
)
from .security import crypt_sha512, decrypt, encrypt, gen_vm_password
from .appsettings import auto_root_password_enabled
from . import statebus

_worker_thread: Optional[threading.Thread] = None
_stop = threading.Event()


# --------------------------------------------------------------------------- #
# Per-job progress helper                                                      #
# --------------------------------------------------------------------------- #
class JobCtx:
    def __init__(self, job_id: int):
        self.job_id = job_id
        self._seq = 0
        self._phase = ""

    def cancelled(self) -> bool:
        with session_scope() as s:
            job = s.get(Job, self.job_id)
            return bool(job and job.cancel_requested)

    def progress(self, pct: int, phase: str) -> None:
        self._phase = phase
        with session_scope() as s:
            job = s.get(Job, self.job_id)
            if job:
                job.pct = max(0, min(100, pct))
                job.phase = phase
                s.add(job)
        statebus.bump()
        self._tick()

    def phase_note(self, note: str) -> None:
        """Append a transient detail to the current phase title (e.g. a live
        download percentage) WITHOUT touching pct — each call replaces the
        previous note, so the dashboard job chip stays current."""
        with session_scope() as s:
            job = s.get(Job, self.job_id)
            if job:
                job.phase = f"{self._phase} · {note}" if self._phase else note
                s.add(job)
        statebus.bump()

    def add_step(self, name: str) -> int:
        with session_scope() as s:
            self._seq += 1
            step = JobStep(job_id=self.job_id, seq=self._seq, name=name, state="pending")
            s.add(step)
        self._tick()
        return self._seq

    def _set_step(self, seq: int, **fields) -> None:
        with session_scope() as s:
            step = s.exec(
                select(JobStep).where(JobStep.job_id == self.job_id, JobStep.seq == seq)
            ).first()
            if step:
                for k, v in fields.items():
                    setattr(step, k, v)
                s.add(step)
        self._tick()

    def start_step(self, seq: int) -> float:
        self._set_step(seq, state="running", started_at=utcnow())
        return time.time()

    def finish_step(self, seq: int, t0: float, state: str = "done") -> None:
        dur = f"{time.time() - t0:.1f}s"
        self._set_step(seq, state=state, dur=dur, finished_at=utcnow())

    def log(self, line: str, cls: str = "") -> None:
        with session_scope() as s:
            s.add(JobEvent(job_id=self.job_id, kind="log", line=line, log_class=cls))

    def _tick(self) -> None:
        with session_scope() as s:
            s.add(JobEvent(job_id=self.job_id, kind="tick"))


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _blocks_by_key() -> dict[str, Block]:
    with session_scope() as s:
        return {b.key: Block(**b.model_dump()) for b in s.exec(select(Block)).all()}


def _secret_lookup_factory(owner_id: Optional[int], sink: Optional[set] = None):
    """Resolve {{ secrets.NAME }} / {{ variable.NAME }}. If `sink` is given, every
    resolved SECRET plaintext is collected into it so the caller can redact those
    values out of streamed job logs (variables are plaintext-by-design and shown in
    the UI, so they are NOT collected)."""
    def lookup(ns: str, name: str) -> str:
        with session_scope() as s:
            if ns == "variable":
                # per-user variable overrides global; value is plaintext. order_by(id)
                # keeps resolution deterministic if a legacy duplicate name exists.
                var = s.exec(
                    select(Variable).where(Variable.name == name, Variable.owner_id == owner_id)
                    .order_by(Variable.id)
                ).first()
                if not var:
                    var = s.exec(
                        select(Variable).where(Variable.name == name, Variable.scope == "global")
                        .order_by(Variable.id)
                    ).first()
                return var.value if var else ""
            # per-user secret overrides global
            sec = s.exec(
                select(Secret).where(Secret.name == name, Secret.owner_id == owner_id)
                .order_by(Secret.id)
            ).first()
            if not sec:
                sec = s.exec(
                    select(Secret).where(Secret.name == name, Secret.scope == "global")
                    .order_by(Secret.id)
                ).first()
            if sec:
                sec.last_used = utcnow()
                s.add(sec)
                val = decrypt(sec.value_enc)
                if sink is not None and val:
                    sink.add(val)
                return val
        return ""
    return lookup


def _redactor(values: set):
    """Return a fn that masks any of `values` (resolved secret plaintexts) in a log
    line. Multiline secrets (e.g. SSH private keys) are also masked line-by-line since
    stdout is processed one line at a time. Longest-first so overlapping values mask
    fully; fragments < 4 chars are skipped to avoid corrupting unrelated log text."""
    frags: set = set()
    for v in values:
        if not v:
            continue
        frags.add(v)
        for ln in v.splitlines():
            ln = ln.strip()
            if len(ln) >= 8:        # individual key/body lines of a multiline secret
                frags.add(ln)
    masks = sorted((f for f in frags if len(f) >= 4), key=len, reverse=True)

    def red(line: str) -> str:
        for v in masks:
            if v in line:
                line = line.replace(v, "***")
        return line
    return red


def _valid_pubkey(key: str) -> bool:
    parts = (key or "").strip().split()
    if len(parts) < 2:
        return False
    if not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        return False
    import base64
    try:
        base64.b64decode(parts[1], validate=True)
    except Exception:  # noqa: BLE001
        return False
    return len(parts[1]) >= 40


def _ssh_pubkey(owner_id: Optional[int]) -> str:
    lookup = _secret_lookup_factory(owner_id)
    for name in ("DEPLOY_SSH_PUBKEY", "TEAM_SSH_PUBKEY"):
        val = lookup("secrets", name)
        if val and _valid_pubkey(val):
            return val
    return ""


def _generate_keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate()
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    return priv, pub + " goblindock"


def _managed_keypair() -> tuple[str, str]:
    """GoblinDock's own SSH keypair, used by ansible to log into VMs. Generated
    once and stored as global secrets; its public key is injected into every VM."""
    with session_scope() as s:
        priv = s.exec(select(Secret).where(Secret.name == "GD_MANAGED_PRIVKEY",
                                            Secret.scope == "global")).first()
        pub = s.exec(select(Secret).where(Secret.name == "GD_MANAGED_PUBKEY",
                                          Secret.scope == "global")).first()
        if priv and pub:
            return decrypt(priv.value_enc), decrypt(pub.value_enc)
    p, pubk = _generate_keypair()
    with session_scope() as s:
        if not s.exec(select(Secret).where(Secret.name == "GD_MANAGED_PRIVKEY")).first():
            s.add(Secret(scope="global", name="GD_MANAGED_PRIVKEY", value_enc=encrypt(p)))
            s.add(Secret(scope="global", name="GD_MANAGED_PUBKEY", value_enc=encrypt(pubk)))
    return p, pubk


def _run_ansible_phase(ctx: "JobCtx", recipe: list, owner_id, ip: str, managed_priv: str,
                       label: str) -> None:
    """Run the post-boot ansible-phase blocks of a recipe against a live VM."""
    if not (recipe and ip):
        return
    blocks = _blocks_by_key()
    if not has_ansible_blocks(recipe, blocks):
        return
    # Collect resolved secret values while compiling so we can redact them from the
    # streamed Ansible stdout — a task that echoes a secret (debug/failed command)
    # must NOT land its plaintext in the job log (which the job's owner can read).
    vault: set = set()
    lookup = _secret_lookup_factory(owner_id, sink=vault)
    playbook = compile_ansible(recipe, blocks, lookup, name=label)
    # Also redact LITERAL password/secret-typed input values: these never pass through
    # `lookup` (only {{ secrets.NAME }} refs do), so they would otherwise appear
    # unmasked in streamed Ansible output on a failed task.
    vault |= collect_sensitive_inputs(recipe, blocks, lookup)
    red = _redactor(vault)
    ctx.log(f"[{_ts()}] ansible: applying {label} to {ip}…", "l-acc")

    def _on_line(ln: str) -> None:
        cls = "l-dim" if ln.lstrip().startswith(("PLAY", "TASK", "ok:", "skipping")) else ("l-ok" if "changed:" in ln else "")
        ctx.log(red(ln), cls)

    try:
        status, rc = run_playbook(
            playbook, ip, "goblin", managed_priv, on_line=_on_line,
            cancelled=ctx.cancelled,
        )
    except Exception as e:  # noqa: BLE001
        ctx.log(f"[{_ts()}] ⚠ ansible run failed to start: {e}", "l-warn")
        return
    if status == "successful":
        ctx.log(f"[{_ts()}] ✓ ansible {label} complete", "l-ok")
    elif status == "canceled":
        # A user cancel terminated the run — signal it as a cancel (not a failure) so
        # _execute reconciles it as cancelled rather than leaving the deploy "error".
        raise JobCancelled()
    else:
        raise RuntimeError(f"ansible {label} failed (status={status}, rc={rc})")


def _deploy_cloud_config(name: str, pubkeys: list[str], recipe_cmds: list[str],
                         root_pw_hash: str = "") -> str:
    """Full #cloud-config: a goblin user, qemu-guest-agent (so the IP is reported),
    python3 (for ansible), and the cloud-init phase blocks run at first boot."""
    # Sink-level hostname hardening: coerce to a valid RFC1123-ish hostname so a
    # stored/legacy name can never inject sibling #cloud-config keys (newline) here.
    host = re.sub(r"[^A-Za-z0-9._-]", "-", (name or "vm")).strip("-.")[:63] or "vm"
    lines = [
        "#cloud-config",
        f"hostname: {host}",
        "manage_etc_hosts: true",
        "users:",
        "  - name: goblin",
        "    sudo: 'ALL=(ALL) NOPASSWD:ALL'",
        "    groups: [sudo, adm]",
        "    shell: /bin/bash",
    ]
    keys = [k.strip() for k in (pubkeys or []) if k and k.strip()]
    if keys:
        lines += ["    ssh_authorized_keys:"] + [f"      - {k}" for k in keys]
    if root_pw_hash:
        lines += [
            "chpasswd:",
            "  expire: false",
            "  users:",
            f'    - {{name: root, password: "{root_pw_hash}", type: hash}}',
        ]
    lines += ["package_update: true", "packages:", "  - qemu-guest-agent", "  - python3"]

    script = [c for c in recipe_cmds if c.strip() and c.strip() != "set -e"]
    if script:
        lines += ["write_files:", "  - path: /opt/goblindock-recipe.sh",
                  "    permissions: '0755'", "    content: |",
                  "      #!/bin/bash", "      set -e"]
        lines += ["      " + ln for ln in script]
    lines += ["runcmd:", "  - [systemctl, enable, --now, qemu-guest-agent]"]
    if script:
        lines += ["  - [/bin/bash, /opt/goblindock-recipe.sh]"]
    lines += ["  - touch /run/goblindock-ready"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Job implementations                                                          #
# --------------------------------------------------------------------------- #
def _ensure_base_disk(ctx: "JobCtx", px: Proxmox, node: str, cfg: dict) -> str:
    """Make sure the base cloud image is cached on the node's image storage.
    Returns the cached filename. Tolerates a concurrent download of the same
    file (deploy + sync racing is fine); raises on real download/checksum failures."""
    src_url = cfg.get("src_url")
    if not src_url:
        raise RuntimeError("no base image source URL")
    filename = base_disk_filename(src_url)
    if px.storage_has_volume(filename, node=node):
        ctx.log(f"[{_ts()}] {filename} already present on node — skipping download", "l-dim")
        return filename
    try:
        ctx.log(f"[{_ts()}] downloading {filename} — large images can take several minutes", "l-acc")
        upid = px.download_url(filename, src_url, node=node,
                               checksum=cfg.get("checksum", ""),
                               checksum_algorithm=cfg.get("checksum_algorithm", ""))
        _last = {"line": None, "tick": 0}

        def _progress(_st):
            # forward the node's wget progress (e.g. "... 62% 468K 8m27s") into the
            # job log every ~3rd poll (~4.5s); best-effort — never fail the download
            _last["tick"] += 1
            if _last["tick"] % 3:
                return
            try:
                tail = px.api.nodes(node).tasks(upid).log.get() or []
                line = ((tail[-1] or {}).get("t") or "").strip()
            except Exception:  # noqa: BLE001
                return
            if "%" in line and line != _last["line"]:
                _last["line"] = line
                ctx.log(f"[{_ts()}] {line}", "l-dim")
                # surface the % on the job phase too → live download progress in
                # the dashboard job chip, not just the job log
                m = re.search(r"(\d{1,3})%", line)
                if m:
                    ctx.phase_note(f"downloading {m.group(1)}%")

        px.wait_task(upid, node=node, cancelled=ctx.cancelled, timeout=3600, on_poll=_progress)
        ctx.log(f"[{_ts()}] ✓ downloaded {filename}", "l-ok")
    except Exception as e:  # noqa: BLE001
        if px.storage_has_volume(filename, node=node):
            ctx.log(f"[{_ts()}] download reported '{e}' but {filename} is present — continuing", "l-warn")
        else:
            raise RuntimeError(f"image download/verification failed: {e}") from e
    return filename


def _load_job_targets(job: Job) -> tuple[Connection, Deployment]:
    """Load the job's Connection + Deployment and detach them into fresh in-memory
    copies that outlive the session scope. Raises if either row is missing."""
    with session_scope() as s:
        conn = s.get(Connection, job.connection_id)
        dep = s.get(Deployment, job.deployment_id)
        conn = Connection(**conn.model_dump()) if conn else None
        dep = Deployment(**dep.model_dump()) if dep else None
    if not conn or not dep:
        raise RuntimeError("missing connection or deployment")
    return conn, dep


def _run_deploy(ctx: JobCtx, job: Job, phase_base: int = 0, phase_total: int = 5) -> None:
    # phase_base/phase_total let a rebuild present this as a continuation (e.g. phases
    # 2..6 of 6) instead of resetting the progress bar to "Phase 1 of 5".
    def _ph(n: int) -> int:
        return phase_base + n
    cfg = json.loads(job.context_json or "{}")
    conn, dep = _load_job_targets(job)

    px = Proxmox(conn)
    # Build on the deployment's node — set at deploy-creation from the template's connection.
    node = dep.node or conn.node or px.pick_node()

    src_url = cfg.get("src_url")
    if not src_url:
        raise RuntimeError("template has no base image source URL")

    ctx.progress(2, f"Phase {_ph(1)} of {phase_total} · Allocate")
    # "lock" is cosmetic — vmid allocation relies on the single-worker invariant; a multi-worker rollout needs real cross-process locking.
    st = ctx.add_step(f"Acquire lock on {conn.name}")
    t = ctx.start_step(st)
    new_vmid = dep.vmid or px.next_free_vmid(settings.vmid_min, settings.vmid_max, node)
    ctx.log(f"[{_ts()}] goblindock: allocated VMID {new_vmid} on {node}", "l-dim")
    ctx.finish_step(st, t)

    ctx.progress(8, f"Phase {_ph(2)} of {phase_total} · Prepare image")
    st = ctx.add_step("Ensure base cloud image on node storage")
    t = ctx.start_step(st)
    filename = _ensure_base_disk(ctx, px, node, cfg)
    ctx.finish_step(st, t)

    ctx.progress(20, f"Phase {_ph(3)} of {phase_total} · Create")
    st = ctx.add_step(f"Create VM and import base disk → {dep.name}")
    t = ctx.start_step(st)
    import_path = px.iso_volume_path(filename)
    ctx.log(f"[{_ts()}] create vm {new_vmid} import-from {import_path}", "l-acc")
    try:
        upid = px.create_vm_import(new_vmid, dep.name, import_path,
                                   cores=int(cfg.get("cpu", 1)),
                                   ram_mb=int(cfg.get("ram", 2)) * 1024, node=node)
        px.wait_task(upid, node=node, cancelled=ctx.cancelled, timeout=900)
    except Exception:
        # best-effort cleanup of a half-created VM so the vmid doesn't orphan
        try:
            px.destroy(new_vmid, node=node)
        except Exception:  # noqa: BLE001
            pass
        raise
    ctx.log(f"[{_ts()}] ✓ disk imported", "l-ok")
    ctx.finish_step(st, t)
    with session_scope() as s:
        d = s.get(Deployment, dep.id)
        d.vmid = new_vmid
        d.node = node
        s.add(d)

    ctx.progress(45, f"Phase {_ph(4)} of {phase_total} · Configure")
    st = ctx.add_step("Apply cloud-init (name, SSH key, network, size)")
    t = ctx.start_step(st)
    # Clamp to the TARGET connection's per-VM ceilings (0 = inherit the global default),
    # mirroring the API. This honours a connection configured with HIGHER limits than the
    # global default, and still acts as a defense-in-depth cap on a stale/oversized context.
    eff_cores = conn.max_cores or settings.max_cores
    eff_ram_mb = conn.max_ram_mb or settings.max_ram_mb
    eff_disk_gb = conn.max_disk_gb or settings.max_disk_gb
    # Floor at 1 (mirroring the API's max(1, min(...))) so a MAX_CORES=0/MAX_RAM_MB=0
    # misconfiguration can't push cores=0/memory=0 to Proxmox.
    cores = max(1, min(int(cfg.get("cpu", 1)), eff_cores))
    ram_mb = max(1, min(int(cfg.get("ram", 2)) * 1024, eff_ram_mb))
    disk_gb = int(cfg.get("disk", 20))
    if eff_disk_gb:
        disk_gb = min(disk_gb, eff_disk_gb)
    user_pubkey = _ssh_pubkey(job.created_by)
    managed_priv, managed_pub = _managed_keypair()
    pubkeys = [k for k in (user_pubkey, managed_pub) if k]
    root_pw = gen_vm_password() if auto_root_password_enabled() else ""
    cred_user = ""
    import urllib.parse
    params = {
        "name": dep.name,
        "cores": cores,
        "memory": ram_mb,
        "ipconfig0": "ip=dhcp" if cfg.get("network_mode", "dhcp") == "dhcp" else cfg.get("ipconfig0", "ip=dhcp"),
        "agent": "enabled=1",
        "serial0": "socket",   # serial console (xterm); Ubuntu cloud images use ttyS0
        "vga": "std",          # real VGA framebuffer so the GRAPHICAL console = the display
    }
    # Apply the chosen network's bridge + VLAN tag (the imported VM otherwise keeps the
    # create-time net0) and DNS, so the operator-configured network is actually honoured
    # rather than silently dropped. NOTE: exercise on real Proxmox hardware.
    _bridge = cfg.get("bridge")
    if _bridge:
        _net0 = f"virtio,bridge={_bridge}"
        if cfg.get("vlan"):
            _net0 += f",tag={int(cfg['vlan'])}"
        params["net0"] = _net0
    if cfg.get("dns"):
        params["nameserver"] = cfg["dns"]
    # The optional template (applied on top of every deploy). Split into the
    # first-boot (cloud-init) part and the post-boot (ansible) part. Ask-on-deploy
    # answers live on the deployment row so a REBUILD re-applies them too.
    with session_scope() as s:
        tpl = s.get(Template, dep.template_id) if dep.template_id else None
        recipe_json = tpl.recipe_json if tpl else "[]"
    recipe = load_recipe(recipe_json)
    try:
        overrides = json.loads(dep.deploy_inputs_json or "{}")
    except (json.JSONDecodeError, TypeError):
        overrides = {}
    if recipe and overrides:
        recipe = merge_deploy_inputs(recipe, overrides)
    recipe_cmds = compile_cloudinit(recipe, _blocks_by_key(), _secret_lookup_factory(job.created_by)) if recipe else []

    used_snippet = False
    if conn.ssh_key_path:
        try:
            cc = _deploy_cloud_config(dep.name, pubkeys, recipe_cmds,
                                      root_pw_hash=crypt_sha512(root_pw) if root_pw else "")
            volid = write_snippet_over_ssh(conn, f"gd-deploy-{new_vmid}.yml", cc)
            params["cicustom"] = f"user={volid}"
            used_snippet = True
            if root_pw:
                cred_user = "root"
            ctx.log(f"[{_ts()}] cloud-init: guest-agent + first-boot blocks via snippet {volid}", "l-acc")
        except Exception as e:  # noqa: BLE001
            ctx.log(f"[{_ts()}] snippet unavailable ({e}); using native cloud-init", "l-warn")
    if not used_snippet:
        params["ciuser"] = "goblin"
        if root_pw:
            # The hashed-snippet path was unavailable (no SSH key on the connection), and
            # native cloud-init can only take a cipassword in PLAINTEXT in the VM config
            # (readable via `qm config`). Rather than persist a plaintext credential, skip
            # it: there is no audited console password on this fallback path.
            ctx.log(f"[{_ts()}] console password unavailable — connection has no SSH key for the hashed cloud-init snippet", "l-warn")
            root_pw = ""
            cred_user = ""
        if pubkeys:
            params["sshkeys"] = urllib.parse.quote("\n".join(pubkeys), safe="")
    px.set_config(new_vmid, node=node, **params)
    ctx.log(f"[{_ts()}] cloud-init: hostname={dep.name} cores={cores} mem={ram_mb}MB", "l-dim")
    # Persist the VM credential as soon as cloud-init config is applied, so a later failure
    # (especially on a rebuild, whose old VM is already destroyed) can never leave a stale
    # password on the row — the stored credential always matches what was pushed to this VMID.
    with session_scope() as s:
        d = s.get(Deployment, dep.id)
        d.root_password_enc = encrypt(root_pw) if root_pw else ""
        d.cred_user = cred_user
        s.add(d)
    # resize disk (grow only). Track success so the deployment records the VM's REAL
    # disk size rather than the requested grow target when the resize silently failed.
    resize_ok = True
    try:
        px.resize_disk(new_vmid, "scsi0", f"{disk_gb}G", node=node)
        ctx.log(f"[{_ts()}] resize scsi0 → {disk_gb}G", "l-dim")
    except Exception as e:  # noqa: BLE001
        resize_ok = False
        ctx.log(f"[{_ts()}] resize skipped: {e}", "l-warn")
    ctx.finish_step(st, t)

    ctx.progress(65, f"Phase {_ph(5)} of {phase_total} · Boot")
    st = ctx.add_step("Start VM & wait for guest agent")
    t = ctx.start_step(st)
    ctx.log(f"[{_ts()}] boot: starting {dep.name}", "l-dim")
    upid = px.start(new_vmid, node=node)
    px.wait_task(upid, node=node, cancelled=ctx.cancelled, timeout=120)
    ip_static = cfg.get("static_ip")
    ip = _wait_for_ip(ctx, px, new_vmid, node, timeout=260) or ip_static
    ctx.finish_step(st, t)

    # Post-boot: apply the ansible-phase blocks of the runtime recipe (if any).
    if recipe and ip and has_ansible_blocks(recipe, _blocks_by_key()):
        st = ctx.add_step("Apply recipe (ansible, post-boot)")
        t = ctx.start_step(st)
        try:
            _run_ansible_phase(ctx, recipe, job.created_by, ip, managed_priv, dep.name)
            ctx.finish_step(st, t)
        except Exception:  # noqa: BLE001
            ctx.finish_step(st, t, state="failed")
            raise

    with session_scope() as s:
        d = s.get(Deployment, dep.id)
        d.ip = ip or ""
        d.mac = px.mac_of(new_vmid, node) or d.mac
        d.cpu = cores
        d.ram = ram_mb // 1024
        d.disk = _effective_disk_gb(resize_ok, disk_gb, _scsi0_size_gb(px, new_vmid, node))
        d.status = "running"
        d.error = ""
        s.add(d)

    ctx.progress(100, "Complete")
    if ip:
        ctx.log(f"[{_ts()}] ✓ {dep.name} ready at {ip}", "l-ok")
    else:
        ctx.log(f"[{_ts()}] ✓ {dep.name} started (agent IP pending)", "l-ok")


def _wait_for_ip(ctx: JobCtx, px: Proxmox, vmid: int, node: str, timeout: int = 180) -> Optional[str]:
    deadline = time.time() + timeout
    logged_wait = False
    while time.time() < deadline:
        if ctx.cancelled():
            raise JobCancelled()
        ip = px.agent_ipv4(vmid, node)
        if ip:
            ctx.log(f"[{_ts()}] ✓ guest agent reports {ip}", "l-ok")
            return ip
        if not logged_wait:
            ctx.log(f"[{_ts()}] waiting for cloud-init / guest agent…", "l-dim")
            logged_wait = True
        time.sleep(4)
    ctx.log(f"[{_ts()}] ⏳ agent IP not reported within {timeout}s (VM still booting?)", "l-warn")
    return None


def _run_rebuild(ctx: JobCtx, job: Job) -> None:
    conn, dep = _load_job_targets(job)
    # rebuild destroys the existing VM first — honour a cancel that landed after claim
    if ctx.cancelled():
        raise JobCancelled()
    px = Proxmox(conn)
    node = dep.node or conn.node or px.pick_node()

    ctx.progress(1, "Phase 1 of 6 · Destroy")
    st = ctx.add_step(f"Stop & destroy old disk for {dep.name}")
    t = ctx.start_step(st)
    old_vmid = dep.vmid
    if old_vmid:
        try:
            px.stop(old_vmid, node=node)
            time.sleep(3)
        except Exception:  # noqa: BLE001
            pass
        try:
            upid = px.destroy(old_vmid, node=node)
            px.wait_task(upid, node=node, cancelled=ctx.cancelled, timeout=300)
        except JobCancelled:
            # A cancel during the pre-rebuild destroy: do NOT recreate — propagate so
            # _execute reconciles the cancel against the VM's actual state.
            raise
        except Exception as e:  # noqa: BLE001
            # The old VM may simply be gone already (then we continue), but if it
            # SURVIVED we must NOT recreate over it: reusing the VMID would collide on
            # create, and the create-failure cleanup would then destroy this very VM.
            if _vm_exists(px, old_vmid, node):
                raise RuntimeError(
                    f"rebuild aborted: could not destroy old VM {old_vmid}: {e}") from e
            ctx.log(f"[{_ts()}] old VM {old_vmid} already absent; continuing", "l-dim")
    ctx.log(f"[{_ts()}] keeping identity: name={dep.name} ip={dep.ip or 'dhcp'}", "l-dim")
    ctx.finish_step(st, t)

    # Re-run a deploy keeping the same name (and ip if static) by reusing the job's
    # existing context. phase_base=1/total=6 so progress continues (Phase 2..6 of 6)
    # instead of jumping back to "Phase 1 of 5".
    _run_deploy(ctx, job, phase_base=1, phase_total=6)


def _run_destroy(ctx: JobCtx, job: Job) -> None:
    conn, dep = _load_job_targets(job)
    # Honour a cancel requested before the first (irreversible) destroy op — the claim
    # filter catches still-queued cancels; this catches one that landed just after claim.
    if ctx.cancelled():
        raise JobCancelled()
    px = Proxmox(conn)
    node = dep.node or conn.node or px.pick_node()

    ctx.progress(10, "Stopping")
    st = ctx.add_step(f"Stop {dep.name}")
    t = ctx.start_step(st)
    if dep.vmid:
        try:
            px.stop(dep.vmid, node=node)
            time.sleep(3)
        except Exception:  # noqa: BLE001
            pass
    ctx.finish_step(st, t)

    ctx.progress(50, "Destroying")
    st = ctx.add_step(f"Destroy {dep.name} (purge disk)")
    t = ctx.start_step(st)
    if dep.vmid:
        try:
            upid = px.destroy(dep.vmid, node=node)
            px.wait_task(upid, node=node, cancelled=ctx.cancelled, timeout=300)
        except Exception as e:  # noqa: BLE001
            # Idempotent: a VM that's already gone IS a successful destroy. Re-raise a
            # cancel (handled upstream) or a destroy that genuinely left the VM behind.
            if isinstance(e, JobCancelled) or _vm_exists(px, dep.vmid, node):
                raise
            ctx.log(f"[{_ts()}] {dep.name} (vmid {dep.vmid}) already absent — treating as destroyed", "l-warn")
    ctx.log(f"[{_ts()}] ✓ destroyed {dep.name}", "l-ok")
    ctx.finish_step(st, t)

    # release the static IP reservation + remove the node-side cloud-init snippet
    if dep.vmid and conn.ssh_key_path:
        try:
            delete_snippet_over_ssh(conn, f"gd-deploy-{dep.vmid}.yml")
        except Exception:  # noqa: BLE001
            pass
    _drop_deployment(dep.id)
    ctx.progress(100, "Complete")


def _run_image_sync(ctx: JobCtx, job: Job) -> None:
    with session_scope() as s:
        conn = s.get(Connection, job.connection_id)
        conn = Connection(**conn.model_dump()) if conn else None
    if not conn:
        raise RuntimeError("missing connection")
    cfg = json.loads(job.context_json or "{}")
    px = Proxmox(conn)
    node = conn.node or px.pick_node()
    ctx.progress(5, "Phase 1 of 1 · Prepare image")
    st = ctx.add_step("Ensure base cloud image on node storage")
    t = ctx.start_step(st)
    _ensure_base_disk(ctx, px, node, cfg)
    ctx.finish_step(st, t)
    ctx.progress(100, "Complete")


_DISPATCH = {
    "deploy": _run_deploy,
    "rebuild": _run_rebuild,
    "destroy": _run_destroy,
    "image_sync": _run_image_sync,
}


# --------------------------------------------------------------------------- #
# Worker loop                                                                  #
# --------------------------------------------------------------------------- #
def _claim_next_job() -> Optional[int]:
    canceled_id = None
    with session_scope() as s:
        job = s.exec(
            select(Job).where(Job.status == "queued").order_by(Job.id)
        ).first()
        if not job:
            return None
        if job.cancel_requested:
            # Cancelled while still queued — honour it BEFORE it runs, so a cancelled
            # destroy/deploy never executes its (irreversible) work.
            job.status = "canceled"
            job.finished_at = utcnow()
            s.add(job)
            canceled_id = job.id
        else:
            job.status = "running"
            job.started_at = utcnow()
            s.add(job)
            statebus.bump()
            return job.id
    # A queued job was cancelled: reconcile its deployment so it isn't stranded in
    # "working" with a leaked IP reservation (see _reconcile_canceled_job).
    _reconcile_canceled_job(canceled_id)
    statebus.bump()
    return None


def _execute(job_id: int) -> None:
    ctx = JobCtx(job_id)
    with session_scope() as s:
        job = s.get(Job, job_id)
        job_copy = Job(**job.model_dump()) if job else None
    if not job_copy:
        return
    impl = _DISPATCH.get(job_copy.type)
    try:
        if not impl:
            raise RuntimeError(f"unknown job type {job_copy.type}")
        impl(ctx, job_copy)
        with session_scope() as s:
            job = s.get(Job, job_id)
            job.status = "succeeded"
            job.pct = 100
            job.finished_at = utcnow()
            s.add(job)
        statebus.bump()
    except Exception as e:  # noqa: BLE001
        # Cancellation is identified by TYPE, never by error text — a genuine failure
        # whose message contains the word "cancel" (e.g. a VM named 'cancel-svc') must
        # be treated as a failure, not a user cancel that would destroy the VM.
        cancelled = isinstance(e, JobCancelled)
        ctx.log(f"[{_ts()}] ✗ {'canceled' if cancelled else e}", "l-err")
        with session_scope() as s:
            job = s.get(Job, job_id)
            job.status = "canceled" if cancelled else "failed"
            job.error = "canceled" if cancelled else str(e)
            job.finished_at = utcnow()
            s.add(job)
            if job.image_id and job.type == "image_build":
                # legacy golden-build rows only — a failed image_sync must NOT
                # touch the base image's build_status (it stays 'ready')
                img = s.get(Image, job.image_id)
                if img:
                    img.build_status = "failed" if not cancelled else "none"
                    s.add(img)
            has_dep = job.deployment_id is not None
        # Reconcile the deployment in its OWN session so a long Proxmox cleanup call
        # (deploy-cancel destroys the half-built VM) doesn't hold the job-status txn open.
        if has_dep:
            if cancelled:
                _reconcile_canceled_job(job_id)
            else:
                _reconcile_failed_job(job_id)
        statebus.bump()
        traceback.print_exc()


def _vm_exists(px: Proxmox, vmid: int, node: Optional[str]) -> bool:
    """Is `vmid` still present on the node? On a listing failure assume it EXISTS
    (fail safe) — a rebuild must never recreate over a possibly-live VM."""
    try:
        return vmid in {int(v["vmid"]) for v in px.list_qemu(node)}
    except Exception:  # noqa: BLE001
        return True


def _px_for_conn(conn_id: Optional[int]) -> Optional[Proxmox]:
    """Build a Proxmox client for `conn_id`, or None if the connection is gone or the
    client can't be constructed. Used by the cancel/cleanup paths."""
    if not conn_id:
        return None
    try:
        with session_scope() as s:
            conn = s.get(Connection, conn_id)
            conn = Connection(**conn.model_dump()) if conn else None
        return Proxmox(conn) if conn else None
    except Exception:  # noqa: BLE001
        return None


def _drop_deployment(dep_id: int) -> None:
    """Delete a deployment row and free its static-IP reservation(s)."""
    with session_scope() as s:
        for a in s.exec(select(IpAllocation).where(IpAllocation.deployment_id == dep_id)).all():
            s.delete(a)
        d = s.get(Deployment, dep_id)
        if d:
            s.delete(d)


def _set_dep_status(dep_id: int, status: str, error: str = "") -> None:
    with session_scope() as s:
        d = s.get(Deployment, dep_id)
        if d:
            d.status = status
            d.error = error[:300] if error else ""
            s.add(d)


def _best_effort_destroy(conn_id: Optional[int], vmid: Optional[int], node: Optional[str]) -> None:
    """Destroy `vmid` on its connection, swallowing every error. Used to tear down a
    half-built VM left by a cancelled deploy so it doesn't orphan on Proxmox."""
    if not conn_id or not vmid:
        return
    px = _px_for_conn(conn_id)
    if px is None:
        return
    try:
        upid = px.destroy(vmid, node=node or px.node)
        px.wait_task(upid, node=node or px.node, timeout=120)
    except Exception:  # noqa: BLE001
        pass


def _best_effort_delete_snippet(conn_id: Optional[int], vmid: Optional[int]) -> None:
    """Remove the node-side cloud-init snippet (gd-deploy-<vmid>.yml — carries the
    root-password hash + injected pubkeys) left behind by a deploy. Best-effort:
    needs an SSH key on the connection and swallows every error."""
    if not conn_id or not vmid:
        return
    with session_scope() as s:
        conn = s.get(Connection, conn_id)
        conn = Connection(**conn.model_dump()) if conn else None
    if not conn or not conn.ssh_key_path:
        return
    try:
        delete_snippet_over_ssh(conn, f"gd-deploy-{vmid}.yml")
    except Exception:  # noqa: BLE001
        pass


def _scsi0_size_gb(px: Proxmox, vmid: int, node: Optional[str]) -> Optional[int]:
    """The VM's actual scsi0 size in GiB from its live config, or None if it can't be
    determined (config read fails / no scsi0 / unparsable size)."""
    try:
        cfg = px.vm_config(vmid, node=node)
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"size=(\d+(?:\.\d+)?)\s*([KMGT])", str((cfg or {}).get("scsi0", "")))
    if not m:
        return None
    factor = {"K": 1 / 1048576, "M": 1 / 1024, "G": 1, "T": 1024}[m.group(2)]
    return int(float(m.group(1)) * factor) or 1


def _effective_disk_gb(resize_ok: bool, requested: int, actual: Optional[int]) -> int:
    """Disk size to record on the deployment: the requested grow target if the resize
    succeeded, otherwise the VM's actual current size (falling back to the requested
    value only when the actual size is unknown)."""
    if resize_ok:
        return requested
    return actual if actual else requested


def _reconcile_canceled_job(job_id: Optional[int]) -> None:
    """Leave a cancelled job's deployment consistent — reconciled against the VM's
    ACTUAL Proxmox state, because a cancel can land after the (irreversible) destroy
    task was already submitted, so we must never just assume the VM survived.

    deploy  → nothing should survive: best-effort destroy any half-built VM, free the
              IP reservation, and delete the deployment row ("undo").
    rebuild
    destroy → probe the VM:
              • still present  → keep it + its IP; take the deployment out of "working"
                so /state live-probes its real run state again (serialize.vm_dict skips
                the probe while status == "working").
              • gone, destroy  → the destroy effectively completed: free the IP and
                delete the deployment row.
              • gone, rebuild  → the old VM was removed before the recreate was
                cancelled: mark the deployment "error" (rebuild again to recreate) and
                KEEP its IP reservation so the retry reuses the same address.
              If the VM's state can't be determined, fail safe = assume present (keep).
    """
    if not job_id:
        return
    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job or not job.deployment_id:
            return
        dep = s.get(Deployment, job.deployment_id)
        if not dep:
            return
        job_type = job.type
        vmid, node, conn_id, dep_id = dep.vmid, dep.node, dep.connection_id, dep.id

    if job_type == "deploy":
        _best_effort_destroy(conn_id, vmid, node)
        _best_effort_delete_snippet(conn_id, vmid)
        _drop_deployment(dep_id)
        return

    # rebuild / destroy: reconcile against ground truth
    if vmid:
        px = _px_for_conn(conn_id)
        vm_present = _vm_exists(px, vmid, node) if px is not None else True
    else:
        vm_present = False   # nothing was ever provisioned

    if vm_present:
        _set_dep_status(dep_id, "stopped")           # un-stick; live-probe corrects it
    elif job_type == "destroy":
        _drop_deployment(dep_id)                     # destroy effectively completed
    else:  # rebuild whose old VM is gone, recreate cancelled
        _set_dep_status(dep_id, "error",
                        "rebuild canceled after the old VM was removed — rebuild again to recreate")


def _reconcile_failed_job(job_id: int) -> None:
    """Mark a failed job's deployment as errored. Only a failed DEPLOY produced no
    surviving VM, so only it releases the IP reservation; a failed rebuild/destroy
    leaves the existing VM (and therefore its reserved IP) in place."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job or not job.deployment_id:
            return
        dep = s.get(Deployment, job.deployment_id)
        if not dep:
            return
        dep.status = "error"
        dep.error = (job.error or "")[:300]
        s.add(dep)
        if job.type == "deploy":
            for a in s.exec(select(IpAllocation).where(IpAllocation.deployment_id == dep.id)).all():
                s.delete(a)


def _reconcile_ips() -> None:
    """Fill in IPs that the guest agent reports after a deploy's wait timed out
    (e.g. a slow first-boot agent install). Best-effort, runs when idle."""
    with session_scope() as s:
        deps = s.exec(select(Deployment).where(Deployment.status == "running")).all()
        targets = [(d.id, d.connection_id, d.vmid, d.node) for d in deps if not d.ip and d.vmid]
        conns = {c.id: Connection(**c.model_dump()) for c in s.exec(select(Connection)).all()}
    for dep_id, conn_id, vmid, node in targets:
        conn = conns.get(conn_id)
        if not conn:
            continue
        try:
            ip = Proxmox(conn).agent_ipv4(vmid, node or conn.node)
        except Exception:  # noqa: BLE001
            ip = None
        if ip:
            with session_scope() as s:
                d = s.get(Deployment, dep_id)
                if d and not d.ip:
                    d.ip = ip
                    s.add(d)


def _recover_orphans() -> None:
    """Crash recovery: a job left 'running' by a previous process is dead. Fail it AND
    reconcile the resource it was mutating — otherwise the deployment stays "working"
    forever (serialize skips live-polling 'working') and the image stays "building"."""
    with session_scope() as s:
        for job in s.exec(select(Job).where(Job.status == "running")).all():
            job.status = "failed"
            job.error = "interrupted (worker restart)"
            job.finished_at = utcnow()
            s.add(job)
            if job.deployment_id:
                dep = s.get(Deployment, job.deployment_id)
                if dep and dep.status == "working":
                    dep.status = "error"
                    dep.error = "interrupted by restart"
                    s.add(dep)
                    # free the interrupted deploy's static-IP reservation
                    for a in s.exec(select(IpAllocation).where(IpAllocation.deployment_id == dep.id)).all():
                        s.delete(a)
            if job.image_id:
                img = s.get(Image, job.image_id)
                if img and img.build_status == "building":
                    # keep template_vmid so an admin can identify and manually clean up the ghost on the node
                    img.build_status = "failed"
                    s.add(img)


def _loop() -> None:
    _recover_orphans()
    idle = 0
    while not _stop.is_set():
        try:
            job_id = _claim_next_job()
            if job_id is None:
                idle += 1
                if idle % 15 == 0:  # ~ every 15s while idle
                    _reconcile_ips()
                time.sleep(1.0)
                continue
            idle = 0
            _execute(job_id)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            time.sleep(2.0)


def start_worker() -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _stop.clear()
    _worker_thread = threading.Thread(target=_loop, name="gd-worker", daemon=True)
    _worker_thread.start()


def stop_worker(join_timeout: float = 30) -> None:
    _stop.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=join_timeout)
