"""GoblinDock HTTP API — one router covering auth, state, VMs, images,
templates, blocks, secrets, settings and jobs (incl. SSE).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlmodel import Session, select

from .config import settings
from .db import engine, get_session
from .deps import current_user, require_admin, widget_key_user
from .netutil import client_ip, current_request_ip
from .models import (
    Audit,
    Block,
    Connection,
    Deployment,
    Image,
    IpAllocation,
    Job,
    JobEvent,
    Network,
    Template,
    Secret,
    Variable,
    User,
    ensure_utc,
    utcnow,
)
from .proxmox import Proxmox, base_disk_filename
from .recipes import ask_map, compile_playbook, lint_block, load_recipe
from . import backup
from . import statebus
from .security import (
    decrypt,
    encrypt,
    hash_password,
    hash_widget_key,
    new_csrf_token,
    new_widget_key,
    password_problem,
    verify_password,
)
from . import serialize as S

router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------- #
# cross-cutting helpers                                                        #
# --------------------------------------------------------------------------- #
def ensure_csrf(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = new_csrf_token()
        request.session["csrf"] = tok
    return tok


def record_audit(session: Session, user: User, action: str, target_type: str,
                 target_id, detail: str = "") -> None:
    session.add(Audit(user_id=user.id, user_name=user.name, action=action,
                      target_type=target_type, target_id=str(target_id),
                      ip=current_request_ip(), detail=detail))


# VM / image names flow UNESCAPED into the cloud-init `#cloud-config` (hostname:)
# and into the generated Ansible play header (- name:). `.strip()` alone leaves an
# embedded newline able to inject sibling YAML keys (e.g. runcmd: → root RCE in the
# baked image). Allow only a safe charset with no newline/YAML metacharacters.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}$")


def _clean_name(name: str, what: str = "name") -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise HTTPException(
            400, f"invalid {what}: use letters, digits, space, dot, dash or underscore "
                 "(1–63 chars, no newlines)")
    return name


# Secret/variable names are referenced as {{ secrets.NAME }} / {{ variable.NAME }},
# whose resolver only matches [A-Za-z0-9_]+ (recipes._REF_RE). A name with a space,
# dash or dot would be stored but be permanently unreferenceable, so enforce the
# resolver's charset at creation/rename — stricter than _NAME_RE on purpose.
_REF_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _clean_ref_name(name: str, what: str = "name") -> str:
    name = (name or "").strip()
    if not _REF_NAME_RE.match(name):
        raise HTTPException(
            400, f"invalid {what}: use letters, digits and underscore only "
                 "(must be referenceable as {{ secrets.NAME }} / {{ variable.NAME }})")
    return name


# Proxmox node and storage ids flow into proxmoxer URL paths; constrain them to a
# safe allowlist at connection create/edit so they can't traverse or inject the path.
_STORAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _clean_storage_id(value: str, what: str = "id") -> str:
    value = (value or "").strip()
    if value and not _STORAGE_ID_RE.match(value):
        raise HTTPException(400, f"invalid {what}: use letters, digits, dot, dash or underscore")
    return value


def _has_control_chars(s: str) -> bool:
    """True if `s` contains any C0 control char (incl. newline/tab) or DEL — used to
    reject deploy-input values that could inject sibling YAML keys into a module-arg
    dict or otherwise break out of a single rendered scalar."""
    return any(ord(c) < 0x20 or ord(c) == 0x7f for c in s)


# Proxmox download-url needs both a checksum AND its algorithm. The base-image form
# stores only the hex digest, so infer the algorithm from its length (cloud images
# publish hex digests). Unknown length → no algorithm → no verification attempted
# (better than verifying with the wrong algorithm and always failing).
_CHECKSUM_ALGO = {32: "md5", 40: "sha1", 64: "sha256", 96: "sha384", 128: "sha512"}


def _checksum_algo(checksum: str) -> str:
    cs = (checksum or "").strip().lower()
    return _CHECKSUM_ALGO.get(len(cs), "") if re.fullmatch(r"[0-9a-f]*", cs) else ""


def _check_password(pw: str) -> None:
    problem = password_problem(pw)
    if problem:
        raise HTTPException(400, problem)


def _enforce_quota(session: Session, user: User, kind: str) -> None:
    """Per-user resource cap (0 = unlimited; admins exempt). kind: 'vm'."""
    if user.role == "admin":
        return
    if kind == "vm" and settings.max_vms_per_user:
        n = len(session.exec(select(Deployment).where(Deployment.owner_id == user.id)).all())
        if n >= settings.max_vms_per_user:
            raise HTTPException(429, f"VM quota reached ({settings.max_vms_per_user}) — "
                                     "destroy one first or ask an admin to raise your limit")


def validate_image_url(url: str) -> str:
    """SSRF guard for the cloud-image download URL: https only, every resolved
    address must be globally routable.

    Reachable by any authenticated user, so this is a real user->infra boundary.
    Residual risk we accept by design: the
    multi-GB fetch is done by the Proxmox NODE (download-url), not by GoblinDock, so
    an HTTP redirect to an internal host or DNS rebinding between this check and the
    node's fetch (TOCTOU) can't be fully closed here without proxying GBs through the
    container (which defeats the point of download-url). The integrity backstop is the
    checksum download-url verifies on the node — supply one for untrusted URLs."""
    u = urlparse(url or "")
    if u.scheme != "https":
        raise HTTPException(400, "image URL must be https")
    host = u.hostname or ""
    if not host:
        raise HTTPException(400, "image URL has no host")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise HTTPException(400, f"cannot resolve image host {host}")
    if not infos:
        raise HTTPException(400, f"cannot resolve image host {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # is_global rejects private, loopback, link-local, CGNAT, reserved, etc. in
        # one shot; multicast is belt-and-braces (already non-global).
        if not ip.is_global or ip.is_multicast:
            raise HTTPException(400, "image URL resolves to a non-public address (blocked)")
    return url


def default_network_for(session: Session, conn: Connection, user_id) -> Network:
    net = session.exec(select(Network).where(Network.connection_id == conn.id)).first()
    if net:
        return net
    net = Network(connection_id=conn.id, name="lan-dhcp", mode="dhcp",
                  bridge=conn.bridge or "vmbr0", created_by=user_id)
    session.add(net)
    session.commit()
    session.refresh(net)
    return net


# Serialise static-IP picking within this (single) process so two concurrent deploys
# to the same pool can't read the same "taken" set and pick the same address. The
# reservation is committed INSIDE the lock so the next waiter sees it; the unique index
# on (network_id, ip) is the cross-process backstop.
_ip_alloc_lock = threading.Lock()


def allocate_ip(session: Session, net: Network, deployment_id: int) -> Optional[str]:
    """Static-pool allocation: next free IP in range, remembered on the deployment."""
    if net.mode != "static" or not net.range_start:
        return None
    with _ip_alloc_lock:
        # reuse an existing reservation for this deployment (rebuild keeps the IP)
        existing = session.exec(select(IpAllocation).where(
            IpAllocation.network_id == net.id, IpAllocation.deployment_id == deployment_id)).first()
        if existing:
            return existing.ip
        taken = {a.ip for a in session.exec(select(IpAllocation).where(
            IpAllocation.network_id == net.id, IpAllocation.state == "reserved")).all()}
        try:
            start = ipaddress.ip_address(net.range_start)
            end = ipaddress.ip_address(net.range_end or net.range_start)
        except ValueError:
            return None
        cur = start
        while int(cur) <= int(end):
            ip = str(cur)
            if ip not in taken:
                session.add(IpAllocation(network_id=net.id, ip=ip,
                                         deployment_id=deployment_id, state="reserved"))
                session.commit()   # persist within the lock so the next waiter sees it
                return ip
            cur = cur + 1
        raise HTTPException(409, "static IP pool exhausted")


def _network_ctx(session: Session, net: Network, dep_id: int) -> dict:
    """The job-context fragment describing a VM's network: mode, and (for a static
    network) the reserved IP + ipconfig0 + vlan. Shared by deploy and rebuild so a
    rebuild keeps the VM's static IP / VLAN instead of silently reverting to DHCP.
    allocate_ip() reuses an existing reservation for the deployment, so a rebuild
    re-acquires the same address."""
    ctx: dict = {"network_mode": net.mode}
    if net.mode == "static":
        ip = allocate_ip(session, net, dep_id)   # commits the reservation internally
        if ip:
            ctx["static_ip"] = ip
            cidr = net.subnet_cidr.split("/")[-1] if "/" in (net.subnet_cidr or "") else "24"
            ctx["ipconfig0"] = f"ip={ip}/{cidr},gw={net.gateway}"
    if net.vlan:
        ctx["vlan"] = net.vlan
    if net.bridge:
        ctx["bridge"] = net.bridge
    if net.dns:
        ctx["dns"] = net.dns
    return ctx


def _build_job_ctx(session: Session, base: Image, cpu: int, ram: int, disk: int,
                   net: Optional[Network], dep_id: int) -> str:
    """The full worker job context shared by deploy and rebuild: image source,
    sizing, and network (no network => plain DHCP)."""
    ctx = {"src_url": base.source_url, "checksum": base.checksum or "",
           "checksum_algorithm": _checksum_algo(base.checksum or ""),
           "cpu": cpu, "ram": ram, "disk": disk}
    ctx.update(_network_ctx(session, net, dep_id) if net else {"network_mode": "dhcp"})
    return json.dumps(ctx)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _maps(session: Session):
    users = {u.id: u for u in session.exec(select(User)).all()}
    conns = {c.id: c for c in session.exec(select(Connection)).all()}
    return users, conns


def _px_cache(conns: dict) -> dict:
    cache = {}
    for cid, c in conns.items():
        try:
            cache[cid] = Proxmox(c)
        except Exception:  # noqa: BLE001
            pass
    return cache


def _job_owned(job: Job, user: User) -> bool:
    return user.role == "admin" or job.created_by == user.id


def _scoped_owned(obj, user: User) -> bool:
    """Shared ownership rule for scoped items (secrets, variables):
    global-scoped => admin only; user-scoped => owner or admin."""
    if obj.scope == "global":
        return user.role == "admin"
    return obj.owner_id == user.id or user.role == "admin"


# Reserved namespace for app-managed secrets (e.g. the fleet-wide SSH keypair
# GD_MANAGED_PRIVKEY/PUBKEY injected into every VM). These are infrastructure, not
# user secrets: they must never be revealed, deleted, listed, or shadowed by a
# user-created secret — exposing the private key is root on every managed VM.
_SYSTEM_SECRET_PREFIX = "GD_MANAGED_"


def _is_system_secret(sec) -> bool:
    return bool(getattr(sec, "name", "") and sec.name.startswith(_SYSTEM_SECRET_PREFIX))


def _owned_deployment(session: Session, dep_id: int, user: User) -> Deployment:
    """Fetch a deployment and enforce the owner-or-admin rule (404 / 403)."""
    dep = session.get(Deployment, dep_id)
    if not dep:
        raise HTTPException(404, "not found")
    if dep.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "not your VM")
    return dep


# --------------------------------------------------------------------------- #
# auth                                                                          #
# --------------------------------------------------------------------------- #
class LoginBody(BaseModel):
    email: str
    password: str


class SetupBody(BaseModel):
    email: str
    name: str
    password: str


@router.get("/auth/status")
def auth_status(request: Request, session: Session = Depends(get_session)):
    # Always 200 (never 401), so the client can cheaply re-verify after a stray 401
    # without that very check tripping the "drop to login" path.
    has_users = session.exec(select(User)).first() is not None
    uid = request.session.get("uid")
    authed = False
    if uid:
        u = session.get(User, uid)
        # mirror current_user: a stale session epoch (post password-change) is NOT authed
        authed = bool(u and not u.disabled and request.session.get("sv", 0) == u.session_epoch)
    return {"needsSetup": not has_users, "authenticated": authed}


# Serialises first-run admin creation so two concurrent /auth/setup calls can't both
# pass the "no users yet" check and create two admins (TOCTOU). Single-worker only — see
# the deployment note about running uvicorn with --workers 1.
_setup_lock = threading.Lock()


@router.post("/auth/setup")
def auth_setup(body: SetupBody, request: Request, session: Session = Depends(get_session)):
    with _setup_lock:
        if session.exec(select(User)).first():
            raise HTTPException(400, "already initialised")
        _check_password(body.password)
        user = User(email=body.email.strip().lower(), name=body.name.strip() or "Admin",
                    password_hash=hash_password(body.password), role="admin",
                    last_login=utcnow())
        session.add(user)
        session.commit()
        session.refresh(user)
    request.session["uid"] = user.id
    request.session["sv"] = user.session_epoch
    csrf = ensure_csrf(request)
    return {"ok": True, "me": S.me_dict(user), "csrf": csrf}


# Tiny in-memory login throttle (per email+ip). Resets on restart — fine for v1.
_login_attempts: dict[str, list[float]] = {}


def _throttle(key: str) -> None:
    now = time.time()
    window = [t for t in _login_attempts.get(key, []) if now - t < 300]
    _login_attempts[key] = window
    if len(window) >= 8:
        raise HTTPException(429, "too many attempts — try again in a few minutes")


def _record_attempt(key: str) -> None:
    _login_attempts.setdefault(key, []).append(time.time())


_LOCK_THRESHOLD = 5      # consecutive failures before a temporary lock
_LOCK_MINUTES = 15


@router.post("/auth/login")
def login(body: LoginBody, request: Request, session: Session = Depends(get_session)):
    ip = client_ip(request) or "?"
    email = body.email.strip().lower()
    key = f"{email}|{ip}"
    _throttle(key)
    user = session.exec(select(User).where(User.email == email)).first()
    # Per-account lockout — persists across restarts (unlike the in-memory IP throttle),
    # blunting password-spray and proxy-IP-collapsed brute force. SQLite returns the
    # stored datetime as naive, so normalise to UTC before comparing with utcnow().
    if user and user.locked_until:
        lu = ensure_utc(user.locked_until)
        if lu > utcnow():
            raise HTTPException(429, "account temporarily locked — try again later")
    if not user or user.disabled or not verify_password(body.password, user.password_hash):
        _record_attempt(key)
        if user and not user.disabled:
            user.failed_logins = (user.failed_logins or 0) + 1
            if user.failed_logins >= _LOCK_THRESHOLD:
                user.locked_until = utcnow() + timedelta(minutes=_LOCK_MINUTES)
                user.failed_logins = 0
            session.add(user)
            session.commit()
        raise HTTPException(401, "invalid email or password")
    _login_attempts.pop(key, None)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login = utcnow()
    session.add(user)
    session.commit()
    request.session["uid"] = user.id
    request.session["sv"] = user.session_epoch
    csrf = ensure_csrf(request)
    return {"ok": True, "me": S.me_dict(user), "csrf": csrf}


@router.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request, user: User = Depends(current_user)):
    return {"me": S.me_dict(user), "csrf": ensure_csrf(request)}


# --------------------------------------------------------------------------- #
# bootstrap state                                                              #
# --------------------------------------------------------------------------- #
@router.get("/state")
def state(request: Request, user: User = Depends(current_user), session: Session = Depends(get_session)):
    users, conns = _maps(session)
    px_cache = _px_cache(conns)

    # Non-admins only ever see their OWN VMs — filter at the DB so we never serialize
    # (and live-probe Proxmox for) other users' VMs just to discard them. GET /state
    # stays read-only: each connection's default DHCP network is created at
    # connection-add and at startup (seed_default_networks), not lazily on this read.
    deps_q = select(Deployment).order_by(Deployment.id.desc())
    if user.role != "admin":
        deps_q = deps_q.where(Deployment.owner_id == user.id)
    deps = session.exec(deps_q).all()
    vms = [S.vm_dict(session, d, user, px_cache, users, conns) for d in deps]

    base = [S.base_image_dict(i) for i in session.exec(select(Image).where(Image.kind == "base")).all()]
    tpls = session.exec(select(Template).order_by(Template.id)).all()
    if user.role != "admin":
        tpls = [t for t in tpls if t.public or t.owner_id == user.id]
    templates = [S.template_dict(session, t, viewer=user) for t in tpls]
    blocks_all = session.exec(select(Block).order_by(Block.id)).all()
    if user.role != "admin":
        blocks_all = [b for b in blocks_all if b.builtin or b.owner_id == user.id]
    blocks = [S.block_dict(b) for b in blocks_all]

    secrets_q = session.exec(select(Secret)).all()
    if user.role != "admin":
        secrets_q = [s for s in secrets_q if s.scope == "global" or s.owner_id == user.id]
    # App-managed secrets (fleet SSH key) are infrastructure — never surface them.
    secrets = [S.secret_dict(s, users) for s in secrets_q if not _is_system_secret(s)]

    variables_q = session.exec(select(Variable)).all()
    if user.role != "admin":
        variables_q = [v for v in variables_q if v.scope == "global" or v.owner_id == user.id]
    variables = [S.variable_dict(v, users) for v in variables_q]

    conn_list = list(conns.values())
    is_admin = user.role == "admin"
    connections = []
    for c in conn_list:
        st = None
        px = px_cache.get(c.id)
        if px:
            try:
                v = px.version()
                st = {"status": "online", "version": v.get("version", "—")}
            except Exception:  # noqa: BLE001
                st = {"status": "offline"}
        # Non-admins get a REDACTED connection (target name + node + sizing only) — never
        # the Proxmox host / token id / SSH paths / storage backends, which are admin-only
        # config. They still need this much to pick a build/deploy target and size a VM.
        connections.append(S.connection_dict(session, c, st) if is_admin
                           else S.connection_public_dict(session, c, st))

    networks = S.network_dicts(session, conn_list, public=not is_admin)
    # The full user directory (names, emails, roles, last-login) is admin-only — a
    # non-admin never needs it (their own identity comes from `me`) and it must not leak.
    users_list = [S.user_dict(session, u) for u in users.values()] if is_admin else []

    jobs_q = select(Job).where(Job.dismissed == False).order_by(Job.id.desc()).limit(20)  # noqa: E712
    if user.role != "admin":
        jobs_q = select(Job).where(Job.dismissed == False,  # noqa: E712
                                   Job.created_by == user.id).order_by(Job.id.desc()).limit(20)
    jobs = [S.job_brief(session, j) for j in session.exec(jobs_q).all()]

    return {
        "me": S.me_dict(user),
        "csrf": ensure_csrf(request),
        "limits": {"maxCores": settings.max_cores, "maxRam": settings.max_ram_mb // 1024,
                   "maxDisk": settings.max_disk_gb,  # 0 = no global cap
                   "vmidMin": settings.vmid_min, "vmidMax": settings.vmid_max},
        "VMS": vms,
        "BASE_IMAGES": base,
        "TEMPLATES": templates,
        "PALETTE": blocks,
        "SECRETS": secrets,
        "VARIABLES": variables,
        "CONNECTIONS": connections,
        "NETWORKS": networks,
        "USERS": users_list,
        "JOBS": jobs,
    }


# --------------------------------------------------------------------------- #
# Homepage widget — read-only, API-key-authed summary counts                   #
# --------------------------------------------------------------------------- #
@router.get("/widget/summary")
def widget_summary(user: User = Depends(widget_key_user),
                   session: Session = Depends(get_session)):
    """Counts for the Homepage ``customapi`` widget.

    Authenticated by the per-user widget API key (``X-API-Key``), tenant-scoped
    like ``/state`` (a non-admin sees only their own VMs/jobs), and PROBE-FREE:
    every number is read from SQLite — this frequently-polled path never calls
    Proxmox.
    """
    is_admin = user.role == "admin"

    vm_q = select(Deployment.status)
    if not is_admin:
        vm_q = vm_q.where(Deployment.owner_id == user.id)
    statuses = list(session.exec(vm_q).all())

    def _count(*names: str) -> int:
        return sum(1 for st in statuses if st in names)

    job_q = select(Job.id).where(Job.status.in_(("queued", "running")))
    if not is_admin:
        job_q = job_q.where(Job.created_by == user.id)
    jobs_active = len(session.exec(job_q).all())

    tpl_q = select(Template).where(
        Template.base_image_id.is_not(None),
        Template.connection_id.is_not(None))
    if not is_admin:
        # tenant-scope like /state: a non-admin only sees public + own templates
        tpl_q = tpl_q.where(or_(Template.public == True,  # noqa: E712
                                Template.owner_id == user.id))
    tpl_rows = session.exec(tpl_q).all()

    return {
        "vms_total": len(statuses),
        "vms_running": _count("running"),
        "vms_stopped": _count("stopped"),
        "vms_working": _count("working"),
        "vms_error": _count("error"),
        "jobs_active": jobs_active,
        "templates": len(tpl_rows),
    }


# --------------------------------------------------------------------------- #
# deployments (VMs)                                                            #
# --------------------------------------------------------------------------- #
class DeployBody(BaseModel):
    templateId: int                    # the template to deploy from (required)
    deployInputs: dict = {}            # {"<si>.<bi>": {"<input>": value}} for ask-flagged inputs
    name: Optional[str] = None
    cpu: Optional[int] = Field(default=None, ge=1, le=256)     # None → template default
    ram: Optional[int] = Field(default=None, ge=1, le=1024)    # GB
    disk: Optional[int] = Field(default=None, ge=1, le=16384)  # GB
    tags: str = ""
    notes: str = ""


def _validate_deploy_inputs(session: Session, tpl: Template, supplied: dict) -> str:
    """Validate ask-on-deploy answers against the template. Only inputs the
    template flags `ask` may be overridden (tamper-proof); ask-flagged
    text/secret inputs MUST end up non-empty (answer or stored value).
    Returns canonical JSON to persist on the deployment."""
    recipe = load_recipe(tpl.recipe_json)
    allowed = ask_map(recipe)
    supplied = supplied or {}
    for addr in supplied:
        if addr not in allowed:
            raise HTTPException(400, f"deployInputs: unknown block address {addr!r}")
    blocks = {b.key: b for b in session.exec(select(Block)).all()}
    cleaned: dict[str, dict] = {}
    for addr, names in allowed.items():
        si, bi = (int(x) for x in addr.split("."))
        placed = recipe[si]["blocks"][bi]
        blk = blocks.get(placed.get("ref"))
        try:
            schema = json.loads(blk.input_schema_json or "[]") if blk else []
        except (json.JSONDecodeError, TypeError):
            schema = []
        ftypes = {f.get("name"): f.get("type", "text") for f in schema if isinstance(f, dict)}
        answers = supplied.get(addr) or {}
        if not isinstance(answers, dict):
            raise HTTPException(400, f"deployInputs: {addr!r} must be an object")
        for name in answers:
            if name not in names:
                raise HTTPException(400, f"deployInputs: {name!r} is not ask-on-deploy")
        out = {}
        for name in names:
            if name not in ftypes:
                continue  # ask references an input the block no longer has — ignore
            ftype = ftypes.get(name, "text")
            if name in answers:
                v = answers[name]
                if ftype in ("bool", "boolean", "toggle") and not isinstance(v, bool):
                    raise HTTPException(400, f"deployInputs: {name!r} must be a boolean")
                if ftype in ("tags", "list") and not isinstance(v, list):
                    raise HTTPException(400, f"deployInputs: {name!r} must be a list")
                if ftype not in ("bool", "boolean", "toggle", "tags", "list") and not isinstance(v, str):
                    raise HTTPException(400, f"deployInputs: {name!r} must be a string")
                # Reject control chars / newlines in non-code answers: a multi-line scalar
                # could inject sibling keys into the rendered module-arg dict. 'code' (Run
                # Script) is intentionally free-form shell on the deployer's own VM.
                if isinstance(v, str) and ftype != "code" and _has_control_chars(v):
                    raise HTTPException(400, f"deployInputs: {name!r} must not contain control characters or newlines")
                if ftype in ("text", "secret", "password") and not v.strip():
                    raise HTTPException(400, f"template requires input {name!r}")
                out[name] = v
            elif ftype in ("text", "secret", "password"):
                # unanswered: the template's stored value must carry it
                stored = (placed.get("inputs") or {}).get(name)
                if not (stored and str(stored).strip()):
                    raise HTTPException(400, f"template requires input {name!r}")
        if out:
            cleaned[addr] = out
    return json.dumps(cleaned)


def _auto_name(session: Session, base: str = "gd") -> str:
    n = 1
    existing = {d.name for d in session.exec(select(Deployment)).all()}
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


@router.post("/deployments")
def deploy(body: DeployBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    _enforce_quota(session, user, "vm")
    tpl = session.get(Template, body.templateId)
    if not tpl or not (tpl.public or tpl.owner_id == user.id or user.role == "admin"):
        raise HTTPException(404, "template not found")
    base = session.get(Image, tpl.base_image_id) if tpl.base_image_id else None
    if not base or base.kind != "base":
        raise HTTPException(400, "template has no base image — edit it first")
    conn = session.get(Connection, tpl.connection_id) if tpl.connection_id else None
    if not conn:
        raise HTTPException(400, "template has no location — edit it and pick a Proxmox connection")

    deploy_inputs_json = _validate_deploy_inputs(session, tpl, body.deployInputs)

    # The connection's per-VM ceiling is authoritative; 0 = unlimited for that
    # dimension (CPU, RAM and disk all behave identically). A connection is required
    # above, so settings.max_* only act as a no-connection fallback.
    cap_cores = conn.max_cores if conn else settings.max_cores
    cap_ram_mb = conn.max_ram_mb if conn else settings.max_ram_mb
    cap_disk_gb = conn.max_disk_gb if conn else settings.max_disk_gb
    cpu_req = body.cpu if body.cpu is not None else tpl.default_cpu
    ram_req = body.ram if body.ram is not None else tpl.default_ram
    disk_req = body.disk if body.disk is not None else tpl.default_disk
    cpu = max(1, min(cpu_req, cap_cores) if cap_cores else cpu_req)
    ram = max(1, min(ram_req, cap_ram_mb // 1024) if cap_ram_mb else ram_req)
    disk = max(1, min(disk_req, cap_disk_gb) if cap_disk_gb else disk_req)
    name = _clean_name(body.name) if (body.name or "").strip() else _auto_name(session)

    net = session.get(Network, tpl.network_id) if tpl.network_id else None
    if net and net.connection_id != conn.id:
        net = None
    if not net:
        net = default_network_for(session, conn, user.id)

    dep = Deployment(name=name, owner_id=user.id, connection_id=conn.id,
                     image_id=base.id, template_id=tpl.id, cpu=cpu, ram=ram,
                     disk=disk, status="working", node=conn.node,
                     network_id=net.id, tags=body.tags, notes=body.notes,
                     deploy_inputs_json=deploy_inputs_json)
    session.add(dep)
    session.commit()
    session.refresh(dep)

    job = Job(type="deploy", title=f"Deploying {name}", deployment_id=dep.id,
              connection_id=conn.id, created_by=user.id, status="queued",
              context_json=_build_job_ctx(session, base, cpu, ram, disk, net, dep.id))
    session.add(job)
    record_audit(session, user, "deploy", "deployment", dep.id, name)
    session.commit()
    session.refresh(job)
    statebus.bump()
    return {"ok": True, "jobId": job.id, "depId": dep.id}


class ActionBody(BaseModel):
    action: str  # start | stop | restart


@router.post("/deployments/{dep_id}/action")
def vm_action(dep_id: int, body: ActionBody, user: User = Depends(current_user),
              session: Session = Depends(get_session)):
    dep = _owned_deployment(session, dep_id, user)
    conn = session.get(Connection, dep.connection_id)
    if not conn or not dep.vmid:
        raise HTTPException(400, "VM not provisioned")
    px = Proxmox(conn)
    node = dep.node or conn.node
    try:
        if body.action == "start":
            px.start(dep.vmid, node)
        elif body.action == "stop":
            px.stop(dep.vmid, node)
        elif body.action == "restart":
            px.reboot(dep.vmid, node)
        else:
            raise HTTPException(400, "unknown action")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"proxmox: {e}")
    record_audit(session, user, f"vm.{body.action}", "deployment", dep.id, dep.name)
    session.commit()
    statebus.bump()
    return {"ok": True}


@router.post("/deployments/{dep_id}/rebuild")
def vm_rebuild(dep_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    dep = _owned_deployment(session, dep_id, user)
    if not dep.template_id:
        raise HTTPException(400, "legacy VM — it predates templates; redeploy it from a template")
    tpl = session.get(Template, dep.template_id)
    base = session.get(Image, tpl.base_image_id) if tpl and tpl.base_image_id else None
    if not base or base.kind != "base":
        raise HTTPException(400, "template has no base image — edit it first")
    dep.status = "working"
    session.add(dep)
    # Preserve the VM's network identity (static IP / VLAN) across the rebuild.
    # The existing IpAllocation row is reused by allocate_ip() inside _network_ctx —
    # it looks up the deployment_id and returns the same IP, so no new address is
    # allocated and the VM keeps its reserved static IP after the rebuild.
    net = session.get(Network, dep.network_id) if dep.network_id else None
    job = Job(type="rebuild", title=f"Rebuilding {dep.name}", deployment_id=dep.id,
              connection_id=dep.connection_id, created_by=user.id, status="queued",
              context_json=_build_job_ctx(session, base, dep.cpu, dep.ram, dep.disk, net, dep.id))
    session.add(job)
    record_audit(session, user, "vm.rebuild", "deployment", dep.id, dep.name)
    session.commit()
    session.refresh(job)
    statebus.bump()
    return {"ok": True, "jobId": job.id}


@router.delete("/deployments/{dep_id}")
def vm_destroy(dep_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    dep = _owned_deployment(session, dep_id, user)
    dep.status = "working"
    session.add(dep)
    job = Job(type="destroy", title=f"Destroying {dep.name}", deployment_id=dep.id,
              connection_id=dep.connection_id, created_by=user.id, status="queued",
              context_json="{}")
    session.add(job)
    record_audit(session, user, "vm.destroy", "deployment", dep.id, dep.name)
    session.commit()
    session.refresh(job)
    statebus.bump()
    return {"ok": True, "jobId": job.id}


def _iface_summary(ifaces) -> list:
    out = []
    for i in ifaces or []:
        name = i.get("name", "")
        if name in ("lo", "lo0"):
            continue
        ips = [a.get("ip-address", "") for a in (i.get("ip-addresses") or [])
               if a.get("ip-address") and not a["ip-address"].startswith(("127.", "::1", "fe80"))]
        out.append({"name": name, "mac": i.get("hardware-address", ""), "ips": ips})
    return out


@router.get("/vms/{dep_id}/detail")
def vm_detail(dep_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    dep = _owned_deployment(session, dep_id, user)
    conn = session.get(Connection, dep.connection_id)
    img = session.get(Image, dep.image_id) if dep.image_id else None
    tpl = session.get(Template, dep.template_id) if dep.template_id else None
    owner = session.get(User, dep.owner_id) if dep.owner_id else None
    job = session.exec(select(Job).where(Job.deployment_id == dep.id).order_by(Job.id.desc())).first()
    out = {
        "name": dep.name, "vmid": dep.vmid,
        "node": dep.node or (conn.node if conn else ""),
        "status": dep.status, "ip": dep.ip, "mac": dep.mac, "tags": dep.tags,
        "created": S._rel(dep.created_at), "owner": owner.name if owner else "—",
        "connection": conn.name if conn else "—",
        "baseImage": img.name if img else "—", "template": tpl.name if tpl else None,
        "templateId": dep.template_id,
        "os": img.os_family if img else "generic",
        "reqCpu": dep.cpu, "reqRam": dep.ram, "reqDisk": dep.disk,
        "jobId": job.id if job else None,
        "live": None, "config": None, "agent": None, "consoleReady": False,
        "hasRootPassword": bool(dep.root_password_enc),
        "credUser": dep.cred_user or "root",
    }
    if conn and dep.vmid and dep.status not in ("working", "error"):
        try:
            px = Proxmox(conn)
            node = dep.node or conn.node or px.pick_node()
            cur = px.vm_current(dep.vmid, node)
            running = cur.get("status") == "running"
            out["live"] = {
                "status": cur.get("status", "unknown"), "uptime": int(cur.get("uptime") or 0),
                "cpuPct": round((cur.get("cpu") or 0) * 100, 1),
                "memUsed": int(cur.get("mem") or 0), "memMax": int(cur.get("maxmem") or 0),
                "diskUsed": int(cur.get("disk") or 0), "diskMax": int(cur.get("maxdisk") or 0),
                "agentRunning": bool(cur.get("agent")),
            }
            cfg = px.vm_config(dep.vmid, node)
            out["config"] = {
                "cores": cfg.get("cores"), "memoryMb": cfg.get("memory"),
                "ostype": cfg.get("ostype", ""), "net0": cfg.get("net0", ""),
                "scsi0": cfg.get("scsi0", ""),
                "agent": cfg.get("agent", ""), "serial0": cfg.get("serial0", ""),
            }
            out["consoleReady"] = running
            if running and cur.get("agent"):
                out["agent"] = {"os": px.agent_osinfo(dep.vmid, node),
                                "interfaces": _iface_summary(px.agent_interfaces(dep.vmid, node))}
        except Exception:  # noqa: BLE001
            pass
    return out


# --------------------------------------------------------------------------- #
# snapshots                                                                     #
# --------------------------------------------------------------------------- #
_SNAPNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")


class SnapshotBody(BaseModel):
    name: str = ""
    description: str = ""
    includeRam: bool = False


def _snapshot_px(session: Session, dep: Deployment) -> tuple[Proxmox, str]:
    conn = session.get(Connection, dep.connection_id)
    if not conn or not dep.vmid:
        raise HTTPException(400, "VM not provisioned")
    px = Proxmox(conn)
    return px, dep.node or conn.node or px.pick_node()


@router.get("/vms/{dep_id}/snapshots")
def list_vm_snapshots(dep_id: int, user: User = Depends(current_user),
                      session: Session = Depends(get_session)):
    dep = _owned_deployment(session, dep_id, user)
    px, node = _snapshot_px(session, dep)
    try:
        raw = px.list_snapshots(dep.vmid, node)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"proxmox: {e}")
    # Proxmox includes a synthetic 'current' entry whose parent is the snapshot the
    # VM currently sits on — surface that as a "current" flag instead of a row.
    on_snap = next((s.get("parent") for s in raw if s.get("name") == "current"), None)
    snaps = [{
        "name": s.get("name", ""),
        "description": (s.get("description") or "").strip(),
        "created": S._rel(datetime.fromtimestamp(s["snaptime"], tz=timezone.utc)) if s.get("snaptime") else "—",
        "snaptime": s.get("snaptime") or 0,
        "vmstate": bool(s.get("vmstate")),
        "current": s.get("name") == on_snap,
    } for s in raw if s.get("name") != "current"]
    snaps.sort(key=lambda s: s["snaptime"], reverse=True)
    return {"snapshots": snaps}


@router.post("/vms/{dep_id}/snapshots")
def create_vm_snapshot(dep_id: int, body: SnapshotBody, user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    dep = _owned_deployment(session, dep_id, user)
    name = (body.name or "").strip() or "snap-" + utcnow().strftime("%Y%m%d-%H%M%S")
    if not _SNAPNAME_RE.fullmatch(name):
        raise HTTPException(400, "snapshot name must start with a letter and contain only "
                                 "letters, digits, '-' or '_' (max 40 chars)")
    px, node = _snapshot_px(session, dep)
    try:
        upid = px.create_snapshot(dep.vmid, name, description=(body.description or "")[:200],
                                  vmstate=body.includeRam, node=node)
        px.wait_task(upid, node=node, timeout=300)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"proxmox: {e}")
    record_audit(session, user, "vm.snapshot.create", "deployment", dep.id,
                 f"{dep.name} · {name}")
    session.commit()
    statebus.bump()
    return {"ok": True, "name": name}


@router.delete("/vms/{dep_id}/snapshots/{snapname}")
def delete_vm_snapshot(dep_id: int, snapname: str, user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    dep = _owned_deployment(session, dep_id, user)
    if not _SNAPNAME_RE.fullmatch(snapname or ""):
        raise HTTPException(400, "invalid snapshot name")
    px, node = _snapshot_px(session, dep)
    try:
        upid = px.delete_snapshot(dep.vmid, snapname, node=node)
        px.wait_task(upid, node=node, timeout=300)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"proxmox: {e}")
    record_audit(session, user, "vm.snapshot.delete", "deployment", dep.id,
                 f"{dep.name} · {snapname}")
    session.commit()
    statebus.bump()
    return {"ok": True}


@router.post("/vms/{dep_id}/snapshots/{snapname}/rollback")
def rollback_vm_snapshot(dep_id: int, snapname: str, user: User = Depends(current_user),
                         session: Session = Depends(get_session)):
    dep = _owned_deployment(session, dep_id, user)
    if not _SNAPNAME_RE.fullmatch(snapname or ""):
        raise HTTPException(400, "invalid snapshot name")
    px, node = _snapshot_px(session, dep)
    try:
        upid = px.rollback_snapshot(dep.vmid, snapname, node=node)
        px.wait_task(upid, node=node, timeout=300)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"proxmox: {e}")
    record_audit(session, user, "vm.snapshot.rollback", "deployment", dep.id,
                 f"{dep.name} · {snapname}")
    session.commit()
    statebus.bump()
    return {"ok": True}


def _ws_origin_ok(ws: WebSocket) -> bool:
    """Reject cross-site WebSocket handshakes (CSWSH defense-in-depth beyond SameSite).
    A browser always sends Origin on a WS upgrade; allow only same-origin (Origin host
    == Host) or an explicitly allow-listed origin (GOBLINDOCK_CORS). A missing Origin
    (a non-browser client, which can't be driven cross-site by a victim) is allowed."""
    origin = ws.headers.get("origin")
    if not origin:
        return True
    try:
        o = urlparse(origin).netloc.lower()
    except ValueError:
        return False
    if not o:
        return False
    host = (ws.headers.get("host") or "").lower()
    if o == host:
        return True
    allow = {a.rstrip("/").lower() for a in settings.cors_origins}
    return origin.rstrip("/").lower() in allow


async def _pump_ws(websocket: WebSocket, pve, prefer_bytes: bool) -> None:
    """Pipe frames both ways between the browser WS and the Proxmox WS until either
    side closes. `prefer_bytes` picks which field of a Starlette message wins when
    both are present (the serial console is text-first, VNC is binary-first).
    Shared by vm_console and vm_vnc."""

    async def browser_to_pve():
        first, second = ("bytes", "text") if prefer_bytes else ("text", "bytes")
        try:
            while True:
                m = await websocket.receive()
                if m.get("type") == "websocket.disconnect":
                    break
                payload = m.get(first) if m.get(first) is not None else m.get(second)
                if payload is not None:
                    await pve.send(payload)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                await pve.close()
            except Exception:  # noqa: BLE001
                pass

    async def pve_to_browser():
        try:
            async for data in pve:
                if isinstance(data, (bytes, bytearray)):
                    await websocket.send_bytes(bytes(data))
                else:
                    await websocket.send_text(data)
        except Exception:  # noqa: BLE001
            pass

    await asyncio.gather(browser_to_pve(), pve_to_browser())


async def _ws_authorized_dep(websocket: WebSocket, dep_id: int):
    """Shared console-WS handshake guard: origin check + session auth + deployment
    ownership, all BEFORE accepting the upgrade — an unauthorized or cross-site peer
    is rejected at the HTTP layer and never completes the upgrade. Returns a detached
    (conn, dep) pair, or (None, None) after closing the socket."""
    if not _ws_origin_ok(websocket):
        await websocket.close(code=4403)
        return None, None
    uid = websocket.session.get("uid")
    with Session(engine) as s:
        user = s.get(User, uid) if uid else None
        dep = s.get(Deployment, dep_id)
        if (not user or user.disabled or websocket.session.get("sv", 0) != user.session_epoch
                or not dep or (dep.owner_id != user.id and user.role != "admin")):
            await websocket.close(code=4403)
            return None, None
        c = s.get(Connection, dep.connection_id)
        conn = Connection(**c.model_dump()) if c else None
        dep = Deployment(**dep.model_dump())
    if not conn:
        await websocket.close(code=4404)
        return None, None
    return conn, dep


async def _accept_binary(websocket: WebSocket) -> None:
    subs = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol="binary" if "binary" in subs else None)


def _pve_ws_kwargs(px: Proxmox, conn: Connection) -> dict:
    """Common kwargs for the server-side websocket to Proxmox (token auth + TLS)."""
    import ssl as _ssl
    ctx = _ssl.create_default_context()
    if not conn.verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
    return {"additional_headers": {"Authorization": px.token_auth_header()},
            "ssl": ctx, "subprotocols": ["binary"], "max_size": None, "open_timeout": 15}


@router.websocket("/vms/{dep_id}/console")
async def vm_console(websocket: WebSocket, dep_id: int):
    """Bridge the browser's xterm to the VM's serial console. We open a Proxmox
    termproxy (authenticated with our API token, kept server-side) and pipe bytes;
    the browser only ever talks to GoblinDock."""
    conn, dep = await _ws_authorized_dep(websocket, dep_id)
    if not conn:
        return
    if not dep.vmid:
        await websocket.close(code=4403)
        return
    vmid, node = dep.vmid, dep.node or conn.node
    await _accept_binary(websocket)

    import websockets as _ws

    try:
        px = Proxmox(conn)
        node = node or px.pick_node()
        px.ensure_serial(vmid, node)
        tp = px.termproxy(vmid, node)
        ticket, port, puser = tp.get("ticket"), tp.get("port"), tp.get("user")
        async with _ws.connect(
            px.console_ws_url(vmid, node, port, ticket), **_pve_ws_kwargs(px, conn),
        ) as pve:
            await pve.send(f"{puser}:{ticket}\n")
            await _pump_ws(websocket, pve, prefer_bytes=False)
    except Exception as e:  # noqa: BLE001
        try:
            await websocket.send_text(f"\r\n[goblindock] console unavailable: {e}\r\n")
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


# Graphical (noVNC) console: a short-lived vncproxy session is created over HTTP and
# then consumed by the websocket. noVNC needs the VNC ticket (as password) up front,
# so we hand it to the browser via the HTTP call and keep the same proxy session for
# the websocket (keyed by a one-time token).
_VNC_SESS: dict = {}


@router.post("/vms/{dep_id}/vncproxy")
def vm_vncproxy(dep_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    dep = _owned_deployment(session, dep_id, user)
    conn = session.get(Connection, dep.connection_id)
    if not conn or not dep.vmid:
        raise HTTPException(400, "VM not provisioned")
    px = Proxmox(conn)
    node = dep.node or conn.node or px.pick_node()
    try:
        r = px.vncproxy(dep.vmid, node)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"proxmox: {e}")
    import secrets as _secrets
    now = time.time()
    for k in [k for k, v in list(_VNC_SESS.items()) if v["exp"] < now]:
        _VNC_SESS.pop(k, None)
    tok = _secrets.token_urlsafe(24)
    _VNC_SESS[tok] = {"vmid": dep.vmid, "node": node, "port": r["port"],
                      "ticket": r["ticket"], "dep_id": dep_id, "exp": now + 30}
    return {"ticket": r["ticket"], "wsToken": tok}


@router.post("/vms/{dep_id}/credentials/reveal")
def reveal_vm_credentials(dep_id: int, response: Response,
                          user: User = Depends(current_user),
                          session: Session = Depends(get_session)):
    # POST (CSRF-protected) + audited + never cached — mirrors POST /secrets/{id}/reveal.
    dep = _owned_deployment(session, dep_id, user)
    if not dep.root_password_enc:
        raise HTTPException(404, "no stored password for this VM")
    # Decrypt BEFORE auditing so a decryption failure (key rotation / corruption) is
    # surfaced as a 500 and never recorded as a successful credential reveal.
    try:
        password = decrypt(dep.root_password_enc, strict=True)
    except ValueError:
        raise HTTPException(500, "credential decryption failed (key mismatch or corrupt ciphertext)")
    response.headers["Cache-Control"] = "no-store"
    record_audit(session, user, "vm.password.reveal", "deployment", dep.id, dep.name)
    session.commit()
    return {"user": dep.cred_user or "root", "password": password}


@router.websocket("/vms/{dep_id}/vnc")
async def vm_vnc(websocket: WebSocket, dep_id: int):
    tok = websocket.query_params.get("t")
    sess = _VNC_SESS.pop(tok, None) if tok else None
    conn, _dep = await _ws_authorized_dep(websocket, dep_id)
    if not conn:
        return
    if not sess or sess.get("dep_id") != dep_id or sess.get("exp", 0) < time.time():
        await websocket.close(code=4403)
        return
    await _accept_binary(websocket)

    import websockets as _ws

    try:
        px = Proxmox(conn)
        async with _ws.connect(
            px.console_ws_url(sess["vmid"], sess["node"], sess["port"], sess["ticket"]),
            **_pve_ws_kwargs(px, conn),
        ) as pve:
            await _pump_ws(websocket, pve, prefer_bytes=True)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# images                                                                        #
# --------------------------------------------------------------------------- #
class BaseImageBody(BaseModel):
    name: str
    os_family: str = "ubuntu"
    source_url: str
    checksum: str = ""


@router.post("/images/base")
def add_base_image(body: BaseImageBody, user: User = Depends(require_admin),
                   session: Session = Depends(get_session)):
    url = validate_image_url(body.source_url)
    img = Image(kind="base", name=body.name, os_family=body.os_family,
                source_url=url, checksum=body.checksum,
                build_status="ready", created_by=user.id, size="cloud image")
    session.add(img)
    record_audit(session, user, "image.add_base", "image", "-", body.name)
    session.commit()
    return {"ok": True}


@router.get("/images/cached")
def cached_images(connectionId: int, user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    """Which base images are already downloaded on the connection's image storage.
    One Proxmox listing per call; an unreachable node returns online=False with
    HTTP 200 (the ISOs page renders 'target offline', never an error toast)."""
    conn = session.get(Connection, connectionId)
    if not conn:
        raise HTTPException(404, "connection not found")
    bases = session.exec(select(Image).where(Image.kind == "base")).all()
    px = Proxmox(conn)
    try:
        vols = px.storage_volumes(node=conn.node or None)
    except Exception:  # noqa: BLE001 — unreachable node is an expected state
        return {"online": False, "cached": {}}
    cached = {}
    for img in bases:
        if not (img.source_url or "").strip():
            continue  # nothing to download — UI shows unknown
        cached[str(img.id)] = px.iso_volume_path(base_disk_filename(img.source_url)) in vols
    return {"online": True, "cached": cached}


class SyncBody(BaseModel):
    connectionId: int


@router.post("/images/{img_id}/sync")
def sync_image(img_id: int, body: SyncBody, user: User = Depends(current_user),
               session: Session = Depends(get_session)):
    """Pre-pull a base cloud image onto the connection's storage — the same
    cached download a deploy triggers, just ahead of time."""
    img = session.get(Image, img_id)
    if not img:
        raise HTTPException(404, "image not found")
    if img.kind != "base":
        raise HTTPException(400, "only base images can be synced")
    if not (img.source_url or "").strip():
        raise HTTPException(400, "image has no source URL")
    conn = session.get(Connection, body.connectionId)
    if not conn:
        raise HTTPException(404, "connection not found")
    job = Job(type="image_sync", title=f"Syncing {img.name} → {conn.name}",
              image_id=img.id, connection_id=conn.id, created_by=user.id, status="queued",
              context_json=json.dumps({"src_url": img.source_url,
                                       "checksum": img.checksum or "",
                                       "checksum_algorithm": _checksum_algo(img.checksum or "")}))
    session.add(job)
    record_audit(session, user, "image.sync", "image", img.id, img.name)
    session.commit()
    session.refresh(job)
    statebus.bump()
    return {"ok": True, "jobId": job.id}


# --------------------------------------------------------------------------- #
# templates                                                                    #
# --------------------------------------------------------------------------- #
class TemplateBody(BaseModel):
    name: str
    description: str = ""
    os_family: str = "ubuntu"
    recipe: list = []
    cpu: int = Field(default=1, ge=1, le=256)
    ram: int = Field(default=2, ge=1, le=1024)        # GB
    disk: int = Field(default=20, ge=1, le=16384)     # GB
    public: bool = False   # private by default — publishing is an explicit opt-in
    baseImageId: Optional[int] = None
    connectionId: Optional[int] = None
    networkId: Optional[int] = None


def _validate_template_refs(session: Session, body: TemplateBody) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Resolve baseImageId/connectionId/networkId or 400. The network must belong
    to the template's connection — deploys use them together."""
    bid = cid = nid = None
    if body.baseImageId is not None:
        img = session.get(Image, body.baseImageId)
        if not img or img.kind != "base":
            raise HTTPException(400, "baseImageId must reference a base image (ISO)")
        bid = img.id
    if body.connectionId is not None:
        conn = session.get(Connection, body.connectionId)
        if not conn:
            raise HTTPException(400, "connectionId not found")
        cid = conn.id
    if body.networkId is not None:
        if cid is None:
            raise HTTPException(400, "networkId requires connectionId")
        net = session.get(Network, body.networkId)
        if not net or net.connection_id != cid:
            raise HTTPException(400, "network does not belong to the template's connection")
        nid = net.id
    return bid, cid, nid


@router.post("/templates")
def save_template(body: TemplateBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    bid, cid, nid = _validate_template_refs(session, body)
    # Store the authored sizes verbatim. The per-VM ceiling is enforced at deploy
    # time from the connection (0 = unlimited), so a template default is never
    # silently shrunk to a cap here.
    rc = Template(name=body.name.strip() or "template", description=body.description,
                os_family=body.os_family, recipe_json=json.dumps(body.recipe or []),
                default_cpu=body.cpu, default_ram=body.ram, default_disk=body.disk,
                public=body.public, owner_id=user.id,
                base_image_id=bid, connection_id=cid, network_id=nid)
    session.add(rc)
    record_audit(session, user, "template.create", "template", "-", rc.name)
    session.commit()
    return {"ok": True}


def _template_owned(rc: Template, user: User) -> bool:
    return user.role == "admin" or rc.owner_id == user.id


@router.put("/templates/{rid}")
def edit_template_ep(rid: int, body: TemplateBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    rc = session.get(Template, rid)
    if not rc:
        raise HTTPException(404, "not found")
    if not _template_owned(rc, user):
        raise HTTPException(403, "not yours")
    rc.name = body.name.strip() or rc.name
    rc.description = body.description
    rc.os_family = body.os_family
    rc.recipe_json = json.dumps(body.recipe or [])
    rc.base_image_id, rc.connection_id, rc.network_id = _validate_template_refs(session, body)
    rc.default_cpu = body.cpu
    rc.default_ram = body.ram
    rc.default_disk = body.disk
    rc.public = body.public
    session.add(rc)
    record_audit(session, user, "template.update", "template", rc.id, rc.name)
    session.commit()
    return {"ok": True}


@router.delete("/templates/{rid}")
def delete_template_ep(rid: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    rc = session.get(Template, rid)
    if not rc:
        raise HTTPException(404, "not found")
    if not _template_owned(rc, user):
        raise HTTPException(403, "not yours")
    session.delete(rc)
    record_audit(session, user, "template.delete", "template", rid, rc.name)
    session.commit()
    return {"ok": True}


class CompileBody(BaseModel):
    recipe: list = []
    name: str = "template"


@router.post("/templates/compile")
def compile_template(body: CompileBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    # Resolve only blocks the caller may see (built-in or their own) — same visibility
    # rule as /state and /blocks — so a crafted recipe can't render another user's
    # private block template. Unknown/forbidden keys simply render as no-ops.
    blocks = {b.key: b for b in session.exec(select(Block)).all()
              if user.role == "admin" or b.builtin or b.owner_id == user.id}
    recipe = body.recipe or []
    # PREVIEW-ONLY masking: password-typed inputs must not appear in the YAML
    # preview (the worker compiles the real values at deploy time).
    masked = json.loads(json.dumps(recipe))
    for sec in masked:
        for b in (sec.get("blocks") or []) if isinstance(sec, dict) else []:
            if not isinstance(b, dict):
                continue
            blk = blocks.get(b.get("ref"))
            if not blk:
                continue
            try:
                schema = json.loads(blk.input_schema_json or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            pw_fields = {f.get("name") for f in schema if isinstance(f, dict) and f.get("type") == "password"}
            inputs = b.get("inputs") or {}
            for name in pw_fields:
                if inputs.get(name):
                    inputs[name] = "********"
            b["inputs"] = inputs
    yaml = compile_playbook(masked, blocks, body.name)
    return {"yaml": yaml}


# --------------------------------------------------------------------------- #
# secrets                                                                        #
# --------------------------------------------------------------------------- #
class SecretBody(BaseModel):
    name: str
    value: str
    scope: str = "global"  # global | user


@router.post("/secrets")
def add_secret(body: SecretBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    scope = body.scope if body.scope in ("global", "user") else "global"
    if scope == "global" and user.role != "admin":
        scope = "user"
    name = _clean_ref_name(body.name, "secret name")
    if name.startswith(_SYSTEM_SECRET_PREFIX):
        raise HTTPException(400, f"the {_SYSTEM_SECRET_PREFIX!r} name prefix is reserved for app-managed secrets")
    owner = user.id if scope == "user" else None
    # Reject a duplicate (scope, owner, name) so resolution stays deterministic — two
    # same-named secrets in a scope would make the {{ secrets.NAME }} lookup ambiguous.
    if session.exec(select(Secret).where(Secret.name == name, Secret.scope == scope,
                                         Secret.owner_id == owner)).first():
        raise HTTPException(409, f"a {scope} secret named {name!r} already exists")
    sec = Secret(name=name, value_enc=encrypt(body.value), scope=scope,
                 owner_id=owner, created_by=user.id)
    session.add(sec)
    record_audit(session, user, "secret.create", "secret", "-", name)
    session.commit()
    return {"ok": True}


@router.delete("/secrets/{sec_id}")
def del_secret(sec_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    sec = session.get(Secret, sec_id)
    if not sec:
        raise HTTPException(404, "not found")
    if _is_system_secret(sec):
        raise HTTPException(403, "app-managed secret cannot be deleted")
    if not _scoped_owned(sec, user):
        raise HTTPException(403, "not yours")
    session.delete(sec)
    record_audit(session, user, "secret.delete", "secret", sec_id, sec.name)
    session.commit()
    return {"ok": True}


@router.post("/secrets/{sec_id}/reveal")
def reveal_secret(sec_id: int, response: Response, user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    # POST (not GET): mutating-method CSRF protection applies, and the plaintext can't
    # be triggered by a stray link / cached as a GET. Audited; never cached.
    sec = session.get(Secret, sec_id)
    if not sec:
        raise HTTPException(404, "not found")
    if _is_system_secret(sec):
        raise HTTPException(403, "app-managed secret cannot be revealed")
    if not _scoped_owned(sec, user):
        raise HTTPException(403, "not yours")
    # Decrypt BEFORE auditing so a decrypt failure (key mismatch / corruption) surfaces
    # as a clear 500 and is not recorded as a successful reveal — and never returned as
    # a misleading empty value with a 200.
    try:
        value = decrypt(sec.value_enc, strict=True)
    except ValueError:
        raise HTTPException(500, "secret decryption failed (key mismatch or corrupt ciphertext)")
    response.headers["Cache-Control"] = "no-store"
    record_audit(session, user, "secret.reveal", "secret", sec.id, sec.name)
    session.commit()
    users = {u.id: u for u in session.exec(select(User)).all()}
    return {**S.secret_dict(sec, users), "val": value}


# --------------------------------------------------------------------------- #
# variables (like secrets, but plaintext + visible; referenced as variable.NAME) #
# --------------------------------------------------------------------------- #
class VariableBody(BaseModel):
    name: str
    value: str = ""
    scope: str = "global"  # global | user


@router.post("/variables")
def add_variable(body: VariableBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    scope = body.scope if body.scope in ("global", "user") else "global"
    if scope == "global" and user.role != "admin":
        scope = "user"
    name = _clean_ref_name(body.name, "variable name")
    owner = user.id if scope == "user" else None
    if session.exec(select(Variable).where(Variable.name == name, Variable.scope == scope,
                                           Variable.owner_id == owner)).first():
        raise HTTPException(409, f"a {scope} variable named {name!r} already exists")
    v = Variable(name=name, value=body.value, scope=scope,
                 owner_id=owner, created_by=user.id)
    session.add(v)
    record_audit(session, user, "variable.create", "variable", "-", v.name)
    session.commit()
    return {"ok": True}


@router.put("/variables/{var_id}")
def edit_variable(var_id: int, body: VariableBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    v = session.get(Variable, var_id)
    if not v:
        raise HTTPException(404, "not found")
    if not _scoped_owned(v, user):
        raise HTTPException(403, "not yours")
    if body.name is not None and body.name.strip():
        newname = _clean_ref_name(body.name, "variable name")
        if newname != v.name and session.exec(select(Variable).where(
                Variable.name == newname, Variable.scope == v.scope,
                Variable.owner_id == v.owner_id, Variable.id != v.id)).first():
            raise HTTPException(409, f"a {v.scope} variable named {newname!r} already exists")
        v.name = newname
    v.value = body.value
    session.add(v)
    record_audit(session, user, "variable.update", "variable", v.id, v.name)
    session.commit()
    return {"ok": True}


@router.delete("/variables/{var_id}")
def del_variable(var_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    v = session.get(Variable, var_id)
    if not v:
        raise HTTPException(404, "not found")
    if not _scoped_owned(v, user):
        raise HTTPException(403, "not yours")
    session.delete(v)
    record_audit(session, user, "variable.delete", "variable", v.id, v.name)
    session.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# connections (admin)                                                          #
# --------------------------------------------------------------------------- #
class ConnBody(BaseModel):
    name: str
    host: str
    port: int = 8006
    token_id: str
    token_secret: str
    verify_tls: bool = True   # verify the Proxmox TLS cert by default; opt out for self-signed
    node: str = ""
    storage: str = ""
    iso_storage: str = "local"
    snippet_storage: str = "local"
    bridge: str = "vmbr0"
    ssh_host: str = ""
    ssh_user: str = "root"
    ssh_key_path: str = ""
    max_cores: int = 0       # per-VM ceilings for this target (0 = inherit global)
    max_ram_gb: int = 0
    max_disk_gb: int = 0


@router.post("/connections")
def add_connection(body: ConnBody, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    c = Connection(name=body.name, host=body.host, port=body.port, token_id=body.token_id,
                   token_secret_enc=encrypt(body.token_secret), verify_tls=body.verify_tls,
                   node=_clean_storage_id(body.node, "node"),
                   storage=_clean_storage_id(body.storage, "storage"),
                   iso_storage=_clean_storage_id(body.iso_storage, "iso storage"),
                   snippet_storage=_clean_storage_id(body.snippet_storage, "snippet storage"),
                   bridge=body.bridge,
                   ssh_host=body.ssh_host, ssh_user=body.ssh_user,
                   ssh_key_path=body.ssh_key_path,
                   max_cores=max(0, body.max_cores), max_ram_mb=max(0, body.max_ram_gb) * 1024,
                   max_disk_gb=max(0, body.max_disk_gb), created_by=user.id)
    session.add(c)
    record_audit(session, user, "connection.create", "connection", "-", c.name)
    session.commit()
    session.refresh(c)
    # give it a default DHCP network now (so GET /state never has to write one)
    default_network_for(session, c, user.id)
    return {"ok": True}


@router.post("/connections/{conn_id}/test")
def test_connection(conn_id: int, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    c = session.get(Connection, conn_id)
    if not c:
        raise HTTPException(404, "not found")
    try:
        px = Proxmox(c)
        v = px.version()
        ns = px.nodes()
        return {"ok": True, "status": "online", "version": v.get("version", "—"),
                "nodes": [n["node"] for n in ns if n.get("status") == "online"]}
    except Exception as e:  # noqa: BLE001
        # Don't echo the raw exception back (it can carry internal/connection detail) —
        # log it server-side and return a generic, actionable message to the admin UI.
        import logging
        logging.getLogger("goblindock").warning("connection test failed for %s: %s", c.name, e)
        return {"ok": False, "status": "offline",
                "error": "could not reach the Proxmox API — check the host, port, token and TLS settings"}


class ConnProbeBody(BaseModel):
    host: str
    port: int = 8006
    token_id: str = ""
    token_secret: Optional[str] = None
    verify_tls: Optional[bool] = None   # None = unset (reuse stored / secure default)
    conn_id: Optional[int] = None


@router.post("/connections/probe")
def probe_connection(body: ConnProbeBody, user: User = Depends(require_admin),
                     session: Session = Depends(get_session)):
    """Discover a Proxmox host's nodes / storages / bridges from the add/edit form
    BEFORE the connection is saved — so the admin picks values instead of typing them.

    Builds a TRANSIENT Connection (never added to the session). On an existing
    connection (conn_id given) a blank token_secret/token_id/verify_tls reuses what's
    already stored, so 'Load' works when editing without re-entering the secret.
    Errors are redacted: logged server-side, generic message returned."""
    import logging

    stored: Optional[Connection] = None
    if body.conn_id is not None:
        stored = session.get(Connection, body.conn_id)
        if not stored:
            raise HTTPException(404, "not found")

    token_id = body.token_id or (stored.token_id if stored else "")
    port = body.port or 8006
    # verify_tls: an explicit value (True/False) wins; unset (None) reuses the stored
    # connection's value, else falls back to the secure default. (Can't use falsy-ness
    # to detect "unset" now that the default is True.)
    if body.verify_tls is not None:
        verify_tls = body.verify_tls
    elif stored:
        verify_tls = stored.verify_tls
    else:
        verify_tls = True
    if stored:
        if not body.token_id:
            token_id = stored.token_id
        if not body.port:
            port = stored.port or 8006

    # Fresh secret supplied → encrypt it; otherwise reuse the stored encrypted blob.
    if body.token_secret:
        token_secret_enc = encrypt(body.token_secret)
    elif stored:
        token_secret_enc = stored.token_secret_enc
    else:
        token_secret_enc = encrypt("")

    transient = Connection(
        name="(probe)", host=body.host, port=port, token_id=token_id,
        token_secret_enc=token_secret_enc, verify_tls=verify_tls,
    )

    try:
        px = Proxmox(transient)
        version = px.version().get("version")
        all_nodes = px.nodes()
        nodes = [n["node"] for n in all_nodes if n.get("status") == "online"]
        if not nodes:
            nodes = [n["node"] for n in all_nodes if n.get("node")]
        if not nodes:
            raise RuntimeError("no nodes returned")
        node = nodes[0]
        storages = []
        for s in px.storage_status(node):
            content = s.get("content", "")
            storages.append({
                "name": s.get("storage"),
                "content": content.split(",") if content else [],
                "type": s.get("type"),
                "freeGb": int(s.get("avail", 0)) // (1024 ** 3),
            })
        bridges = px.bridges(node)
        return {"ok": True, "version": version, "nodes": nodes,
                "storages": storages, "bridges": bridges}
    except Exception as e:  # noqa: BLE001
        logging.getLogger("goblindock").warning(
            "connection probe failed for host %s: %s", body.host, e)
        return {"ok": False,
                "error": "could not reach the Proxmox API — check the host, port, token and TLS settings"}


# --------------------------------------------------------------------------- #
# node capacity (deploy-modal headroom + connection gauge)                     #
# --------------------------------------------------------------------------- #
_CAPACITY_CACHE: dict[int, tuple[float, dict]] = {}
_CAPACITY_TTL = 30.0  # seconds — bounds Proxmox probing under repeated opens


def _gb(n) -> int:
    try:
        return int(n) // (1024 ** 3)
    except (TypeError, ValueError):
        return 0


def _probe_capacity(conn: Connection) -> dict:
    try:
        px = Proxmox(conn)
        node = px.pick_node()
        st = px.node_status(node)
        mem = st.get("memory", {}) or {}
        total, used = int(mem.get("total", 0)), int(mem.get("used", 0))
        cores = int((st.get("cpuinfo", {}) or {}).get("cpus", 0))
        cpu_used_pct = round(float(st.get("cpu", 0)) * 100)
        stores, deploy_store = [], None
        for s in px.storage_status(node):
            entry = {"name": s.get("storage", ""), "totalGb": _gb(s.get("total")),
                     "usedGb": _gb(s.get("used")), "freeGb": _gb(s.get("avail"))}
            stores.append(entry)
            if s.get("storage") == conn.storage:
                deploy_store = entry
        if deploy_store is None and stores:
            deploy_store = stores[0]
        return {
            "online": True, "node": node,
            "cpu": {"cores": cores, "usedPct": cpu_used_pct},
            "mem": {"totalGb": _gb(total), "usedGb": _gb(used), "freeGb": _gb(total - used)},
            "storage": deploy_store or {"name": conn.storage or "", "totalGb": 0,
                                        "usedGb": 0, "freeGb": 0},
            "stores": stores,
        }
    except Exception:  # noqa: BLE001 — unreachable node is an expected state
        return {"online": False}


@router.get("/connections/{conn_id}/capacity")
def connection_capacity(conn_id: int, user: User = Depends(current_user),
                        session: Session = Depends(get_session)):
    conn = session.get(Connection, conn_id)
    if not conn:
        raise HTTPException(404, "not found")
    now = time.monotonic()
    cached = _CAPACITY_CACHE.get(conn_id)
    if cached and now - cached[0] < _CAPACITY_TTL:
        data = cached[1]
    else:
        data = _probe_capacity(conn)
        _CAPACITY_CACHE[conn_id] = (now, data)
    if not data.get("online"):
        return {"online": False}
    if user.role != "admin":
        # non-admins get node headroom + deploy-store free SPACE only — never the
        # full store list, and never a storage backend NAME (connection_public_dict
        # withholds storage backends from non-admins). Copy `storage` so we never
        # mutate the shared cached object.
        data = {k: v for k, v in data.items() if k != "stores"}
        data["storage"] = {**data["storage"], "name": ""}
    return data


# --------------------------------------------------------------------------- #
# users (admin)                                                                 #
# --------------------------------------------------------------------------- #
class UserBody(BaseModel):
    name: str
    email: str
    password: str
    role: str = "user"


@router.post("/users")
def add_user(body: UserBody, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    if session.exec(select(User).where(User.email == body.email.strip().lower())).first():
        raise HTTPException(400, "email already exists")
    _check_password(body.password)
    u = User(email=body.email.strip().lower(), name=body.name.strip(),
             password_hash=hash_password(body.password),
             role="admin" if body.role == "admin" else "user")
    session.add(u)
    record_audit(session, user, "user.create", "user", "-", u.email)
    session.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# jobs + SSE                                                                     #
# --------------------------------------------------------------------------- #
@router.get("/jobs")
def list_jobs(user: User = Depends(current_user), session: Session = Depends(get_session)):
    q = select(Job).where(Job.dismissed == False).order_by(Job.id.desc()).limit(30)  # noqa: E712
    if user.role != "admin":
        q = select(Job).where(Job.dismissed == False, Job.created_by == user.id  # noqa: E712
                              ).order_by(Job.id.desc()).limit(30)
    return [S.job_brief(session, j) for j in session.exec(q).all()]


@router.get("/jobs/history")
def jobs_history(user: User = Depends(current_user), session: Session = Depends(get_session)):
    """ALL jobs, newest first — a persistent, auto-populated log (like Audit, but for
    deploy/start/stop/destroy/sync). NOT gated by dismissal: dismissing only hides a job
    from the activity bell; it always remains here until pruned (retention) or purged."""
    q = select(Job).order_by(Job.id.desc()).limit(200)
    if user.role != "admin":
        q = select(Job).where(Job.created_by == user.id).order_by(Job.id.desc()).limit(200)
    return [S.job_brief(session, j) for j in session.exec(q).all()]


@router.get("/jobs/{job_id}")
def get_job(job_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "not found")
    if not _job_owned(job, user):
        raise HTTPException(403, "not your job")
    return S.job_detail(session, job, viewer=user)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "not found")
    if not _job_owned(job, user):
        raise HTTPException(403, "not your job")
    if job.status in ("queued", "running"):
        job.cancel_requested = True
        session.add(job)
        session.commit()
    return {"ok": True}


def _purge_job(session: Session, job: Job) -> None:
    from .models import JobStep
    for s in session.exec(select(JobStep).where(JobStep.job_id == job.id)).all():
        session.delete(s)
    for e in session.exec(select(JobEvent).where(JobEvent.job_id == job.id)).all():
        session.delete(e)
    session.delete(job)


def prune_old_jobs() -> int:
    """Permanently purge FINISHED jobs older than the configured retention window.
    Retention is the UI-set `job_retention_days` (0 = keep forever). Called daily by the
    scheduler. History shows all jobs, so this — not dismissal — is what bounds the log."""
    from . import appsettings
    days = appsettings.get_job_retention_days()
    if days <= 0:
        return 0  # retention off → keep forever, like the audit log
    cutoff = utcnow() - timedelta(days=days)
    n = 0
    with Session(engine) as session:
        rows = session.exec(select(Job).where(
            Job.status.in_(["succeeded", "failed", "canceled"]),
            Job.finished_at.is_not(None),     # retention is measured from when a job ENDED
            Job.finished_at < cutoff,
        )).all()
        for job in rows:
            _purge_job(session, job)
            n += 1
        session.commit()
    return n


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "not found")
    if not _job_owned(job, user):
        raise HTTPException(403, "not your job")
    if job.status in ("queued", "running"):
        raise HTTPException(409, "job is still running — cancel it first")
    job.dismissed = True
    job.dismissed_at = utcnow()
    session.add(job)
    session.commit()
    statebus.bump()
    return {"ok": True}


@router.post("/jobs/clear")
def clear_jobs(user: User = Depends(current_user), session: Session = Depends(get_session)):
    q = select(Job).where(Job.status.in_(["succeeded", "failed", "canceled"]),
                          Job.dismissed == False)  # noqa: E712
    if user.role != "admin":
        q = select(Job).where(Job.status.in_(["succeeded", "failed", "canceled"]),
                              Job.dismissed == False,  # noqa: E712
                              Job.created_by == user.id)
    n = 0
    for job in session.exec(q).all():
        job.dismissed = True
        job.dismissed_at = utcnow()
        session.add(job)
        n += 1
    session.commit()
    statebus.bump()
    return {"ok": True, "cleared": n}


@router.delete("/jobs/{job_id}/purge")
def purge_job_permanently(job_id: int, user: User = Depends(current_user),
                          session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "not found")
    if not _job_owned(job, user):
        raise HTTPException(403, "not your job")
    if job.status in ("queued", "running"):
        raise HTTPException(409, "job is still running — cancel it first")
    _purge_job(session, job)
    session.commit()
    return {"ok": True}


@router.post("/jobs/purge-all")
def purge_all_jobs(user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Hard-delete every FINISHED job the viewer can see (admin = all jobs, else own) plus
    their steps/logs. Running/queued jobs are left untouched."""
    q = select(Job).where(Job.status.in_(["succeeded", "failed", "canceled"]))
    if user.role != "admin":
        q = q.where(Job.created_by == user.id)
    n = 0
    for job in session.exec(q).all():
        _purge_job(session, job)
        n += 1
    session.commit()
    statebus.bump()
    return {"ok": True, "purged": n}


class JobRetentionBody(BaseModel):
    days: int = Field(ge=0, le=3650)   # 0 = keep forever (no auto-prune)


@router.get("/settings/job-retention")
def get_job_retention(user: User = Depends(current_user)):
    from . import appsettings
    return {"days": appsettings.get_job_retention_days()}


@router.put("/settings/job-retention")
def set_job_retention(body: JobRetentionBody, user: User = Depends(require_admin)):
    from . import appsettings
    appsettings.set_setting(appsettings.JOB_RETENTION_DAYS, str(body.days))
    return {"ok": True, "days": body.days}


class AutoRootPwBody(BaseModel):
    enabled: bool


@router.get("/settings/auto-root-password")
def get_auto_root_password(user: User = Depends(current_user)):
    from . import appsettings
    return {"enabled": appsettings.auto_root_password_enabled()}


@router.put("/settings/auto-root-password")
def set_auto_root_password(body: AutoRootPwBody, user: User = Depends(require_admin)):
    from . import appsettings
    appsettings.set_setting(appsettings.AUTO_ROOT_PASSWORD, "1" if body.enabled else "0")
    return {"ok": True, "enabled": body.enabled}


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: int, request: Request, user: User = Depends(current_user)):
    """SSE: emit a job snapshot whenever steps/logs/progress change."""
    # Authenticate + authorise up front (the generator below runs unattended).
    with Session(engine) as s0:
        job0 = s0.get(Job, job_id)
        if not job0:
            raise HTTPException(404, "not found")
        if not _job_owned(job0, user):
            raise HTTPException(403, "not your job")

    async def gen():
        last_event_id = 0
        last_sig = None
        first = True
        while True:
            if await request.is_disconnected():
                break
            with Session(engine) as session:
                job = session.get(Job, job_id)
                if not job:
                    yield "event: error\ndata: {}\n\n"
                    break
                # Only load+send the FULL log on the first frame; afterwards send just
                # the new lines so each tick is O(new), not O(total) — the client
                # appends `newLogs` to what it already has (see web/job.js).
                detail = S.job_detail(session, job, include_log=first, viewer=user)
                if first:
                    last_event_id = detail.get("lastEventId", 0)
                    new_logs = []
                else:
                    new_logs = session.exec(
                        select(JobEvent).where(JobEvent.job_id == job_id,
                                               JobEvent.kind == "log",
                                               JobEvent.id > last_event_id).order_by(JobEvent.id)
                    ).all()
                    if new_logs:
                        last_event_id = new_logs[-1].id
                sig = (detail["pct"], detail["phase"], detail["rawStatus"],
                       tuple((s["name"], s["state"]) for s in detail["steps"]),
                       last_event_id)
            if sig != last_sig or first:
                last_sig = sig
                if first:
                    payload = {**detail, "newLogs": []}   # full log already included
                    first = False
                else:
                    payload = {k: v for k, v in detail.items() if k != "log"}
                    payload["newLogs"] = [{"cls": e.log_class, "text": e.line} for e in new_logs]
                yield f"data: {json.dumps(payload)}\n\n"
            if detail["rawStatus"] in ("succeeded", "failed", "canceled"):
                yield "event: done\ndata: {}\n\n"
                break
            await asyncio.sleep(0.8)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# SSE live-push tuning: poll the in-memory statebus once a second; after this many
# quiet ticks (~25s) emit a keepalive comment so proxies don't drop an idle stream.
_SSE_POLL_SECONDS = 1
_SSE_KEEPALIVE_TICKS = 25


@router.get("/state/stream")
async def state_stream(request: Request, user: User = Depends(current_user)):
    """SSE: emit a tiny version ping whenever app state changes. web/app.js
    refetches /api/state on each ping. No tenant data crosses the wire — the
    refetch is already scoped per user."""
    async def gen():
        last = -1
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            v = statebus.version()
            if v != last:
                last = v
                yield f"event: state\ndata: {json.dumps({'v': v})}\n\n"
                idle = 0
            else:
                idle += 1
                if idle >= _SSE_KEEPALIVE_TICKS:   # ~25s quiet → keepalive so proxies don't drop us
                    idle = 0
                    yield ": keepalive\n\n"
            await asyncio.sleep(_SSE_POLL_SECONDS)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# =========================================================================== #
# CRUD: profile                                                               #
# =========================================================================== #
class ProfileBody(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None


class PasswordChangeBody(BaseModel):
    current: str
    new: str


@router.put("/profile")
def update_profile(body: ProfileBody, user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    u = session.get(User, user.id)
    if body.name is not None and body.name.strip():
        u.name = body.name.strip()
    if body.email is not None and body.email.strip():
        email = body.email.strip().lower()
        clash = session.exec(select(User).where(User.email == email, User.id != u.id)).first()
        if clash:
            raise HTTPException(400, "email already in use")
        u.email = email
    session.add(u)
    record_audit(session, user, "profile.update", "user", u.id)
    session.commit()
    return {"ok": True, "me": S.me_dict(u)}


@router.post("/profile/password")
def change_password(body: PasswordChangeBody, request: Request,
                    user: User = Depends(current_user), session: Session = Depends(get_session)):
    u = session.get(User, user.id)
    if not verify_password(body.current, u.password_hash):
        raise HTTPException(403, "current password is incorrect")
    _check_password(body.new)
    u.password_hash = hash_password(body.new)
    u.session_epoch = (u.session_epoch or 0) + 1   # revoke other existing sessions
    session.add(u)
    record_audit(session, user, "profile.password", "user", u.id)
    session.commit()
    # keep THIS session valid by re-stamping it with the new epoch
    request.session["sv"] = u.session_epoch
    return {"ok": True}


@router.post("/profile/widget-key")
def gen_widget_key(user: User = Depends(current_user),
                   session: Session = Depends(get_session)):
    """Generate (or regenerate) this user's Homepage widget key.

    Returns the plaintext token EXACTLY once — only its sha256 hash is stored, so
    it can never be shown again. Regenerating invalidates the previous key."""
    token = new_widget_key()
    u = session.get(User, user.id)
    u.widget_key_hash = hash_widget_key(token)
    u.widget_key_prefix = token[:9]
    u.widget_key_created_at = utcnow()
    u.widget_key_last_used = None
    session.add(u)
    record_audit(session, user, "profile.widget_key.generate", "user", u.id)
    session.commit()
    return {"ok": True, "key": token, "prefix": u.widget_key_prefix}


@router.delete("/profile/widget-key")
def revoke_widget_key(user: User = Depends(current_user),
                      session: Session = Depends(get_session)):
    """Revoke this user's widget key — the token immediately stops working."""
    u = session.get(User, user.id)
    u.widget_key_hash = None
    u.widget_key_prefix = ""
    u.widget_key_created_at = None
    u.widget_key_last_used = None
    session.add(u)
    record_audit(session, user, "profile.widget_key.revoke", "user", u.id)
    session.commit()
    return {"ok": True}


# =========================================================================== #
# CRUD: connections (admin)                                                   #
# =========================================================================== #
class ConnEditBody(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    token_id: Optional[str] = None
    token_secret: Optional[str] = None
    verify_tls: Optional[bool] = None
    node: Optional[str] = None
    storage: Optional[str] = None
    iso_storage: Optional[str] = None
    snippet_storage: Optional[str] = None
    bridge: Optional[str] = None
    ssh_host: Optional[str] = None
    ssh_user: Optional[str] = None
    ssh_key_path: Optional[str] = None
    max_cores: Optional[int] = None
    max_ram_gb: Optional[int] = None
    max_disk_gb: Optional[int] = None


@router.put("/connections/{conn_id}")
def edit_connection(conn_id: int, body: ConnEditBody, user: User = Depends(require_admin),
                    session: Session = Depends(get_session)):
    c = session.get(Connection, conn_id)
    if not c:
        raise HTTPException(404, "not found")
    data = body.model_dump(exclude_unset=True)
    if "token_secret" in data:
        secret = data.pop("token_secret")
        if secret:
            c.token_secret_enc = encrypt(secret)
    if "max_ram_gb" in data:                  # body is GB, model stores MB
        gb = data.pop("max_ram_gb")
        if gb is not None:
            c.max_ram_mb = max(0, gb) * 1024
    for fld in ("node", "storage", "iso_storage", "snippet_storage"):
        if data.get(fld) is not None:
            data[fld] = _clean_storage_id(data[fld], fld.replace("_", " "))
    for k, v in data.items():
        if v is not None:                      # 0 is allowed (resets to inherit global)
            setattr(c, k, v)
    session.add(c)
    record_audit(session, user, "connection.update", "connection", c.id, c.name)
    session.commit()
    return {"ok": True}


@router.delete("/connections/{conn_id}")
def delete_connection(conn_id: int, user: User = Depends(require_admin),
                      session: Session = Depends(get_session)):
    c = session.get(Connection, conn_id)
    if not c:
        raise HTTPException(404, "not found")
    if session.exec(select(Deployment).where(Deployment.connection_id == conn_id)).first():
        raise HTTPException(409, "connection still has deployments — destroy them first")
    if session.exec(select(Image).where(Image.connection_id == conn_id, Image.kind == "golden")).first():
        raise HTTPException(409, "connection still has golden images — delete them first")
    for n in session.exec(select(Network).where(Network.connection_id == conn_id)).all():
        session.delete(n)
    session.delete(c)
    record_audit(session, user, "connection.delete", "connection", conn_id, c.name)
    session.commit()
    return {"ok": True}


# =========================================================================== #
# CRUD: networks (admin)                                                      #
# =========================================================================== #
class NetworkBody(BaseModel):
    connectionId: int
    name: str
    mode: str = "dhcp"
    bridge: str = "vmbr0"
    vlan: Optional[int] = None
    subnet_cidr: str = ""
    gateway: str = ""
    range_start: str = ""
    range_end: str = ""
    dns: str = ""


def _validate_network_body(body: NetworkBody) -> None:
    """Reject a malformed static network at write time, instead of silently degrading
    to DHCP at deploy time (allocate_ip swallows a bad range)."""
    if body.vlan is not None and not (1 <= body.vlan <= 4094):
        raise HTTPException(400, "vlan must be between 1 and 4094")

    def _ip(field: str, val: str):
        try:
            return ipaddress.ip_address(val)
        except ValueError:
            raise HTTPException(400, f"{field} must be a valid IP address")

    for d in (body.dns or "").replace(",", " ").split():
        _ip("dns", d)
    if body.mode != "static":
        return
    try:
        net = ipaddress.ip_network(body.subnet_cidr, strict=False)
    except ValueError:
        raise HTTPException(400, "a static network needs a valid subnet_cidr (e.g. 10.0.50.0/24)")
    if body.gateway and _ip("gateway", body.gateway) not in net:
        raise HTTPException(400, "gateway is outside the subnet")
    if body.range_start or body.range_end:
        start, end = _ip("range_start", body.range_start), _ip("range_end", body.range_end)
        if start > end:
            raise HTTPException(400, "range_start must be <= range_end")
        if start not in net or end not in net:
            raise HTTPException(400, "the IP range is outside the subnet")


@router.post("/networks")
def add_network(body: NetworkBody, user: User = Depends(require_admin),
                session: Session = Depends(get_session)):
    if not session.get(Connection, body.connectionId):
        raise HTTPException(400, "unknown connection")
    _validate_network_body(body)
    n = Network(connection_id=body.connectionId, name=body.name.strip(),
                mode="static" if body.mode == "static" else "dhcp", bridge=body.bridge,
                vlan=body.vlan, subnet_cidr=body.subnet_cidr, gateway=body.gateway,
                range_start=body.range_start, range_end=body.range_end, dns=body.dns,
                created_by=user.id)
    session.add(n)
    record_audit(session, user, "network.create", "network", "-", n.name)
    session.commit()
    return {"ok": True}


@router.put("/networks/{net_id}")
def edit_network(net_id: int, body: NetworkBody, user: User = Depends(require_admin),
                 session: Session = Depends(get_session)):
    n = session.get(Network, net_id)
    if not n:
        raise HTTPException(404, "not found")
    _validate_network_body(body)
    if body.connectionId != n.connection_id:
        if not session.get(Connection, body.connectionId):
            raise HTTPException(400, "unknown connection")
        if session.exec(select(Deployment).where(Deployment.network_id == net_id)).first():
            raise HTTPException(409, "network is in use by a deployment — can't move it to another connection")
        n.connection_id = body.connectionId
    n.name = body.name.strip()
    n.mode = "static" if body.mode == "static" else "dhcp"
    n.bridge = body.bridge
    n.vlan = body.vlan
    n.subnet_cidr = body.subnet_cidr
    n.gateway = body.gateway
    n.range_start = body.range_start
    n.range_end = body.range_end
    n.dns = body.dns
    session.add(n)
    record_audit(session, user, "network.update", "network", n.id, n.name)
    session.commit()
    return {"ok": True}


@router.delete("/networks/{net_id}")
def delete_network(net_id: int, user: User = Depends(require_admin),
                   session: Session = Depends(get_session)):
    n = session.get(Network, net_id)
    if not n:
        raise HTTPException(404, "not found")
    if session.exec(select(Deployment).where(Deployment.network_id == net_id)).first():
        raise HTTPException(409, "network is in use by a deployment")
    for a in session.exec(select(IpAllocation).where(IpAllocation.network_id == net_id)).all():
        session.delete(a)
    session.delete(n)
    record_audit(session, user, "network.delete", "network", net_id, n.name)
    session.commit()
    return {"ok": True}


# =========================================================================== #
# CRUD: users (admin)                                                         #
# =========================================================================== #
class UserEditBody(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    disabled: Optional[bool] = None


def _last_admin_guard(session: Session, target: User):
    if target.role == "admin":
        admins = session.exec(select(User).where(User.role == "admin", User.disabled == False)).all()  # noqa: E712
        if len(admins) <= 1:
            raise HTTPException(409, "cannot remove the last active admin")


@router.put("/users/{uid}")
def edit_user(uid: int, body: UserEditBody, user: User = Depends(require_admin),
              session: Session = Depends(get_session)):
    u = session.get(User, uid)
    if not u:
        raise HTTPException(404, "not found")
    if body.name is not None and body.name.strip():
        u.name = body.name.strip()
    if body.role is not None and body.role in ("admin", "user") and body.role != u.role:
        if u.role == "admin":
            _last_admin_guard(session, u)
        u.role = body.role
    if body.disabled is not None and body.disabled != u.disabled:
        if body.disabled and u.id == user.id:
            raise HTTPException(409, "you cannot disable yourself")
        if body.disabled:
            _last_admin_guard(session, u)
        u.disabled = body.disabled
    session.add(u)
    record_audit(session, user, "user.update", "user", u.id, u.email)
    session.commit()
    return {"ok": True}


class PasswordResetBody(BaseModel):
    value: str


@router.post("/users/{uid}/password")
def reset_user_password(uid: int, body: PasswordResetBody, request: Request,
                        user: User = Depends(require_admin),
                        session: Session = Depends(get_session)):
    u = session.get(User, uid)
    if not u:
        raise HTTPException(404, "not found")
    _check_password(body.value)
    u.password_hash = hash_password(body.value)
    u.session_epoch = (u.session_epoch or 0) + 1   # revoke the target user's sessions
    u.failed_logins = 0
    u.locked_until = None                          # an admin reset also clears a lockout
    session.add(u)
    record_audit(session, user, "user.password_reset", "user", u.id, u.email)
    session.commit()
    # if the admin reset their OWN password, keep this session alive with the new epoch
    if u.id == user.id:
        request.session["sv"] = u.session_epoch
    return {"ok": True}


@router.delete("/users/{uid}")
def delete_user(uid: int, user: User = Depends(require_admin),
                session: Session = Depends(get_session)):
    u = session.get(User, uid)
    if not u:
        raise HTTPException(404, "not found")
    if u.id == user.id:
        raise HTTPException(409, "you cannot delete yourself")
    _last_admin_guard(session, u)
    if session.exec(select(Deployment).where(Deployment.owner_id == uid)).first():
        raise HTTPException(409, "user still owns VMs — reassign or destroy them first")
    session.delete(u)
    record_audit(session, user, "user.delete", "user", uid, u.email)
    session.commit()
    return {"ok": True}


# =========================================================================== #
# CRUD: secrets (edit)                                                        #
# =========================================================================== #
class SecretEditBody(BaseModel):
    name: Optional[str] = None
    value: Optional[str] = None


@router.put("/secrets/{sec_id}")
def edit_secret(sec_id: int, body: SecretEditBody, user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    s = session.get(Secret, sec_id)
    if not s:
        raise HTTPException(404, "not found")
    if not _scoped_owned(s, user):
        raise HTTPException(403, "not yours")
    if body.name is not None and body.name.strip():
        newname = body.name.strip()
        if newname != s.name and session.exec(select(Secret).where(
                Secret.name == newname, Secret.scope == s.scope,
                Secret.owner_id == s.owner_id, Secret.id != s.id)).first():
            raise HTTPException(409, f"a {s.scope} secret named {newname!r} already exists")
        s.name = newname
    if body.value:
        s.value_enc = encrypt(body.value)
    session.add(s)
    record_audit(session, user, "secret.update", "secret", s.id, s.name)
    session.commit()
    return {"ok": True}


# =========================================================================== #
# CRUD: blocks (custom + fork)                                                #
# =========================================================================== #
class BlockBody(BaseModel):
    name: str
    category: str = "Custom"
    icon: str = "spark"
    section: str = "Scripts"
    phase: str = "ansible"          # cloudinit | ansible
    description: str = ""
    input_schema: list = []
    ansible_template: str = ""
    cloudinit_template: str = ""


def _new_block_key(session: Session) -> str:
    import secrets as _s
    while True:
        k = "c-" + _s.token_hex(4)
        if not session.exec(select(Block).where(Block.key == k)).first():
            return k


@router.post("/blocks")
def create_block(body: BlockBody, user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    problems = lint_block(body.phase, body.input_schema or [],
                          body.ansible_template, body.cloudinit_template)
    if problems:
        raise HTTPException(400, "Block validation failed: " + "; ".join(problems))
    b = Block(key=_new_block_key(session), kind="custom", builtin=False,
              name=body.name.strip() or "Custom block", category=body.category, icon=body.icon,
              section=body.section, phase="cloudinit" if body.phase == "cloudinit" else "ansible",
              description=body.description,
              input_schema_json=json.dumps(body.input_schema or []),
              ansible_template=body.ansible_template, cloudinit_template=body.cloudinit_template,
              owner_id=user.id)
    session.add(b)
    record_audit(session, user, "block.create", "block", "-", b.name)
    session.commit()
    return {"ok": True, "key": b.key}


@router.post("/blocks/{key}/fork")
def fork_block(key: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    src = session.exec(select(Block).where(Block.key == key)).first()
    if not src:
        raise HTTPException(404, "not found")
    # Only fork a block you can see (built-in or your own) — a private block must not be
    # copyable by key. 404 (not 403) so keys can't be probed.
    if not (src.builtin or src.owner_id == user.id or user.role == "admin"):
        raise HTTPException(404, "not found")
    b = Block(key=_new_block_key(session), kind="custom", builtin=False,
              name=src.name + " (copy)", category=src.category, icon=src.icon,
              section=src.section, phase=src.phase, description=src.description,
              input_schema_json=src.input_schema_json, ansible_template=src.ansible_template,
              cloudinit_template=src.cloudinit_template, owner_id=user.id)
    session.add(b)
    record_audit(session, user, "block.fork", "block", src.key, b.name)
    session.commit()
    return {"ok": True, "key": b.key}


@router.put("/blocks/{key}")
def edit_block(key: str, body: BlockBody, user: User = Depends(current_user),
               session: Session = Depends(get_session)):
    b = session.exec(select(Block).where(Block.key == key)).first()
    if not b:
        raise HTTPException(404, "not found")
    if b.builtin:
        raise HTTPException(403, "built-in blocks can't be edited — fork it first")
    if b.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "not yours")
    problems = lint_block(body.phase, body.input_schema or [],
                          body.ansible_template, body.cloudinit_template)
    if problems:
        raise HTTPException(400, "Block validation failed: " + "; ".join(problems))
    b.name = body.name.strip() or b.name
    b.category = body.category
    b.icon = body.icon
    b.section = body.section
    b.phase = "cloudinit" if body.phase == "cloudinit" else "ansible"
    b.description = body.description
    b.input_schema_json = json.dumps(body.input_schema or [])
    b.ansible_template = body.ansible_template
    b.cloudinit_template = body.cloudinit_template
    session.add(b)
    record_audit(session, user, "block.update", "block", b.key, b.name)
    session.commit()
    return {"ok": True}


@router.delete("/blocks/{key}")
def delete_block(key: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    b = session.exec(select(Block).where(Block.key == key)).first()
    if not b:
        raise HTTPException(404, "not found")
    if b.builtin:
        raise HTTPException(403, "built-in blocks can't be deleted")
    if b.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "not yours")
    session.delete(b)
    record_audit(session, user, "block.delete", "block", key, b.name)
    session.commit()
    return {"ok": True}


# =========================================================================== #
# CRUD: images (edit/delete base images)                                     #
# =========================================================================== #
class BaseImageEditBody(BaseModel):
    name: Optional[str] = None
    os_family: Optional[str] = None
    source_url: Optional[str] = None
    checksum: Optional[str] = None


@router.put("/images/{img_id}")
def edit_image(img_id: int, body: BaseImageEditBody, user: User = Depends(current_user),
               session: Session = Depends(get_session)):
    img = session.get(Image, img_id)
    if not img:
        raise HTTPException(404, "not found")
    # base images are admin-managed; golden images belong to their builder
    if img.kind == "base" and user.role != "admin":
        raise HTTPException(403, "admin only")
    if img.kind == "golden" and img.created_by != user.id and user.role != "admin":
        raise HTTPException(403, "not yours")
    if body.name is not None and body.name.strip():
        img.name = _clean_name(body.name, "image name")
    if body.os_family:
        img.os_family = body.os_family
    if body.source_url:
        # Same SSRF-via-node rationale: a raw download URL is admin-only.
        if user.role != "admin":
            raise HTTPException(403, "custom image URLs are admin-only")
        img.source_url = validate_image_url(body.source_url)
    if body.checksum is not None:
        img.checksum = body.checksum
    session.add(img)
    record_audit(session, user, "image.update", "image", img.id, img.name)
    session.commit()
    return {"ok": True}


@router.delete("/images/{img_id}")
def delete_image(img_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    img = session.get(Image, img_id)
    if not img:
        raise HTTPException(404, "not found")
    if img.kind == "golden" and img.created_by != user.id and user.role != "admin":
        raise HTTPException(403, "not yours")
    if img.kind == "base" and user.role != "admin":
        raise HTTPException(403, "admin only")
    if session.exec(select(Deployment).where(Deployment.image_id == img_id)).first():
        raise HTTPException(409, "image has deployed VMs — destroy them first")
    session.delete(img)
    record_audit(session, user, "image.delete", "image", img_id, img.name)
    session.commit()
    return {"ok": True}



# =========================================================================== #
# deployment metadata edit                                                    #
# =========================================================================== #
class DeploymentPatchBody(BaseModel):
    name: Optional[str] = None
    tags: Optional[str] = None
    notes: Optional[str] = None


@router.patch("/deployments/{dep_id}")
def patch_deployment(dep_id: int, body: DeploymentPatchBody, user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
    dep = _owned_deployment(session, dep_id, user)
    if body.name is not None and body.name.strip():
        dep.name = _clean_name(body.name)
    if body.tags is not None:
        dep.tags = body.tags
    if body.notes is not None:
        dep.notes = body.notes
    session.add(dep)
    session.commit()
    return {"ok": True}


# =========================================================================== #
# audit log (admin)                                                           #
# =========================================================================== #
@router.get("/audit")
def list_audit(q: str = "", limit: int = 100, offset: int = 0,
               user: User = Depends(require_admin), session: Session = Depends(get_session)):
    """Search + paged audit trail. `q` matches (case-insensitive) across user, action,
    target, detail and IP; `limit` is capped to bound the scan; newest first."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    rows_q = select(Audit)
    count_q = select(func.count(Audit.id))
    qn = (q or "").strip()
    if qn:
        like = f"%{qn}%"
        cond = or_(
            Audit.user_name.ilike(like), Audit.action.ilike(like),
            Audit.target_type.ilike(like), Audit.target_id.ilike(like),
            Audit.detail.ilike(like), Audit.ip.ilike(like),
        )
        rows_q = rows_q.where(cond)
        count_q = count_q.where(cond)
    total = session.exec(count_q).one()
    rows = session.exec(rows_q.order_by(Audit.id.desc()).limit(limit).offset(offset)).all()
    return {
        "rows": [{
            "id": a.id, "user": a.user_name, "action": a.action,
            "target": f"{a.target_type}:{a.target_id}", "detail": a.detail,
            "ip": a.ip, "ts": S._rel(a.ts),
        } for a in rows],
        "total": total, "limit": limit, "offset": offset,
    }


# =========================================================================== #
# admin: scheduled DB backups                                                 #
# =========================================================================== #
@router.get("/admin/backups")
def list_db_backups(user: User = Depends(require_admin)):
    return {
        "enabled": settings.backup_enabled,
        "intervalHours": settings.backup_interval_hours,
        "keep": settings.backup_keep,
        "dir": str(settings.backup_dir),
        "backups": backup.list_backups(),
    }


@router.post("/admin/backup")
def run_db_backup(user: User = Depends(require_admin), session: Session = Depends(get_session)):
    try:
        dest = backup.backup_now("manual")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"backup failed: {e}")
    record_audit(session, user, "db.backup", "system", dest.name, "manual backup")
    session.commit()
    return {"ok": True, "name": dest.name, "bytes": dest.stat().st_size}
