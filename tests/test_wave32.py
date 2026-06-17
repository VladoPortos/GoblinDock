"""Wave 32 — review Batch G: misc robustness.

G1 (crypt->passlib) is covered by the updated tests/test_wave24.py.
G2: Proxmox.agent_ipv4 classifies addresses with `ipaddress` — it skips loopback /
    link-local / unspecified and never latches a Docker-bridge (172.17/16) address
    ahead of the VM's real management lease.
G4: one-shot VNC tokens are swept by a shared helper (called from proxy-create AND
    websocket-accept), and the proxy response carries Cache-Control: no-store.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave32.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave32-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from fastapi import Response                       # noqa: E402
from app.db import init_db, session_scope         # noqa: E402
from app import api                               # noqa: E402
from app.config import settings                   # noqa: E402
from app.proxmox import Proxmox                    # noqa: E402
from app.models import Connection, Deployment, User  # noqa: E402
from app.security import hash_password             # noqa: E402

init_db()


# --------------------------------------------------------------------------- #
# G2 — agent_ipv4 address selection                                            #
# --------------------------------------------------------------------------- #
def _agent_ipv4(ifaces):
    """Call Proxmox.agent_ipv4 with a fake api that returns `ifaces`, bypassing the
    network. vmid is the configured floor so guard_vmid() passes."""
    class _Api:
        def nodes(self, n): return self
        def qemu(self, v): return self
        def agent(self, c): return self
        def get(self): return {"result": ifaces}

    class _Self:
        api = _Api()
        def pick_node(self): return "pve"
    return Proxmox.agent_ipv4(_Self(), settings.vmid_min, "pve")


def _iface(name, *ips):
    return {"name": name, "ip-addresses": [{"ip-address-type": "ipv4", "ip-address": ip} for ip in ips]}


def test_agent_ipv4_skips_docker_bridge():
    # docker0 reported FIRST, real LAN address second — must return the LAN address
    ip = _agent_ipv4([_iface("docker0", "172.17.0.1"), _iface("eth0", "192.168.1.50")])
    assert ip == "192.168.1.50", f"must skip docker0 bridge, got {ip}"
    print("test_agent_ipv4_skips_docker_bridge OK")


def test_agent_ipv4_skips_loopback_and_linklocal():
    ip = _agent_ipv4([_iface("lo", "127.0.0.1"), _iface("eth0", "169.254.10.10", "10.0.50.20")])
    assert ip == "10.0.50.20", f"must skip loopback + link-local, got {ip}"
    print("test_agent_ipv4_skips_loopback_and_linklocal OK")


def test_agent_ipv4_none_when_only_nonroutable():
    assert _agent_ipv4([_iface("lo", "127.0.0.1"), _iface("eth0", "169.254.1.1")]) is None
    print("test_agent_ipv4_none_when_only_nonroutable OK")


# --------------------------------------------------------------------------- #
# G4 — VNC session sweep + no-store                                            #
# --------------------------------------------------------------------------- #
def test_sweep_vnc_sessions_drops_expired():
    api._VNC_SESS.clear()
    now = time.time()
    api._VNC_SESS["fresh"] = {"exp": now + 30}
    api._VNC_SESS["stale"] = {"exp": now - 5}
    api._sweep_vnc_sessions()
    assert "fresh" in api._VNC_SESS and "stale" not in api._VNC_SESS, list(api._VNC_SESS)
    api._VNC_SESS.clear()
    print("test_sweep_vnc_sessions_drops_expired OK")


def test_vncproxy_sets_no_store_and_sweeps():
    with session_scope() as s:
        u = User(email="w32-vnc@x.io", name="U", password_hash=hash_password("StrongPass12!"))
        s.add(u); s.flush(); uid = u.id
        c = Connection(name="w32-c", host="10.0.0.1", token_id="t@pve!x", node="pve")
        s.add(c); s.flush(); cid = c.id
        d = Deployment(name="vm", owner_id=uid, connection_id=cid, vmid=settings.vmid_min,
                       node="pve", status="running")
        s.add(d); s.flush(); did = d.id

    class _Px:
        def __init__(self, conn): pass
        def pick_node(self): return "pve"
        def vncproxy(self, vmid, node): return {"port": 5900, "ticket": "TICKET"}

    api._VNC_SESS.clear()
    api._VNC_SESS["stale"] = {"exp": time.time() - 5}   # an abandoned token
    orig = api.Proxmox
    api.Proxmox = _Px
    resp = Response()
    try:
        with session_scope() as s:
            out = api.vm_vncproxy(did, resp, user=s.get(User, uid), session=s)
    finally:
        api.Proxmox = orig
    assert out["ticket"] == "TICKET" and out["wsToken"], out
    assert resp.headers.get("Cache-Control") == "no-store", "VNC ticket must not be cached"
    assert "stale" not in api._VNC_SESS, "abandoned token must be swept on proxy-create"
    api._VNC_SESS.clear()
    print("test_vncproxy_sets_no_store_and_sweeps OK")


if __name__ == "__main__":
    test_agent_ipv4_skips_docker_bridge()
    test_agent_ipv4_skips_loopback_and_linklocal()
    test_agent_ipv4_none_when_only_nonroutable()
    test_sweep_vnc_sessions_drops_expired()
    test_vncproxy_sets_no_store_and_sweeps()
    print("\nALL WAVE 32 UNIT TESTS PASSED")
