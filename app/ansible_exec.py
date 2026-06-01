"""Run a compiled Ansible playbook against a VM via ansible-runner.

Used for the POST-BOOT (phase='ansible') blocks of a recipe — both when baking a
golden image (against the temporary build VM) and when configuring a freshly
deployed VM. First-boot (phase='cloudinit') blocks go through cloud-init instead.
"""
from __future__ import annotations

import os
import tempfile
from typing import Callable, Optional


def run_playbook(
    playbook_yaml: str,
    host: str,
    ssh_user: str,
    private_key_pem: str,
    on_line: Optional[Callable[[str], None]] = None,
    timeout: int = 1200,
) -> tuple[str, int]:
    """Run the playbook against `host` over SSH. Returns (status, rc).

    status is ansible-runner's: 'successful' | 'failed' | 'timeout' | 'canceled'.
    Streams stdout lines to `on_line` if provided.
    """
    import ansible_runner

    with tempfile.TemporaryDirectory(prefix="gd-ansible-") as d:
        proj = os.path.join(d, "project")
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, "play.yml"), "w") as f:
            f.write(playbook_yaml)
        key_path = os.path.join(d, "id_key")
        with open(key_path, "w") as f:
            f.write(private_key_pem if private_key_pem.endswith("\n") else private_key_pem + "\n")
        os.chmod(key_path, 0o600)

        # The container runs non-root with an overridable uid (compose `user:`), so
        # HOME is usually `/` and unwritable — ansible would fail creating
        # ~/.ansible/tmp. Point HOME and ansible's local temp at the per-run tempdir,
        # which is always writable by whatever uid the process runs as.
        home = os.path.join(d, "home")
        local_tmp = os.path.join(home, ".ansible", "tmp")
        os.makedirs(local_tmp, exist_ok=True)

        inventory = {
            "all": {"hosts": {host: {"ansible_host": host, "ansible_user": ssh_user,
                                      "ansible_python_interpreter": "/usr/bin/python3"}}}
        }

        def _event(ev):
            if on_line:
                line = ev.get("stdout")
                if line:
                    for ln in line.splitlines():
                        if ln.strip():
                            on_line(ln)
            return True

        r = ansible_runner.run(
            private_data_dir=d,
            project_dir=proj,
            playbook="play.yml",
            inventory=inventory,
            envvars={
                "HOME": home,
                "ANSIBLE_LOCAL_TEMP": local_tmp,
                # pre-built blocks use ansible.posix / community.* collections that the
                # image installs here; the runner's tempdir HOME wouldn't otherwise see them
                "ANSIBLE_COLLECTIONS_PATH": "/usr/share/ansible/collections",
                "ANSIBLE_HOST_KEY_CHECKING": "False",
                "ANSIBLE_RETRY_FILES_ENABLED": "False",
                "ANSIBLE_SSH_ARGS": "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                                    "-o ConnectTimeout=15 -o ServerAliveInterval=15",
                "ANSIBLE_TIMEOUT": "30",
                "ANSIBLE_NOCOLOR": "1",
            },
            cmdline=f"--private-key {key_path}",
            quiet=True,
            event_handler=_event,
        )
        return r.status, (r.rc if r.rc is not None else 1)
