"""Process-global 'state changed' signal for the SSE live-push.

The app runs one uvicorn worker with an in-process daemon worker thread
(app/worker.py), so a plain in-memory counter guarded by a lock is enough to
fan a "something changed — refetch" hint out to every connected
/api/state/stream client. No payload is carried: the hint only says "refetch";
each client's /api/state response is already tenant-scoped, so nothing leaks.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_version = 0


def bump() -> None:
    """Signal that user-visible state changed. Cheap and safe to over-call."""
    global _version
    with _lock:
        _version += 1


def version() -> int:
    with _lock:
        return _version
