"""Wave 0 — deployment posture / proxy-IP / audit-IP unit checks.

Run: GOBLINDOCK_SECRET_KEY=<64hex> GOBLINDOCK_DATA_DIR=/tmp/gd-data-test \
     .venv/bin/python tests/test_wave0.py
"""
import os
import sys
from types import SimpleNamespace

# Ensure repo root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _conn(peer, xff=None):
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_env_or_file(tmpdir="/tmp/gd-wave0"):
    from app.config import _env_or_file
    os.makedirs(tmpdir, exist_ok=True)
    # env wins
    os.environ["GD_X_TEST"] = "fromenv"
    assert _env_or_file("GD_X_TEST") == "fromenv"
    # file fallback
    del os.environ["GD_X_TEST"]
    p = os.path.join(tmpdir, "secret")
    with open(p, "w") as f:
        f.write("  fromfile\n")
    os.environ["GD_X_TEST_FILE"] = p
    assert _env_or_file("GD_X_TEST") == "fromfile"  # stripped
    # an empty/whitespace env var falls through to the file
    os.environ["GD_X_TEST"] = "  "
    assert _env_or_file("GD_X_TEST") == "fromfile"
    # env value is whitespace-stripped (no trailing-newline secret breakage)
    os.environ["GD_X_TEST"] = " tok\n"
    assert _env_or_file("GD_X_TEST") == "tok"
    del os.environ["GD_X_TEST"]
    del os.environ["GD_X_TEST_FILE"]
    # default
    assert _env_or_file("GD_X_TEST", "def") == "def"
    print("test_env_or_file OK")


def test_client_ip():
    from app import netutil
    from app.config import settings

    # no trusted proxies -> always the socket peer, even with a spoofed XFF
    settings.forwarded_allow_ips = set()
    assert netutil.client_ip(_conn("203.0.113.9", xff="1.2.3.4")) == "203.0.113.9"

    # trusted proxy peer -> real client is the RIGHT-most (proxy-appended) entry,
    # NOT the spoofable left-most one
    settings.forwarded_allow_ips = {"203.0.113.9"}
    assert netutil.client_ip(_conn("203.0.113.9", xff="1.2.3.4, 10.0.0.1")) == "10.0.0.1"

    # anti-spoof: a client that PREPENDS a fake IP cannot forge it; the proxy appends
    # the real peer (8.8.8.8) to the right, which is what we return
    assert netutil.client_ip(_conn("203.0.113.9", xff="1.1.1.1, 8.8.8.8")) == "8.8.8.8"

    # untrusted peer (not in allow-list) -> ignore XFF, use peer
    assert netutil.client_ip(_conn("198.51.100.5", xff="1.2.3.4")) == "198.51.100.5"

    # CIDR-range proxy trust (e.g. a docker bridge subnet); single client entry
    settings.forwarded_allow_ips = {"172.18.0.0/16"}
    assert netutil.client_ip(_conn("172.18.0.5", xff="1.2.3.4")) == "1.2.3.4"
    assert netutil.client_ip(_conn("10.1.2.3", xff="1.2.3.4")) == "10.1.2.3"  # outside CIDR
    # chained in-CIDR proxies are skipped; real client (outside CIDR) is returned
    assert netutil.client_ip(_conn("172.18.0.3", xff="50.50.50.50, 172.18.0.9, 172.18.0.3")) == "50.50.50.50"

    # garbage / non-IP XFF token is never returned -> fall back to the socket peer
    assert netutil.client_ip(_conn("172.18.0.5", xff="not-an-ip")) == "172.18.0.5"

    # wildcard trust -> take the right-most appended entry
    settings.forwarded_allow_ips = {"*"}
    assert netutil.client_ip(_conn("10.9.9.9", xff="9.9.9.9")) == "9.9.9.9"
    assert netutil.client_ip(_conn("10.9.9.9", xff="1.1.1.1, 9.9.9.9")) == "9.9.9.9"

    # trusted peer but no XFF header -> fall back to peer
    settings.forwarded_allow_ips = {"10.0.0.2"}
    assert netutil.client_ip(_conn("10.0.0.2")) == "10.0.0.2"

    settings.forwarded_allow_ips = set()
    print("test_client_ip OK")


def test_audit_records_ip():
    from app import netutil
    from app.models import Audit
    # record_audit pulls the IP from the contextvar
    netutil.set_request_ip("192.0.2.77")
    a = Audit(user_id=1, user_name="x", action="login", target_type="user",
              target_id="1", ip=netutil.current_request_ip())
    assert a.ip == "192.0.2.77"
    netutil.set_request_ip("")
    print("test_audit_records_ip OK")


if __name__ == "__main__":
    test_env_or_file()
    test_client_ip()
    test_audit_records_ip()
    print("\nALL WAVE 0 UNIT TESTS PASSED")
