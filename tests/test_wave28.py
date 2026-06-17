"""Wave 28 — review Batch C: input validation & injection hardening.

C1: deploy-input answers must reject control characters / newlines (a multi-line
    scalar could otherwise inject sibling YAML keys into a module-arg dict).
C2: cloud-init render_shell must NOT corrupt literal shell braces (awk '{..}',
    ${VAR}, brace-expansion) — it now uses the regex substitutor like the ansible path.
C4: the MariaDB/PostgreSQL built-in blocks shell-quote {database}/{allow_cidr} so a
    crafted value can't break out of the shell command.
C5: secret/variable names must match the resolver charset [A-Za-z0-9_]+ (a name with
    a space/dash/dot is silently unreferenceable as {{ secrets.NAME }}).
C6: Proxmox node/storage ids (which flow into proxmoxer URL paths) must match a safe
    allowlist at connection create/edit.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave28.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave28-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from fastapi import HTTPException                 # noqa: E402
from app.db import init_db, session_scope         # noqa: E402
from app import api, recipes                       # noqa: E402
from app.models import Block, Template, User       # noqa: E402
from app.security import hash_password             # noqa: E402

init_db()


def _mk_user(email, role="user"):
    with session_scope() as s:
        u = User(email=email, name="U", password_hash=hash_password("StrongPass12!"), role=role)
        s.add(u); s.flush()
        return u.id


def _expect_http(code, fn):
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == code, (e.status_code, e.detail)
        return e
    raise AssertionError(f"expected HTTPException {code}")


# --------------------------------------------------------------------------- #
# C1 — deploy inputs reject control chars / newlines                           #
# --------------------------------------------------------------------------- #
def test_deploy_input_rejects_newline():
    with session_scope() as s:
        s.add(Block(key="b-c1", name="C1", phase="ansible",
                    ansible_template="- name: x\n  ansible.builtin.debug: msg={hostname}",
                    input_schema_json=json.dumps([{"name": "hostname", "type": "text"}])))
        t = Template(name="c1", recipe_json=json.dumps(
            [{"id": "s-inst", "name": "Install",
              "blocks": [{"ref": "b-c1", "inputs": {"hostname": "ok"}, "ask": ["hostname"]}]}]))
        s.add(t); s.flush(); tid = t.id
    with session_scope() as s:
        tpl = s.get(Template, tid)
        # a clean value is accepted
        api._validate_deploy_inputs(s, tpl, {"0.0": {"hostname": "web01"}})
        # a value with a newline (sibling-key injection vector) is rejected
        _expect_http(400, lambda: api._validate_deploy_inputs(
            s, tpl, {"0.0": {"hostname": "web01\n      evilkey: evil"}}))
    print("test_deploy_input_rejects_newline OK")


# --------------------------------------------------------------------------- #
# C2 — render_shell preserves literal braces                                   #
# --------------------------------------------------------------------------- #
def test_render_shell_preserves_literal_braces():
    # C2 fix: these idioms previously made str.format_map raise (or collapse), so the
    # WHOLE cloud-init template rendered unchanged (no input substituted). The regex
    # substitutor leaves non-{word} braces intact while still filling real placeholders.
    # (Note: bare ${VAR} collides with the {word} placeholder syntax and is a separate,
    # pre-existing limitation on BOTH the ansible and cloud-init paths — out of scope.)
    tmpl = "awk '{print $1}' /etc/hostname\nNAME={name}\nJQ=$(echo x | jq '{a:.b}')\ncp a.{txt,bak} /tmp/"
    out = recipes.render_shell(tmpl, {"name": "web"}, {"name": "text"}, lambda ns, n: "")
    assert "awk '{print $1}'" in out, f"awk braces corrupted: {out!r}"
    assert "jq '{a:.b}'" in out, f"jq braces corrupted: {out!r}"
    assert "a.{txt,bak}" in out, f"brace expansion corrupted: {out!r}"
    assert "NAME=web" in out, f"real placeholder not substituted: {out!r}"
    print("test_render_shell_preserves_literal_braces OK")


# --------------------------------------------------------------------------- #
# C4 — DB blocks shell-quote {database} / {allow_cidr}                          #
# --------------------------------------------------------------------------- #
def test_db_block_shell_quotes_database():
    from app.seed import BUILTIN_BLOCKS
    spec = next(b for b in BUILTIN_BLOCKS if b["key"] == "b-mariadb")
    blk = Block(key="b-mariadb", name="m", phase="ansible",
                ansible_template=spec["ansible"],
                input_schema_json=json.dumps(spec["input_schema"]))
    recipe = [{"id": "s-inst", "name": "Install", "blocks": [
        {"ref": "b-mariadb", "inputs": {"database": "x'; touch /tmp/pwn; '", "user": "u", "password": "p"}}]}]
    yaml = recipes.compile_ansible(recipe, {"b-mariadb": blk}, lambda ns, n: "", name="t")
    # the dangerous value must not appear unquoted such that it closes the SQL quote
    assert "x'; touch /tmp/pwn; '" not in yaml, f"database value not shell-quoted:\n{yaml}"
    print("test_db_block_shell_quotes_database OK")


# --------------------------------------------------------------------------- #
# C5 — secret/variable names validated against resolver charset                #
# --------------------------------------------------------------------------- #
def test_secret_name_charset_enforced():
    u = _mk_user("w28-s1@x.io", role="admin")
    with session_scope() as s:
        _expect_http(400, lambda: api.add_secret(
            api.SecretBody(name="bad-name space", value="v", scope="global"),
            user=s.get(User, u), session=s))
        api.add_secret(api.SecretBody(name="GOOD_NAME_1", value="v", scope="global"),
                       user=s.get(User, u), session=s)
    print("test_secret_name_charset_enforced OK")


def test_variable_name_charset_enforced():
    u = _mk_user("w28-v1@x.io", role="admin")
    with session_scope() as s:
        _expect_http(400, lambda: api.add_variable(
            api.VariableBody(name="bad.name", value="v", scope="global"),
            user=s.get(User, u), session=s))
        api.add_variable(api.VariableBody(name="OK_VAR", value="v", scope="global"),
                         user=s.get(User, u), session=s)
    print("test_variable_name_charset_enforced OK")


# --------------------------------------------------------------------------- #
# C6 — Proxmox node/storage ids allowlisted                                    #
# --------------------------------------------------------------------------- #
def test_connection_storage_id_allowlisted():
    adm = _mk_user("w28-c6@x.io", role="admin")
    with session_scope() as s:
        _expect_http(400, lambda: api.add_connection(
            api.ConnBody(name="c6", host="10.0.0.1", token_id="t@pve!x", token_secret="s",
                         node="../../etc"),
            user=s.get(User, adm), session=s))
        # a valid node id is accepted
        api.add_connection(
            api.ConnBody(name="c6ok", host="10.0.0.1", token_id="t@pve!x", token_secret="s",
                         node="pve-dell", iso_storage="local-lvm"),
            user=s.get(User, adm), session=s)
    print("test_connection_storage_id_allowlisted OK")


if __name__ == "__main__":
    test_deploy_input_rejects_newline()
    test_render_shell_preserves_literal_braces()
    test_db_block_shell_quotes_database()
    test_secret_name_charset_enforced()
    test_variable_name_charset_enforced()
    test_connection_storage_id_allowlisted()
    print("\nALL WAVE 28 UNIT TESTS PASSED")
