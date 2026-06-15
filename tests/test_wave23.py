"""Wave 23 — Debian-only blocks are now OS-aware (apt / dnf / yum, + RHEL tools).

Each of these blocks used to assume Debian/Ubuntu (ansible.builtin.apt, ufw,
update-ca-certificates, unattended-upgrades, Debian package/service names) and failed
on Oracle Linux / RHEL. They now detect the package manager / tooling at runtime
(the playbook runs gather_facts: false, so detection is shell-based, not via facts).

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave23.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GOBLINDOCK_DEV", "1")

from app.seed import BUILTIN_BLOCKS  # noqa: E402

_BYK = {b["key"]: b for b in BUILTIN_BLOCKS}


def _both(key: str) -> str:
    b = _BYK[key]
    return (b.get("ansible", "") or "") + "\n" + (b.get("cloudinit", "") or "")


# (block key, substrings that prove it handles a non-Debian path)
_EXPECT = {
    "b-clean":       ["dnf clean all", "yum clean all"],
    "b-pip":         ["dnf install -y python3-pip"],
    "b-mountshare":  ["dnf install -y nfs-utils cifs-utils"],
    "b-fail2ban":    ["epel-release", "dnf install -y fail2ban"],
    "b-cacert":      ["update-ca-trust", "/etc/pki/ca-trust/source/anchors"],
    "b-mariadb":     ["dnf install -y mariadb-server", "/etc/my.cnf.d"],
    "b-pgserver":    ["postgresql-server", "postgresql-setup"],
    "b-redis":       ["dnf install -y redis"],
    "b-autoupdates": ["dnf-automatic"],
    "b-ufw":         ["firewall-cmd"],
}


def test_no_block_is_apt_only():
    # None of these blocks may still use the Debian-only `ansible.builtin.apt` module.
    for key in _EXPECT:
        assert "ansible.builtin.apt:" not in _BYK[key].get("ansible", ""), \
            f"{key} still uses ansible.builtin.apt"
    print("test_no_block_is_apt_only OK")


def test_blocks_handle_rhel():
    for key, needles in _EXPECT.items():
        body = _both(key)
        for n in needles:
            assert n in body, f"{key} missing RHEL handling: {n!r}"
    print("test_blocks_handle_rhel OK")


def test_ufw_renamed_and_firewalld_mapped():
    b = _BYK["b-ufw"]
    assert b["name"] == "Firewall Rule", b["name"]
    assert "community.general.ufw" not in b.get("ansible", "")
    assert "--add-port=" in b["ansible"] and "--remove-port=" in b["ansible"]
    print("test_ufw_renamed_and_firewalld_mapped OK")


if __name__ == "__main__":
    test_no_block_is_apt_only()
    test_blocks_handle_rhel()
    test_ufw_renamed_and_firewalld_mapped()
    print("\nALL WAVE 23 UNIT TESTS PASSED")
