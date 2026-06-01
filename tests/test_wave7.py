"""Wave 7 — stale-image cleanup report (classification + RBAC scoping).

Run: GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave7.py
(The console clipboard/resize feature is pure frontend — covered by the browser smoke.)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DB = "/tmp/gd-wave7-test.sqlite3"
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", "/tmp/gd-data-test")
os.environ["GOBLINDOCK_DEV"] = "1"

from sqlmodel import select                          # noqa: E402

from app.db import init_db, session_scope            # noqa: E402
from app import api                                  # noqa: E402
from app.models import Image, Deployment, User       # noqa: E402

init_db()


def _user(email, role):
    with session_scope() as s:
        u = s.exec(select(User).where(User.email == email)).first()
        if not u:
            u = User(email=email, name=email.split("@")[0], password_hash="x", role=role)
            s.add(u)
            s.flush()
        return User(**u.model_dump())


ADMIN = _user("admin@w7", "admin")
USER2 = _user("user2@w7", "user")


def _seed():
    with session_scope() as s:
        ids = {}
        rows = [
            ("g-failed", "failed", None, ADMIN.id),
            ("g-none", "none", None, ADMIN.id),
            ("g-ready-unused", "ready", 8001, ADMIN.id),
            ("g-ready-used", "ready", 8002, ADMIN.id),
            ("g-building", "building", None, ADMIN.id),
            ("g-user2-failed", "failed", None, USER2.id),
        ]
        for name, st, vmid, owner in rows:
            img = Image(kind="golden", name=name, build_status=st,
                        template_vmid=vmid, created_by=owner)
            s.add(img)
            s.flush()
            ids[name] = img.id
        # a base image must NEVER appear in the golden stale report
        s.add(Image(kind="base", name="b-ubuntu", build_status="ready", created_by=ADMIN.id))
        # one deployment referencing g-ready-used → keeps it OUT of "stale"
        s.add(Deployment(name="live-vm", owner_id=ADMIN.id, image_id=ids["g-ready-used"],
                         status="running"))
        return ids


IDS = _seed()


def test_admin_sees_all_stale_goldens():
    with session_scope() as s:
        out = api.stale_images(user=ADMIN, session=s)["candidates"]
    by = {c["name"]: c for c in out}
    assert set(by) == {"g-failed", "g-none", "g-ready-unused", "g-user2-failed"}, set(by)
    assert by["g-failed"]["reason"] == "build failed"
    assert by["g-none"]["reason"] == "never finished building"
    assert by["g-ready-unused"]["reason"] == "no deployments use it"
    assert all(c["canDelete"] for c in out), "admin can delete every candidate"
    print("test_admin_sees_all_stale_goldens OK")


def test_in_use_and_building_excluded():
    with session_scope() as s:
        names = {c["name"] for c in api.stale_images(user=ADMIN, session=s)["candidates"]}
    assert "g-ready-used" not in names, "a golden with a deployment must not be stale"
    assert "g-building" not in names, "an in-progress build must not be stale"
    print("test_in_use_and_building_excluded OK")


def test_base_images_never_reported():
    with session_scope() as s:
        names = {c["name"] for c in api.stale_images(user=ADMIN, session=s)["candidates"]}
    assert "b-ubuntu" not in names, "base images are out of scope for the golden cleanup"
    print("test_base_images_never_reported OK")


def test_non_admin_sees_only_own():
    with session_scope() as s:
        out = api.stale_images(user=USER2, session=s)["candidates"]
    names = {c["name"] for c in out}
    assert names == {"g-user2-failed"}, names
    assert out[0]["canDelete"] is True
    # non-admin report must not leak other users' identities
    assert all(c["owner"] == "—" for c in out), out
    print("test_non_admin_sees_only_own OK")


def test_admin_report_includes_owner_names():
    with session_scope() as s:
        out = api.stale_images(user=ADMIN, session=s)["candidates"]
    by = {c["name"]: c for c in out}
    assert by["g-user2-failed"]["owner"] == "user2", by["g-user2-failed"]
    print("test_admin_report_includes_owner_names OK")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\nAll {len(fns)} wave-7 tests passed.")
