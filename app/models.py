"""SQLModel tables — the whole GoblinDock data store.

Recipes are kept as JSON on images/templates (rather than the fully-normalised
recipe_sections/recipe_blocks tables in the design spec) — pragmatic for v1 and
plenty for a homelab. Everything else follows the design's data model closely.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    __tablename__ = "users"
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    name: str
    password_hash: str
    role: str = "user"  # admin | user
    disabled: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    last_login: Optional[datetime] = None
    # bumped on every password change/reset → invalidates sessions signed under the old
    # value (the cookie carries the epoch it was issued with).
    session_epoch: int = 0
    # per-account lockout (survives restart, unlike the in-memory IP throttle).
    failed_logins: int = 0
    locked_until: Optional[datetime] = None


class Connection(SQLModel, table=True):
    __tablename__ = "connections"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    host: str
    port: int = 8006
    token_id: str  # e.g. goblindock@pve!app
    token_secret_enc: str = ""
    verify_tls: bool = False
    node: str = ""          # default node
    storage: str = ""       # VM disk storage (zfs/lvm)
    iso_storage: str = "local"
    snippet_storage: str = "local"
    bridge: str = "vmbr0"
    ssh_host: str = ""
    ssh_user: str = "root"
    ssh_key_path: str = ""  # optional; enables cloud-init snippet baking
    # Per-target resource ceilings per VM (0 = inherit the global default).
    max_cores: int = 0
    max_ram_mb: int = 0
    max_disk_gb: int = 0
    created_by: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow)


class Secret(SQLModel, table=True):
    __tablename__ = "secrets"
    id: Optional[int] = Field(default=None, primary_key=True)
    scope: str = "global"  # global | user
    owner_id: Optional[int] = None
    name: str = Field(index=True)
    value_enc: str = ""
    created_by: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow)
    last_used: Optional[datetime] = None


class Variable(SQLModel, table=True):
    """Like a Secret, but NOT secret: value is stored plaintext and shown in the UI.
    Referenced in scripts/recipes as {{ variable.NAME }} (vs {{ secrets.NAME }})."""
    __tablename__ = "variables"
    id: Optional[int] = Field(default=None, primary_key=True)
    scope: str = "global"  # global | user
    owner_id: Optional[int] = None
    name: str = Field(index=True)
    value: str = ""
    created_by: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow)


class Block(SQLModel, table=True):
    __tablename__ = "blocks"
    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True)             # stable id e.g. b-apt
    kind: str = "builtin"                    # builtin | custom
    name: str = ""
    description: str = ""
    category: str = ""
    icon: str = "box"
    section: str = "Install"                 # default canvas section
    phase: str = "ansible"                   # cloudinit (first boot) | ansible (post-boot)
    input_schema_json: str = "[]"
    ansible_template: str = ""               # YAML ansible task(s) — used when phase=ansible
    cloudinit_template: str = ""             # shell lines — used when phase=cloudinit
    owner_id: Optional[int] = None
    builtin: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class Image(SQLModel, table=True):
    __tablename__ = "images"
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str = "base"          # base | golden
    name: str = Field(index=True)
    os_family: str = "ubuntu"
    connection_id: Optional[int] = None     # golden: which Proxmox the template lives on
    node: str = ""                          # golden: target node (location)
    storage: str = ""                       # golden: target VM-disk storage (location)
    base_image_id: Optional[int] = None
    source_url: str = ""
    checksum: str = ""
    recipe_json: str = "[]"     # bake-time blocks (ordered sections of placed blocks)
    disk_gb: int = 20           # golden: disk size baked in (so a rebuild reuses it)
    template_vmid: Optional[int] = None
    build_status: str = "none"  # none | building | ready | failed | importing
    progress: int = 0
    size: str = ""
    created_by: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow)
    built_at: Optional[datetime] = None


class Recipe(SQLModel, table=True):
    """A named, reusable RUNTIME customization (lego blocks) applied on top of a
    deployed VM — independent of any golden image. e.g. 'MySQL', 'k8s-node'."""
    __tablename__ = "recipes"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: str = ""
    os_family: str = "ubuntu"
    recipe_json: str = "[]"
    default_cpu: int = 1
    default_ram: int = 2       # GB
    default_disk: int = 20     # GB
    owner_id: Optional[int] = None
    public: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class Deployment(SQLModel, table=True):
    __tablename__ = "deployments"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    owner_id: Optional[int] = None
    connection_id: Optional[int] = None
    image_id: Optional[int] = None        # the golden image deployed from
    recipe_id: Optional[int] = None       # optional runtime recipe applied on top
    vmid: Optional[int] = None
    node: str = ""
    network_id: Optional[int] = None
    cpu: int = 1
    ram: int = 2          # GB
    disk: int = 20        # GB
    ip: str = ""
    mac: str = ""
    status: str = "working"  # running | stopped | working | error | unknown
    tags: str = ""
    notes: str = ""
    error: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Job(SQLModel, table=True):
    __tablename__ = "jobs"
    id: Optional[int] = Field(default=None, primary_key=True)
    type: str = "deploy"   # deploy | image_build | rebuild | destroy
    title: str = ""
    deployment_id: Optional[int] = None
    image_id: Optional[int] = None
    connection_id: Optional[int] = None
    status: str = "queued"  # queued | running | succeeded | failed | canceled
    pct: int = 0
    phase: str = ""
    created_by: Optional[int] = None
    cancel_requested: bool = False
    error: str = ""
    context_json: str = "{}"
    created_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class JobStep(SQLModel, table=True):
    __tablename__ = "job_steps"
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)
    seq: int = 0
    name: str = ""
    state: str = "pending"  # pending | running | done | failed | skipped
    dur: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class JobEvent(SQLModel, table=True):
    """Append-only feed tailed by the SSE endpoint (log lines + refresh ticks)."""
    __tablename__ = "job_events"
    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(index=True)
    ts: datetime = Field(default_factory=utcnow)
    kind: str = "log"       # log | tick
    line: str = ""
    log_class: str = ""     # l-ok | l-warn | l-err | l-dim | l-acc | ''


class Network(SQLModel, table=True):
    __tablename__ = "networks"
    id: Optional[int] = Field(default=None, primary_key=True)
    connection_id: Optional[int] = Field(default=None, index=True)
    name: str = Field(index=True)
    mode: str = "dhcp"          # dhcp | static
    bridge: str = "vmbr0"
    vlan: Optional[int] = None
    subnet_cidr: str = ""       # e.g. 10.0.50.0/24 (static)
    gateway: str = ""
    range_start: str = ""
    range_end: str = ""
    dns: str = ""
    created_by: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow)


class IpAllocation(SQLModel, table=True):
    __tablename__ = "ip_allocations"
    id: Optional[int] = Field(default=None, primary_key=True)
    network_id: int = Field(index=True)
    ip: str = Field(index=True)
    deployment_id: Optional[int] = None
    state: str = "reserved"     # always "reserved" — a freed allocation row is deleted
    created_at: datetime = Field(default_factory=utcnow)


class Audit(SQLModel, table=True):
    __tablename__ = "audit"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = None
    user_name: str = ""
    action: str = ""
    target_type: str = ""
    target_id: str = ""
    ip: str = ""
    ts: datetime = Field(default_factory=utcnow)
    detail: str = ""
