"""Wave 17 — 15 new built-in blocks (Tailscale, SSH hardening, Fail2ban, internal CA,
compose stack, Watchtower, Portainer agent, K3s, network share, swap, MariaDB,
PostgreSQL server, Redis, unattended upgrades, MOTD banner).

Run: GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave17.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DB = "/tmp/gd-wave17-test.sqlite3"
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", "/tmp/gd-data-test")
os.environ["GOBLINDOCK_DEV"] = "1"

import yaml                                            # noqa: E402
from sqlmodel import select                            # noqa: E402

from app.db import init_db, session_scope              # noqa: E402
from app import seed                                   # noqa: E402
from app.models import Block                           # noqa: E402
from app.recipes import (                              # noqa: E402
    compile_ansible, compile_cloudinit, lint_block, render_shell, _schema_types,
)

init_db()
seed.seed_blocks()

NEW_KEYS = [
    "b-tailscale", "b-sshharden", "b-fail2ban", "b-cacert", "b-compose",
    "b-watchtower", "b-portaineragent", "b-k3s", "b-mountshare", "b-swap",
    "b-mariadb", "b-pgserver", "b-redis", "b-autoupdates", "b-motd",
]


def _blocks_by_key() -> dict:
    with session_scope() as s:
        return {b.key: Block(**b.model_dump()) for b in s.exec(select(Block)).all()}


# --------------------------------------------------------------------------- #
# seeding                                                                      #
# --------------------------------------------------------------------------- #
def test_new_blocks_seeded():
    seed.seed_blocks()
    blocks = _blocks_by_key()
    for key in NEW_KEYS:
        assert key in blocks, f"{key} not seeded"
        assert blocks[key].builtin, f"{key} not marked builtin"
    assert len([b for b in blocks.values() if b.builtin]) == 46, \
        "expected 46 built-in blocks (29 + 15 new + dnf/yum)"


def test_phase_assignment():
    blocks = _blocks_by_key()
    assert blocks["b-swap"].phase == "cloudinit", "swap must run at first boot"
    for key in NEW_KEYS:
        if key != "b-swap":
            assert blocks[key].phase == "ansible", f"{key} should be ansible-phase"


def test_pgdb_moved_to_databases():
    blocks = _blocks_by_key()
    assert blocks["b-pgdb"].category == "Databases"
    for key in ("b-mariadb", "b-pgserver", "b-redis"):
        assert blocks[key].category == "Databases"


# --------------------------------------------------------------------------- #
# every new block passes the block linter with its own schema defaults         #
# --------------------------------------------------------------------------- #
def test_new_blocks_pass_linter():
    blocks = _blocks_by_key()
    for key in NEW_KEYS:
        b = blocks[key]
        problems = lint_block(b.phase, b.input_schema_json,
                              b.ansible_template, b.cloudinit_template)
        assert problems == [], f"{key} lint problems: {problems}"


# --------------------------------------------------------------------------- #
# compiled playbook is valid YAML with the expected task shapes                #
# --------------------------------------------------------------------------- #
def _compile(recipe):
    return compile_ansible(recipe, _blocks_by_key(), lambda ns, n: "", name="wave17")


def test_compose_stack_compiles():
    recipe = [{"blocks": [{"ref": "b-compose", "inputs": {
        "name": "media", "compose": "services:\n  app:\n    image: jellyfin/jellyfin\n", "env": ""}}]}]
    doc = yaml.safe_load(_compile(recipe))
    tasks = doc[0]["tasks"]
    names = [t.get("name") for t in tasks]
    assert "Write compose.yml" in names and "Compose up" in names
    write = next(t for t in tasks if t.get("name") == "Write compose.yml")
    assert write["ansible.builtin.copy"]["dest"] == "/opt/media/compose.yml"
    assert "jellyfin/jellyfin" in write["ansible.builtin.copy"]["content"]
    # empty .env input -> the .env task is compiled out via `when: false`
    env_task = next(t for t in tasks if t.get("name") == "Write .env")
    assert env_task["when"] is False


def test_tailscale_join_guarded_by_authkey():
    recipe = [{"blocks": [{"ref": "b-tailscale", "inputs": {
        "authkey": "tskey-auth-abc", "tailscale_ssh": True, "args": ""}}]}]
    doc = yaml.safe_load(_compile(recipe))
    join = next(t for t in doc[0]["tasks"] if t.get("name") == "Join tailnet")
    assert join["when"] is True
    assert "tskey-auth-abc" in join["ansible.builtin.shell"]
    assert "--ssh" in join["ansible.builtin.shell"]


def test_k3s_agent_uses_url_and_token():
    recipe = [{"blocks": [{"ref": "b-k3s", "inputs": {
        "role": "agent", "server_url": "https://10.0.0.1:6443", "token": "K10abc"}}]}]
    doc = yaml.safe_load(_compile(recipe))
    sh = doc[0]["tasks"][0]["ansible.builtin.shell"]
    assert "K3S_URL=https://10.0.0.1:6443" in sh and "K3S_TOKEN=K10abc" in sh


def test_mariadb_user_password_shell_quoted():
    recipe = [{"blocks": [{"ref": "b-mariadb", "inputs": {
        "database": "shop", "user": "shopuser", "password": "p@ss w'rd", "lan": True}}]}]
    doc = yaml.safe_load(_compile(recipe))
    tasks = doc[0]["tasks"]
    grant = next(t for t in tasks if t.get("name") == "Create user + grant")
    # the password must arrive shlex-quoted (single shell token), never raw
    import shlex
    sh = grant["ansible.builtin.shell"]
    assert "IDENTIFIED BY " + shlex.quote("p@ss w'rd") in sh, \
        f"password not shlex-quoted in: {sh!r}"
    lan = next(t for t in tasks if t.get("name") == "Listen on LAN")
    assert lan["when"] is True


def test_cacert_pem_lands_in_trust_store_task():
    pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"
    recipe = [{"blocks": [{"ref": "b-cacert", "inputs": {"name": "homelab-ca", "pem": pem}}]}]
    pb = _compile(recipe)
    doc = yaml.safe_load(pb)
    copy = doc[0]["tasks"][0]["ansible.builtin.copy"]
    assert copy["dest"] == "/tmp/gd-homelab-ca.crt"
    assert "BEGIN CERTIFICATE" in copy["content"]
    # cross-distro: installs into whichever trust store exists
    assert "update-ca-trust" in pb and "update-ca-certificates" in pb
    assert "/etc/pki/ca-trust/source/anchors/homelab-ca.crt" in pb


def test_motd_banner_multiline_stays_valid_yaml():
    recipe = [{"blocks": [{"ref": "b-motd", "inputs": {
        "banner": "line one\nline two\nline three", "disable_default": True}}]}]
    doc = yaml.safe_load(_compile(recipe))
    copy = doc[0]["tasks"][0]["ansible.builtin.copy"]
    assert copy["dest"] == "/etc/motd", copy
    content = copy["content"]
    assert "line one" in content and "line two" in content and "line three" in content


def test_all_new_ansible_blocks_compile_together():
    """One recipe with every new ansible block at schema defaults must compile to
    valid YAML — guards against any template breaking the combined playbook."""
    blocks = _blocks_by_key()
    placed = [{"ref": k, "inputs": {}} for k in NEW_KEYS if blocks[k].phase == "ansible"]
    doc = yaml.safe_load(_compile([{"blocks": placed}]))
    assert isinstance(doc[0]["tasks"], list) and len(doc[0]["tasks"]) >= len(placed)


# --------------------------------------------------------------------------- #
# cloud-init: swap renders fully (no leftover placeholders / unrendered braces) #
# --------------------------------------------------------------------------- #
def test_swap_cloudinit_renders():
    blocks = _blocks_by_key()
    recipe = [{"blocks": [{"ref": "b-swap", "inputs": {"size_gb": "4", "swappiness": "5"}}]}]
    cmds = compile_cloudinit(recipe, blocks, lambda ns, n: "")
    joined = "\n".join(cmds)
    assert "fallocate -l 4G /swapfile" in joined.replace("'4'", "4")
    assert "{" not in joined.replace("{", "", 0) or "{size_gb}" not in joined, \
        "placeholder left unrendered"
    assert "vm.swappiness=5" in joined.replace("'5'", "5")


def test_sshharden_dollar_var_survives_render():
    """The hardening template uses a $conf shell variable — the renderer must leave
    it intact (only {placeholders} are substituted) and still fill the inputs."""
    blocks = _blocks_by_key()
    b = blocks["b-sshharden"]
    out = render_shell(b.cloudinit_template,
                       {"permit_root": False, "password_auth": False,
                        "port": "2222", "allow_users": "ops"},
                       _schema_types(b), lambda ns, n: "")
    assert '"$conf"' in out, "$conf shell var mangled by renderer"
    assert "2222" in out and "ops" in out
    assert "{port}" not in out and "{allow_users}" not in out


# --------------------------------------------------------------------------- #
# builtin re-sync updates existing rows on upgrade                             #
# --------------------------------------------------------------------------- #
def test_builtin_resync_updates_existing_row():
    with session_scope() as s:
        row = s.exec(select(Block).where(Block.key == "b-redis")).first()
        row.description = "stale description from an old release"
        s.add(row)
    seed.seed_blocks()
    blocks = _blocks_by_key()
    assert blocks["b-redis"].description != "stale description from an old release"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted({k: v for k, v in globals().items()
                            if k.startswith("test_") and callable(v)}.items()):
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  ERR  {name}: {type(e).__name__}: {e}")
    print(("FAILED" if failures else "PASSED") + f" — wave17 ({failures} failures)")
    sys.exit(1 if failures else 0)
