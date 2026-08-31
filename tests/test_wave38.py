"""Wave 38 — data-integrity and live-console revocation regressions."""
import asyncio
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave38-test.sqlite3")
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from fastapi import HTTPException                 # noqa: E402
from sqlmodel import Session, select              # noqa: E402
from app.db import engine, init_db, session_scope  # noqa: E402
from app import api, backup, execution_plan, recipes, seed  # noqa: E402
from app.config import settings                     # noqa: E402
from app import serialize as S                     # noqa: E402
from app.models import (                           # noqa: E402
    Block, Connection, Deployment, Image, IpAllocation, Job, Network, Template,
    User, ensure_utc, utcnow,
)
from app.security import encrypt, hash_password    # noqa: E402

init_db()


_MISSING = object()


@contextmanager
def _patched(obj, name, value):
    original = getattr(obj, name, _MISSING)
    setattr(obj, name, value)
    try:
        yield
    finally:
        if original is _MISSING:
            delattr(obj, name)
        else:
            setattr(obj, name, original)


@contextmanager
def _isolated_backups(*, keep):
    original_dir = settings.backup_dir
    original_keep = settings.backup_keep
    with tempfile.TemporaryDirectory(prefix="gd-wave38-backup-") as root:
        settings.backup_dir = Path(root) / "backups"
        settings.backup_keep = keep
        try:
            yield settings.backup_dir
        finally:
            settings.backup_dir = original_dir
            settings.backup_keep = original_keep


def _expect_raises(exc_type, fn):
    try:
        fn()
    except exc_type as exc:
        return exc
    raise AssertionError(f"expected {exc_type.__name__}")


def test_static_pool_parser_defensively_counts_and_skips_reserved_slots():
    try:
        from app import network_pool
    except ImportError:
        network_pool = None
    assert network_pool is not None, "the static-pool parser module is missing"
    pool = network_pool.parse_static_pool(
        "2001:db8:38::/64", "2001:db8:38::", "2001:db8:38:0:ffff:ffff:ffff:ffff",
        "2001:db8:38::1",
    )
    assert pool.usable_total == (1 << 64) - 2
    usable = pool.iter_usable()
    assert str(next(usable)) == "2001:db8:38::2"
    assert pool.is_reserved(pool.network.network_address)
    assert not pool.is_reserved(pool.network.broadcast_address), \
        "IPv6's numerically last address is not a broadcast address"
    _expect_raises(AttributeError, lambda: setattr(pool, "start", pool.end))


def test_gatewayless_network_context_is_exact_and_static_capacity_fails_closed():
    with Session(engine) as s:
        network = Network(
            connection_id=1, name="w38-gatewayless", mode="static",
            subnet_cidr="10.38.50.0/24", gateway="",
            range_start="10.38.50.10", range_end="10.38.50.10",
        )
        deployment = Deployment(name="w38-gatewayless")
        s.add(network)
        s.add(deployment)
        s.flush()
        ctx = api._network_ctx(s, network, deployment.id)
        assert ctx["ipconfig0"] == "ip=10.38.50.10/24"
        assert "gw=" not in ctx["ipconfig0"]

    assert S._pool_total(Network(mode="dhcp")) == 254
    assert S._pool_total(Network(
        mode="static", subnet_cidr="10.38.60.0/24",
        range_start="", range_end="",
    )) == 0
    assert S._pool_total(Network(
        mode="static", subnet_cidr="not-a-subnet",
        range_start="10.38.60.10", range_end="10.38.60.20",
    )) == 0
    assert S._pool_total(Network(
        mode="static", subnet_cidr="10.38.60.0/24", gateway="10.38.60.1",
        range_start="10.38.60.0", range_end="10.38.60.255",
    )) == 253


def _assert_valid_sqlite_backup(path):
    con = sqlite3.connect(str(path))
    try:
        assert con.execute("PRAGMA quick_check").fetchall() == [("ok",)]
    finally:
        con.close()


def _seed_published_backup():
    path = backup.backup_now("seed")
    _assert_valid_sqlite_backup(path)
    return path, path.read_bytes(), backup.list_backups()


def _assert_failed_publish_preserved(seed, seed_bytes, before):
    assert backup.list_backups() == before
    assert seed.exists() and seed.read_bytes() == seed_bytes
    assert not list(backup.backup_dir().glob(".goblindock-backup-*.tmp"))


def test_backup_verification_failure_preserves_published_listing_and_rotation():
    with _isolated_backups(keep=1):
        seed, seed_bytes, before = _seed_published_backup()

        def fail_verification(_path):
            raise sqlite3.DatabaseError("injected verification failure")

        with _patched(backup, "_verify_sqlite_backup", fail_verification):
            _expect_raises(
                sqlite3.DatabaseError,
                lambda: backup.backup_now("verification-failure"),
            )

        _assert_failed_publish_preserved(seed, seed_bytes, before)


def test_backup_replace_failure_preserves_published_listing_and_rotation():
    with _isolated_backups(keep=1):
        seed, seed_bytes, before = _seed_published_backup()

        def fail_replace(_source, _destination):
            raise OSError("injected publication failure")

        with _patched(backup.os, "replace", fail_replace):
            _expect_raises(
                OSError,
                lambda: backup.backup_now("publication-failure"),
            )

        _assert_failed_publish_preserved(seed, seed_bytes, before)


def test_successful_backup_is_valid_secure_and_rotated_to_requested_count():
    with _isolated_backups(keep=2) as directory:
        made = [backup.backup_now("success") for _ in range(4)]
        published = backup.list_backups()

        assert [item["name"] for item in published] == [
            path.name for path in made[-2:][::-1]
        ]
        assert len(published) == 2
        assert not list(directory.glob(".goblindock-backup-*.tmp"))
        for item in published:
            _assert_valid_sqlite_backup(directory / item["name"])

        if os.name == "posix":
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            for item in published:
                assert stat.S_IMODE((directory / item["name"]).stat().st_mode) == 0o600


def test_first_admin_setup_contract_is_unchanged():
    request = SimpleNamespace(session={})
    with Session(engine) as s:
        out = api.auth_setup(
            api.SetupBody(
                email="admin@example.com", name="Admin", password="StrongPass12!",
            ),
            request,
            s,
        )

    assert out["ok"] and request.session["uid"]


def _run_synchronized_wrong_passwords(*, count, distinct_ips):
    email = f"w38-lockout-{os.urandom(3).hex()}@example.com"
    user_id = _mk_user(email)
    barrier = threading.Barrier(count)
    original_verify_password = api.verify_password
    outcomes = [None] * count

    def synchronized_wrong_password(_password, _password_hash):
        barrier.wait(timeout=2)
        return False

    def attempt(index):
        ip = f"192.0.2.{index + 1}" if distinct_ips else "192.0.2.1"
        request = SimpleNamespace(
            session={}, client=SimpleNamespace(host=ip), headers={},
        )
        with Session(engine) as s:
            try:
                api.login(
                    api.LoginBody(email=email, password="WrongPass12!"),
                    request,
                    s,
                )
            except HTTPException as exc:
                outcomes[index] = exc.status_code
            else:
                outcomes[index] = "authenticated"

    threads = [
        threading.Thread(target=attempt, args=(index,), daemon=True)
        for index in range(count)
    ]
    api.verify_password = synchronized_wrong_password
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
    finally:
        api.verify_password = original_verify_password

    assert not [thread for thread in threads if thread.is_alive()], \
        "concurrent login attempts did not finish within five seconds"
    assert outcomes == [401] * count, outcomes
    return user_id


def test_five_concurrent_failures_lock_account():
    for _attempt in range(3):
        user_id = _run_synchronized_wrong_passwords(count=5, distinct_ips=True)
        with Session(engine) as s:
            user = s.get(User, user_id)
            locked_until = ensure_utc(user.locked_until)
            assert locked_until and locked_until > utcnow(), (
                f"five concurrent failures left failed_logins={user.failed_logins} "
                f"and locked_until={user.locked_until!r}"
            )


def _legacy_template(inputs, ask, *, legacy_block=False):
    recipe = [{
        "id": "s-legacy",
        "name": "Legacy Accounts",
        "metadata": {"color": "rose", "collapsed": False},
        "blocks": [
            {
                "id": "placed-ssh-1",
                "ref": "b-ssh",
                "name": "Original SSH User",
                "inputs": inputs,
                "ask": ask,
                "metadata": {"x": 17, "locked": True},
            },
            {
                "id": "placed-other-1",
                "ref": "c-unrelated",
                "name": "Unrelated placement",
                "inputs": {"value": "keep"},
                "metadata": {"x": 23},
            },
        ],
    }]
    with session_scope() as s:
        template = Template(
            name="legacy-ssh-" + os.urandom(3).hex(),
            recipe_json=json.dumps(recipe),
        )
        s.add(template)
        if legacy_block:
            s.add(Block(
                key="b-ssh", name="Legacy SSH", kind="builtin", builtin=True,
                phase="cloudinit", input_schema_json="[]",
            ))
        s.commit()
        s.refresh(template)
        return template.id


def _placed(template_id):
    with session_scope() as s:
        recipe = json.loads(s.get(Template, template_id).recipe_json)
    return recipe[0]["blocks"][0]


def _template_json(template_id):
    with session_scope() as s:
        return s.get(Template, template_id).recipe_json


def test_seed_migrates_b_ssh_before_pruning():
    # This template has no legacy Block row: migration must use persisted recipes,
    # not the catalog row that seed_blocks() is about to prune.
    tid_absent = _legacy_template({
        "user": "alice", "password": "pw", "public_key": "ssh-ed25519 AAAA",
        "sudo": False, "ssh_password_login": True,
    }, ask=["password", "sudo"])
    # This one still has the old built-in row at migration time; both paths must
    # produce the same placement and the row must then be removed.
    tid_present = _legacy_template({
        "user": "bob", "password": "pw2", "public_key": "ssh-ed25519 BBBB",
        "sudo": True, "ssh_password_login": False,
    }, ask=["user", "sudo", "not-an-input"], legacy_block=True)

    seed.seed_blocks()

    placed = _placed(tid_absent)
    assert placed["ref"] == "b-user"
    assert placed["inputs"] == {
        "user": "alice", "password": "pw", "public_key": "ssh-ed25519 AAAA",
        "ssh_password_login": True, "shell": "/bin/bash", "home": "",
        "groups": ["sudo"], "sudoers": True, "nopasswd": False,
    }
    assert placed["ask"] == ["password", "nopasswd"]
    assert placed["id"] == "placed-ssh-1"
    assert placed["name"] == "Original SSH User"
    assert placed["metadata"] == {"x": 17, "locked": True}

    placed_present = _placed(tid_present)
    assert placed_present["ref"] == "b-user"
    assert placed_present["inputs"]["nopasswd"] is True
    assert placed_present["ask"] == ["user", "nopasswd"]
    with session_scope() as s:
        assert s.exec(select(Block).where(Block.key == "b-ssh")).first() is None
        recipe = json.loads(s.get(Template, tid_absent).recipe_json)
        assert recipe[0]["id"] == "s-legacy"
        assert recipe[0]["metadata"] == {"color": "rose", "collapsed": False}
        assert recipe[0]["blocks"][1]["ref"] == "c-unrelated"

    first = _template_json(tid_absent)
    first_present = _template_json(tid_present)
    seed.seed_blocks()
    assert _template_json(tid_absent) == first
    assert _template_json(tid_present) == first_present


def _expect_http(code, fn):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == code, (exc.status_code, exc.detail)
        return exc
    raise AssertionError(f"expected HTTPException {code}")


def _mk_user(email):
    with session_scope() as s:
        user = User(
            email=email, name=email.split("@", 1)[0],
            password_hash=hash_password("StrongPass12!"),
        )
        s.add(user)
        s.flush()
        return user.id


def _sensitive_fixture():
    suffix = os.urandom(3).hex()
    author_id = _mk_user(f"w38-author-{suffix}@example.com")
    deployer_id = _mk_user(f"w38-deployer-{suffix}@example.com")
    with session_scope() as s:
        connection = Connection(
            name=f"w38-conn-{suffix}", host="pve", token_id="u@pve!token", node="pve",
        )
        image = Image(
            kind="base", name=f"w38-image-{suffix}",
            source_url="https://example.com/base.img", build_status="ready",
        )
        s.add(connection)
        s.add(image)
        s.flush()
        network = Network(
            connection_id=connection.id, name=f"w38-net-{suffix}", mode="static",
            subnet_cidr="10.38.0.0/24", range_start="10.38.0.20",
            range_end="10.38.0.30", gateway="10.38.0.1",
        )
        block = Block(
            key=f"c-w38-sensitive-{suffix}", kind="custom", builtin=False,
            owner_id=author_id, name="Sensitive setup", phase="cloudinit",
            input_schema_json=json.dumps([
                {"name": "password", "type": "password"},
                {"name": "token", "type": "secret"},
                {"name": "note", "type": "text"},
            ]),
            cloudinit_template="echo configured",
        )
        s.add(network)
        s.add(block)
        s.flush()
        return {
            "author": author_id,
            "deployer": deployer_id,
            "connection": connection.id,
            "image": image.id,
            "network": network.id,
            "block": block.key,
        }


def _recipe(ref, *, password="", token="", note="", ask=None):
    placed = {
        "ref": ref,
        "inputs": {"password": password, "token": token, "note": note},
    }
    if ask is not None:
        placed["ask"] = ask
    return [{"blocks": [placed]}]


def _body(fixture, *, public, recipe):
    return api.TemplateBody(
        name="w38-sensitive-template", public=public, recipe=recipe,
        baseImageId=fixture["image"], connectionId=fixture["connection"],
        networkId=fixture["network"],
    )


def _save(fixture, *, public, recipe):
    with session_scope() as s:
        return api.save_template(
            _body(fixture, public=public, recipe=recipe),
            user=s.get(User, fixture["author"]), session=s,
        )


def _insert_template(fixture, recipe, *, public=True):
    with session_scope() as s:
        template = Template(
            name="w38-imported-" + os.urandom(3).hex(),
            owner_id=fixture["author"], public=public,
            base_image_id=fixture["image"], connection_id=fixture["connection"],
            network_id=fixture["network"], recipe_json=json.dumps(recipe),
        )
        s.add(template)
        s.flush()
        return template.id


def _row_counts(session):
    return tuple(len(session.exec(select(model)).all()) for model in (
        Deployment, Job, IpAllocation,
    ))


def test_custom_block_create_and_edit_canonicalize_omitted_type():
    user_id = _mk_user(f"w38-block-author-{os.urandom(3).hex()}@example.com")
    with Session(engine) as s:
        created = api.create_block(
            api.BlockBody(
                name="Implicit text input", phase="cloudinit",
                input_schema=[{"name": "message", "default": "hello"}],
                cloudinit_template="echo {message}",
            ),
            user=s.get(User, user_id), session=s,
        )
        block = s.exec(select(Block).where(Block.key == created["key"])).one()
        assert json.loads(block.input_schema_json) == [
            {"name": "message", "default": "hello", "type": "text"},
        ]

        assert api.edit_block(
            block.key,
            api.BlockBody(
                name="Updated implicit text input", phase="cloudinit",
                input_schema=[{"name": "updated", "default": "bye"}],
                cloudinit_template="echo {updated}",
            ),
            user=s.get(User, user_id), session=s,
        )["ok"]
        block = s.exec(select(Block).where(Block.key == created["key"])).one()
        assert json.loads(block.input_schema_json) == [
            {"name": "updated", "default": "bye", "type": "text"},
        ]


def _assert_public_legacy_implicit_text_schema(schema, label):
    fixture = _sensitive_fixture()
    with session_scope() as s:
        block = s.exec(select(Block).where(Block.key == fixture["block"])).one()
        block.input_schema_json = json.dumps(schema)
        block.cloudinit_template = "echo {message}"
        s.add(block)

    template_name = f"w38-public-legacy-{label}"
    create_recipe = [{"blocks": [{
        "ref": fixture["block"],
        "inputs": {"message": f"PUBLIC-CREATE-{label}"},
    }]}]
    edit_recipe = [{"blocks": [{
        "ref": fixture["block"],
        "inputs": {"message": f"PUBLIC-EDIT-{label}"},
    }]}]

    with Session(engine) as s:
        create_body = _body(fixture, public=True, recipe=create_recipe)
        create_body.name = template_name
        assert api.save_template(
            create_body,
            user=s.get(User, fixture["author"]), session=s,
        )["ok"]
        template = s.exec(select(Template).where(
            Template.name == template_name,
            Template.owner_id == fixture["author"],
        )).one()
        assert template.public is True
        assert json.loads(template.recipe_json) == create_recipe
        stored_block = s.exec(select(Block).where(
            Block.key == fixture["block"],
        )).one()
        assert json.loads(stored_block.input_schema_json) == schema

        edit_body = _body(fixture, public=True, recipe=edit_recipe)
        edit_body.name = template_name
        assert api.edit_template_ep(
            template.id, edit_body,
            user=s.get(User, fixture["author"]), session=s,
        )["ok"]
        assert json.loads(s.get(Template, template.id).recipe_json) == edit_recipe
        stored_block = s.exec(select(Block).where(
            Block.key == fixture["block"],
        )).one()
        assert json.loads(stored_block.input_schema_json) == schema

        result = api.deploy(
            api.DeployBody(
                templateId=template.id, name=f"w38-public-legacy-{label}",
            ),
            user=s.get(User, fixture["deployer"]), session=s,
        )
        plan = execution_plan.open_execution_plan(
            s.get(Job, result["jobId"]).execution_plan_enc,
        )
        assert plan["recipe"] == edit_recipe
        assert json.loads(plan["blocks"][fixture["block"]]["input_schema_json"]) == [
            {"name": "message", "default": "legacy", "type": "text"},
        ]


def test_public_create_and_edit_accept_legacy_omitted_text_type():
    _assert_public_legacy_implicit_text_schema(
        [{"name": "message", "default": "legacy"}], "omitted",
    )


def test_public_create_and_edit_accept_legacy_null_text_type():
    _assert_public_legacy_implicit_text_schema(
        [{"name": "message", "type": None, "default": "legacy"}], "null",
    )


def test_public_legacy_explicit_invalid_types_remain_rejected_without_echo():
    invalid_types = ("opaque", "secrett", ["secret"], "")
    for index, field_type in enumerate(invalid_types):
        fixture = _sensitive_fixture()
        with session_scope() as s:
            block = s.exec(select(Block).where(Block.key == fixture["block"])).one()
            block.input_schema_json = json.dumps([
                {"name": "message", "type": field_type},
            ])
            s.add(block)
        literal = f"INVALID-TYPE-LITERAL-{index}"
        recipe = [{"blocks": [{
            "ref": fixture["block"], "inputs": {"message": literal},
        }]}]
        with Session(engine) as s:
            before = len(s.exec(select(Template)).all())
            body = _body(fixture, public=True, recipe=recipe)
            body.name = f"w38-invalid-public-type-{index}"
            exc = _expect_http(400, lambda: api.save_template(
                body, user=s.get(User, fixture["author"]), session=s,
            ))
            assert literal not in str(exc.detail)
            assert len(s.exec(select(Template)).all()) == before


def test_same_owner_private_legacy_missing_type_schema_is_canonicalized_in_plan():
    fixture = _sensitive_fixture()
    with session_scope() as s:
        block = s.exec(select(Block).where(Block.key == fixture["block"])).one()
        block.input_schema_json = json.dumps([
            {"name": "message", "default": "hello"},
        ])
        block.cloudinit_template = "echo {message}"
        s.add(block)
    template_id = _insert_template(
        fixture,
        [{"blocks": [{
            "ref": fixture["block"], "inputs": {"message": "hello"},
        }]}],
        public=False,
    )

    with Session(engine) as s:
        result = api.deploy(
            api.DeployBody(templateId=template_id, name="w38-legacy-schema"),
            user=s.get(User, fixture["author"]), session=s,
        )
        job = s.get(Job, result["jobId"])
        plan = execution_plan.open_execution_plan(job.execution_plan_enc)
        assert json.loads(plan["blocks"][fixture["block"]]["input_schema_json"]) == [
            {"name": "message", "default": "hello", "type": "text"},
        ]
        recipe, blocks = execution_plan.materialize_execution_plan(plan)
        assert "echo hello" in recipes.compile_cloudinit(
            recipe, blocks, lambda _owner_id, _secret_name: "",
        )


def test_authenticated_imported_plan_with_missing_type_is_rejected():
    fixture = _sensitive_fixture()
    template_id = _insert_template(
        fixture, _recipe(fixture["block"], password="private"), public=False,
    )
    with Session(engine) as s:
        plan = execution_plan.build_execution_plan(
            s, s.get(Template, template_id), fixture["author"], "{}",
        )
    schema = json.loads(plan["blocks"][fixture["block"]]["input_schema_json"])
    del schema[0]["type"]
    plan["blocks"][fixture["block"]]["input_schema_json"] = json.dumps(schema)

    try:
        execution_plan.seal_execution_plan(plan)
    except ValueError:
        pass
    else:
        raise AssertionError("missing-type execution plan was sealed")

    authenticated = encrypt(json.dumps(plan))
    try:
        execution_plan.open_execution_plan(authenticated)
    except ValueError:
        pass
    else:
        raise AssertionError("authenticated missing-type execution plan was opened")


def test_public_literal_is_rejected_without_echo():
    fixture = _sensitive_fixture()
    for field, value in (("password", "DO-NOT-ECHO"), ("token", "TOKEN-NOT-ECHO")):
        inputs = {
            "password": "{{ secrets.DEPLOY_PASSWORD }}",
            "token": "{{ secrets.DEPLOY_TOKEN }}",
            "note": "public",
        }
        inputs[field] = value
        exc = _expect_http(400, lambda inputs=inputs: _save(
            fixture, public=True,
            recipe=_recipe(fixture["block"], **inputs),
        ))
        assert field in str(exc.detail)
        assert value not in str(exc.detail)


def test_public_ask_and_exact_deployer_secret_references_are_allowed():
    fixture = _sensitive_fixture()
    assert recipes.is_deployer_secret_ref("{{ secrets.DEPLOY_KEY }}") is True
    assert recipes.is_deployer_secret_ref("{{  secrets.DEPLOY_KEY  }}") is True
    assert recipes.is_deployer_secret_ref("prefix {{ secrets.DEPLOY_KEY }}") is False
    assert recipes.is_deployer_secret_ref("{{ secrets.DEPLOY-KEY }}") is False

    assert _save(
        fixture, public=True,
        recipe=_recipe(
            fixture["block"], password="", token="{{ secrets.DEPLOY_KEY }}",
            ask=["password"],
        ),
    )["ok"]
    exc = _expect_http(400, lambda: _save(
        fixture, public=True,
        recipe=_recipe(
            fixture["block"], password="prefix {{ secrets.DEPLOY_KEY }}",
            ask=["password"],
        ),
    ))
    assert "password" in str(exc.detail)
    assert "DEPLOY_KEY" not in str(exc.detail)


def test_public_blank_sensitive_fields_require_exact_ask_on_save_and_edit():
    fixture = _sensitive_fixture()
    for field in ("password", "token"):
        values = {
            "password": "{{ secrets.DEPLOY_PASSWORD }}",
            "token": "{{ secrets.DEPLOY_TOKEN }}",
        }
        values[field] = ""
        exc = _expect_http(400, lambda values=values: _save(
            fixture, public=True,
            recipe=_recipe(fixture["block"], **values),
        ))
        assert field in str(exc.detail)
        assert "DEPLOY_" not in str(exc.detail)

    safe_recipe = _recipe(
        fixture["block"], password="", token="{{ secrets.DEPLOY_TOKEN }}",
        ask=["password"],
    )
    assert _save(fixture, public=True, recipe=safe_recipe)["ok"]
    with session_scope() as s:
        template = s.exec(select(Template).where(
            Template.owner_id == fixture["author"],
            Template.name == "w38-sensitive-template",
        ).order_by(Template.id.desc())).first()
        exc = _expect_http(400, lambda: api.edit_template_ep(
            template.id,
            _body(
                fixture, public=True,
                recipe=_recipe(
                    fixture["block"], password="",
                    token="{{ secrets.DEPLOY_TOKEN }}",
                ),
            ),
            user=s.get(User, fixture["author"]), session=s,
        ))
        assert "password" in str(exc.detail)
        assert "DEPLOY_TOKEN" not in str(exc.detail)


def test_public_edit_rejects_literal_but_private_and_owner_paths_remain_allowed():
    fixture = _sensitive_fixture()
    private_recipe = _recipe(
        fixture["block"], password="PRIVATE-AUTHOR-VALUE", token="PRIVATE-TOKEN",
    )
    assert _save(fixture, public=False, recipe=private_recipe)["ok"]
    with session_scope() as s:
        template = s.exec(select(Template).where(
            Template.owner_id == fixture["author"],
            Template.name == "w38-sensitive-template",
        )).first()
        template_id = template.id
        exc = _expect_http(400, lambda: api.edit_template_ep(
            template_id, _body(fixture, public=True, recipe=private_recipe),
            user=s.get(User, fixture["author"]), session=s,
        ))
        assert "password" in str(exc.detail)
        assert "PRIVATE-AUTHOR-VALUE" not in str(exc.detail)

    imported_id = _insert_template(fixture, private_recipe, public=True)
    with Session(engine) as s:
        result = api.deploy(
            api.DeployBody(templateId=imported_id, name="w38-owner-deploy"),
            user=s.get(User, fixture["author"]), session=s,
        )
        assert result["ok"]


def test_cross_owner_missing_sensitive_ask_answer_cannot_fallback_to_author_value():
    fixture = _sensitive_fixture()
    author_value = "AUTHOR-FALLBACK-MUST-NOT-RUN"
    template_id = _insert_template(
        fixture,
        _recipe(fixture["block"], password=author_value, ask=["password"]),
    )
    with Session(engine) as s:
        before = _row_counts(s)
        exc = _expect_http(409, lambda: api.deploy(
            api.DeployBody(templateId=template_id, name="w38-missing-answer"),
            user=s.get(User, fixture["deployer"]), session=s,
        ))
        assert "password" in str(exc.detail)
        assert author_value not in str(exc.detail)
        assert _row_counts(s) == before


def test_cross_owner_imported_literal_is_rejected_before_any_rows_are_inserted():
    fixture = _sensitive_fixture()
    author_value = "IMPORTED-AUTHOR-SECRET"
    template_id = _insert_template(
        fixture, _recipe(fixture["block"], password=author_value),
    )
    with Session(engine) as s:
        before = _row_counts(s)
        exc = _expect_http(409, lambda: api.deploy(
            api.DeployBody(templateId=template_id, name="w38-unsafe-import"),
            user=s.get(User, fixture["deployer"]), session=s,
        ))
        assert "password" in str(exc.detail)
        assert author_value not in str(exc.detail)
        assert _row_counts(s) == before


def test_cross_owner_blank_sensitive_without_ask_is_rejected_before_persistence():
    fixture = _sensitive_fixture()
    template_id = _insert_template(
        fixture,
        _recipe(
            fixture["block"], password="", token="{{ secrets.DEPLOY_TOKEN }}",
        ),
    )
    with Session(engine) as s:
        before = _row_counts(s)
        exc = _expect_http(409, lambda: api.deploy(
            api.DeployBody(templateId=template_id, name="w38-blank-no-ask"),
            user=s.get(User, fixture["deployer"]), session=s,
        ))
        assert "password" in str(exc.detail)
        assert "DEPLOY_TOKEN" not in str(exc.detail)
        assert _row_counts(s) == before


def test_cross_owner_malformed_snapshot_schemas_fail_before_persistence():
    malformed_schemas = (
        [{"name": "credential", "type": "secrett"}],
        [{"name": "credential", "type": "opaque"}],
        [{"type": "secret"}],
        [{"name": "bad-name", "type": "secret"}],
        [{"name": "credential", "type": ["secret"]}],
        [{"name": "credential", "type": ""}],
    )
    for schema in malformed_schemas:
        fixture = _sensitive_fixture()
        with session_scope() as s:
            block = s.exec(select(Block).where(Block.key == fixture["block"])).one()
            block.input_schema_json = json.dumps(schema)
            s.add(block)
        sentinel = "MALFORMED-SCHEMA-MUST-NOT-RUN"
        template_id = _insert_template(
            fixture,
            [{"blocks": [{
                "ref": fixture["block"], "inputs": {"credential": sentinel},
            }]}],
        )
        with Session(engine) as s:
            before = _row_counts(s)
            exc = _expect_http(409, lambda: api.deploy(
                api.DeployBody(templateId=template_id, name="w38-malformed-schema"),
                user=s.get(User, fixture["deployer"]), session=s,
            ))
            assert sentinel not in str(exc.detail)
            assert _row_counts(s) == before


def test_cross_owner_unknown_block_is_rejected_before_any_rows_are_inserted():
    fixture = _sensitive_fixture()
    unknown_value = "UNKNOWN-BLOCK-PRIVATE-VALUE"
    template_id = _insert_template(
        fixture,
        [{"blocks": [{"ref": "b-pruned", "inputs": {"note": unknown_value}}]}],
    )
    with Session(engine) as s:
        before = _row_counts(s)
        exc = _expect_http(409, lambda: api.deploy(
            api.DeployBody(templateId=template_id, name="w38-unknown-import"),
            user=s.get(User, fixture["deployer"]), session=s,
        ))
        assert "b-pruned" in str(exc.detail)
        assert unknown_value not in str(exc.detail)
        assert _row_counts(s) == before


def test_cross_owner_sensitive_ask_answer_and_exact_ref_deploy():
    fixture = _sensitive_fixture()
    template_id = _insert_template(
        fixture,
        _recipe(
            fixture["block"], password="", token="{{ secrets.DEPLOY_TOKEN }}",
            ask=["password"],
        ),
    )
    with Session(engine) as s:
        result = api.deploy(
            api.DeployBody(
                templateId=template_id, name="w38-safe-cross-owner",
                deployInputs={"0.0": {"password": "DEPLOYER-PROVIDED"}},
            ),
            user=s.get(User, fixture["deployer"]), session=s,
        )
        assert result["ok"]


def test_unknown_legacy_block_masks_all_nonempty_inputs():
    recipe = [{"blocks": [{"ref": "b-pruned", "inputs": {
        "password": "p", "note": "private", "empty": "",
    }}]}]
    with Session(engine) as s:
        assert S._mask_recipe_passwords(s, recipe)[0]["blocks"][0]["inputs"] == {
            "password": "********", "note": "********", "empty": "",
        }


def test_unknown_ref_less_block_masks_all_nonempty_inputs():
    for placed in (
        {"inputs": {"token": "literal", "empty": ""}},
        {"ref": "", "inputs": {"token": "literal", "empty": ""}},
    ):
        fixture = _sensitive_fixture()
        recipe = [{"blocks": [placed]}]
        template_id = _insert_template(fixture, recipe)
        with Session(engine) as s:
            template = s.get(Template, template_id)
            masked = S.template_dict(
                s, template, viewer=s.get(User, fixture["deployer"]),
            )["recipe"]
            owner_recipe = S.template_dict(
                s, template, viewer=s.get(User, fixture["author"]),
            )["recipe"]
        assert masked[0]["blocks"][0]["inputs"] == {
            "token": "********", "empty": "",
        }
        assert owner_recipe[0]["blocks"][0]["inputs"] == {
            "token": "literal", "empty": "",
        }


# --------------------------------------------------------------------------- #
# Live console authorization + coordinated pump shutdown                       #
# --------------------------------------------------------------------------- #
_CONSOLE_END = object()


class _ConsoleBrowser:
    """Starlette-WebSocket-shaped peer with observable close and relay effects."""

    def __init__(self, *, late_on_cancel=False):
        self._incoming = asyncio.Queue()
        self._late_on_cancel = late_on_cancel
        self._late_returned = False
        self.receive_started = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_codes = []
        self.sent = []

    async def receive(self):
        self.receive_started.set()
        try:
            return await self._incoming.get()
        except asyncio.CancelledError:
            # Model a real receive that wins the cancellation race and surfaces one
            # already-buffered frame. The pump's stopping check must still suppress it.
            if self._late_on_cancel and not self._late_returned:
                self._late_returned = True
                return {"type": "websocket.receive", "text": "late-browser-frame"}
            raise

    async def send_bytes(self, data):
        self.sent.append(("bytes", data))

    async def send_text(self, data):
        self.sent.append(("text", data))

    async def close(self, code=1000):
        self.close_codes.append(code)
        self.closed.set()

    async def disconnect(self):
        await self._incoming.put({"type": "websocket.disconnect"})


class _ConsolePve:
    """websockets-client-shaped peer with a controllable async iterator."""

    def __init__(self, *, late_on_cancel=False):
        self._incoming = asyncio.Queue()
        self._late_on_cancel = late_on_cancel
        self._late_returned = False
        self.iter_started = asyncio.Event()
        self.closed = asyncio.Event()
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.iter_started.set()
        try:
            item = await self._incoming.get()
        except asyncio.CancelledError:
            if self._late_on_cancel and not self._late_returned:
                self._late_returned = True
                return b"late-pve-frame"
            raise
        if item is _CONSOLE_END:
            raise StopAsyncIteration
        return item

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed.set()

    async def end(self):
        await self._incoming.put(_CONSOLE_END)


def _live_console_fixture(*, role="user", owns_deployment=True):
    suffix = os.urandom(5).hex()
    with session_scope() as s:
        user = User(
            email=f"w38-console-{suffix}@example.com", name="Console user",
            password_hash=hash_password("StrongPass12!"), role=role, session_epoch=7,
        )
        other = User(
            email=f"w38-console-other-{suffix}@example.com", name="Other owner",
            password_hash=hash_password("StrongPass12!"),
        )
        s.add(user)
        s.add(other)
        s.flush()
        conn = Connection(
            name=f"w38-console-{suffix}", host="pve.example",
            token_id="goblin@pve!console", node="pve",
        )
        s.add(conn)
        s.flush()
        dep = Deployment(
            name=f"w38-console-vm-{suffix}",
            owner_id=user.id if owns_deployment else other.id,
            connection_id=conn.id, vmid=700, node="pve", status="running",
        )
        s.add(dep)
        s.flush()
        snapshot = {
            "conn": Connection(**conn.model_dump()),
            "deployment": Deployment(**dep.model_dump()),
            "user_id": user.id,
            "session_epoch": user.session_epoch,
        }
        fixture = {
            "user_id": user.id, "other_id": other.id, "deployment_id": dep.id,
        }

    return api._ConsoleGrant(**snapshot), fixture


def _pump_task(browser, pve, prefer_bytes, grant):
    return asyncio.create_task(api._pump_ws(browser, pve, prefer_bytes, grant))


async def _bounded_event(event, timeout):
    waiter = asyncio.create_task(event.wait())
    done, _pending = await asyncio.wait({waiter}, timeout=timeout)
    if waiter not in done:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        return False
    return True


async def _cleanup_console_task(task, browser, pve):
    """Make a failed pre-fix pump settle without weakening its bounded assertion."""
    if not task.done():
        browser._late_on_cancel = False
        pve._late_on_cancel = False
        await browser.disconnect()
        await pve.end()
        done, _pending = await asyncio.wait({task}, timeout=0.2)
        if task not in done:
            task.cancel()
            await asyncio.wait({task}, timeout=0.2)
    if task.done():
        await asyncio.gather(task, return_exceptions=True)


async def _assert_revoked(mutate, prefer_bytes, *, role="user", owns_deployment=True):
    grant, fixture = _live_console_fixture(
        role=role, owns_deployment=owns_deployment,
    )
    browser = _ConsoleBrowser(late_on_cancel=True)
    pve = _ConsolePve(late_on_cancel=True)
    had_interval = hasattr(api, "_CONSOLE_AUTH_INTERVAL_S")
    old_interval = getattr(api, "_CONSOLE_AUTH_INTERVAL_S", None)
    api._CONSOLE_AUTH_INTERVAL_S = 0.01
    task = _pump_task(browser, pve, prefer_bytes, grant)
    try:
        assert await _bounded_event(browser.receive_started, 0.2), "browser pump did not start"
        assert await _bounded_event(pve.iter_started, 0.2), "PVE pump did not start"
        mutate(fixture)
        revoked_closed = await _bounded_event(browser.closed, 0.5)
        done, _pending = await asyncio.wait({task}, timeout=0.5)
        pump_stopped = task in done
    finally:
        await _cleanup_console_task(task, browser, pve)
        if had_interval:
            api._CONSOLE_AUTH_INTERVAL_S = old_interval
        else:
            del api._CONSOLE_AUTH_INTERVAL_S

    assert revoked_closed, "revoked live console was not closed within 0.5s"
    assert pump_stopped, "revoked live console pump did not terminate within 0.5s"
    assert 4403 in browser.close_codes, browser.close_codes
    assert pve.closed.is_set(), "revocation must close the Proxmox socket too"
    assert browser.sent == [], "a PVE frame relayed after revocation began"
    assert pve.sent == [], "a browser frame relayed after revocation began"


def _for_serial_and_vnc(assertion):
    for prefer_bytes in (False, True):
        asyncio.run(assertion(prefer_bytes))


def test_disabled_user_revokes_live_serial_and_vnc():
    def mutate(fixture):
        with session_scope() as s:
            user = s.get(User, fixture["user_id"])
            user.disabled = True
            s.add(user)

    _for_serial_and_vnc(lambda prefer: _assert_revoked(mutate, prefer))


def test_deleted_user_revokes_live_serial_and_vnc():
    def mutate(fixture):
        with session_scope() as s:
            s.delete(s.get(User, fixture["user_id"]))

    _for_serial_and_vnc(lambda prefer: _assert_revoked(mutate, prefer))


def test_epoch_change_revokes_live_serial_and_vnc():
    def mutate(fixture):
        with session_scope() as s:
            user = s.get(User, fixture["user_id"])
            user.session_epoch += 1
            s.add(user)

    _for_serial_and_vnc(lambda prefer: _assert_revoked(mutate, prefer))


def test_admin_demotion_revokes_non_owner_live_serial_and_vnc():
    def mutate(fixture):
        with session_scope() as s:
            user = s.get(User, fixture["user_id"])
            user.role = "user"
            s.add(user)

    def assertion(prefer):
        return _assert_revoked(
            mutate, prefer, role="admin", owns_deployment=False,
        )

    _for_serial_and_vnc(assertion)


def test_ownership_transfer_revokes_live_serial_and_vnc():
    def mutate(fixture):
        with session_scope() as s:
            dep = s.get(Deployment, fixture["deployment_id"])
            dep.owner_id = fixture["other_id"]
            s.add(dep)

    _for_serial_and_vnc(lambda prefer: _assert_revoked(mutate, prefer))


def test_console_authorization_db_error_fails_closed():
    def broken_session(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    original = api.Session
    try:
        api.Session = broken_session
        _for_serial_and_vnc(
            lambda prefer: _assert_revoked(lambda _fixture: None, prefer)
        )
    finally:
        api.Session = original


async def _assert_first_completion(prefer_bytes, first):
    grant, _fixture = _live_console_fixture()
    browser = _ConsoleBrowser()
    pve = _ConsolePve()
    task = _pump_task(browser, pve, prefer_bytes, grant)
    try:
        assert await _bounded_event(browser.receive_started, 0.2), "browser pump did not start"
        assert await _bounded_event(pve.iter_started, 0.2), "PVE pump did not start"
        if first == "pve":
            await pve.end()
        else:
            await browser.disconnect()
        done, _pending = await asyncio.wait({task}, timeout=0.5)
        stopped = task in done
        browser_closed = await _bounded_event(browser.closed, 0.1)
        pve_closed = await _bounded_event(pve.closed, 0.1)
    finally:
        await _cleanup_console_task(task, browser, pve)

    assert stopped, f"{first} first-completion left the opposite pump blocked"
    assert browser_closed and pve_closed, "first completion must close both sockets"


def test_pve_iterator_ending_first_terminates_serial_and_vnc():
    _for_serial_and_vnc(
        lambda prefer: _assert_first_completion(prefer, "pve")
    )


def test_browser_disconnect_ending_first_terminates_serial_and_vnc():
    _for_serial_and_vnc(
        lambda prefer: _assert_first_completion(prefer, "browser")
    )


def test_console_grant_is_a_frozen_snapshot():
    grant, _fixture = _live_console_fixture()
    try:
        grant.user_id = -1
    except (AttributeError, TypeError):
        return
    raise AssertionError("console grant snapshot must be frozen")


if __name__ == "__main__":
    test_static_pool_parser_defensively_counts_and_skips_reserved_slots()
    test_gatewayless_network_context_is_exact_and_static_capacity_fails_closed()
    test_backup_verification_failure_preserves_published_listing_and_rotation()
    test_backup_replace_failure_preserves_published_listing_and_rotation()
    test_successful_backup_is_valid_secure_and_rotated_to_requested_count()
    test_first_admin_setup_contract_is_unchanged()
    test_five_concurrent_failures_lock_account()
    test_seed_migrates_b_ssh_before_pruning()
    test_custom_block_create_and_edit_canonicalize_omitted_type()
    test_public_create_and_edit_accept_legacy_omitted_text_type()
    test_public_create_and_edit_accept_legacy_null_text_type()
    test_public_legacy_explicit_invalid_types_remain_rejected_without_echo()
    test_same_owner_private_legacy_missing_type_schema_is_canonicalized_in_plan()
    test_authenticated_imported_plan_with_missing_type_is_rejected()
    test_public_literal_is_rejected_without_echo()
    test_public_ask_and_exact_deployer_secret_references_are_allowed()
    test_public_blank_sensitive_fields_require_exact_ask_on_save_and_edit()
    test_public_edit_rejects_literal_but_private_and_owner_paths_remain_allowed()
    test_cross_owner_missing_sensitive_ask_answer_cannot_fallback_to_author_value()
    test_cross_owner_imported_literal_is_rejected_before_any_rows_are_inserted()
    test_cross_owner_blank_sensitive_without_ask_is_rejected_before_persistence()
    test_cross_owner_malformed_snapshot_schemas_fail_before_persistence()
    test_cross_owner_unknown_block_is_rejected_before_any_rows_are_inserted()
    test_cross_owner_sensitive_ask_answer_and_exact_ref_deploy()
    test_unknown_legacy_block_masks_all_nonempty_inputs()
    test_unknown_ref_less_block_masks_all_nonempty_inputs()
    test_disabled_user_revokes_live_serial_and_vnc()
    test_deleted_user_revokes_live_serial_and_vnc()
    test_epoch_change_revokes_live_serial_and_vnc()
    test_admin_demotion_revokes_non_owner_live_serial_and_vnc()
    test_ownership_transfer_revokes_live_serial_and_vnc()
    test_console_authorization_db_error_fails_closed()
    test_pve_iterator_ending_first_terminates_serial_and_vnc()
    test_browser_disconnect_ending_first_terminates_serial_and_vnc()
    test_console_grant_is_a_frozen_snapshot()
    print("\nALL WAVE 38 UNIT TESTS PASSED")
