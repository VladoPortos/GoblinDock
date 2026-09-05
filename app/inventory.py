"""Bounded background Proxmox inventory; HTTP readers only copy snapshots."""
from __future__ import annotations

import copy
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from sqlmodel import select

from .db import session_scope
from .models import Connection
from .proxmox import Proxmox

log = logging.getLogger("goblindock.inventory")


def _enabled(conn):
    # Recheck before each remote stage: disabling a source stops subsequent probes.
    with session_scope() as session:
        current = session.get(Connection, conn.id)
        return bool(current and not current.disabled and
                    current.model_dump() == conn.model_dump())


def _probe_connection(conn):
    if not _enabled(conn):
        raise RuntimeError("connection changed")
    px = Proxmox(conn)
    version = px.version()
    if not _enabled(conn):
        raise RuntimeError("connection changed")
    guests = px.list_cluster_guests()
    vms = {int(item["vmid"]): item for item in guests if item.get("type") == "qemu"}
    if not _enabled(conn):
        raise RuntimeError("connection changed")
    # Image storage is independent of guest inventory permissions/availability.
    image_node = conn.node
    try:
        image_node = conn.node or px.pick_node()
        if not _enabled(conn):
            raise RuntimeError("connection changed")
        volumes = px.storage_volumes(node=image_node)
        volume_error = None
    except Exception as exc:  # a storage failure must not hide healthy VMs
        volumes = None
        volume_error = f"Image inventory unavailable ({type(exc).__name__})"
    if not _enabled(conn):
        raise RuntimeError("connection changed")
    return {"status": "online", "version": version.get("version", "—"),
            "vms": vms, "volumes": volumes, "volumeError": volume_error,
            "imageNode": image_node}


class InventoryCache:
    def __init__(self, *, probe=None, ttl=5.0, max_workers=4):
        self.probe = probe or _probe_connection
        self.ttl = ttl
        self.max_workers = max_workers
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="gd-inventory")
        self._inflight = set()
        self._snapshots = {}
        self._generations = {}
        self._stopped = False

    def snapshot(self, connection_id):
        with self._lock:
            data = copy.deepcopy(self._snapshots.get(connection_id, {}))
        completed = data.pop("_completed", None)
        data.setdefault("status", "unknown")
        data.setdefault("vms", {})
        data.setdefault("volumes", None)
        data.setdefault("updatedAt", None)
        data.setdefault("completedAt", None)
        data.setdefault("error", None)
        data["stale"] = bool(data["error"] or completed is None or time.monotonic() - completed >= self.ttl)
        return data

    def refresh(self, conn, *, force=False):
        with self._lock:
            if self._stopped or conn.disabled or conn.id in self._inflight:
                return False
            old = self._snapshots.get(conn.id, {})
            if not force and time.monotonic() - old.get("_completed", float("-inf")) < self.ttl:
                return False
            # Never queue unlimited work behind slow/unreachable connections.
            if len(self._inflight) >= self.max_workers:
                return False
            self._inflight.add(conn.id)
            self._pool.submit(self._refresh, conn, self._generations.get(conn.id, 0))
            return True

    def invalidate(self, connection_id):
        with self._lock:
            self._snapshots.pop(connection_id, None)
            self._generations[connection_id] = self._generations.get(connection_id, 0) + 1

    def _refresh(self, conn, generation):
        try:
            result = self.probe(conn)
            error = None
        except Exception as exc:
            result = None
            # Upstream exception strings may include infrastructure credentials.
            error = f"Inventory unavailable ({type(exc).__name__})"
        completed = time.monotonic()
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._inflight.discard(conn.id)
            if generation != self._generations.get(conn.id, 0):
                return  # an edit invalidated the configuration used by this probe
            old = self._snapshots.get(conn.id, {})
            data = dict(result) if result is not None else dict(old)
            data.update({"completedAt": timestamp, "_completed": completed, "error": error})
            if error:
                data["status"] = "offline"
            else:
                data["updatedAt"] = timestamp
            self._snapshots[conn.id] = data
        from . import statebus
        statebus.bump()

    def stop(self):
        with self._lock:
            self._stopped = True
        self._pool.shutdown(wait=True, cancel_futures=True)


def freshness(snapshot):
    return {"updatedAt": snapshot.get("updatedAt"), "completedAt": snapshot.get("completedAt"),
            "stale": snapshot.get("stale", True), "error": snapshot.get("error")}


_cache = InventoryCache()
_stop = threading.Event()
_thread = None
_lifecycle_lock = threading.Lock()


def get_snapshot(connection_id):
    return _cache.snapshot(connection_id)


def invalidate(connection_id):
    _cache.invalidate(connection_id)


class SnapshotProxmox:
    """Small read-only adapter for the existing VM serializer."""
    def __init__(self, conn):
        self.conn = conn
        self.snapshot = get_snapshot(conn.id)

    def vm_current(self, vmid, node=None):
        return self.snapshot["vms"].get(int(vmid), {"status": "unknown"})


def snapshot_proxy(conn):
    return SnapshotProxmox(conn)


def _run():
    while not _stop.is_set():
        try:
            with session_scope() as session:
                conns = [Connection(**c.model_dump()) for c in session.exec(select(Connection)).all() if not c.disabled]
            # Oldest completed first prevents a large fleet starving behind 4 slots.
            conns.sort(key=lambda c: get_snapshot(c.id).get("completedAt") or "")
            for conn in conns:
                if _stop.is_set():
                    break
                _cache.refresh(conn)
        except Exception:
            log.exception("could not schedule inventory refresh")
        _stop.wait(0.5)


def start_inventory():
    global _cache, _thread
    with _lifecycle_lock:
        if _thread and _thread.is_alive():
            return
        if _cache._stopped:
            _cache = InventoryCache()
        _stop.clear()
        _thread = threading.Thread(target=_run, name="gd-inventory-scheduler", daemon=True)
        _thread.start()


def stop_inventory():
    global _thread
    with _lifecycle_lock:
        _stop.set()
        if _thread:
            _thread.join()
            _thread = None
        _cache.stop()
