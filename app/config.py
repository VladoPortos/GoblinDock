"""Runtime configuration, read from the environment.

Everything that isn't a secret has a sensible default so the container runs with
zero config. The one thing you should set in production is GOBLINDOCK_SECRET_KEY
(used both for session signing and to derive the at-rest encryption key for
stored secrets / Proxmox tokens).
"""
from __future__ import annotations

import os
from pathlib import Path


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _env_or_file(name: str, default: str = "") -> str:
    """Read a value from ${NAME}, or from the file at ${NAME_FILE} (Docker/compose
    `secrets:` mount the value as a file). Env wins if both are set. File-mounted
    secrets are not exposed via `docker inspect`/`/proc/<pid>/environ`, unlike env."""
    val = os.environ.get(name)
    if val and val.strip():
        return val.strip()
    path = os.environ.get(f"{name}_FILE")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return default
    return default


class Settings:
    def __init__(self) -> None:
        self.data_dir = Path(os.environ.get("GOBLINDOCK_DATA_DIR", "./data")).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # The data dir holds the SQLite DB (Argon2 password hashes, Fernet-encrypted
        # secrets), the plaintext audit log and rotating backups — keep it owner-only.
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass

        self.db_path = os.environ.get(
            "GOBLINDOCK_DB", str(self.data_dir / "goblindock.sqlite3")
        )
        self.database_url = f"sqlite:///{self.db_path}"

        # Dev mode relaxes a few prod safeguards (ephemeral key allowed, cookies
        # not Secure so http://localhost works). NEVER set this in production.
        self.dev_mode = _bool(os.environ.get("GOBLINDOCK_DEV"), False)

        # Secret key — drives session signing + Fernet key derivation. It MUST be
        # stable: a per-boot random key would invalidate sessions AND make every
        # stored secret/token undecryptable on restart. So we fail fast in prod.
        _key = _env_or_file("GOBLINDOCK_SECRET_KEY").strip()
        _weak = {"", "please-change-me", "change-me-to-a-long-random-string", "changeme"}
        if _key in _weak or len(_key) < 16:
            if self.dev_mode or _bool(os.environ.get("GOBLINDOCK_ALLOW_EPHEMERAL_KEY")):
                _key = _key or os.urandom(32).hex()
            else:
                raise RuntimeError(
                    "GOBLINDOCK_SECRET_KEY is unset or weak. Set a long random value, e.g.\n"
                    '  python -c "import secrets; print(secrets.token_hex(32))"\n'
                    "(or set GOBLINDOCK_DEV=1 for an ephemeral dev key)."
                )
        self.secret_key = _key

        # Cookie Secure flag — on by default; auto-off in dev so http localhost works.
        self.cookie_secure = _bool(
            os.environ.get("GOBLINDOCK_COOKIE_SECURE"), not self.dev_mode
        )

        # Reverse-proxy trust: which direct-peer IPs are allowed to set the real
        # client address via X-Forwarded-For. Empty = trust nobody (use the socket
        # peer). "*" = trust any upstream (only safe when nothing untrusted can reach
        # the container directly, e.g. bound to localhost / behind one proxy).
        self.forwarded_allow_ips = {
            p.strip() for p in os.environ.get("GOBLINDOCK_FORWARDED_ALLOW_IPS", "").split(",") if p.strip()
        }

        # Web (static SPA) directory.
        self.web_dir = Path(
            os.environ.get("GOBLINDOCK_WEB_DIR", str(Path(__file__).parent.parent / "web"))
        ).resolve()

        # Guard rails: GoblinDock only ever creates VMs inside this id window, and never touches
        # anything outside it.
        self.vmid_min = int(os.environ.get("GOBLINDOCK_VMID_MIN", "8000"))
        self.vmid_max = int(os.environ.get("GOBLINDOCK_VMID_MAX", "8099"))

        # Fallback per-VM cap, applied ONLY when a deploy target has no connection
        # record (in practice never — deploys require a connection). The authoritative
        # per-VM ceiling is set per-connection in the UI (Settings → Proxmox), where
        # 0 = unlimited for that dimension.
        self.max_cores = int(os.environ.get("GOBLINDOCK_MAX_CORES", "1"))
        self.max_ram_mb = int(os.environ.get("GOBLINDOCK_MAX_RAM_MB", "2048"))
        self.max_disk_gb = int(os.environ.get("GOBLINDOCK_MAX_DISK_GB", "0"))

        # Per-user quotas (0 = unlimited). Stop one tenant exhausting the shared VMID
        # pool / node storage. Admins are exempt.
        self.max_vms_per_user = int(os.environ.get("GOBLINDOCK_MAX_VMS_PER_USER", "0"))

        # Scheduled SQLite backups (driven by APScheduler — see app/scheduler.py).
        # The single SQLite file IS the whole datastore (users, deployments, audit,
        # and the encrypted Proxmox tokens / secrets), so a rotating local snapshot
        # turns "one corrupt file = total loss" into a quick restore. The backup uses
        # sqlite3's online-backup API (WAL-safe) — never a raw file copy. Backups carry
        # the SAME Fernet-encrypted secrets as the live DB and are only restorable with
        # the matching GOBLINDOCK_SECRET_KEY. Disable with GOBLINDOCK_BACKUP_ENABLED=0.
        self.backup_enabled = _bool(os.environ.get("GOBLINDOCK_BACKUP_ENABLED"), True)
        self.backup_interval_hours = max(
            1, int(os.environ.get("GOBLINDOCK_BACKUP_INTERVAL_HOURS", "24")))
        self.backup_keep = max(1, int(os.environ.get("GOBLINDOCK_BACKUP_KEEP", "7")))
        self.backup_dir = Path(
            os.environ.get("GOBLINDOCK_BACKUP_DIR", str(self.data_dir / "backups")))

        # Optional first-run admin bootstrap.
        self.admin_email = os.environ.get("GOBLINDOCK_ADMIN_EMAIL", "")
        self.admin_password = os.environ.get("GOBLINDOCK_ADMIN_PASSWORD", "")
        self.admin_name = os.environ.get("GOBLINDOCK_ADMIN_NAME", "Admin")

        # Optional auto-seed of the test Proxmox connection from env (handy for dev).
        self.seed_proxmox = _bool(os.environ.get("GOBLINDOCK_SEED_PROXMOX"), False)
        self.proxmox_token_id = _env_or_file("PROXMOX_TOKEN_ID")
        self.proxmox_token = _env_or_file("PROXMOX_TOKEN")
        self.proxmox_host = os.environ.get("PROXMOX_HOST", "")
        self.proxmox_node = os.environ.get("PROXMOX_NODE", "pve")
        self.proxmox_storage = os.environ.get("PROXMOX_STORAGE", "local-zfs")
        self.proxmox_iso_storage = os.environ.get("PROXMOX_ISO_STORAGE", "local")
        self.proxmox_snippet_storage = os.environ.get("PROXMOX_SNIPPET_STORAGE", "local")
        self.proxmox_bridge = os.environ.get("PROXMOX_BRIDGE", "vmbr0")
        self.proxmox_ssh_host = os.environ.get("PROXMOX_SSH_HOST", "")
        self.proxmox_ssh_user = os.environ.get("PROXMOX_SSH_USER", "root")
        self.proxmox_ssh_key = os.environ.get("PROXMOX_SSH_KEY", "")  # path inside container

        # SSH host-key verification for snippet upload. Strict rejects unknown
        # hosts; otherwise we honour known_hosts entries (so a pinned node is
        # MITM-protected) and trust-on-first-use for the rest (homelab default).
        self.ssh_strict = _bool(os.environ.get("GOBLINDOCK_SSH_STRICT"), False)
        self.ssh_known_hosts = os.environ.get("GOBLINDOCK_SSH_KNOWN_HOSTS", "")

        self.cors_origins = [
            o for o in os.environ.get("GOBLINDOCK_CORS", "").split(",") if o
        ]


settings = Settings()
