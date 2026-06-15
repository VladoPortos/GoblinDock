"""Wave 21 — MOTD block writes a static /etc/motd (cross-distro).

The MOTD block used to drop an executable script into /etc/update-motd.d, which only
exists on Debian/Ubuntu (the dynamic-MOTD framework) — it failed on RHEL/Oracle with
"Destination directory /etc/update-motd.d does not exist". It now writes a static
/etc/motd, which pam_motd/sshd read on every distro.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave21.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")

import yaml  # noqa: E402

from app.recipes import _ansible_flat, _substitute, render_shell  # noqa: E402
from app.seed import BUILTIN_BLOCKS  # noqa: E402

_MOTD = next(b for b in BUILTIN_BLOCKS if b["key"] == "b-motd")
_INP = {"banner": "line one\nline two\n", "disable_default": True}
_TYPES = {"banner": "code", "disable_default": "bool"}


def test_motd_writes_etc_motd_ansible():
    pb = _substitute(_MOTD["ansible"], _ansible_flat(_INP, _TYPES, None))
    assert "dest: /etc/motd" in pb, pb
    # the writing task must not target the Debian-only dynamic-MOTD dir
    assert "/etc/update-motd.d/01-goblindock" not in pb, pb
    # multi-line banner stays valid YAML (indentation-aware substitution)
    tasks = yaml.safe_load(pb)
    assert tasks[0]["ansible.builtin.copy"]["content"].splitlines()[:2] == ["line one", "line two"], pb
    print("test_motd_writes_etc_motd_ansible OK")


def test_motd_writes_etc_motd_cloudinit():
    sh = render_shell(_MOTD["cloudinit"], _INP, _TYPES, None)
    assert "cat > /etc/motd <<" in sh, sh
    assert "/etc/update-motd.d/01-goblindock" not in sh, sh
    print("test_motd_writes_etc_motd_cloudinit OK")


if __name__ == "__main__":
    test_motd_writes_etc_motd_ansible()
    test_motd_writes_etc_motd_cloudinit()
    print("\nALL WAVE 21 UNIT TESTS PASSED")
