"""Wave 24 — Unified User block + auto-generated VM root password.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave24.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GOBLINDOCK_DEV", "1")
# Point the DB at a throwaway file BEFORE importing app.config/app.db (config reads env at import).
os.environ["GOBLINDOCK_DB"] = tempfile.mktemp(suffix=".sqlite3")


def test_deployment_has_password_columns():
    from app.db import init_db, engine
    init_db()
    with engine.begin() as c:
        cols = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(deployments)")}
    assert "root_password_enc" in cols, cols
    assert "cred_user" in cols, cols
    print("test_deployment_has_password_columns OK")


def test_password_helpers():
    import crypt
    from app.security import gen_vm_password, crypt_sha512, encrypt, decrypt
    p = gen_vm_password()
    assert len(p) == 20, len(p)
    assert not (set(p) & set("O0lI1")), "ambiguous chars must be excluded"
    assert gen_vm_password() != gen_vm_password(), "must be random"
    h = crypt_sha512("hunter2hunter2")
    assert h.startswith("$6$"), h
    assert crypt.crypt("hunter2hunter2", h) == h, "hash must verify"
    assert decrypt(encrypt(p)) == p, "encrypt/decrypt round-trip"
    print("test_password_helpers OK")


def test_auto_root_password_setting():
    from app import appsettings
    # default ON when unset
    assert appsettings.auto_root_password_enabled() is True
    appsettings.set_setting(appsettings.AUTO_ROOT_PASSWORD, "0")
    assert appsettings.auto_root_password_enabled() is False
    appsettings.set_setting(appsettings.AUTO_ROOT_PASSWORD, "1")
    assert appsettings.auto_root_password_enabled() is True
    # Junk / empty stored values must fail OPEN (feature stays on).
    appsettings.set_setting(appsettings.AUTO_ROOT_PASSWORD, "garbage")
    assert appsettings.auto_root_password_enabled() is True
    appsettings.set_setting(appsettings.AUTO_ROOT_PASSWORD, "")
    assert appsettings.auto_root_password_enabled() is True
    print("test_auto_root_password_setting OK")


def test_unified_user_block():
    from app.seed import BUILTIN_BLOCKS
    byk = {b["key"]: b for b in BUILTIN_BLOCKS}
    assert "b-ssh" not in byk, "b-ssh must be removed (merged into b-user)"
    b = byk["b-user"]
    assert b["name"] == "User", b["name"]
    names = {f["name"] for f in b["input_schema"]}
    for need in ("user", "password", "public_key", "groups", "home", "shell",
                 "sudoers", "nopasswd", "ssh_password_login"):
        assert need in names, f"missing field {need}"
    a = b["ansible"]
    assert "ansible.posix.authorized_key" in a, "must push SSH key"
    assert "visudo -cf %s" in a, "sudoers must be validated"
    assert "NOPASSWD:ALL" in a and "ALL=(ALL) ALL" in a, "both sudo modes present"
    print("test_unified_user_block OK")


def test_user_block_compiles():
    from app.seed import BUILTIN_BLOCKS
    from app.recipes import compile_ansible
    from app.models import Block
    spec = next(b for b in BUILTIN_BLOCKS if b["key"] == "b-user")
    block = Block(key="b-user", phase="ansible", name="User",
                  ansible_template=spec["ansible"],
                  input_schema_json=json.dumps(spec["input_schema"]))
    nolookup = lambda kind, name: ""  # noqa: E731
    # passwordless sudo ON → the NOPASSWD sudoers task is ACTIVE (when: true and true)
    recipe = [{"blocks": [{"ref": "b-user",
                           "inputs": {"user": "alice", "sudoers": True, "nopasswd": True}}]}]
    out = compile_ansible(recipe, {"b-user": block}, nolookup, "t")
    assert "alice ALL=(ALL) NOPASSWD:ALL" in out, out
    assert "/etc/sudoers.d/90-alice" in out
    assert "when: true and true" in out, "passwordless sudoers task must be active"
    # sudoers OFF → the sudoers tasks still render but are gated off. Ansible always emits
    # the task body, so assert the GATING (when: false ...), not the absence of the path.
    recipe2 = [{"blocks": [{"ref": "b-user", "inputs": {"user": "bob", "sudoers": False}}]}]
    out2 = compile_ansible(recipe2, {"b-user": block}, nolookup, "t")
    assert "when: false and false" in out2, "sudoers tasks must be gated off when sudoers=False"
    print("test_user_block_compiles OK")


def test_seed_prunes_removed_builtins():
    from sqlmodel import select
    from app.db import session_scope
    from app.models import Block
    from app.seed import seed_blocks
    seed_blocks()
    with session_scope() as s:
        s.add(Block(key="b-ssh", name="Legacy", kind="builtin", builtin=True,
                    phase="cloudinit", input_schema_json="[]"))
    seed_blocks()
    with session_scope() as s:
        assert s.exec(select(Block).where(Block.key == "b-ssh")).first() is None, \
            "removed builtin must be pruned"
    with session_scope() as s:
        s.add(Block(key="c-mine", name="Mine", kind="custom", builtin=False,
                    phase="ansible", input_schema_json="[]"))
    seed_blocks()
    with session_scope() as s:
        assert s.exec(select(Block).where(Block.key == "c-mine")).first() is not None, \
            "custom block must NOT be pruned"
    print("test_seed_prunes_removed_builtins OK")


def test_user_block_yaml_injection_safe():
    import yaml
    from app.seed import BUILTIN_BLOCKS
    from app.recipes import compile_ansible
    from app.models import Block
    spec = next(b for b in BUILTIN_BLOCKS if b["key"] == "b-user")
    block = Block(key="b-user", phase="ansible", name="User",
                  ansible_template=spec["ansible"],
                  input_schema_json=json.dumps(spec["input_schema"]))
    nolookup = lambda kind, name: ""  # noqa: E731
    # Hostile home/public_key values must NOT break the playbook YAML or inject sibling keys.
    recipe = [{"blocks": [{"ref": "b-user", "inputs": {
        "user": "carol",
        "home": "/srv/x\n      shell: /evil",
        "public_key": "ssh-ed25519 AAA\"q\nfoo: bar",
    }}]}]
    out = compile_ansible(recipe, {"b-user": block}, nolookup, "t")
    doc = yaml.safe_load(out)  # must parse — raises if the values injected structure
    assert doc, "playbook must remain valid YAML with hostile home/key values"
    print("test_user_block_yaml_injection_safe OK")


def test_cloud_config_root_password():
    from app.worker import _deploy_cloud_config
    cc = _deploy_cloud_config("vm1", ["ssh-ed25519 AAAA goblindock"], [], root_pw_hash="$6$abc$DEFhash")
    assert "chpasswd:" in cc, cc
    assert "name: root" in cc and "type: hash" in cc, cc
    assert "$6$abc$DEFhash" in cc, "the hash must be embedded"
    cc2 = _deploy_cloud_config("vm1", [], [], root_pw_hash="")
    assert "chpasswd:" not in cc2, cc2
    print("test_cloud_config_root_password OK")


def test_vm_detail_and_reveal():
    from sqlmodel import Session
    from fastapi import Response, HTTPException
    from app.db import init_db, engine
    from app.models import User, Deployment
    from app.security import encrypt, hash_password
    from app.api import vm_detail, reveal_vm_credentials
    init_db()
    with Session(engine) as s:
        owner = User(email="owner24@x.io", name="Owner", password_hash=hash_password("xxxxxxxxxxxx"), role="user")
        s.add(owner); s.commit(); s.refresh(owner)
        dep = Deployment(name="vm24", owner_id=owner.id, status="stopped",
                         root_password_enc=encrypt("SuperSecretPw24"), cred_user="root")
        s.add(dep); s.commit(); s.refresh(dep)
        oid, did = owner.id, dep.id
    with Session(engine) as s:
        out = vm_detail(did, user=s.get(User, oid), session=s)
        assert out["hasRootPassword"] is True
        assert out["credUser"] == "root"
        assert "SuperSecretPw24" not in json.dumps(out), "plaintext must not be serialized"
    with Session(engine) as s:
        r = reveal_vm_credentials(did, Response(), user=s.get(User, oid), session=s)
        assert r == {"user": "root", "password": "SuperSecretPw24"}, r
    with Session(engine) as s:
        other = User(email="other24@x.io", name="Other", password_hash=hash_password("xxxxxxxxxxxx"), role="user")
        s.add(other); s.commit(); s.refresh(other)
        try:
            reveal_vm_credentials(did, Response(), user=other, session=s)
            assert False, "expected 403"
        except HTTPException as e:
            assert e.status_code == 403, e.status_code
    # a VM with no stored password → 404 on reveal
    with Session(engine) as s:
        nopw = Deployment(name="vm24b", owner_id=oid, status="stopped")
        s.add(nopw); s.commit(); s.refresh(nopw)
        try:
            reveal_vm_credentials(nopw.id, Response(), user=s.get(User, oid), session=s)
            assert False, "expected 404"
        except HTTPException as e:
            assert e.status_code == 404, e.status_code
    print("test_vm_detail_and_reveal OK")


if __name__ == "__main__":
    test_deployment_has_password_columns()
    test_password_helpers()
    test_auto_root_password_setting()
    test_unified_user_block()
    test_user_block_compiles()
    test_seed_prunes_removed_builtins()
    test_user_block_yaml_injection_safe()
    test_cloud_config_root_password()
    test_vm_detail_and_reveal()
    print("\nALL WAVE 24 UNIT TESTS PASSED")
