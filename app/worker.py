"""Background job runner.

A single daemon thread claims queued jobs from SQLite and executes them, writing
JobStep / JobEvent rows as it goes so the SSE endpoint can stream live progress.
This is the "worker" of the design's web+worker split, collapsed into one process
(a daemon thread) — appropriate for a single-container homelab tool.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlmodel import select

from .config import settings
from .db import session_scope
from .execution_plan import (
    build_execution_plan,
    materialize_execution_plan,
    open_deploy_inputs,
    open_execution_plan,
    seal_execution_plan,
)
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
    User,
    Variable,
    ensure_utc,
    utcnow,
)
from .proxmox import (
    VM_ABSENT,
    VM_PRESENT,
    VM_UNKNOWN,
    JobCancelled,
    Proxmox,
    base_disk_filename,
    delete_snippet_over_ssh,
    probe_vm_presence as _probe_vm_presence,
    write_snippet_over_ssh,
)
from .recipes import (
    collect_sensitive_inputs,
    compile_ansible,
    compile_cloudinit,
    has_ansible_blocks,
)
from .security import crypt_sha512, decrypt, encrypt, gen_vm_password
from .appsettings import auto_root_password_enabled, get_setting, set_setting
from . import statebus

_worker_thread: Optional[threading.Thread] = None
_waiting_thread: Optional[threading.Thread] = None
_stop = threading.Event()
WAITING_TIMEOUT = timedelta(minutes=30)


class JobDeferred(Exception):
    """Control-flow signal: the job is durably waiting and is not terminal."""


def _defer_for_guest_ip(ctx: "JobCtx") -> None:
    """Persist a resumable post-boot wait before unwinding the active execution."""
    with session_scope() as s:
        job = s.get(Job, ctx.job_id)
        if not job:
            raise RuntimeError("missing job while deferring for guest IP")
        job.status = "waiting"
        job.waiting_since = job.waiting_since or utcnow()
        job.phase = "Waiting for guest IP"
        job.finished_at = None
        s.add(job)
    ctx.log(f"[{_ts()}] waiting for guest IP before applying Ansible…", "l-dim")
    raise JobDeferred()


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

    def remote(self, upid: str, node: str) -> None:
        """Commit the accepted remote task before waiting for its result."""
        with session_scope() as s:
            job = s.get(Job, self.job_id)
            if job:
                job.remote_task, job.remote_node = upid or "", node
                s.add(job)

    def creation(self, state: str) -> None:
        with session_scope() as s:
            job = s.get(Job, self.job_id)
            if job:
                job.create_state = state
                s.add(job)
                dep = s.get(Deployment, job.deployment_id) if job.deployment_id else None
                if dep:
                    dep.identity_state = state
                    s.add(dep)

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


def _clamp_resource(requested: int, limit: int) -> int:
    """Clamp a positive request only when the connection has a real limit."""
    requested = max(1, int(requested))
    return min(requested, int(limit)) if int(limit) > 0 else requested


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _blocks_by_key() -> dict[str, Block]:
    with session_scope() as s:
        return {b.key: Block(**b.model_dump()) for b in s.exec(select(Block)).all()}


def _secret_lookup_factory(owner_id: Optional[int], sink: Optional[set] = None,
                           *, allow_global: bool = False):
    """Resolve {{ secrets.NAME }} / {{ variable.NAME }}. If `sink` is given, every
    resolved SECRET plaintext is collected into it so the caller can redact those
    values out of streamed job logs (variables are plaintext-by-design and shown in
    the UI, so they are NOT collected)."""
    def lookup(ns: str, name: str) -> str:
        with session_scope() as s:
            if ns == "variable":
                # per-user variable overrides global; value is plaintext. order_by(id)
                # keeps resolution deterministic if a legacy duplicate name exists.
                var = None
                if owner_id is not None:
                    var = s.exec(
                        select(Variable).where(
                            Variable.name == name, Variable.scope == "user",
                            Variable.owner_id == owner_id,
                        ).order_by(Variable.id)
                    ).first()
                if not var and allow_global:
                    var = s.exec(
                        select(Variable).where(Variable.name == name, Variable.scope == "global")
                        .order_by(Variable.id)
                    ).first()
                return var.value if var else ""
            # per-user secret overrides global
            sec = None
            if owner_id is not None:
                sec = s.exec(
                    select(Secret).where(
                        Secret.name == name, Secret.scope == "user",
                        Secret.owner_id == owner_id,
                    ).order_by(Secret.id)
                ).first()
            if not sec and allow_global:
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


def _owner_secret_context(owner_id: Optional[int]) -> tuple[Optional[int], bool]:
    """Return the identity and global-value permission used to compile a deployment.

    The deployment owner is the security principal.  The actor who queued a rebuild may
    be an admin acting on somebody else's VM and must not substitute their own values.
    """
    with session_scope() as s:
        owner = s.get(User, owner_id) if owner_id else None
        return owner_id, bool(owner and owner.role == "admin")


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


def _ssh_pubkey(owner_id: Optional[int], *, allow_global: bool = False) -> str:
    lookup = _secret_lookup_factory(owner_id, allow_global=allow_global)
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


def _run_ansible_phase(ctx: "JobCtx", recipe: list, blocks: dict[str, Block], owner_id, ip: str, managed_priv: str,
                       label: str, *, allow_global: bool = False) -> None:
    """Run the post-boot ansible-phase blocks of a recipe against a live VM."""
    if not (recipe and ip):
        return
    if not has_ansible_blocks(recipe, blocks):
        return
    # Collect resolved secret values while compiling so we can redact them from the
    # streamed Ansible stdout — a task that echoes a secret (debug/failed command)
    # must NOT land its plaintext in the job log (which the job's owner can read).
    vault: set = set()
    lookup = _secret_lookup_factory(owner_id, sink=vault, allow_global=allow_global)
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
        ctx.log(f"[{_ts()}] ✕ ansible run failed to start: {e}", "l-err")
        raise RuntimeError(f"ansible run failed to start: {e}") from e
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
                  "    permissions: '0700'", "    content: |",
                  "      #!/bin/bash", "      set -eo pipefail",
                  "      trap 'rm -f -- \"$0\"' EXIT"]
        lines += ["      " + ln for ln in script]
    lines += ["runcmd:", "  - [systemctl, enable, --now, qemu-guest-agent]"]
    if script:
        # The fixed outer shell removes the secret-bearing child even when the
        # child has a parse error, exits non-zero, replaces itself, or changes traps.
        lines += [
            "  - [/bin/bash, -c, \"trap 'rm -f -- /opt/goblindock-recipe.sh' "
            "EXIT; /bin/bash /opt/goblindock-recipe.sh; rc=$?; "
            "echo $rc > /var/lib/goblindock-recipe-result; exit $rc\"]"
        ]
    else:
        lines += ["  - [/bin/bash, -c, 'echo 0 > /var/lib/goblindock-recipe-result']"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Job implementations                                                          #
# --------------------------------------------------------------------------- #
def _base_disk_validation_marker(
    px: Proxmox, node: str, filename: str, checksum: str, checksum_algorithm: str,
) -> tuple[str, str]:
    """Stable DB marker for one connection/node/storage cache target."""
    conn = getattr(px, "conn", None)
    identity = "\0".join((
        str(getattr(conn, "id", "") or ""),
        str(getattr(conn, "host", "") or ""),
        str(getattr(conn, "port", "") or ""),
        str(node or ""),
        str(getattr(px, "iso_storage", "") or getattr(conn, "iso_storage", "") or "local"),
        filename,
    ))
    key = "image_cache_validation:" + hashlib.sha256(identity.encode()).hexdigest()
    digest = (checksum or "").strip().lower()
    algorithm = (checksum_algorithm or "").strip().lower()
    value = f"verified:{algorithm}:{digest}" if digest else "downloaded"
    return key, value


def _discard_base_disk(px: Proxmox, node: str, filename: str, *, required: bool) -> None:
    """Remove a target that may be partial; optionally fail unless absence is known."""
    try:
        upid = px.delete_storage_volume(filename, node=node)
        if isinstance(upid, str) and upid:
            px.wait_task(upid, node=node, timeout=120)
        if px.storage_has_volume(filename, node=node):
            raise RuntimeError(f"untrusted cache target {filename} is still present")
    except Exception as exc:  # noqa: BLE001
        if required:
            raise RuntimeError(f"could not remove untrusted cache target {filename}: {exc}") from exc


def _ensure_base_disk(ctx: "JobCtx", px: Proxmox, node: str, cfg: dict) -> str:
    """Make sure the base cloud image is cached on the node's image storage.
    Returns the cached filename. Checksum-bearing entries are reusable only after
    this installation durably recorded successful Proxmox validation."""
    src_url = cfg.get("src_url")
    if not src_url:
        raise RuntimeError("no base image source URL")
    checksum = cfg.get("checksum", "")
    checksum_algorithm = cfg.get("checksum_algorithm", "")
    from .image_cache import active_filename, record_download
    from uuid import uuid4
    conn = getattr(px, "conn", None)
    filename = active_filename(conn, node, src_url, checksum, checksum_algorithm)
    if cfg.get("force_refresh"):
        # Keep every previously usable volume intact. Import may still be reading
        # it, and older VMs or manual clones can reference it outside GoblinDock.
        stem = base_disk_filename(src_url, checksum, checksum_algorithm).removesuffix(".qcow2")
        filename = f"{stem}-refresh-{uuid4().hex[:12]}.qcow2"
    marker_key, verified_value = _base_disk_validation_marker(
        px, node, filename, checksum, checksum_algorithm,
    )
    marker = get_setting(marker_key)
    if px.storage_has_volume(filename, node=node):
        trusted = marker == verified_value
        if (checksum and not trusted) or marker == "untrusted":
            ctx.log(f"[{_ts()}] retaining unverified cache target {filename}; downloading a replacement", "l-warn")
            stem = base_disk_filename(src_url, checksum, checksum_algorithm).removesuffix(".qcow2")
            filename = f"{stem}-refresh-{uuid4().hex[:12]}.qcow2"
            marker_key, verified_value = _base_disk_validation_marker(
                px, node, filename, checksum, checksum_algorithm,
            )
        else:
            ctx.log(f"[{_ts()}] {filename} already present on node — skipping download", "l-dim")
            return filename
    # Commit distrust before submitting work: cancellation, process death, task
    # failure, or checksum mismatch can then never make a leftover reusable.
    set_setting(marker_key, "untrusted")
    try:
        ctx.log(f"[{_ts()}] downloading {filename} — large images can take several minutes", "l-acc")
        upid = px.download_url(filename, src_url, node=node,
                               checksum=checksum,
                               checksum_algorithm=checksum_algorithm)
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
        set_setting(marker_key, verified_value)
        record_download(conn, node, src_url, checksum, checksum_algorithm, filename)
        ctx.log(f"[{_ts()}] ✓ downloaded {filename}", "l-ok")
    except JobCancelled:
        _discard_base_disk(px, node, filename, required=False)
        raise
    except Exception as e:  # noqa: BLE001
        # A target appearing after a failed task may be partial or checksum-invalid.
        # Only a successful Proxmox task is evidence that the cache entry is usable.
        _discard_base_disk(px, node, filename, required=False)
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
    if conn.disabled:
        raise RuntimeError("connection is disabled")
    return conn, dep


def _load_materialized_job_plan(job: Job, dep: Deployment) -> tuple[dict, list[dict], dict[str, Block]]:
    """Load the immutable job snapshot, creating one once for legacy queued jobs."""
    ciphertext = job.execution_plan_enc
    if not ciphertext and dep.template_id:
        with session_scope() as s:
            stored_job = s.get(Job, job.id)
            stored_dep = s.get(Deployment, dep.id)
            if stored_job and stored_job.execution_plan_enc:
                ciphertext = stored_job.execution_plan_enc
            elif stored_job and stored_dep:
                template = s.get(Template, stored_dep.template_id)
                if not template:
                    raise RuntimeError("missing template for legacy execution plan")
                try:
                    deploy_inputs_json = open_deploy_inputs(stored_dep.deploy_inputs_enc)
                except ValueError as exc:
                    raise RuntimeError("stored deployment answers cannot be "
                                       "decrypted — key mismatch") from exc
                ciphertext = seal_execution_plan(build_execution_plan(
                    s, template, stored_dep.owner_id, deploy_inputs_json,
                ))
                stored_job.execution_plan_enc = ciphertext
                s.add(stored_job)
    if not ciphertext:
        return {"owner_id": dep.owner_id}, [], {}
    try:
        plan = open_execution_plan(ciphertext)
    except ValueError as exc:
        raise RuntimeError("invalid execution plan") from exc
    if plan["owner_id"] != dep.owner_id:
        raise RuntimeError("execution plan owner mismatch")
    recipe, blocks = materialize_execution_plan(plan)
    return plan, recipe, blocks


@dataclass(frozen=True)
class DeployPreflight:
    """Cloud-init material prepared and, when required, delivered before create."""
    config: dict
    managed_private_key: str
    root_password: str
    credential_user: str
    snippet_volume: str = ""


def _preflight_deploy_cloud_init(
    ctx: JobCtx, px: Proxmox, conn: Connection, dep: Deployment, node: str, vmid: int,
    cfg: dict, recipe_cmds: list[str], user_pubkey: str, recipe_requires_snippet: bool,
    *, snippet_name: Optional[str] = None,
) -> DeployPreflight:
    """Prepare native cloud-init or prove required snippet delivery before create."""
    snippet_name = snippet_name or f"gd-deploy-{vmid}.yml"
    managed_private_key, managed_pubkey = _managed_keypair()
    pubkeys = [key for key in (user_pubkey, managed_pubkey) if key]
    effective_recipe = recipe_requires_snippet or any(
        cmd.strip() and cmd.strip() != "set -e" for cmd in recipe_cmds
    )
    # Keep the established no-key/native path credential-free: native cloud-init
    # would expose a plaintext cipassword. When a snippet channel is selected, a
    # generated root credential is safe because only its hash is delivered.
    root_password = gen_vm_password() if auto_root_password_enabled() and (
        effective_recipe or conn.ssh_key_path
    ) else ""
    credential_user = ""
    import urllib.parse
    params = {
        "name": dep.name,
        "cores": _clamp_resource(int(cfg.get("cpu", 1)), conn.max_cores),
        "memory": _clamp_resource(int(cfg.get("ram", 2)) * 1024, conn.max_ram_mb),
        "ipconfig0": "ip=dhcp" if cfg.get("network_mode", "dhcp") == "dhcp" else cfg.get("ipconfig0", "ip=dhcp"),
        "agent": "enabled=1",
        "serial0": "socket",
        "vga": "std",
    }
    bridge = cfg.get("bridge")
    if bridge:
        net0 = f"virtio,bridge={bridge}"
        if cfg.get("vlan"):
            net0 += f",tag={int(cfg['vlan'])}"
        params["net0"] = net0
    if cfg.get("dns"):
        params["nameserver"] = cfg["dns"]

    if not (effective_recipe or root_password):
        params["ciuser"] = "goblin"
        if pubkeys:
            params["sshkeys"] = urllib.parse.quote("\n".join(pubkeys), safe="")
        return DeployPreflight(params, managed_private_key, root_password, credential_user)

    volid = ""
    try:
        cloud_config = _deploy_cloud_config(
            dep.name, pubkeys, recipe_cmds,
            root_pw_hash=crypt_sha512(root_password) if root_password else "",
        )
        volid = write_snippet_over_ssh(conn, snippet_name, cloud_config)
        px.validate_snippet_volume(volid, node=node)
    except Exception:
        if volid:
            try:
                delete_snippet_over_ssh(conn, snippet_name)
            except Exception:  # noqa: BLE001
                pass
        raise
    params["cicustom"] = f"user={volid}"
    credential_user = "root" if root_password else ""
    ctx.log(f"[{_ts()}] cloud-init: guest-agent + first-boot blocks via snippet {volid}", "l-acc")
    return DeployPreflight(params, managed_private_key, root_password, credential_user, volid)


def _clear_selected_vmid(dep_id: Optional[int], vmid: int) -> None:
    """Undo a local VMID choice that was never accepted by Proxmox."""
    if not dep_id:
        return
    with session_scope() as s:
        dep = s.get(Deployment, dep_id)
        if dep and dep.vmid == vmid:
            dep.vmid = None
            s.add(dep)


def _run_deploy(ctx: JobCtx, job: Job, phase_base: int = 0, phase_total: int = 5) -> None:
    # phase_base/phase_total let a rebuild present this as a continuation (e.g. phases
    # 2..6 of 6) instead of resetting the progress bar to "Phase 1 of 5".
    def _ph(n: int) -> int:
        return phase_base + n
    cfg = json.loads(job.context_json or "{}")
    conn, dep = _load_job_targets(job)
    plan, recipe, blocks = _load_materialized_job_plan(job, dep)
    secret_owner_id, allow_global_secrets = _owner_secret_context(plan["owner_id"])
    # Clamp before the create request so a stale queued context cannot reserve more
    # than the connection's current limits even temporarily.
    cores = _clamp_resource(int(cfg.get("cpu", 1)), conn.max_cores)
    ram_mb = _clamp_resource(int(cfg.get("ram", 2)) * 1024, conn.max_ram_mb)
    disk_gb = _clamp_resource(int(cfg.get("disk", 20)), conn.max_disk_gb)

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

    # Resolve all recipe inputs before claiming any remote identity.
    user_pubkey = _ssh_pubkey(secret_owner_id, allow_global=allow_global_secrets)
    recipe_cmds = compile_cloudinit(
        recipe, blocks,
        _secret_lookup_factory(secret_owner_id, allow_global=allow_global_secrets),
    ) if recipe else []
    recipe_requires_snippet = bool(recipe and has_ansible_blocks(recipe, blocks))

    # Persist the candidate before delivery so an accepted create request always has
    # an identity for later reconciliation. Pre-submission failures clear it below.
    with session_scope() as s:
        d = s.get(Deployment, dep.id)
        d.vmid = new_vmid
        d.node = node
        s.add(d)

    preflight: Optional[DeployPreflight] = None
    create_submitted = False
    try:
        preflight = _preflight_deploy_cloud_init(
            ctx, px, conn, dep, node, new_vmid, cfg, recipe_cmds, user_pubkey,
            recipe_requires_snippet,
        )

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
        ctx.creation("submitting")
        create_submitted = True
        try:
            upid = px.create_vm_import(new_vmid, dep.name, import_path,
                                       cores=cores, ram_mb=ram_mb, node=node)
        except Exception as exc:
            from proxmoxer.core import ResourceException
            if isinstance(exc, ResourceException) and 400 <= int(exc.status_code) < 500:
                create_submitted = False
                ctx.creation("rejected")
            raise
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            raise RuntimeError("VM creation returned no valid task identity; reconcile in Recovery")
        ctx.creation("accepted")
        ctx.remote(upid, node)
        px.wait_task(upid, node=node, cancelled=ctx.cancelled, timeout=900)
    except Exception:
        # A failure before a UPID can be a collision with a VM we do not own. Undo
        # only our uploaded snippet and local candidate; an accepted request retains
        # its identity for reconciliation rather than risking a destructive guess.
        if not create_submitted:
            if preflight and preflight.snippet_volume:
                try:
                    delete_snippet_over_ssh(conn, f"gd-deploy-{new_vmid}.yml")
                except Exception:  # noqa: BLE001
                    pass
            _clear_selected_vmid(dep.id, new_vmid)
        raise
    ctx.log(f"[{_ts()}] ✓ disk imported", "l-ok")
    ctx.finish_step(st, t)
    ctx.progress(45, f"Phase {_ph(4)} of {phase_total} · Configure")
    st = ctx.add_step("Apply cloud-init (name, SSH key, network, size)")
    t = ctx.start_step(st)
    px.set_config(new_vmid, node=node, cancelled=ctx.cancelled,
                  on_task=lambda upid: ctx.remote(upid, node), **preflight.config)
    ctx.log(f"[{_ts()}] cloud-init: hostname={dep.name} cores={cores} mem={ram_mb}MB", "l-dim")
    # Persist the VM credential as soon as cloud-init config is applied, so a later failure
    # (especially on a rebuild, whose old VM is already destroyed) can never leave a stale
    # password on the row — the stored credential always matches what was pushed to this VMID.
    with session_scope() as s:
        d = s.get(Deployment, dep.id)
        d.root_password_enc = encrypt(preflight.root_password) if preflight.root_password else ""
        d.cred_user = preflight.credential_user
        s.add(d)
    # Never boot while a disk resize has an unknown or failed outcome.
    resize_ok = True
    current_disk = _scsi0_size_gb(px, new_vmid, node)
    if current_disk and current_disk >= disk_gb:
        disk_gb = current_disk
    else:
        px.resize_disk(new_vmid, "scsi0", f"{disk_gb}G", node=node,
                       cancelled=ctx.cancelled, on_task=lambda upid: ctx.remote(upid, node))
        ctx.log(f"[{_ts()}] resize scsi0 → {disk_gb}G", "l-dim")
    ctx.finish_step(st, t)

    ctx.progress(65, f"Phase {_ph(5)} of {phase_total} · Boot")
    st = ctx.add_step("Start VM & wait for guest agent")
    t = ctx.start_step(st)
    ctx.log(f"[{_ts()}] boot: starting {dep.name}", "l-dim")
    upid = px.start(new_vmid, node=node)
    ctx.remote(upid, node)
    px.wait_task(upid, node=node, cancelled=ctx.cancelled, timeout=120)
    ctx.progress(75, "Verify cloud-init and first-boot recipe")
    px.wait_guest_ready(new_vmid, node=node, cancelled=ctx.cancelled,
                        require_marker=bool(preflight.snippet_volume))
    ip_static = cfg.get("static_ip")
    ip = _wait_for_ip(ctx, px, new_vmid, node, timeout=260) or ip_static
    ctx.finish_step(st, t)

    # Persist the accepted VM's identity and resource facts before post-boot Ansible.
    # If the guest agent is slow, the durable waiting job must still retain everything
    # needed to identify and reconcile the VM after a process restart.
    with session_scope() as s:
        d = s.get(Deployment, dep.id)
        d.ip = ip or ""
        d.mac = px.mac_of(new_vmid, node) or d.mac
        d.cpu = cores
        d.ram = ram_mb // 1024
        d.disk = _effective_disk_gb(resize_ok, disk_gb, _scsi0_size_gb(px, new_vmid, node))
        if cfg.get("base_image_id") and s.get(Image, cfg["base_image_id"]):
            d.image_id = cfg["base_image_id"]
        s.add(d)

    # Post-boot: apply the ansible-phase blocks of the immutable runtime recipe.
    requires_ansible = bool(recipe and has_ansible_blocks(recipe, blocks))
    if requires_ansible and not ip:
        ctx.add_step("Apply recipe (ansible, post-boot)")
        _defer_for_guest_ip(ctx)
    if requires_ansible:
        st = ctx.add_step("Apply recipe (ansible, post-boot)")
        t = ctx.start_step(st)
        try:
            _run_ansible_phase(
                ctx, recipe, blocks, secret_owner_id, ip, preflight.managed_private_key, dep.name,
                allow_global=allow_global_secrets,
            )
            ctx.finish_step(st, t)
        except Exception:  # noqa: BLE001
            ctx.finish_step(st, t, state="failed")
            raise

    with session_scope() as s:
        d = s.get(Deployment, dep.id)
        d.status = "running"
        d.error = ""
        d.cleanup_origin = None
        d.cleanup_last_attempt_at = None
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


def _stop_vm_for_lifecycle(ctx: JobCtx, px: Proxmox, vmid: int, node: str) -> bool:
    """Stop a confirmed-present VM when needed and wait for the stop task.

    False means the inventory probe confirmed that the VM is already absent.
    """
    try:
        status = (px.vm_current(vmid, node) or {}).get("status")
    except Exception as e:  # noqa: BLE001
        presence, detail = _probe_vm_presence(px, vmid, node)
        if presence == VM_ABSENT:
            return False
        if presence == VM_UNKNOWN:
            raise RuntimeError(f"could not inspect VM {vmid} before stop: {detail}") from e
        status = "unknown"
    if status == "stopped":
        return True
    upid = px.stop(vmid, node=node)
    ctx.remote(upid, node)
    px.wait_task(upid, node=node, cancelled=ctx.cancelled, timeout=300)
    return True


def _run_rebuild(ctx: JobCtx, job: Job) -> None:
    conn, dep = _load_job_targets(job)
    # rebuild destroys the existing VM first — honour a cancel that landed after claim
    if ctx.cancelled():
        raise JobCancelled()
    px = Proxmox(conn)
    node = dep.node or conn.node or px.pick_node()

    # Complete fallible image/recipe/snippet preparation while the old VM still exists.
    cfg = json.loads(job.context_json or "{}")
    plan, recipe, blocks = _load_materialized_job_plan(job, dep)
    owner_id, allow_global = _owner_secret_context(plan["owner_id"])
    commands = compile_cloudinit(recipe, blocks, _secret_lookup_factory(
        owner_id, allow_global=allow_global)) if recipe else []
    ctx.progress(1, "Preflight rebuild: image and cloud-init delivery")
    _ensure_base_disk(ctx, px, node, cfg)
    staged_name = f"gd-preflight-{job.id}.yml"
    staged = _preflight_deploy_cloud_init(ctx, px, conn, dep, node,
        dep.vmid or px.next_free_vmid(settings.vmid_min, settings.vmid_max, node), cfg, commands,
        _ssh_pubkey(owner_id, allow_global=allow_global), has_ansible_blocks(recipe, blocks),
        snippet_name=staged_name)
    if staged.snippet_volume:
        delete_snippet_over_ssh(conn, staged_name)
    if ctx.cancelled():
        raise JobCancelled()

    ctx.progress(1, "Phase 1 of 6 · Destroy")
    st = ctx.add_step(f"Stop & destroy old disk for {dep.name}")
    t = ctx.start_step(st)
    old_vmid = dep.vmid
    if old_vmid:
        if _stop_vm_for_lifecycle(ctx, px, old_vmid, node):
            try:
                upid = px.destroy(old_vmid, node=node)
                ctx.remote(upid, node)
                px.wait_task(upid, node=node, cancelled=ctx.cancelled, timeout=300)
            except JobCancelled:
                # A cancel during the pre-rebuild destroy: do NOT recreate — propagate so
                # _execute reconciles the cancel against the VM's actual state.
                raise
            except Exception as e:  # noqa: BLE001
                # Registry absence cannot prove an accepted destroy task has stopped.
                raise RuntimeError(
                    f"rebuild aborted: could not confirm destruction of old VM {old_vmid}: {e}") from e
        else:
            ctx.log(f"[{_ts()}] old VM {old_vmid} already absent; continuing", "l-dim")
        presence, detail = _probe_vm_presence(px, old_vmid, node)
        if presence != VM_ABSENT:
            raise RuntimeError(f"rebuild aborted: old VM {old_vmid} absence not confirmed: {detail}")
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
    vm_present = True
    if dep.vmid:
        vm_present = _stop_vm_for_lifecycle(ctx, px, dep.vmid, node)
    ctx.finish_step(st, t)

    ctx.progress(50, "Destroying")
    st = ctx.add_step(f"Destroy {dep.name} (purge disk)")
    t = ctx.start_step(st)
    if dep.vmid and vm_present:
        try:
            upid = px.destroy(dep.vmid, node=node)
            ctx.remote(upid, node)
            px.wait_task(upid, node=node, cancelled=ctx.cancelled, timeout=300)
        except Exception:  # noqa: BLE001
            # Let reconciliation verify both task completion and resource presence.
            raise
    presence, detail = _probe_vm_presence(px, dep.vmid, node)
    if presence != VM_ABSENT:
        raise RuntimeError(f"destroy cleanup requires confirmed VM absence: {detail}")
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
    if conn.disabled:
        raise RuntimeError("connection is disabled")
    cfg = json.loads(job.context_json or "{}")
    px = Proxmox(conn)
    node = conn.node or px.pick_node()
    ctx.progress(5, "Phase 1 of 1 · Prepare image")
    st = ctx.add_step("Ensure base cloud image on node storage")
    t = ctx.start_step(st)
    _ensure_base_disk(ctx, px, node, cfg)
    ctx.finish_step(st, t)
    ctx.progress(100, "Complete")


def _run_configure(ctx: JobCtx, job: Job) -> None:
    """Explicitly replay only captured post-boot configuration on the existing VM."""
    conn, dep = _load_job_targets(job)
    if ctx.cancelled():
        raise JobCancelled()
    plan, recipe, blocks = _load_materialized_job_plan(job, dep)
    if not has_ansible_blocks(recipe, blocks):
        raise RuntimeError("captured plan has no post-boot configuration to retry")
    px = Proxmox(conn)
    node = px.find_vm_node(dep.vmid, dep.node or conn.node)
    if not node or px.vm_current(dep.vmid, node).get("status") != "running":
        raise RuntimeError("configuration retry requires the existing VM to be running")
    ctx.progress(10, "Verify first-boot completion")
    px.wait_guest_ready(dep.vmid, node=node, cancelled=ctx.cancelled)
    ip = px.agent_ipv4(dep.vmid, node) or dep.ip
    if not ip:
        raise RuntimeError("guest IP is unavailable")
    owner_id, allow_global = _owner_secret_context(plan["owner_id"])
    private_key, _ = _managed_keypair()
    ctx.progress(50, "Retry post-boot configuration")
    _run_ansible_phase(ctx, recipe, blocks, owner_id, ip, private_key, dep.name,
                       allow_global=allow_global)
    _set_dep_status(dep.id, "running", "", dep.vmid)
    ctx.progress(100, "Complete")


_DISPATCH = {
    "deploy": _run_deploy,
    "rebuild": _run_rebuild,
    "destroy": _run_destroy,
    "image_sync": _run_image_sync,
    "configure": _run_configure,
}


# --------------------------------------------------------------------------- #
# Worker loop                                                                  #
# --------------------------------------------------------------------------- #
def _claim_next_job() -> Optional[int]:
    canceled_id = None
    failed_id = None
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
        elif (job.connection_id
              and (conn := s.get(Connection, job.connection_id)) is not None
              and conn.disabled):
            # The admin disabled the source after this job was queued. Admission
            # blocks new jobs on disabled connections; a stranded queued one must
            # not silently start talking to a target the operator switched off.
            job.status = "failed"
            job.error = (f"Proxmox connection {conn.name!r} was disabled while this "
                         "job was queued — enable it and retry")
            job.finished_at = utcnow()
            s.add(job)
            failed_id = job.id
        else:
            job.status = "running"
            job.started_at = utcnow()
            s.add(job)
            statebus.bump()
            return job.id
    # A queued job was cancelled/failed pre-start: reconcile its deployment so it
    # isn't stranded in "working" with a leaked IP reservation.
    _reconcile_canceled_job(canceled_id)
    if failed_id:
        _reconcile_failed_job(failed_id)
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
    except JobDeferred:
        # The job and VM facts were committed before this control-flow signal. It is
        # active, not successful or failed, and the idle loop will resume it later.
        statebus.bump()
    except Exception as e:  # noqa: BLE001
        # Cancellation is identified by TYPE, never by error text — a genuine failure
        # whose message contains the word "cancel" (e.g. a VM named 'cancel-svc') must
        # be treated as a failure, not a user cancel that would destroy the VM.
        cancelled = isinstance(e, JobCancelled)
        ctx.log(f"[{_ts()}] ✗ {'canceled' if cancelled else e}", "l-err")
        with session_scope() as s:
            job = s.get(Job, job_id)
            # Preserve the active lifecycle guard until remote reconciliation finishes.
            job.status = "running"
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
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job:
                job.status = "canceled" if cancelled else "failed"
                job.finished_at = utcnow()
                s.add(job)
        statebus.bump()
        traceback.print_exc()


def _vm_exists(px: Proxmox, vmid: int, node: Optional[str]) -> bool:
    """Fail-safe compatibility helper used by rebuild's post-destroy check."""
    state, _detail = _probe_vm_presence(px, vmid, node)
    return state != VM_ABSENT


def _probe_vm_runtime_status(
    px: Proxmox, vmid: int, node: Optional[str],
) -> tuple[Optional[str], str]:
    """Read a present VM's actionable power state without inventing a default."""
    try:
        status = str((px.vm_current(vmid, node) or {}).get("status") or "").lower()
    except Exception as exc:  # noqa: BLE001
        return None, f"VM runtime status probe failed: {exc}"
    if status in ("running", "stopped"):
        return status, f"VM {vmid} reports {status}"
    return None, f"VM runtime status was unavailable ({status or 'empty response'})"


def _px_for_conn(conn_id: Optional[int]) -> Optional[Proxmox]:
    """Build a Proxmox client for `conn_id`, or None if the connection is gone or the
    client can't be constructed. Used by the cancel/cleanup paths."""
    if not conn_id:
        return None
    try:
        with session_scope() as s:
            conn = s.get(Connection, conn_id)
            # An admin-disabled source must never be contacted, even by cancel/
            # cleanup reconciliation — callers treat None as "inventory unknown"
            # and keep ownership, which is the fail-safe outcome.
            if conn is not None and conn.disabled:
                return None
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


def _drop_deployment_if_matches(
    dep_id: int, vmid: Optional[int], required_status: Optional[str] = None
) -> None:
    """Drop only the same ownership record that was externally proven absent."""
    with session_scope() as s:
        d = s.get(Deployment, dep_id)
        if not d or d.vmid != vmid or (required_status and d.status != required_status):
            return
        for allocation in s.exec(
            select(IpAllocation).where(IpAllocation.deployment_id == dep_id)
        ).all():
            s.delete(allocation)
        s.delete(d)


def _set_dep_status(
    dep_id: int,
    status: str,
    error: str,
    expected_vmid: Optional[int],
    cleanup_origin: Optional[str] = None,
) -> None:
    with session_scope() as s:
        d = s.get(Deployment, dep_id)
        if d and d.vmid == expected_vmid:
            if status == "cleanup_pending":
                if cleanup_origin in ("deploy", "destroy"):
                    if d.status != "cleanup_pending" or d.cleanup_origin != cleanup_origin:
                        d.cleanup_last_attempt_at = None
                    d.cleanup_origin = cleanup_origin
            else:
                d.cleanup_origin = None
                d.cleanup_last_attempt_at = None
            d.status = status
            d.error = error[:300] if error else ""
            s.add(d)


def _best_effort_destroy(conn_id: Optional[int], vmid: Optional[int], node: Optional[str]) -> Optional[bool]:
    """Destroy `vmid` on its connection, swallowing every error. Used to tear down a
    half-built VM left by a cancelled deploy so it doesn't orphan on Proxmox."""
    if not conn_id or not vmid:
        return
    px = _px_for_conn(conn_id)
    if px is None:
        return
    submitted = False
    def track_task(upid):
        with session_scope() as s:
            dep = s.exec(select(Deployment).where(Deployment.connection_id == conn_id, Deployment.vmid == vmid)).first()
            job = s.exec(select(Job).where(Job.deployment_id == dep.id).order_by(Job.id.desc())).first() if dep else None
            if job:
                job.remote_task, job.remote_node = upid, node
                s.add(job)
    try:
        node = px.find_vm_node(vmid, node)
        if not node:
            return True
        status = (px.vm_current(vmid, node) or {}).get("status")
        if status != "stopped":
            submitted = True
            upid = px.stop(vmid, node=node)
            track_task(upid)
            px.wait_task(upid, node=node, timeout=300)
        submitted = True
        upid = px.destroy(vmid, node=node)
        track_task(upid)
        px.wait_task(upid, node=node, timeout=300)
        return True
    except Exception:  # noqa: BLE001
        if not submitted:
            return None
        with session_scope() as s:
            dep = s.exec(select(Deployment).where(Deployment.connection_id == conn_id, Deployment.vmid == vmid)).first()
            if dep:
                dep.identity_state = "submitting"
                dep.status = "error"
                dep.error = "Cleanup task outcome is unconfirmed; inspect Proxmox and confirm ownership in Recovery"
                s.add(dep)
        return False


def _best_effort_delete_snippet(conn_id: Optional[int], vmid: Optional[int]) -> None:
    """Remove the node-side cloud-init snippet (gd-deploy-<vmid>.yml — carries the
    root-password hash + injected pubkeys) left behind by a deploy. Best-effort:
    needs an SSH key on the connection and swallows every error."""
    if not conn_id or not vmid:
        return
    with session_scope() as s:
        conn = s.get(Connection, conn_id)
        conn = Connection(**conn.model_dump()) if conn else None
    if not conn or conn.disabled or not conn.ssh_key_path:
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


def _retain_unsettled_task(job_id: int) -> bool:
    """A registry absence cannot prove safety while an accepted task may still run."""
    with session_scope() as s:
        job = s.get(Job, job_id)
        dep = s.get(Deployment, job.deployment_id) if job and job.deployment_id else None
        if not job or not dep or not job.remote_task:
            return False
        task, node, conn_id, dep_id = job.remote_task, job.remote_node, job.connection_id, dep.id
    try:
        px = _px_for_conn(conn_id)
        if px and px.task_status(task, node).get("status") == "stopped":
            return False
    except Exception:
        pass
    with session_scope() as s:
        dep = s.get(Deployment, dep_id)
        if dep:
            dep.identity_state = "submitting"
            dep.status = "error"
            dep.error = "Remote task outcome is unconfirmed; inspect Proxmox task history and confirm ownership in Recovery"
            s.add(dep)
    return True


def _reconcile_canceled_job(job_id: Optional[int]) -> None:
    """Reconcile cancellation without releasing ownership on ambiguous inventory."""
    if not job_id:
        return
    if _retain_unsettled_task(job_id):
        return
    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job or not job.deployment_id:
            return
        dep = s.get(Deployment, job.deployment_id)
        if not dep:
            return
        job_type = job.type
        new_rebuild_vm = job_type == "rebuild" and job.create_state == "accepted"
        vmid, node, conn_id, dep_id = dep.vmid, dep.node, dep.connection_id, dep.id
        if dep.identity_state == "submitting" or job.create_state == "submitting":
            dep.status = "error"
            dep.error = "Create response lost; reconcile VM identity before cleanup or retry"
            s.add(dep)
            return

    if job_type == "deploy" and vmid is not None:
        if _best_effort_destroy(conn_id, vmid, node) is False:
            return

    px = _px_for_conn(conn_id) if vmid is not None else None
    presence, detail = _probe_vm_presence(px, vmid, node)

    if presence == VM_ABSENT:
        _best_effort_delete_snippet(conn_id, vmid)
        if job_type in ("deploy", "destroy"):
            _drop_deployment_if_matches(dep_id, vmid)
        else:
            _set_dep_status(
                dep_id,
                "error",
                "rebuild canceled after the old VM was removed — rebuild again to recreate",
                expected_vmid=vmid,
            )
        return

    if new_rebuild_vm:
        _set_dep_status(dep_id, "error", "Rebuild canceled after creating the replacement VM; review first-boot and configuration before recovery", expected_vmid=vmid)
        return

    if presence == VM_PRESENT and job_type in ("rebuild", "destroy"):
        runtime_status, runtime_detail = _probe_vm_runtime_status(px, vmid, node)
        if runtime_status:
            _set_dep_status(dep_id, runtime_status, "", expected_vmid=vmid)
        else:
            _set_dep_status(
                dep_id,
                "error",
                f"canceled {job_type} VM status not confirmed: {runtime_detail}",
                expected_vmid=vmid,
            )
        return

    if job_type in ("deploy", "destroy"):
        _set_dep_status(
            dep_id,
            "cleanup_pending",
            f"canceled {job_type} cleanup not confirmed: {detail}",
            expected_vmid=vmid,
            cleanup_origin=job_type,
        )
    else:
        _set_dep_status(
            dep_id,
            "error",
            f"rebuild canceled while VM presence is unknown: {detail}",
            expected_vmid=vmid,
        )


def _reconcile_failed_job(job_id: int) -> None:
    """Mark a failure visible while releasing ownership only on proven absence."""
    if _retain_unsettled_task(job_id):
        return
    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job or not job.deployment_id:
            return
        dep = s.get(Deployment, job.deployment_id)
        if not dep:
            return
        job_type, job_error = job.type, job.error or ""
        dep_id, conn_id, vmid, node = dep.id, dep.connection_id, dep.vmid, dep.node
        if dep.identity_state == "submitting" or job.create_state == "submitting":
            dep.status = "error"
            dep.error = "Create response lost; reconcile VM identity before cleanup or retry"
            s.add(dep)
            return

    px = _px_for_conn(conn_id) if vmid is not None else None
    presence, _detail = _probe_vm_presence(px, vmid, node)

    if presence == VM_ABSENT:
        _best_effort_delete_snippet(conn_id, vmid)
        if job_type == "destroy":
            _drop_deployment_if_matches(dep_id, vmid)
            return

    with session_scope() as s:
        dep = s.get(Deployment, dep_id)
        if not dep or dep.vmid != vmid:
            return
        dep.status = "error"
        dep.error = job_error[:300]
        dep.cleanup_origin = None
        dep.cleanup_last_attempt_at = None
        s.add(dep)
        if job_type == "deploy" and presence == VM_ABSENT:
            for allocation in s.exec(
                select(IpAllocation).where(IpAllocation.deployment_id == dep.id)
            ).all():
                s.delete(allocation)


def _retry_cleanup_pending(now: Optional[datetime] = None) -> None:
    """Retry ambiguous cleanup at most once per minute, outside DB transactions."""
    with session_scope() as s:
        dep_ids = [dep.id for dep in s.exec(
            select(Deployment).where(
                Deployment.status == "cleanup_pending"
            ).order_by(Deployment.id)
        ).all()]

    from .api import _deployment_operation_lock
    for dep_id in dep_ids:
        with _deployment_operation_lock(dep_id):
            _retry_cleanup_deployment(dep_id, now)


def _retry_cleanup_deployment(dep_id: int, now: Optional[datetime]) -> None:
    attempt_at = ensure_utc(now) or utcnow()
    cutoff = attempt_at - timedelta(seconds=60)
    with session_scope() as s:
        dep = s.get(Deployment, dep_id)
        if not dep or dep.status != "cleanup_pending":
            return
        last_attempt = ensure_utc(dep.cleanup_last_attempt_at)
        if last_attempt is not None and last_attempt > cutoff:
            return
        dep.cleanup_last_attempt_at = attempt_at
        s.add(dep)
        target = (dep.id, dep.connection_id, dep.vmid, dep.node, dep.cleanup_origin or "")

    dep_id, conn_id, vmid, node, job_type = target
    if job_type == "deploy" and vmid is not None:
        if _best_effort_destroy(conn_id, vmid, node) is False:
            return
    px = _px_for_conn(conn_id) if vmid is not None else None
    presence, detail = _probe_vm_presence(px, vmid, node)
    if presence == VM_ABSENT:
        _best_effort_delete_snippet(conn_id, vmid)
        _drop_deployment_if_matches(dep_id, vmid, required_status="cleanup_pending")
    elif presence == VM_PRESENT and job_type == "destroy":
        _set_dep_status(dep_id, "stopped", "", expected_vmid=vmid)
    else:
        _set_dep_status(
            dep_id,
            "cleanup_pending",
            f"cleanup not confirmed: {detail}",
            expected_vmid=vmid,
        )


def _reconcile_ips() -> None:
    """Fill in IPs that the guest agent reports after a deploy's wait timed out
    (e.g. a slow first-boot agent install). Best-effort, runs when idle."""
    with session_scope() as s:
        deps = s.exec(select(Deployment).where(Deployment.status == "running")).all()
        targets = [(d.id, d.connection_id, d.vmid, d.node) for d in deps if not d.ip and d.vmid]
        conns = {c.id: Connection(**c.model_dump()) for c in s.exec(select(Connection)).all()}
    for dep_id, conn_id, vmid, node in targets:
        conn = conns.get(conn_id)
        if not conn or conn.disabled:
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


def _waiting_ansible_step(job_id: int) -> int:
    """Return the pending Ansible step, repairing old/test waits without one."""
    name = "Apply recipe (ansible, post-boot)"
    with session_scope() as s:
        steps = s.exec(
            select(JobStep).where(JobStep.job_id == job_id).order_by(JobStep.seq)
        ).all()
        for step in reversed(steps):
            if step.name == name and step.state in ("pending", "running"):
                return step.seq
        seq = max((step.seq for step in steps), default=0) + 1
        s.add(JobStep(job_id=job_id, seq=seq, name=name, state="pending"))
        return seq


def _resume_waiting_ansible(job_id: int, ip: str) -> None:
    """Run only the captured Ansible plan and complete one durable waiting job."""
    with session_scope() as s:
        stored_job = s.get(Job, job_id)
        if not stored_job or stored_job.status != "waiting":
            return
        conn = s.get(Connection, stored_job.connection_id)
        if conn and conn.disabled:
            return
        if not stored_job.execution_plan_enc:
            raise RuntimeError("waiting job has no captured execution plan")
        job = Job(**stored_job.model_dump())
        dep = s.get(Deployment, job.deployment_id) if job.deployment_id else None
        if not dep:
            raise RuntimeError("waiting job deployment is missing")
        dep_id, dep_name, dep_owner_id = dep.id, dep.name, dep.owner_id
        dep.ip = ip
        s.add(dep)
        # A crash after this commit fails visibly on startup; it never replays a script.
        stored_job.status = "running"
        stored_job.phase = "Apply recipe (ansible, post-boot)"
        s.add(stored_job)

    try:
        plan = open_execution_plan(job.execution_plan_enc)
    except ValueError as exc:
        raise RuntimeError("invalid execution plan") from exc
    if plan["owner_id"] != dep_owner_id:
        raise RuntimeError("execution plan owner mismatch")
    recipe, blocks = materialize_execution_plan(plan)
    if not has_ansible_blocks(recipe, blocks):
        raise RuntimeError("waiting job captured no Ansible plan")

    owner_id, allow_global = _owner_secret_context(plan["owner_id"])
    managed_private_key, _managed_public_key = _managed_keypair()
    ctx = JobCtx(job_id)
    step = _waiting_ansible_step(job_id)
    ctx.progress(90, "Apply recipe (ansible, post-boot)")
    started = ctx.start_step(step)
    try:
        _run_ansible_phase(
            ctx, recipe, blocks, owner_id, ip, managed_private_key, dep_name,
            allow_global=allow_global,
        )
        ctx.finish_step(step, started)
    except Exception:
        ctx.finish_step(step, started, state="failed")
        raise

    with session_scope() as s:
        job_row = s.get(Job, job_id)
        dep_row = s.get(Deployment, dep_id)
        if not job_row or job_row.status != "running" or not dep_row:
            return
        dep_row.ip = ip
        dep_row.status = "running"
        dep_row.error = ""
        dep_row.cleanup_origin = None
        dep_row.cleanup_last_attempt_at = None
        job_row.status = "succeeded"
        job_row.pct = 100
        job_row.phase = "Complete"
        job_row.finished_at = utcnow()
        s.add(dep_row)
        s.add(job_row)
    ctx.log(f"[{_ts()}] ✓ {dep_name} ready at {ip}", "l-ok")
    statebus.bump()


def _finish_waiting_error(job_id: int, exc: Exception) -> None:
    """Finish a resumed wait through the same failure/cancel reconciliation invariant."""
    cancelled = isinstance(exc, JobCancelled)
    ctx = JobCtx(job_id)
    ctx.log(f"[{_ts()}] ✗ {'canceled' if cancelled else exc}", "l-err")
    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job or job.status not in ("waiting", "running"):
            return
        job.status = "canceled" if cancelled else "failed"
        job.error = "canceled" if cancelled else str(exc)
        job.finished_at = utcnow()
        s.add(job)
        has_dep = job.deployment_id is not None
    if has_dep:
        if cancelled:
            _reconcile_canceled_job(job_id)
        else:
            _reconcile_failed_job(job_id)
    statebus.bump()


def _timeout_waiting_job(job_id: int) -> None:
    """Fail an expired IP wait without releasing its VM or reserved IP ownership."""
    error = "guest IP was not reported within 30 minutes"
    cancel_requested = False
    with session_scope() as s:
        job = s.get(Job, job_id)
        if not job or job.status != "waiting":
            return
        cancel_requested = job.cancel_requested
        if not cancel_requested:
            job.status = "failed"
            job.error = error
            job.finished_at = utcnow()
            s.add(job)
            dep = s.get(Deployment, job.deployment_id) if job.deployment_id else None
            if dep:
                dep.status = "error"
                dep.error = error
                dep.cleanup_origin = None
                dep.cleanup_last_attempt_at = None
                s.add(dep)
    if cancel_requested:
        _finish_waiting_error(job_id, JobCancelled())
        return
    JobCtx(job_id).log(f"[{_ts()}] ✗ {error}", "l-err")
    statebus.bump()


def _poll_waiting_job(job_id: int, poll_at: datetime) -> None:
    """Reload and process one waiting row without retaining its DB session."""
    with session_scope() as s:
        stored = s.get(Job, job_id)
        if not stored or stored.status != "waiting":
            return
        job = Job(**stored.model_dump())
        dep = s.get(Deployment, job.deployment_id) if job.deployment_id else None
        conn = s.get(Connection, job.connection_id) if job.connection_id else None
        dep = Deployment(**dep.model_dump()) if dep else None
        conn = Connection(**conn.model_dump()) if conn else None

    if job.cancel_requested:
        _finish_waiting_error(job.id, JobCancelled())
        return

    if not dep or not conn or dep.vmid is None:
        _finish_waiting_error(job.id, RuntimeError("waiting job target is missing"))
        return
    if conn.disabled:
        return
    try:
        ip = Proxmox(conn).agent_ipv4(dep.vmid, dep.node or conn.node)
    except Exception:  # noqa: BLE001
        ip = None
    if not ip:
        waiting_since = (
            ensure_utc(job.waiting_since)
            or ensure_utc(job.started_at)
            or ensure_utc(job.created_at)
        )
        if waiting_since is not None and poll_at >= waiting_since + WAITING_TIMEOUT:
            _timeout_waiting_job(job.id)
        return
    try:
        _resume_waiting_ansible(job.id, ip)
    except Exception as exc:  # noqa: BLE001
        _finish_waiting_error(job.id, exc)


def _poll_waiting_jobs(now: Optional[datetime] = None) -> bool:
    """Poll an ordered snapshot of durable waits independently of queued work."""
    poll_at = ensure_utc(now) or utcnow()
    with session_scope() as s:
        waiting_ids = s.exec(
            select(Job.id).where(Job.status == "waiting")
            .order_by(Job.waiting_since, Job.id)
        ).all()
    if not waiting_ids:
        return False

    for job_id in waiting_ids:
        try:
            _poll_waiting_job(job_id, poll_at)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    return True


def _recover_orphans() -> None:
    """Crash recovery: a job left 'running' by a previous process is dead. Fail it AND
    reconcile the resource it was mutating — otherwise the deployment stays "working"
    forever (serialize skips live-polling 'working') and the image stays "building"."""
    deployment_job_ids = []
    with session_scope() as s:
        for job in s.exec(select(Job).where(Job.status == "running")).all():
            job.status = "failed"
            job.error = "interrupted (worker restart)"
            job.finished_at = utcnow()
            s.add(job)
            if job.deployment_id:
                deployment_job_ids.append(job.id)
            if job.image_id:
                img = s.get(Image, job.image_id)
                if img and img.build_status == "building":
                    # keep template_vmid so an admin can identify and manually clean up the ghost on the node
                    img.build_status = "failed"
                    s.add(img)
    for job_id in deployment_job_ids:
        _reconcile_failed_job(job_id)


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
                    _retry_cleanup_pending()
                time.sleep(1.0)
                continue
            idle = 0
            _execute(job_id)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            time.sleep(2.0)


def _waiting_loop() -> None:
    """Keep durable guest-IP waits moving while the main worker is busy."""
    while not _stop.is_set():
        try:
            _poll_waiting_jobs()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        _stop.wait(1.0)


def start_worker() -> None:
    global _worker_thread, _waiting_thread
    _stop.clear()
    if not _worker_thread or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(target=_loop, name="gd-worker", daemon=True)
        _worker_thread.start()
    if not _waiting_thread or not _waiting_thread.is_alive():
        _waiting_thread = threading.Thread(
            target=_waiting_loop, name="gd-waiting-worker", daemon=True,
        )
        _waiting_thread.start()


def worker_health() -> dict:
    """Liveness of the two background threads, for the admin Health page."""
    return {
        "jobWorkerAlive": bool(_worker_thread and _worker_thread.is_alive()),
        "waitingWorkerAlive": bool(_waiting_thread and _waiting_thread.is_alive()),
    }


def stop_worker(join_timeout: float = 30) -> None:
    _stop.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=join_timeout)
    if _waiting_thread and _waiting_thread.is_alive():
        _waiting_thread.join(timeout=join_timeout)
