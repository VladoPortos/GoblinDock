"""Wave 49 — no literal credentials at rest in the database.

Two plaintext sinks are closed: templates.recipe_json can no longer store a
literal password/secret input value (save/edit reject it; startup migrates
legacy literals into the encrypted Secret store), and ask-on-deploy answers
now live in the Fernet-encrypted deployments.deploy_inputs_enc column (the
legacy plaintext column is encrypted, blanked, then dropped on upgrade).

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave49.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave49-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from fastapi import HTTPException  # noqa: E402
from sqlmodel import select  # noqa: E402

from app import api, db  # noqa: E402
from app.db import engine, init_db, session_scope  # noqa: E402
from app.execution_plan import encrypt_deploy_inputs, open_deploy_inputs  # noqa: E402
from app.models import Block, Connection, Deployment, Image, Secret, Template, User  # noqa: E402
from app.security import decrypt  # noqa: E402
from app.seed import migrate_template_literal_secrets  # noqa: E402

init_db()


def _expect_http(code, fn):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == code, (exc.status_code, exc.detail)
        return exc
    raise AssertionError(f"expected HTTPException {code}")


def _mk_user(email: str) -> int:
    with session_scope() as s:
        user = User(email=email, name=email.split("@", 1)[0], password_hash="unused")
        s.add(user)
        s.flush()
        return user.id


def _mk_sensitive_block(owner_id: int, suffix: str) -> str:
    with session_scope() as s:
        block = Block(
            key=f"c-wave49-{suffix}", kind="custom", builtin=False,
            owner_id=owner_id, name="Credentialed block", phase="cloudinit",
            input_schema_json=json.dumps([
                {"name": "hostname", "type": "text", "label": "Host"},
                {"name": "admin_password", "type": "password", "label": "Password"},
            ]),
            cloudinit_template="echo {hostname} {admin_password}",
        )
        s.add(block)
        s.flush()
        return block.key


def test_template_save_rejects_literal_sensitive_values():
    suffix = os.urandom(4).hex()
    uid = _mk_user(f"wave49-author-{suffix}@example.com")
    key = _mk_sensitive_block(uid, suffix)

    def _save(inputs, ask=()):
        with session_scope() as s:
            return api.save_template(api.TemplateBody(
                name=f"wave49-{suffix}-{os.urandom(2).hex()}",
                recipe=[{"blocks": [{"ref": key, "inputs": inputs, "ask": list(ask)}]}],
                public=False,
            ), user=s.get(User, uid), session=s)

    exc = _expect_http(400, lambda: _save(
        {"hostname": "h", "admin_password": "Hunter2Hunter2!"},
    ))
    assert "secret reference" in exc.detail
    assert "Hunter2" not in exc.detail
    assert _save({"hostname": "h", "admin_password": "{{ secrets.ADMIN_PW }}"})["ok"]
    assert _save({"hostname": "h", "admin_password": ""}, ask=["admin_password"])["ok"]


def test_migration_moves_template_literals_into_secret_store():
    suffix = os.urandom(4).hex()
    literal = f"Literal-{suffix}-Passw0rd!"
    uid = _mk_user(f"wave49-legacy-{suffix}@example.com")
    key = _mk_sensitive_block(uid, suffix)
    with session_scope() as s:
        tpl = Template(
            name=f"wave49-legacy-{suffix}", owner_id=uid,
            recipe_json=json.dumps([{"blocks": [{
                "ref": key,
                "inputs": {"hostname": "h", "admin_password": literal},
            }]}]),
        )
        s.add(tpl)
        s.flush()
        tpl_id = tpl.id

    migrate_template_literal_secrets()

    with session_scope() as s:
        tpl = s.get(Template, tpl_id)
        assert literal not in tpl.recipe_json
        placed = json.loads(tpl.recipe_json)[0]["blocks"][0]
        ref_value = placed["inputs"]["admin_password"]
        assert ref_value.startswith("{{ secrets.TPL") and ref_value.endswith(" }}")
        name = ref_value[len("{{ secrets."):-len(" }}")]
        secret = s.exec(select(Secret).where(
            Secret.name == name, Secret.owner_id == uid, Secret.scope == "user",
        )).first()
        assert secret is not None
        assert decrypt(secret.value_enc, strict=True) == literal
        assert literal not in secret.value_enc

    # idempotent: a second run must not mint duplicate secrets
    migrate_template_literal_secrets()
    with session_scope() as s:
        rows = s.exec(select(Secret).where(Secret.owner_id == uid)).all()
        assert len(rows) == 1, [r.name for r in rows]


def test_deploy_answers_are_encrypted_at_rest():
    suffix = os.urandom(4).hex()
    sentinel = f"TopSecretAnswer-{suffix}"
    uid = _mk_user(f"wave49-deployer-{suffix}@example.com")
    key = _mk_sensitive_block(uid, suffix)
    with session_scope() as s:
        image = Image(kind="base", name=f"wave49-img-{suffix}",
                      source_url="https://example.com/w49.img", build_status="ready")
        conn = Connection(name=f"wave49-conn-{suffix}", host="pve.example",
                          token_id="automation@pve!goblindock", node="pve")
        s.add(image)
        s.add(conn)
        s.flush()
        tpl = Template(
            name=f"wave49-tpl-{suffix}", owner_id=uid,
            recipe_json=json.dumps([{"blocks": [{
                "ref": key, "inputs": {"hostname": "h", "admin_password": ""},
                "ask": ["admin_password"],
            }]}]),
            base_image_id=image.id, connection_id=conn.id,
        )
        s.add(tpl)
        s.flush()
        result = api.deploy(api.DeployBody(
            templateId=tpl.id, name=f"wave49-vm-{suffix}",
            deployInputs={"0.0": {"admin_password": sentinel}},
        ), user=s.get(User, uid), session=s)
        assert result["ok"]

    with session_scope() as s:
        dep = s.exec(select(Deployment).where(
            Deployment.name == f"wave49-vm-{suffix}")).first()
        assert sentinel not in (dep.deploy_inputs_enc or "")
        answers = json.loads(open_deploy_inputs(dep.deploy_inputs_enc))
        assert answers == {"0.0": {"admin_password": sentinel}}
    # the raw database file must not carry the literal answer anywhere
    with engine.begin() as conn:
        for (payload,) in conn.exec_driver_sql(
                "SELECT deploy_inputs_enc FROM deployments").fetchall():
            assert sentinel not in (payload or "")
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(deployments)")}
        assert "deploy_inputs_json" not in cols


def test_open_deploy_inputs_fails_closed_on_corrupt_ciphertext():
    assert open_deploy_inputs("") == "{}"
    assert json.loads(open_deploy_inputs(encrypt_deploy_inputs(""))) == {}
    try:
        open_deploy_inputs("not-a-fernet-token")
    except ValueError:
        pass
    else:
        raise AssertionError("corrupt ciphertext must raise, not silently drop answers")


def test_db_upgrade_encrypts_and_drops_legacy_plaintext_column():
    """Simulate a pre-upgrade DB: re-add the plaintext column with a value, then
    run the migration and prove the value moved into the encrypted column and
    the plaintext column is gone."""
    suffix = os.urandom(4).hex()
    legacy = json.dumps({"0.0": {"admin_password": f"Legacy-{suffix}"}})
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "ALTER TABLE deployments ADD COLUMN deploy_inputs_json TEXT NOT NULL DEFAULT '{}'")
        conn.exec_driver_sql(
            "INSERT INTO deployments (name, node, cpu, ram, disk, ip, mac, status, tags, "
            "notes, error, root_password_enc, cred_user, deploy_inputs_json, deploy_inputs_enc, "
            "identity_state, original_execution_plan_enc, original_context_enc, created_at) "
            "VALUES (?, '', 1, 2, 20, '', '', 'stopped', '', '', '', '', '', ?, '', '', '', '', CURRENT_TIMESTAMP)",
            (f"wave49-legacy-vm-{suffix}", legacy),
        )
    db._migrate()
    with session_scope() as s:
        dep = s.exec(select(Deployment).where(
            Deployment.name == f"wave49-legacy-vm-{suffix}")).first()
        assert dep is not None
        assert json.loads(open_deploy_inputs(dep.deploy_inputs_enc)) == json.loads(legacy)
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(deployments)")}
        assert "deploy_inputs_json" not in cols


if __name__ == "__main__":
    test_template_save_rejects_literal_sensitive_values()
    test_migration_moves_template_literals_into_secret_store()
    test_deploy_answers_are_encrypted_at_rest()
    test_open_deploy_inputs_fails_closed_on_corrupt_ciphertext()
    test_db_upgrade_encrypts_and_drops_legacy_plaintext_column()
    print("\nALL WAVE 49 UNIT TESTS PASSED")
