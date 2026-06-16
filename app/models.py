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


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back stored datetimes as naive UTC — re-attach the timezone so
    they can be compared with utcnow() (one shared helper instead of ad-hoc fixes)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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
    # Homepage widget API key — only the sha256 hash is stored; the plaintext is
    # shown to the user exactly once at generation. NULL hash = no key. The prefix
    # is a non-secret display tag (e.g. gdwk_AbCd). See app/deps.widget_key_user.
    widget_key_hash: Optional[str] = Field(default=None, index=True)
    widget_key_prefix: str = ""
    widget_key_created_at: Optional[datetime] = None
    widget_key_last_used: Optional[datetime] = None


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
    kind: str = "base"          # base | golden (golden = legacy pre-templates rows, kept read-only)
    name: str = Field(index=True)
    os_family: str = "ubuntu"
    connection_id: Optional[int] = None     # legacy golden: which Proxmox the template lives on
    source_url: str = ""
    checksum: str = ""
    template_vmid: Optional[int] = None     # legacy golden: baked Proxmox template (worker refuses to destroy it)
    build_status: str = "none"  # none | ready | failed ("building" only on legacy golden rows)
    size: str = ""
    created_by: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow)


class Template(SQLModel, table=True):
    """A named, reusable DEPLOYMENT PRESET: a base image + location + runtime blocks +
    size/network defaults. Every deploy builds the VM fresh from the base image and
    applies the blocks; per-block inputs can be flagged ask-on-deploy inside recipe_json
    (block key ``ask: ["inputName"]``)."""
    __tablename__ = "templates"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: str = ""
    os_family: str = "ubuntu"
    recipe_json: str = "[]"
    base_image_id: Optional[int] = None     # images.id (kind=base) — the ISO/cloud image
    connection_id: Optional[int] = None     # deploy target (which Proxmox)
    network_id: Optional[int] = None        # default network for one-click deploy
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
    image_id: Optional[int] = None        # the base image this VM was built from
    template_id: Optional[int] = None     # optional template applied on top
    # ask-on-deploy answers, {"<si>.<bi>": {"<input>": value}} — kept on the row
    # (not just the job) so a VM rebuild re-applies them.
    deploy_inputs_json: str = "{}"
    vmid: Optional[int] = None
    node: str = ""
    network_id: Optional[int] = None
    cpu: int = 1
    ram: int = 2          # GB
    disk: int = 20        # GB
    ip: str = ""
    mac: str = ""
    status: str = "working"  # running | stopped | working | error (live "unknown" serializes as stopped)
    tags: str = ""
    notes: str = ""
    error: str = ""
    # Fernet-encrypted VM credential password ('' = none generated). See app/security.encrypt.
    root_password_enc: str = ""
    # OS user the password is for: 'root' (snippet path) | 'goblin' (native cloud-init fallback).
    cred_user: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class Job(SQLModel, table=True):
    __tablename__ = "jobs"
    id: Optional[int] = Field(default=None, primary_key=True)
    type: str = "deploy"   # deploy | rebuild | destroy | image_sync
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
    dismissed: bool = False
    dismissed_at: Optional[datetime] = None


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


class Setting(SQLModel, table=True):
    """Tiny key/value store for runtime-editable app settings (unlike env config,
    these can be changed from the UI). Currently holds the job-history retention."""
    __tablename__ = "app_settings"
    key: str = Field(primary_key=True)
    value: str = ""
