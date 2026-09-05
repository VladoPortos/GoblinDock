"""Durable image cache identity and pin metadata in the existing settings table."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .appsettings import get_setting, set_setting
from .proxmox import base_disk_filename


def source_identity(url, checksum="", algorithm=""):
    return hashlib.sha256(f"{url}\0{algorithm.lower()}\0{checksum.lower()}".encode()).hexdigest()


def _key(conn, node, url, checksum="", algorithm=""):
    identity = "\0".join((str(getattr(conn,"id","") or ""), getattr(conn,"host","") or "",
                            str(getattr(conn,"port","") or ""), node or "",
                            getattr(conn,"iso_storage","") or "local", source_identity(url, checksum, algorithm)))
    return "image_cache_active:" + hashlib.sha256(identity.encode()).hexdigest()


def cache_metadata(conn, node, url, checksum="", algorithm=""):
    try:
        data = json.loads(get_setting(_key(conn,node,url,checksum,algorithm), "{}"))
        if not isinstance(data,dict):
            return {}
        # Only consume safe filenames even if a legacy setting was malformed.
        if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*\.qcow2", data.get("filename", "")):
            return {}
        return data
    except (TypeError, ValueError):
        return {}


def active_filename(conn, node, url, checksum="", algorithm=""):
    return cache_metadata(conn,node,url,checksum,algorithm).get("filename") or base_disk_filename(url,checksum,algorithm)


def record_download(conn, node, url, checksum, algorithm, filename):
    now = datetime.now(timezone.utc).isoformat()
    data = {"filename": filename, "sourceIdentity": source_identity(url,checksum,algorithm),
            "downloadedAt": now, "validatedAt": now if checksum else None,
            "checksum": checksum, "algorithm": algorithm or None}
    set_setting(_key(conn,node,url,checksum,algorithm), json.dumps(data))
    return data


def pin_identity(url, checksum):
    return source_identity(url, checksum)


def validate_pin(url, checksum, immutable):
    if not immutable or not checksum:
        raise ValueError("Pinning requires an immutable version URL, its checksum, and confirmation that the URL is immutable")
    parts = urlsplit(url)
    if parts.query or re.search(r"(?:^|[/_.-])(latest|current|daily|nightly|stable)(?:$|[/_.-])",parts.path.lower()):
        raise ValueError("Use a version-specific immutable URL, not a moving image alias or query URL")


def is_pinned(img):
    return bool(img.checksum and get_setting(f"image_pin:{img.id}") == pin_identity(img.source_url,img.checksum))
