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
from datetime import timedelta, timezone
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
    utcnow,
)
from .proxmox import Proxmox
from .recipes import ask_map, compile_playbook, lint_block, load_recipe
from . import backup
from .security import (
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
    """Per-user resource cap (0 = unlimited; admins exempt). kind: 'vm' | 'image'."""
    if user.role == "admin":
        return
    if kind == "vm" and settings.max_vms_per_user:
        n = len(session.exec(select(Deployment).where(Deployment.owner_id == user.id)).all())
        if n >= settings.max_vms_per_user:
            raise HTTPException(429, f"VM quota reached ({settings.max_vms_per_user}) — "
                                     "destroy one first or ask an admin to raise your limit")
    if kind == "image" and settings.max_images_per_user:
        n = len(session.exec(select(Image).where(
            Image.kind == "golden", Image.created_by == user.id)).all())
        if n >= settings.max_images_per_user:
            raise HTTPException(429, f"golden-image quota reached ({settings.max_images_per_user})")


def validate_image_url(url: str) -> str:
    """SSRF guard for the cloud-image download URL: https only, every resolved
    address must be globally routable.

    Reachable by any authenticated user (build_golden accepts a raw source_url), so
    this is a real user->infra boundary. Residual risk we accept by design: the
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
    import time
    now = time.time()
    window = [t for t in _login_attempts.get(key, []) if now - t < 300]
    _login_attempts[key] = window
    if len(window) >= 8:
        raise HTTPException(429, "too many attempts — try again in a few minutes")


def _record_attempt(key: str) -> None:
    import time
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
        lu = user.locked_until
        if lu.tzinfo is None:
            lu = lu.replace(tzinfo=timezone.utc)
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
    golden = [S.golden_image_dict(session, i) for i in session.exec(select(Image).where(Image.kind == "golden").order_by(Image.id.desc())).all()]
    tpls = session.exec(select(Template).order_by(Template.id)).all()
    if user.role != "admin":
        tpls = [t for t in tpls if t.public or t.owner_id == user.id]
    templates = [S.template_dict(session, t) for t in tpls]
    blocks_all = session.exec(select(Block).order_by(Block.id)).all()
    if user.role != "admin":
        blocks_all = [b for b in blocks_all if b.builtin or b.owner_id == user.id]
    blocks = [S.block_dict(b) for b in blocks_all]

    secrets_q = session.exec(select(Secret)).all()
    if user.role != "admin":
        secrets_q = [s for s in secrets_q if s.scope == "global" or s.owner_id == user.id]
    secrets = [S.secret_dict(s, users) for s in secrets_q]

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

    jobs_q = select(Job).order_by(Job.id.desc()).limit(20)
    if user.role != "admin":
        jobs_q = select(Job).where(Job.created_by == user.id).order_by(Job.id.desc()).limit(20)
    jobs = [S.job_brief(session, j) for j in session.exec(jobs_q).all()]

    return {
        "me": S.me_dict(user),
        "csrf": ensure_csrf(request),
        "limits": {"maxCores": settings.max_cores, "maxRam": settings.max_ram_mb // 1024,
                   "vmidMin": settings.vmid_min, "vmidMax": settings.vmid_max},
        "VMS": vms,
        "BASE_IMAGES": base,
        "GOLDEN_IMAGES": golden,
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

    golden = session.exec(
        select(Image.id).where(Image.kind == "golden",
                               Image.template_vmid.is_not(None))
    ).all()

    return {
        "vms_total": len(statuses),
        "vms_running": _count("running"),
        "vms_stopped": _count("stopped"),
        "vms_working": _count("working"),
        "vms_error": _count("error"),
        "jobs_active": jobs_active,
        "golden_images": len(golden),
    }


# --------------------------------------------------------------------------- #
# deployments (VMs)                                                            #
# --------------------------------------------------------------------------- #
class DeployBody(BaseModel):
    goldenImageId: int                 # the golden image to clone from
    templateId: Optional[int] = None  # optional template applied on top
    deployInputs: dict = {}            # {"<si>.<bi>": {"<input>": value}} for ask-flagged inputs
    connectionId: Optional[int] = None
    networkId: Optional[int] = None
    name: Optional[str] = None
    cpu: int = Field(default=1, ge=1, le=256)
    ram: int = Field(default=2, ge=1, le=1024)        # GB
    disk: int = Field(default=20, ge=1, le=16384)     # GB
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
                if ftype == "bool" and not isinstance(v, bool):
                    raise HTTPException(400, f"deployInputs: {name!r} must be a boolean")
                if ftype in ("tags", "list") and not isinstance(v, list):
                    raise HTTPException(400, f"deployInputs: {name!r} must be a list")
                if ftype not in ("bool", "tags", "list") and not isinstance(v, str):
                    raise HTTPException(400, f"deployInputs: {name!r} must be a string")
                if ftype in ("text", "secret") and not v.strip():
                    raise HTTPException(400, f"template requires input {name!r}")
                out[name] = v
            elif ftype in ("text", "secret"):
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
    img = session.get(Image, body.goldenImageId)
    if not img or img.kind != "golden" or not img.template_vmid:
        raise HTTPException(400, "pick a built golden image to deploy from")

    # the golden template lives on a specific Proxmox — deploy there
    conn = session.get(Connection, img.connection_id) if img.connection_id else None
    if not conn:
        conn = session.exec(select(Connection)).first()
    if not conn:
        raise HTTPException(400, "no Proxmox connection configured")

    tpl = session.get(Template, body.templateId) if body.templateId else None
    if body.templateId and not tpl:
        raise HTTPException(404, "template not found")
    # Only a public template, your own, or (admin) any may be applied — a private template
    # must not be usable by id. 404 (not 403) so ids can't be enumerated.
    if tpl and not (tpl.public or tpl.owner_id == user.id or user.role == "admin"):
        raise HTTPException(404, "template not found")

    if body.deployInputs and not tpl:
        raise HTTPException(400, "deployInputs requires templateId")
    deploy_inputs_json = _validate_deploy_inputs(session, tpl, body.deployInputs) if tpl else "{}"

    # Cap to the target connection's per-VM ceilings (0 = inherit the global default).
    eff_cores = conn.max_cores or settings.max_cores
    eff_ram_mb = conn.max_ram_mb or settings.max_ram_mb
    eff_disk_gb = conn.max_disk_gb or settings.max_disk_gb
    cpu = max(1, min(body.cpu, eff_cores))
    ram = max(1, min(body.ram, eff_ram_mb // 1024))
    disk = max(1, min(body.disk, eff_disk_gb) if eff_disk_gb else body.disk)
    name = _clean_name(body.name) if (body.name or "").strip() else _auto_name(session)

    # resolve network (explicit, else the connection's default)
    net = session.get(Network, body.networkId) if body.networkId else None
    if net and net.connection_id != conn.id:
        net = None
    if not net:
        net = default_network_for(session, conn, user.id)

    dep = Deployment(name=name, owner_id=user.id, connection_id=conn.id,
                     image_id=img.id, template_id=(tpl.id if tpl else None), cpu=cpu, ram=ram,
                     disk=disk, status="working", node=img.node or conn.node,
                     network_id=net.id, tags=body.tags, notes=body.notes,
                     deploy_inputs_json=deploy_inputs_json)
    session.add(dep)
    session.commit()
    session.refresh(dep)

    # Pass the CLAMPED disk (not body.disk) so the per-target ceiling actually applies —
    # the worker resizes to and records cfg["disk"].
    ctx = {"src_vmid": img.template_vmid, "cpu": cpu, "ram": ram, "disk": disk}
    ctx.update(_network_ctx(session, net, dep.id))

    job = Job(type="deploy", title=f"Deploying {name}", deployment_id=dep.id,
              connection_id=conn.id, created_by=user.id, status="queued",
              context_json=json.dumps(ctx))
    session.add(job)
    record_audit(session, user, "deploy", "deployment", dep.id, name)
    session.commit()
    session.refresh(job)
    return {"ok": True, "jobId": job.id, "depId": dep.id}


class ActionBody(BaseModel):
    action: str  # start | stop | restart


@router.post("/deployments/{dep_id}/action")
def vm_action(dep_id: int, body: ActionBody, user: User = Depends(current_user),
              session: Session = Depends(get_session)):
    dep = session.get(Deployment, dep_id)
    if not dep:
        raise HTTPException(404, "not found")
    if dep.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "not your VM")
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
    return {"ok": True}


@router.post("/deployments/{dep_id}/rebuild")
def vm_rebuild(dep_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    dep = session.get(Deployment, dep_id)
    if not dep:
        raise HTTPException(404, "not found")
    if dep.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "not your VM")
    img = session.get(Image, dep.image_id) if dep.image_id else None
    src_vmid = img.template_vmid if img else None
    if not src_vmid:
        raise HTTPException(400, "source image missing")
    dep.status = "working"
    session.add(dep)
    # Preserve the VM's network (static IP / VLAN) across the rebuild instead of forcing
    # DHCP — rebuild a context like deploy does, reusing the existing IP reservation.
    net = session.get(Network, dep.network_id) if dep.network_id else None
    ctx = {"src_vmid": src_vmid, "cpu": dep.cpu, "ram": dep.ram, "disk": dep.disk}
    ctx.update(_network_ctx(session, net, dep.id) if net else {"network_mode": "dhcp"})
    job = Job(type="rebuild", title=f"Rebuilding {dep.name}", deployment_id=dep.id,
              connection_id=dep.connection_id, created_by=user.id, status="queued",
              context_json=json.dumps(ctx))
    session.add(job)
    session.commit()
    session.refresh(job)
    return {"ok": True, "jobId": job.id}


@router.delete("/deployments/{dep_id}")
def vm_destroy(dep_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    dep = session.get(Deployment, dep_id)
    if not dep:
        raise HTTPException(404, "not found")
    if dep.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "not your VM")
    dep.status = "working"
    session.add(dep)
    job = Job(type="destroy", title=f"Destroying {dep.name}", deployment_id=dep.id,
              connection_id=dep.connection_id, created_by=user.id, status="queued",
              context_json="{}")
    session.add(job)
    session.commit()
    session.refresh(job)
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
    dep = session.get(Deployment, dep_id)
    if not dep:
        raise HTTPException(404, "not found")
    if dep.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "not your VM")
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
        "golden": img.name if img else "—", "template": tpl.name if tpl else None,
        "os": img.os_family if img else "generic",
        "reqCpu": dep.cpu, "reqRam": dep.ram, "reqDisk": dep.disk,
        "jobId": job.id if job else None,
        "live": None, "config": None, "agent": None, "consoleReady": False,
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


@router.websocket("/vms/{dep_id}/console")
async def vm_console(websocket: WebSocket, dep_id: int):
    """Bridge the browser's xterm to the VM's serial console. We open a Proxmox
    termproxy (authenticated with our API token, kept server-side) and pipe bytes;
    the browser only ever talks to GoblinDock."""
    # Check origin + authn/authz BEFORE accepting the handshake — an unauthorized or
    # cross-site peer is rejected at the HTTP layer and never completes the upgrade.
    if not _ws_origin_ok(websocket):
        await websocket.close(code=4403)
        return
    uid = websocket.session.get("uid")
    with Session(engine) as s:
        user = s.get(User, uid) if uid else None
        dep = s.get(Deployment, dep_id)
        if (not user or user.disabled or websocket.session.get("sv", 0) != user.session_epoch
                or not dep or not dep.connection_id or not dep.vmid
                or (dep.owner_id != user.id and user.role != "admin")):
            await websocket.close(code=4403)
            return
        c = s.get(Connection, dep.connection_id)
        conn = Connection(**c.model_dump()) if c else None
        vmid, node = dep.vmid, dep.node or (c.node if c else "")
    if not conn:
        await websocket.close(code=4404)
        return
    _subs = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol="binary" if "binary" in _subs else None)

    import ssl as _ssl
    import websockets as _ws

    try:
        px = Proxmox(conn)
        node = node or px.pick_node()
        px.ensure_serial(vmid, node)
        tp = px.termproxy(vmid, node)
        ticket, port, puser = tp.get("ticket"), tp.get("port"), tp.get("user")
        ctx = _ssl.create_default_context()
        if not conn.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
        async with _ws.connect(
            px.console_ws_url(vmid, node, port, ticket),
            additional_headers={"Authorization": px.token_auth_header()},
            ssl=ctx, subprotocols=["binary"], max_size=None, open_timeout=15,
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
import time as _time
_VNC_SESS: dict = {}


@router.post("/vms/{dep_id}/vncproxy")
def vm_vncproxy(dep_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    dep = session.get(Deployment, dep_id)
    if not dep:
        raise HTTPException(404, "not found")
    if dep.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "not your VM")
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
    now = _time.time()
    for k in [k for k, v in list(_VNC_SESS.items()) if v["exp"] < now]:
        _VNC_SESS.pop(k, None)
    tok = _secrets.token_urlsafe(24)
    _VNC_SESS[tok] = {"vmid": dep.vmid, "node": node, "port": r["port"],
                      "ticket": r["ticket"], "dep_id": dep_id, "exp": now + 30}
    return {"ticket": r["ticket"], "wsToken": tok}


@router.websocket("/vms/{dep_id}/vnc")
async def vm_vnc(websocket: WebSocket, dep_id: int):
    if not _ws_origin_ok(websocket):
        await websocket.close(code=4403)
        return
    uid = websocket.session.get("uid")
    tok = websocket.query_params.get("t")
    sess = _VNC_SESS.pop(tok, None) if tok else None
    with Session(engine) as s:
        user = s.get(User, uid) if uid else None
        dep = s.get(Deployment, dep_id)
        if (not user or user.disabled or websocket.session.get("sv", 0) != user.session_epoch
                or not dep or not sess or sess.get("dep_id") != dep_id
                or sess.get("exp", 0) < _time.time()
                or (dep.owner_id != user.id and user.role != "admin")):
            await websocket.close(code=4403)
            return
        c = s.get(Connection, dep.connection_id)
        conn = Connection(**c.model_dump()) if c else None
    if not conn:
        await websocket.close(code=4404)
        return
    _subs = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol="binary" if "binary" in _subs else None)

    import ssl as _ssl
    import websockets as _ws

    try:
        px = Proxmox(conn)
        ctx = _ssl.create_default_context()
        if not conn.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
        async with _ws.connect(
            px.console_ws_url(sess["vmid"], sess["node"], sess["port"], sess["ticket"]),
            additional_headers={"Authorization": px.token_auth_header()},
            ssl=ctx, subprotocols=["binary"], max_size=None, open_timeout=15,
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


class GoldenBody(BaseModel):
    name: str
    os_family: str = "ubuntu"
    connectionId: Optional[int] = None   # location: which Proxmox
    node: Optional[str] = None           # location: which node
    storage: Optional[str] = None        # location: which VM-disk storage
    baseImageId: Optional[int] = None
    source_url: Optional[str] = None
    disk: int = Field(default=20, ge=1, le=16384)     # GB
    recipe: list = []


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


@router.post("/images/golden")
def build_golden(body: GoldenBody, user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    conn = (session.get(Connection, body.connectionId) if body.connectionId
            else session.exec(select(Connection)).first())
    if not conn:
        raise HTTPException(400, "no Proxmox connection configured")
    _enforce_quota(session, user, "image")
    name = _clean_name(body.name, "image name")
    # A raw, user-supplied download URL points the Proxmox NODE at an arbitrary host
    # (SSRF-via-node, redirect/rebind not fully closeable). Restrict that to admins;
    # non-admins must build from a vetted base image (admin-curated) or the default.
    base = session.get(Image, body.baseImageId) if body.baseImageId else None
    if base and base.kind != "base":     # only a base image is a valid build source
        raise HTTPException(400, "baseImageId must reference a base image")
    checksum = base.checksum if base else ""
    if body.source_url and user.role != "admin":
        raise HTTPException(403, "custom image URLs are admin-only — build from a base image")
    source_url = body.source_url
    if base and not source_url:
        source_url = base.source_url
    if not source_url:
        from .seed import UBUNTU_2404_URL
        source_url = UBUNTU_2404_URL
    source_url = validate_image_url(source_url)
    # Clamp the baked disk to the target ceiling (parity with the deploy path).
    eff_disk_gb = conn.max_disk_gb or settings.max_disk_gb
    disk_gb = max(1, min(body.disk, eff_disk_gb) if eff_disk_gb else body.disk)
    img = Image(kind="golden", name=name, os_family=body.os_family,
                connection_id=conn.id, node=body.node or conn.node,
                storage=body.storage or conn.storage,
                base_image_id=body.baseImageId, disk_gb=disk_gb, checksum=checksum,
                source_url=source_url, recipe_json=json.dumps(body.recipe or []),
                build_status="building", progress=0, created_by=user.id)
    session.add(img)
    session.commit()
    session.refresh(img)
    # Plumb the base image's checksum into the build so the node verifies integrity
    # on download (the F19 fix re-raises on a mismatch instead of swallowing it).
    job = Job(type="image_build", title=f"Building image: {img.name}", image_id=img.id,
              connection_id=conn.id, created_by=user.id, status="queued",
              context_json=json.dumps({"source_url": source_url, "disk": disk_gb,
                                        "checksum": checksum,
                                        "checksum_algorithm": _checksum_algo(checksum)}))
    session.add(job)
    record_audit(session, user, "image.build", "image", img.id, img.name)
    session.commit()
    session.refresh(job)
    return {"ok": True, "jobId": job.id, "imgId": img.id}


@router.post("/images/{img_id}/rebuild")
def rebuild_golden(img_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    img = session.get(Image, img_id)
    if not img or img.kind != "golden":
        raise HTTPException(404, "golden image not found")
    # Rebuild is a mutation (churns the node template, resets build_status) — same
    # owner/admin guard as edit_image/delete_image. Golden images are a shared catalog
    # anyone can DEPLOY from, but only the owner (or an admin) may rebuild one.
    if img.created_by != user.id and user.role != "admin":
        raise HTTPException(403, "not yours")
    conn = session.get(Connection, img.connection_id) if img.connection_id else session.exec(select(Connection)).first()
    if not conn:
        raise HTTPException(400, "no connection")
    img.build_status = "building"
    img.progress = 0
    session.add(img)
    job = Job(type="image_build", title=f"Rebuilding image: {img.name}", image_id=img.id,
              connection_id=conn.id, created_by=user.id, status="queued",
              context_json=json.dumps({"source_url": img.source_url, "disk": img.disk_gb or 20,
                                        "checksum": img.checksum or "",
                                        "checksum_algorithm": _checksum_algo(img.checksum or "")}))
    session.add(job)
    session.commit()
    session.refresh(job)
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
    public: bool = True
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
    rc = Template(name=body.name.strip() or "template", description=body.description,
                os_family=body.os_family, recipe_json=json.dumps(body.recipe or []),
                default_cpu=min(body.cpu, settings.max_cores),
                default_ram=min(body.ram, settings.max_ram_mb // 1024),
                default_disk=body.disk, public=body.public, owner_id=user.id,
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
    rc.default_cpu = min(body.cpu, settings.max_cores)
    rc.default_ram = min(body.ram, settings.max_ram_mb // 1024)
    rc.default_disk = body.disk
    rc.public = body.public
    rc.base_image_id, rc.connection_id, rc.network_id = _validate_template_refs(session, body)
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
    yaml = compile_playbook(body.recipe or [], blocks, body.name)
    return {"yaml": yaml}


# --------------------------------------------------------------------------- #
# blocks                                                                        #
# --------------------------------------------------------------------------- #
@router.get("/blocks")
def list_blocks(user: User = Depends(current_user), session: Session = Depends(get_session)):
    # Same visibility rule as /state: a non-admin only sees built-in blocks and
    # their own custom blocks — never another user's private blocks.
    blocks = session.exec(select(Block).order_by(Block.id)).all()
    if user.role != "admin":
        blocks = [b for b in blocks if b.builtin or b.owner_id == user.id]
    return [S.block_dict(b) for b in blocks]


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
    name = body.name.strip()
    owner = user.id if scope == "user" else None
    # Reject a duplicate (scope, owner, name) so resolution stays deterministic — two
    # same-named secrets in a scope would make the {{ secrets.NAME }} lookup ambiguous.
    if session.exec(select(Secret).where(Secret.name == name, Secret.scope == scope,
                                         Secret.owner_id == owner)).first():
        raise HTTPException(409, f"a {scope} secret named {name!r} already exists")
    sec = Secret(name=name, value_enc=encrypt(body.value), scope=scope,
                 owner_id=owner, created_by=user.id)
    session.add(sec)
    session.commit()
    return {"ok": True}


@router.delete("/secrets/{sec_id}")
def del_secret(sec_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    sec = session.get(Secret, sec_id)
    if not sec:
        raise HTTPException(404, "not found")
    if sec.scope == "global" and user.role != "admin":
        raise HTTPException(403, "admin only")
    if sec.scope == "user" and sec.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "not yours")
    session.delete(sec)
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
    if sec.scope == "global" and user.role != "admin":
        raise HTTPException(403, "admin only")
    if sec.scope == "user" and sec.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "not yours")
    response.headers["Cache-Control"] = "no-store"
    record_audit(session, user, "secret.reveal", "secret", sec.id, sec.name)
    session.commit()
    users, _ = _maps(session)
    return S.secret_dict(sec, users, reveal=True)


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
    name = body.name.strip()
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
        newname = body.name.strip()
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
    verify_tls: bool = False
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
                   node=body.node, storage=body.storage, iso_storage=body.iso_storage,
                   snippet_storage=body.snippet_storage, bridge=body.bridge,
                   ssh_host=body.ssh_host, ssh_user=body.ssh_user,
                   ssh_key_path=body.ssh_key_path,
                   max_cores=max(0, body.max_cores), max_ram_mb=max(0, body.max_ram_gb) * 1024,
                   max_disk_gb=max(0, body.max_disk_gb), created_by=user.id)
    session.add(c)
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
    q = select(Job).order_by(Job.id.desc()).limit(30)
    if user.role != "admin":
        q = select(Job).where(Job.created_by == user.id).order_by(Job.id.desc()).limit(30)
    return [S.job_brief(session, j) for j in session.exec(q).all()]


@router.get("/jobs/{job_id}")
def get_job(job_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, "not found")
    if not _job_owned(job, user):
        raise HTTPException(403, "not your job")
    return S.job_detail(session, job)


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


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
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


@router.post("/jobs/clear")
def clear_jobs(user: User = Depends(current_user), session: Session = Depends(get_session)):
    q = select(Job).where(Job.status.in_(["succeeded", "failed", "canceled"]))
    if user.role != "admin":
        q = select(Job).where(Job.status.in_(["succeeded", "failed", "canceled"]),
                              Job.created_by == user.id)
    n = 0
    for job in session.exec(q).all():
        _purge_job(session, job)
        n += 1
    session.commit()
    return {"ok": True, "cleared": n}


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
        idle = 0
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
                detail = S.job_detail(session, job, include_log=first)
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
                idle = 0
            else:
                idle += 1
            if detail["rawStatus"] in ("succeeded", "failed", "canceled"):
                yield "event: done\ndata: {}\n\n"
                break
            await asyncio.sleep(0.8)
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


@router.post("/users/{uid}/password")
def reset_user_password(uid: int, body: SecretBody, request: Request,
                        user: User = Depends(require_admin),
                        session: Session = Depends(get_session)):
    # reuse SecretBody.value as the new password (name ignored)
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
# CRUD: images (edit base, delete base+golden)                               #
# =========================================================================== #
class BaseImageEditBody(BaseModel):
    name: Optional[str] = None
    os_family: Optional[str] = None
    source_url: Optional[str] = None
    checksum: Optional[str] = None
    recipe: Optional[list] = None


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
        # Same SSRF-via-node rationale as build_golden: a raw download URL is admin-only.
        if user.role != "admin":
            raise HTTPException(403, "custom image URLs are admin-only")
        img.source_url = validate_image_url(body.source_url)
    if body.checksum is not None:
        img.checksum = body.checksum
    if body.recipe is not None:
        img.recipe_json = json.dumps(body.recipe)
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
        raise HTTPException(409, "golden image has deployed VMs — destroy them first")
    # Destroy the node template (best effort, guarded to our vmid window). Only for
    # a successfully-built golden, and only if no OTHER image row claims the same
    # vmid — a failed build can leave a stale vmid claim that a later successful
    # build reused, and we must not take out that live template.
    shared = None
    if img.template_vmid and img.connection_id:
        shared = session.exec(
            select(Image).where(
                Image.id != img.id,
                Image.connection_id == img.connection_id,
                Image.template_vmid == img.template_vmid,
            )
        ).first()
    if (img.kind == "golden" and img.build_status == "ready"
            and img.template_vmid and img.connection_id and shared is None):
        conn = session.get(Connection, img.connection_id)
        if conn:
            try:
                px = Proxmox(conn)
                node = img.node or conn.node or px.pick_node()
                px.stop(img.template_vmid, node)
            except Exception:  # noqa: BLE001
                pass
            try:
                upid = px.destroy(img.template_vmid, node)
                px.wait_task(upid, node=node, timeout=180)
            except Exception:  # noqa: BLE001
                pass
    session.delete(img)
    record_audit(session, user, "image.delete", "image", img_id, img.name)
    session.commit()
    return {"ok": True}


@router.get("/images/stale")
def stale_images(user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Read-only report of golden images that look stale — failed/incomplete builds, or
    ready templates that no deployment references. Pure DB analysis: NO Proxmox calls and
    nothing destructive. Cleanup is opt-in and reuses DELETE /api/images/{id}, so its
    409-on-deployments / RBAC / shared-vmid-template guards stay in force. Non-admins only
    see their OWN goldens; admins see all (with owner names)."""
    q = select(Image).where(Image.kind == "golden")
    if user.role != "admin":
        q = q.where(Image.created_by == user.id)
    unames = ({u.id: u.name for u in session.exec(select(User)).all()}
              if user.role == "admin" else {})
    out = []
    for img in session.exec(q.order_by(Image.id.desc())).all():
        deps = session.exec(
            select(func.count(Deployment.id)).where(Deployment.image_id == img.id)).one()
        if img.build_status == "failed":
            reason = "build failed"
        elif img.build_status == "none" and not img.template_vmid:
            reason = "never finished building"
        elif img.build_status == "ready" and deps == 0:
            reason = "no deployments use it"
        else:
            continue  # building/importing, or ready+in-use → not stale
        out.append({
            "imgId": img.id, "name": img.name, "reason": reason,
            "deployments": deps, "state": img.build_status, "vmid": img.template_vmid,
            "built": S._rel(img.built_at) if img.built_at else "—",
            "owner": unames.get(img.created_by, "—"),
            "canDelete": (img.created_by == user.id or user.role == "admin"),
        })
    return {"candidates": out}


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
    dep = session.get(Deployment, dep_id)
    if not dep:
        raise HTTPException(404, "not found")
    if dep.owner_id != user.id and user.role != "admin":
        raise HTTPException(403, "not your VM")
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
