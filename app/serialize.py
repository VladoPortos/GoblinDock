"""Map DB rows + live Proxmox state into the shapes the prototype UI expects.

Keeping the server output identical to the design's mock-data schema means the
React prototype renders unchanged.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from .models import (
    Block,
    Connection,
    Deployment,
    Image,
    IpAllocation,
    Job,
    JobEvent,
    JobStep,
    Network,
    Template,
    Secret,
    Variable,
    User,
)
from .proxmox import Proxmox
from .recipes import recipe_block_chips
from .security import mask

# cache live status briefly to avoid hammering Proxmox on every poll. Keyed by
# (connection_id, node, vmid) — NOT vmid alone — so two Proxmox clusters that
# reuse the same VMID don't collide.
_status_cache: dict[tuple, tuple[float, dict]] = {}
_STATUS_TTL = 3.0


def _fmt_uptime(seconds: int) -> str:
    if not seconds:
        return "—"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _rel(dt: Optional[datetime]) -> str:
    if not dt:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _elapsed(start: Optional[datetime], end: Optional[datetime]) -> str:
    if not start:
        return "00:00"
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = end or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    s = max(0, int((end - start).total_seconds()))
    return f"{s // 60:02d}:{s % 60:02d}"


def _live_status(px: Proxmox, vmid: int, node: str) -> dict:
    now = time.time()
    key = (getattr(getattr(px, "conn", None), "id", None), node, vmid)
    cached = _status_cache.get(key)
    if cached and now - cached[0] < _STATUS_TTL:
        return cached[1]
    try:
        cur = px.vm_current(vmid, node)
        out = {
            "status": cur.get("status", "unknown"),
            "cpu_pct": round(float(cur.get("cpu", 0)) * 100),
            "mem": cur.get("mem", 0),
            "maxmem": cur.get("maxmem", 1) or 1,
            "uptime": cur.get("uptime", 0),
        }
    except Exception:  # noqa: BLE001
        out = {"status": "unknown", "cpu_pct": 0, "mem": 0, "maxmem": 1, "uptime": 0}
    _status_cache[key] = (now, out)
    return out


def vm_dict(session: Session, dep: Deployment, me: User, px_cache: dict, users: dict, conns: dict) -> dict:
    conn = conns.get(dep.connection_id)
    os_family = "generic"
    image_name = ""
    template_name = ""
    if dep.image_id:
        img = session.get(Image, dep.image_id)
        if img:
            os_family = img.os_family
            image_name = img.name
    if dep.template_id:
        tpl = session.get(Template, dep.template_id)
        if tpl:
            template_name = tpl.name

    status = dep.status
    cpu_pct = 0
    ram_pct = 0
    uptime = "—"
    if conn and dep.vmid and dep.status not in ("working", "error"):
        px = px_cache.get(conn.id)
        if px:
            live = _live_status(px, dep.vmid, dep.node or conn.node)
            status = "running" if live["status"] == "running" else "stopped"
            cpu_pct = live["cpu_pct"]
            ram_pct = round(live["mem"] / max(1, live["maxmem"]) * 100)
            uptime = _fmt_uptime(live["uptime"]) if status == "running" else "—"

    # active job → inline chip
    job_chip = None
    active = session.exec(
        select(Job).where(Job.deployment_id == dep.id,
                          Job.status.in_(["queued", "running"])).order_by(Job.id.desc())
    ).first()
    if active:
        steps = session.exec(select(JobStep).where(JobStep.job_id == active.id)).all()
        done = sum(1 for s in steps if s.state in ("done", "skipped"))
        label = {"deploy": "Deploying", "rebuild": "Rebuilding", "destroy": "Destroying"}.get(active.type, "Working")
        job_chip = {"label": label, "step": done, "total": max(len(steps), 1), "jobId": active.id}
        status = "working"

    owner = users.get(dep.owner_id)
    return {
        "id": f"vm-{dep.id}",
        "depId": dep.id,
        "name": dep.name,
        "status": status,
        "ip": dep.ip or "—",
        "owner": "you" if dep.owner_id == me.id else "other",
        "ownerName": owner.name if owner else "—",
        "conn": conn.name if conn else "—",
        "os": os_family,
        "image": image_name or "—",
        "template": template_name or image_name or "—",
        "templateId": dep.template_id,
        "cpu": cpu_pct,
        "ram": ram_pct,
        "uptime": uptime,
        "tags": dep.tags or "",
        "notes": dep.notes or "",
        **({"job": job_chip} if job_chip else {}),
        **({"err": dep.error} if dep.status == "error" and dep.error else {}),
    }


def job_brief(session: Session, job: Job) -> dict:
    steps = session.exec(select(JobStep).where(JobStep.job_id == job.id)).all()
    done = sum(1 for s in steps if s.state in ("done", "skipped"))
    total = max(len(steps), 1)
    status_map = {"running": "working", "queued": "working", "succeeded": "done",
                  "failed": "error", "canceled": "error"}
    return {
        "id": f"j-{job.id}",
        "jobId": job.id,
        "title": job.title,
        "status": status_map.get(job.status, "working"),
        "pct": job.pct,
        "phase": job.phase or job.status.title(),
        "elapsed": _elapsed(job.started_at, job.finished_at),
        "step": done,
        "total": total,
    }


def job_detail(session: Session, job: Job, include_log: bool = True,
               log_limit: int = 2000) -> dict:
    steps = session.exec(
        select(JobStep).where(JobStep.job_id == job.id).order_by(JobStep.seq)
    ).all()
    # Bound the log load to the last `log_limit` rows (a noisy ansible run can emit
    # thousands) and let the stream skip it entirely after the first frame.
    logs = []
    if include_log:
        logs = session.exec(
            select(JobEvent).where(JobEvent.job_id == job.id, JobEvent.kind == "log")
            .order_by(JobEvent.id.desc()).limit(log_limit)
        ).all()
        logs = list(reversed(logs))
    status_map = {"running": "working", "queued": "working", "succeeded": "done",
                  "failed": "error", "canceled": "error"}
    phase_sets = {
        "deploy": ["Allocate", "Prepare image", "Create", "Configure", "Boot"],
        "rebuild": ["Destroy", "Allocate", "Prepare image", "Create", "Configure", "Boot"],
        "destroy": ["Stop", "Destroy"],
    }
    phases = phase_sets.get(job.type, ["Start", "Run", "Finish"])
    return {
        "id": job.id,
        "title": job.title,
        "type": job.type,
        "status": status_map.get(job.status, "working"),
        "rawStatus": job.status,
        "pct": job.pct,
        "phase": job.phase or job.status.title(),
        "phases": phases,
        "elapsed": _elapsed(job.started_at, job.finished_at),
        "error": job.error,
        "steps": [
            {"name": s.name, "state": s.state, "dur": s.dur} for s in steps
        ],
        "log": [{"cls": e.log_class, "text": e.line} for e in logs],
        "lastEventId": logs[-1].id if logs else 0,
    }


def golden_image_dict(session: Session, img: Image) -> dict:
    recipe = json.loads(img.recipe_json or "[]")
    chips = recipe_block_chips(recipe) or ["clean cloud-init base"]
    # building/importing both render as "building"; ready→ready; anything else
    # (failed/none) passes through unchanged so a failed build isn't hidden.
    state = {"building": "building", "importing": "building",
             "ready": "ready"}.get(img.build_status, img.build_status)
    conn = session.get(Connection, img.connection_id) if img.connection_id else None
    return {
        "id": f"img-{img.id}",
        "imgId": img.id,
        "name": img.name,
        "os": img.os_family,
        "base": _base_name(session, img),
        "built": _rel(img.built_at) if img.built_at else "—",
        "state": state,
        "blocks": chips,
        "vmid": img.template_vmid,
        "deployable": bool(img.template_vmid and img.build_status == "ready"),
        "progress": img.progress,
        "osFamily": img.os_family,
        "recipe": recipe,
        "connId": img.connection_id,
        "location": (conn.name if conn else "—") + (" · " + img.node if img.node else ""),
    }


def _base_name(session: Session, img: Image) -> str:
    if img.base_image_id:
        b = session.get(Image, img.base_image_id)
        if b:
            return b.name
    return "Ubuntu 24.04 LTS"


def base_image_dict(img: Image) -> dict:
    return {
        "id": f"img-{img.id}",
        "imgId": img.id,
        "name": img.name,
        "os": img.os_family,
        "size": img.size or "—",
        "checksum": img.checksum or "cloud-init ready",
        "source_url": img.source_url,
    }


def template_dict(session: Session, t: Template) -> dict:
    used = session.exec(select(Deployment).where(Deployment.template_id == t.id)).all()
    recipe = json.loads(t.recipe_json or "[]")
    base = session.get(Image, t.base_image_id) if t.base_image_id else None
    if base and base.kind != "base":
        base = None
    conn = session.get(Connection, t.connection_id) if t.connection_id else None
    return {
        "id": f"t-{t.id}",
        "templateId": t.id,
        "name": t.name,
        "os": t.os_family,
        "desc": t.description or "",
        "cpu": t.default_cpu,
        "mem": t.default_ram,
        "disk": t.default_disk,
        "used": len(used),
        "public": t.public,
        "blocks": recipe_block_chips(recipe),
        "recipe": recipe,
        "baseImageId": t.base_image_id,
        "connectionId": t.connection_id,
        "networkId": t.network_id,
        "base": base.name if base else None,
        "deployable": bool(base and conn),
        "location": ((conn.name + (" · " + conn.node if conn.node else "")) if conn else None),
    }


def block_dict(b: Block) -> dict:
    return {
        "id": b.key,
        "key": b.key,
        "cat": b.category,
        "name": b.name,
        "icon": b.icon,
        "desc": b.description,
        "builtin": b.builtin,
        "section": b.section,
        "phase": b.phase,
        "ansible": b.ansible_template,
        "cloudinit": b.cloudinit_template,
        "schema": json.loads(b.input_schema_json or "[]"),
    }


def secret_dict(s: Secret, users: dict, reveal: bool = False) -> dict:
    from .security import decrypt
    return {
        "id": f"sec-{s.id}",
        "secId": s.id,
        "name": s.name,
        "scope": "Global" if s.scope == "global" else "Personal",
        "by": (users.get(s.created_by).name if users.get(s.created_by) else "—"),
        "used": _rel(s.last_used),
        "val": decrypt(s.value_enc) if reveal else mask(decrypt(s.value_enc)),
    }


def variable_dict(v: Variable, users: dict) -> dict:
    return {
        "id": f"var-{v.id}",
        "varId": v.id,
        "name": v.name,
        "value": v.value,                                  # plaintext — visible by design
        "scope": "Global" if v.scope == "global" else "Personal",
        "rawScope": v.scope,
        "by": (users.get(v.created_by).name if users.get(v.created_by) else "—"),
    }


def connection_dict(session: Session, c: Connection, status: Optional[dict] = None) -> dict:
    vms = session.exec(select(Deployment).where(Deployment.connection_id == c.id)).all()
    return {
        "id": f"c-{c.id}",
        "connId": c.id,
        "name": c.name,
        "url": f"https://{c.host}:{c.port}",
        "status": (status or {}).get("status", "unknown"),
        "version": (status or {}).get("version", "—"),
        "storage": c.storage or "—",
        "bridge": c.bridge,
        "vms": len(vms),
        "node": c.node,
        # round-trippable config for the edit form (token secret is NEVER sent; the
        # SSH/TLS settings are env/API-managed and not exposed to the form)
        "host": c.host,
        "port": c.port,
        "tokenId": c.token_id,
        "isoStorage": c.iso_storage,
        "snippetStorage": c.snippet_storage,
        # per-target VM ceilings (0 = inherit the global default; UI falls back to it)
        "maxCores": c.max_cores,
        "maxRamGb": c.max_ram_mb // 1024,
        "maxDiskGb": c.max_disk_gb,
    }


def connection_public_dict(session: Session, c: Connection, status: Optional[dict] = None) -> dict:
    """Redacted connection view for NON-admins: enough to pick a build/deploy target and
    size a VM, but none of the infrastructure config or credentials (Proxmox host/port,
    token id, SSH host/user/key path, storage backends). The full record + edit form are
    served by connection_dict to admins only."""
    vms = session.exec(select(Deployment).where(Deployment.connection_id == c.id)).all()
    return {
        "id": f"c-{c.id}",
        "connId": c.id,
        "name": c.name,
        "status": (status or {}).get("status", "unknown"),
        "version": (status or {}).get("version", "—"),
        "node": c.node,
        "vms": len(vms),
        # per-target VM ceilings (used to size the deploy/build sliders; 0 = inherit global)
        "maxCores": c.max_cores,
        "maxRamGb": c.max_ram_mb // 1024,
        "maxDiskGb": c.max_disk_gb,
    }


def _pool_total(n: Network) -> int:
    import ipaddress
    if n.mode != "static" or not n.range_start:
        return 254
    try:
        return int(ipaddress.ip_address(n.range_end or n.range_start)) - int(ipaddress.ip_address(n.range_start)) + 1
    except ValueError:
        return 254


def network_dict(session: Session, n: Network, conn_name: dict, public: bool = False) -> dict:
    if n.mode == "static":
        used = len(session.exec(select(IpAllocation).where(
            IpAllocation.network_id == n.id, IpAllocation.state == "reserved")).all())
    else:
        used = len(session.exec(select(Deployment).where(Deployment.network_id == n.id)).all())
    # Non-admins only need enough to PICK a network at deploy time (name, id, the owning
    # connection, mode, capacity). The internal topology — bridge, VLAN tag, subnet CIDR,
    # gateway, DNS and the static IP range — is admin-only (lateral-movement recon).
    base = {
        "id": f"n-{n.id}", "netId": n.id, "connId": n.connection_id, "name": n.name,
        "mode": "Static" if n.mode == "static" else "DHCP", "rawMode": n.mode,
        "used": used, "total": _pool_total(n),
    }
    if public:
        return base
    base.update({
        "conn": conn_name.get(n.connection_id, "—"),
        "bridge": n.bridge, "vlan": n.vlan if n.vlan else "—",
        "subnet": n.subnet_cidr or "(DHCP)", "gateway": n.gateway,
        "rangeStart": n.range_start, "rangeEnd": n.range_end, "dns": n.dns,
    })
    return base


def network_dicts(session: Session, conns: list[Connection], public: bool = False) -> list[dict]:
    conn_name = {c.id: c.name for c in conns}
    nets = session.exec(select(Network).order_by(Network.id)).all()
    return [network_dict(session, n, conn_name, public=public) for n in nets]


def user_dict(session: Session, u: User) -> dict:
    vms = session.exec(select(Deployment).where(Deployment.owner_id == u.id)).all()
    return {
        "id": f"u-{u.id}",
        "userId": u.id,
        "name": u.name,
        "email": u.email,
        "role": "Admin" if u.role == "admin" else "User",
        "rawRole": u.role,
        "disabled": u.disabled,
        "last": _rel(u.last_login),
        "vms": len(vms),
    }


def me_dict(u: User) -> dict:
    initials = "".join(p[0] for p in u.name.split()[:2]).upper() or u.email[:2].upper()
    return {"id": u.id, "name": u.name, "email": u.email,
            "role": "Admin" if u.role == "admin" else "User", "initials": initials,
            "isAdmin": u.role == "admin",
            "createdAt": _rel(u.created_at), "lastLogin": _rel(u.last_login),
            # Non-secret status of the Homepage widget key — never the hash/token.
            "widgetKey": {"present": bool(u.widget_key_hash),
                          "prefix": u.widget_key_prefix or "",
                          "createdAt": _rel(u.widget_key_created_at),
                          "lastUsed": _rel(u.widget_key_last_used)}}
