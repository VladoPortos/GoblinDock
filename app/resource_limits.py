"""Effective per-VM resource ceilings.

Connections may override a dimension with a positive value. Zero means inherit the
finite global default; it never means an unbounded tenant request.
"""
from __future__ import annotations

from .config import settings


def effective_resource_limits(connection) -> tuple[int, int, int]:
    """Return ``(vCPU, RAM MiB, disk GiB)`` for a deployment target."""
    return (
        int(connection.max_cores) if connection and int(connection.max_cores) > 0
        else settings.max_cores,
        int(connection.max_ram_mb) if connection and int(connection.max_ram_mb) > 0
        else settings.max_ram_mb,
        int(connection.max_disk_gb) if connection and int(connection.max_disk_gb) > 0
        else settings.max_disk_gb,
    )
