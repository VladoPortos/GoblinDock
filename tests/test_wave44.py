r"""Wave 44 — final authentication, account lifecycle, and live-SSE regressions.

Run (PowerShell):
  $env:GOBLINDOCK_DEV='1'; .venv\Scripts\python.exe tests\test_wave44.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave44-test.sqlite3")
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault(
    "GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test")
)

from fastapi import HTTPException                    # noqa: E402
from pydantic import ValidationError                 # noqa: E402
from sqlmodel import SQLModel, Session, create_engine, select  # noqa: E402

from app import api, db, seed                        # noqa: E402
from app.db import engine, init_db, session_scope    # noqa: E402
from app.deps import current_user                    # noqa: E402
from app.models import (                             # noqa: E402
    Block,
    Job,
    JobEvent,
    Secret,
    Template,
    User,
    Variable,
    ensure_utc,
    utcnow,
)
from app.security import hash_password               # noqa: E402

init_db()


def _expect_http(code, fn):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == code, (exc.status_code, exc.detail)
        return exc
    raise AssertionError(f"expected HTTPException {code}")


def _request(*, uid=None, session_epoch=0, ip="192.0.2.44"):
    session = {"uid": uid, "sv": session_epoch} if uid is not None else {}
    return SimpleNamespace(
        session=session,
        client=SimpleNamespace(host=ip),
        headers={},
    )


def _mk_user(email, *, role="user", password="StrongPass12!", session=None):
    u = User(
        email=email,
        name=email.split("@", 1)[0],
        password_hash=hash_password(password),
        role=role,
    )
    if session is not None:
        session.add(u)
        session.flush()
        return u
    with session_scope() as s:
        s.add(u)
        s.flush()
        return u.id


def test_deleted_user_is_tombstoned_and_old_cookie_cannot_rebind():
    """Removing hard-delete/tombstones would let SQLite reuse the cookie-bound ID."""
    suffix = os.urandom(4).hex()
    original_email = f"wave44-reuse-{suffix}@example.com"
    with Session(engine) as s:
        admin = _mk_user(f"wave44-admin-{suffix}@example.com", role="admin", session=s)
        victim = _mk_user(original_email, session=s)
        victim.widget_key_hash = "stored-widget-hash"
        victim.widget_key_prefix = "gdwk_old"
        victim.widget_key_created_at = utcnow()
        victim.widget_key_last_used = utcnow()
        s.add(victim)
        s.add(Secret(
            scope="user", owner_id=victim.id, created_by=victim.id,
            name=f"PRIVATE_SECRET_{suffix}", value_enc="ciphertext",
        ))
        s.add(Variable(
            scope="user", owner_id=victim.id, created_by=victim.id,
            name=f"PRIVATE_VARIABLE_{suffix}", value="private-value",
        ))
        owned_template = Template(
            name=f"wave44-owned-template-{suffix}", owner_id=victim.id, public=False,
        )
        owned_block = Block(
            key=f"c-wave44-owned-{suffix}", kind="custom", builtin=False,
            name="Historical block", owner_id=victim.id,
        )
        historical_job = Job(
            type="deploy", title="Historical job", status="succeeded",
            created_by=victim.id, finished_at=utcnow(),
        )
        s.add(owned_template)
        s.add(owned_block)
        s.add(historical_job)
        s.commit()
        victim_id = victim.id
        template_id = owned_template.id
        block_id = owned_block.id
        job_id = historical_job.id
        old_cookie = _request(uid=victim_id, session_epoch=victim.session_epoch)

        assert api.delete_user(victim_id, user=admin, session=s)["ok"]

        tombstone = s.get(User, victim_id)
        assert tombstone is not None, "deleted IDs must remain allocated forever"
        assert tombstone.deleted_at is not None
        assert tombstone.disabled is True
        assert tombstone.session_epoch == 1
        assert tombstone.role == "user"
        assert original_email not in (tombstone.email, tombstone.name)
        assert tombstone.password_hash == ""
        assert tombstone.widget_key_hash is None
        assert tombstone.widget_key_prefix == ""
        assert tombstone.widget_key_created_at is None
        assert tombstone.widget_key_last_used is None
        assert not s.exec(select(Secret).where(Secret.owner_id == victim_id)).all()
        assert not s.exec(select(Variable).where(Variable.owner_id == victim_id)).all()

        assert s.get(Template, template_id).owner_id == victim_id
        assert s.get(Block, block_id).owner_id == victim_id
        assert s.get(Job, job_id).created_by == victim_id

        _expect_http(
            404,
            lambda: api.edit_user(
                victim_id, api.UserEditBody(name="Resurrected"), user=admin, session=s,
            ),
        )
        _expect_http(
            404,
            lambda: api.reset_user_password(
                victim_id,
                api.PasswordResetBody(value="NewPassword12!"),
                _request(uid=admin.id),
                user=admin,
                session=s,
            ),
        )
        _expect_http(404, lambda: api.delete_user(victim_id, user=admin, session=s))

        api.add_user(
            api.UserBody(
                email=original_email,
                name="Replacement",
                password="Replacement12!",
                role="user",
            ),
            user=admin,
            session=s,
        )
        replacement = s.exec(select(User).where(User.email == original_email)).first()
        assert replacement is not None
        assert replacement.id != victim_id, "the replacement must never inherit the old ID"
        _expect_http(401, lambda: current_user(old_cookie, s))

        state = api.state(_request(uid=admin.id), user=admin, session=s)
        listed_ids = {row["userId"] for row in state["USERS"]}
        assert victim_id not in listed_ids, "tombstones are not normal admin-list users"
        assert replacement.id in listed_ids


def test_existing_database_migration_adds_deleted_at():
    """Removing the explicit ALTER migration would break upgraded installations."""
    with tempfile.TemporaryDirectory(prefix="gd-wave44-migrate-") as root:
        path = os.path.join(root, "legacy.sqlite3")
        legacy_engine = create_engine(f"sqlite:///{path}")
        SQLModel.metadata.create_all(legacy_engine)
        # Model metadata represents a fresh install; remove the new field to recreate
        # the schema an existing installation has immediately before this upgrade.
        with legacy_engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE users DROP COLUMN deleted_at")
        old_engine = db.engine
        db.engine = legacy_engine
        try:
            db._migrate()
        finally:
            db.engine = old_engine
        try:
            with legacy_engine.connect() as conn:
                columns = {
                    row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")
                }
            assert "deleted_at" in columns
        finally:
            legacy_engine.dispose()


def test_login_attempt_keys_are_hard_bounded_by_lru():
    """Removing LRU eviction would let unique nonexistent emails grow memory forever."""
    api._login_attempts.clear()
    try:
        for index in range(api._MAX_THROTTLE_KEYS + 250):
            api._record_attempt(f"wave44-missing-{index}@example.com|192.0.2.44")
        assert len(api._login_attempts) == api._MAX_THROTTLE_KEYS
        assert "wave44-missing-0@example.com|192.0.2.44" not in api._login_attempts
        assert (
            f"wave44-missing-{api._MAX_THROTTLE_KEYS + 249}@example.com|192.0.2.44"
            in api._login_attempts
        )
    finally:
        api._login_attempts.clear()


def test_login_rejects_email_longer_than_320_characters():
    """Removing the input bound would permit attacker-sized throttle keys."""
    try:
        api.LoginBody(email="x" * 321, password="WrongPass12!")
    except ValidationError:
        return
    raise AssertionError("a 321-character login email must fail request validation")


def test_active_lock_denies_even_the_correct_password_until_it_expires():
    """The persistent per-account lock is a hard spray brake: while it is active even
    the correct credential gets the generic 401 (no oracle), and once it expires the
    correct credential authenticates and clears the failure state."""
    suffix = os.urandom(4).hex()
    email = f"wave44-locked-{suffix}@example.com"
    password = "CorrectPass12!"
    user_id = _mk_user(email, password=password)
    api._login_attempts.clear()
    try:
        for index in range(api._LOCK_THRESHOLD):
            with Session(engine) as s:
                exc = _expect_http(
                    401,
                    lambda index=index: api.login(
                        api.LoginBody(email=email, password="WrongPass12!"),
                        _request(ip=f"198.51.100.{index + 1}"),
                        s,
                    ),
                )
            assert exc.detail == "invalid email or password"

        with Session(engine) as s:
            locked = s.get(User, user_id)
            assert locked.locked_until is not None
            assert ensure_utc(locked.locked_until) > utcnow()

        # While locked: the correct password from a fresh IP still gets the same
        # generic 401, and the lock is neither extended nor the counter bumped.
        with Session(engine) as s:
            exc = _expect_http(
                401,
                lambda: api.login(
                    api.LoginBody(email=email, password=password),
                    _request(ip="198.51.100.200"),
                    s,
                ),
            )
        assert exc.detail == "invalid email or password"
        with Session(engine) as s:
            still_locked = s.get(User, user_id)
            assert still_locked.failed_logins == 0
            assert ensure_utc(still_locked.locked_until) > utcnow()

        # After the lock expires the legitimate credential works and clears state.
        with Session(engine) as s:
            expired = s.get(User, user_id)
            expired.locked_until = utcnow() - timedelta(minutes=1)
            s.add(expired)
            s.commit()
        request = _request(ip="198.51.100.201")
        with Session(engine) as s:
            out = api.login(
                api.LoginBody(email=email, password=password), request, s,
            )
        assert out["ok"] is True
        assert request.session["uid"] == user_id
        with Session(engine) as s:
            unlocked = s.get(User, user_id)
            assert unlocked.failed_logins == 0
            assert unlocked.locked_until is None
    finally:
        api._login_attempts.clear()


def test_known_and_unknown_wrong_credentials_share_throttle_contract():
    """Reintroducing lock-specific 429s would provide an account-existence oracle."""
    suffix = os.urandom(4).hex()
    known = f"wave44-known-{suffix}@example.com"
    unknown = f"wave44-unknown-{suffix}@example.com"
    _mk_user(known)

    def exhaust(email, ip):
        details = []
        for _index in range(8):
            with Session(engine) as s:
                exc = _expect_http(
                    401,
                    lambda: api.login(
                        api.LoginBody(email=email, password="WrongPass12!"),
                        _request(ip=ip),
                        s,
                    ),
                )
            details.append(exc.detail)
        with Session(engine) as s:
            limited = _expect_http(
                429,
                lambda: api.login(
                    api.LoginBody(email=email, password="WrongPass12!"),
                    _request(ip=ip),
                    s,
                ),
            )
        return details, limited.detail

    api._login_attempts.clear()
    try:
        known_details, known_limit = exhaust(known, "203.0.113.44")
        unknown_details, unknown_limit = exhaust(unknown, "203.0.113.45")
        assert known_details == unknown_details == ["invalid email or password"] * 8
        assert known_limit == unknown_limit == "too many attempts — try again in a few minutes"
    finally:
        api._login_attempts.clear()


def test_env_seeded_admin_identity_is_normalized():
    """Removing seed normalization would create a sole admin that login cannot find."""
    with tempfile.TemporaryDirectory(prefix="gd-wave44-seed-") as root:
        seeded_engine = create_engine(f"sqlite:///{os.path.join(root, 'seed.sqlite3')}")
        SQLModel.metadata.create_all(seeded_engine)
        with Session(seeded_engine) as s:
            s.add(User(
                email="deleted-seed@goblindock.invalid", name="Deleted user",
                password_hash="", role="user", disabled=True, deleted_at=utcnow(),
            ))
            s.commit()

        @contextmanager
        def isolated_scope():
            with Session(seeded_engine) as s:
                try:
                    yield s
                    s.commit()
                except Exception:
                    s.rollback()
                    raise

        old_scope = seed.session_scope
        old_values = (
            seed.settings.admin_email,
            seed.settings.admin_name,
            seed.settings.admin_password,
        )
        seed.session_scope = isolated_scope
        seed.settings.admin_email = "  Seeded.Admin@Example.COM  "
        seed.settings.admin_name = "   "
        seed.settings.admin_password = "SeededPass12!"
        try:
            seed.maybe_seed_admin()
            seed.maybe_seed_admin()
            with Session(seeded_engine) as s:
                users = s.exec(select(User)).all()
                assert len(users) == 2
                active = [user for user in users if user.deleted_at is None]
                assert len(active) == 1
                assert active[0].email == "seeded.admin@example.com"
                assert active[0].name == "Admin"
        finally:
            seed.session_scope = old_scope
            (
                seed.settings.admin_email,
                seed.settings.admin_name,
                seed.settings.admin_password,
            ) = old_values
            seeded_engine.dispose()


class _StreamRequest:
    def __init__(self, user_id, session_epoch):
        self.session = {"uid": user_id, "sv": session_epoch}

    async def is_disconnected(self):
        return False


def _stream_fixture(*, role="user", owns_job=True):
    suffix = os.urandom(4).hex()
    with session_scope() as s:
        viewer = _mk_user(
            f"wave44-stream-{suffix}@example.com", role=role, session=s,
        )
        viewer.session_epoch = 7
        other = _mk_user(f"wave44-stream-other-{suffix}@example.com", session=s)
        s.add(viewer)
        job = Job(
            type="deploy", title=f"wave44 stream {suffix}", status="running",
            created_by=viewer.id if owns_job else other.id, pct=25, phase="Configure",
        )
        s.add(job)
        s.flush()
        return {
            "viewer": User(**viewer.model_dump()),
            "viewer_id": viewer.id,
            "other_id": other.id,
            "job_id": job.id,
            "session_epoch": viewer.session_epoch,
        }


def _revoke_stream(fixture, kind):
    with session_scope() as s:
        viewer = s.get(User, fixture["viewer_id"])
        job = s.get(Job, fixture["job_id"])
        if kind == "disabled":
            viewer.disabled = True
            s.add(viewer)
        elif kind == "deleted":
            viewer.deleted_at = utcnow()
            s.add(viewer)
        elif kind == "epoch":
            viewer.session_epoch += 1
            s.add(viewer)
        elif kind == "demoted":
            viewer.role = "user"
            s.add(viewer)
        elif kind == "transferred":
            job.created_by = fixture["other_id"]
            s.add(job)
        elif kind == "missing":
            s.delete(viewer)
        else:
            raise AssertionError(f"unknown revocation kind: {kind}")


async def _assert_stream_exhausted(iterator, *, timeout):
    try:
        frame = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
    except StopAsyncIteration:
        return
    text = frame.decode() if isinstance(frame, (bytes, bytearray)) else frame
    raise AssertionError(f"revoked stream emitted another frame: {text}")


async def _assert_revoked_before_first_emit(kind, *, role="user", owns_job=True):
    fixture = _stream_fixture(role=role, owns_job=owns_job)
    request = _StreamRequest(fixture["viewer_id"], fixture["session_epoch"])
    response = await api.stream_job(
        fixture["job_id"], request, user=fixture["viewer"],
    )
    try:
        _revoke_stream(fixture, kind)
        await _assert_stream_exhausted(response.body_iterator, timeout=0.3)
    finally:
        await response.body_iterator.aclose()


def test_job_stream_reauthorizes_after_handshake_before_first_emit():
    """Caching handshake authorization would leak the first frame after revocation."""
    for kind in ("disabled", "deleted", "epoch", "missing"):
        asyncio.run(_assert_revoked_before_first_emit(kind))
    asyncio.run(_assert_revoked_before_first_emit("demoted", role="admin", owns_job=False))
    asyncio.run(_assert_revoked_before_first_emit("transferred"))


def test_job_stream_reauthorizes_between_frames_before_reading_new_logs():
    """Authorizing only once in the generator would leak logs after epoch revocation."""
    async def scenario():
        fixture = _stream_fixture()
        request = _StreamRequest(fixture["viewer_id"], fixture["session_epoch"])
        response = await api.stream_job(
            fixture["job_id"], request, user=fixture["viewer"],
        )
        try:
            first = await response.body_iterator.__anext__()
            first_text = first.decode() if isinstance(first, (bytes, bytearray)) else first
            assert first_text.startswith("data: ")
            _revoke_stream(fixture, "epoch")
            with session_scope() as s:
                s.add(JobEvent(
                    job_id=fixture["job_id"], kind="log",
                    line="wave44-secret-log-must-not-stream", log_class="l-err",
                ))
            await _assert_stream_exhausted(response.body_iterator, timeout=1.3)
        finally:
            await response.body_iterator.aclose()

    asyncio.run(scenario())


if __name__ == "__main__":
    test_deleted_user_is_tombstoned_and_old_cookie_cannot_rebind()
    test_existing_database_migration_adds_deleted_at()
    test_login_attempt_keys_are_hard_bounded_by_lru()
    test_login_rejects_email_longer_than_320_characters()
    test_active_lock_denies_even_the_correct_password_until_it_expires()
    test_known_and_unknown_wrong_credentials_share_throttle_contract()
    test_env_seeded_admin_identity_is_normalized()
    test_job_stream_reauthorizes_after_handshake_before_first_emit()
    test_job_stream_reauthorizes_between_frames_before_reading_new_logs()
    print("\nALL WAVE 44 UNIT TESTS PASSED")
