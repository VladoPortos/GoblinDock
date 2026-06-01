"""Real client-IP resolution behind a reverse proxy.

Starlette's ``request.client.host`` is the *socket peer* — behind the documented
TLS-terminating reverse proxy that's the proxy's address, not the user's. We only
trust ``X-Forwarded-For`` when the direct peer is an allow-listed proxy
(``GOBLINDOCK_FORWARDED_ALLOW_IPS``), otherwise a client could spoof the header.

A ContextVar carries the resolved IP for the current request so ``record_audit``
(called from ~27 sites) can stamp it without threading ``Request`` everywhere.
"""
from __future__ import annotations

import ipaddress
from contextvars import ContextVar

from starlette.requests import HTTPConnection

from .config import settings

_client_ip: ContextVar[str] = ContextVar("_client_ip", default="")


def _is_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _explicitly_trusted(ip: str, allow: set[str]) -> bool:
    """Trusted by an EXACT IP or CIDR entry (ignores the '*' wildcard). Used to skip
    known proxy hops while walking the XFF chain."""
    if ip in allow:
        return True
    if not _is_ip(ip):
        return False
    pip = ipaddress.ip_address(ip)
    for entry in allow:
        if "/" in entry:
            try:
                if pip in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
    return False


def _peer_trusted(peer: str, allow: set[str]) -> bool:
    """Is the socket-peer address an allow-listed proxy? Supports '*', exact IPs,
    and CIDR ranges (e.g. a Docker bridge subnet like 172.18.0.0/16)."""
    if not allow:
        return False
    if "*" in allow:
        return True
    return _explicitly_trusted(peer, allow)


def client_ip(conn: HTTPConnection) -> str:
    """Best-effort real client IP for an HTTP/WebSocket connection.

    X-Forwarded-For is APPENDED to by each hop, so the LEFT-most entry is whatever
    the client originally sent — spoofable. We only trust the header when the socket
    peer is an allow-listed proxy; then we walk the chain from the RIGHT (the end our
    trusted proxies appended), skip entries that are themselves explicitly-listed
    proxy hops, and return the first valid IP the trusted chain actually observed —
    the real client. The left-most entry is never trusted, and a non-IP token is
    never returned (so the throttle key / audit IP can't be an attacker-chosen
    string); on any ambiguity we fall back to the socket peer.
    """
    allow = settings.forwarded_allow_ips
    peer = conn.client.host if conn.client else ""
    if not _peer_trusted(peer, allow):
        return peer
    entries = [e.strip() for e in conn.headers.get("x-forwarded-for", "").split(",") if e.strip()]
    # first non-proxy, valid IP from the right = the real client
    for ip in reversed(entries):
        if _is_ip(ip) and not _explicitly_trusted(ip, allow):
            return ip
    # otherwise (e.g. '*' trust, or all hops are listed proxies) take the right-most
    # valid IP the chain appended; if none parse, fall back to the socket peer.
    for ip in reversed(entries):
        if _is_ip(ip):
            return ip
    return peer


def set_request_ip(ip: str) -> None:
    _client_ip.set(ip)


def current_request_ip() -> str:
    return _client_ip.get()
