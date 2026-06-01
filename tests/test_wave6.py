"""Wave 6 — scheduled SQLite backups, block linter, audit search/paging, image catalog.

Run: GOBLINDOCK_SECRET_KEY=<64hex> .venv/bin/python tests/test_wave6.py
(or just GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave6.py)
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DB = "/tmp/gd-wave6-test.sqlite3"
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", "/tmp/gd-data-test")
os.environ["GOBLINDOCK_BACKUP_DIR"] = "/tmp/gd-wave6-backups"
os.environ["GOBLINDOCK_DEV"] = "1"   # ephemeral key OK for tests

import shutil  # noqa: E402

shutil.rmtree("/tmp/gd-wave6-backups", ignore_errors=True)

from sqlmodel import select                          # noqa: E402
from fastapi import HTTPException                     # noqa: E402

from app.db import init_db, session_scope             # noqa: E402
from app import api, backup, seed                     # noqa: E402
from app.config import settings                       # noqa: E402
from app.models import Audit, Block, Image, User      # noqa: E402
from app.recipes import lint_block                    # noqa: E402

init_db()


def _admin() -> User:
    with session_scope() as s:
        u = s.exec(select(User).where(User.email == "a@b.c")).first()
        if not u:
            u = User(email="a@b.c", name="Admin", password_hash="x", role="admin")
            s.add(u)
            s.flush()
        return User(**u.model_dump())


# --------------------------------------------------------------------------- #
# scheduled SQLite backups                                                     #
# --------------------------------------------------------------------------- #
def test_backup_creates_restorable_copy():
    with session_scope() as s:
        s.add(User(email="bk@test", name="Backup Probe", password_hash="x", role="user"))
    dest = backup.backup_now("test")
    assert dest.exists(), "backup file not written"
    # The copy must be a real, openable SQLite DB containing our row.
    con = sqlite3.connect(str(dest))
    try:
        names = {r[0] for r in con.execute("SELECT email FROM users")}
    finally:
        con.close()
    assert "bk@test" in names, f"backup missing seeded row: {names}"
    listed = {b["name"] for b in backup.list_backups()}
    assert dest.name in listed, "list_backups did not include the new backup"
    print("test_backup_creates_restorable_copy OK")


def test_backup_rotation_keeps_newest():
    settings.backup_keep = 3
    made = [backup.backup_now("test").name for _ in range(5)]
    kept = [b["name"] for b in backup.list_backups()]
    assert len(kept) == 3, f"expected 3 kept, got {len(kept)}: {kept}"
    # newest-first, and exactly the 3 most recent of the 5 we made
    assert kept == sorted(made, reverse=True)[:3], (kept, made)
    # rotation must NEVER touch the live DB or its WAL/SHM sidecars
    live = os.path.basename(settings.db_path)
    assert all(not n.startswith(live[:-len('.sqlite3')]) or n.startswith("goblindock-2")
               for n in kept)
    print("test_backup_rotation_keeps_newest OK")


def test_backup_glob_isolates_live_db():
    # A file that isn't a backup (and the live DB name) must be ignored by rotation.
    bd = backup.backup_dir()
    decoy = bd / "notes.txt"
    decoy.write_text("keep me")
    settings.backup_keep = 1
    backup.backup_now("test")
    assert decoy.exists(), "rotation deleted a non-backup file!"
    decoy.unlink()
    print("test_backup_glob_isolates_live_db OK")


# --------------------------------------------------------------------------- #
# block linter / dry-run validation                                            #
# --------------------------------------------------------------------------- #
GOOD_ANSIBLE = (
    "- name: install {pkg}\n"
    "  ansible.builtin.apt:\n"
    "    name: {pkg}\n"
    "    state: present\n"
)


def test_lint_valid_ansible():
    schema = [{"name": "pkg", "type": "text", "default": "nginx"}]
    assert lint_block("ansible", schema, GOOD_ANSIBLE, "") == [], "valid block flagged"
    print("test_lint_valid_ansible OK")


def test_lint_bad_yaml():
    bad = "- name: oops\n  ansible.builtin.apt:\n   name: \"unterminated\n"
    problems = lint_block("ansible", [], bad, "")
    assert any("YAML" in p for p in problems), problems
    print("test_lint_bad_yaml OK")


def test_lint_non_task_ansible():
    problems = lint_block("ansible", [], "just some free text, not tasks", "")
    assert any("no tasks" in p for p in problems), problems
    print("test_lint_non_task_ansible OK")


def test_lint_bad_schema():
    # schema not a list
    assert lint_block("ansible", {"name": "x"}, GOOD_ANSIBLE, "") == \
        ["input schema must be a list of fields"]
    # missing name + duplicate + bad identifier
    probs = lint_block("ansible", [{"type": "text"}, {"name": "1bad"},
                                   {"name": "ok"}, {"name": "ok"}], GOOD_ANSIBLE, "")
    joined = " | ".join(probs)
    assert "missing a name" in joined and "1bad" in joined and "duplicate" in joined, probs
    print("test_lint_bad_schema OK")


def test_lint_empty_template():
    assert any("non-empty ansible template" in p for p in lint_block("ansible", [], "", "")), \
        "empty ansible template should be rejected"
    assert any("non-empty cloudinit template" in p for p in lint_block("cloudinit", [], "", "")), \
        "empty cloudinit template should be rejected"
    print("test_lint_empty_template OK")


def test_lint_valid_cloudinit():
    schema = [{"name": "msg", "type": "text", "default": "hi"}]
    assert lint_block("cloudinit", schema, "", "echo {msg}") == [], "valid cloud-init flagged"
    print("test_lint_valid_cloudinit OK")


def test_create_block_rejects_bad():
    admin = _admin()
    body = api.BlockBody(name="Broken", phase="ansible",
                         ansible_template="- name: x\n  bad: \"unterminated")
    with session_scope() as s:
        try:
            api.create_block(body, user=admin, session=s)
            raise AssertionError("create_block accepted an invalid block")
        except HTTPException as e:
            assert e.status_code == 400 and "validation failed" in e.detail.lower(), e.detail
    print("test_create_block_rejects_bad OK")


def test_create_block_accepts_good():
    admin = _admin()
    body = api.BlockBody(name="Good", phase="ansible",
                         input_schema=[{"name": "pkg", "default": "git"}],
                         ansible_template=GOOD_ANSIBLE)
    with session_scope() as s:
        out = api.create_block(body, user=admin, session=s)
        assert out.get("ok") and out.get("key"), out
        made = s.exec(select(Block).where(Block.key == out["key"])).first()
        assert made and made.kind == "custom", "block not persisted"
    print("test_create_block_accepts_good OK")


# --------------------------------------------------------------------------- #
# audit search + paging                                                        #
# --------------------------------------------------------------------------- #
def test_audit_search_and_paging():
    with session_scope() as s:
        for i in range(25):
            s.add(Audit(user_name="alice" if i % 2 else "bob", action="vm.deploy",
                        target_type="vm", target_id=str(i), ip="10.0.0.1",
                        detail=f"deploy number {i}"))
        s.add(Audit(user_name="carol", action="secret.reveal", target_type="secret",
                    target_id="9", ip="10.0.0.2", detail="needle-xyz"))
    admin = _admin()
    with session_scope() as s:
        # full set + paging
        page1 = api.list_audit(q="", limit=10, offset=0, user=admin, session=s)
        assert page1["total"] >= 26 and len(page1["rows"]) == 10, page1["total"]
        page3 = api.list_audit(q="", limit=10, offset=20, user=admin, session=s)
        assert page1["rows"][0]["id"] > page3["rows"][0]["id"], "not newest-first / paged"
        # search narrows
        hit = api.list_audit(q="needle-xyz", limit=50, offset=0, user=admin, session=s)
        assert hit["total"] == 1 and hit["rows"][0]["user"] == "carol", hit
        # case-insensitive across fields (user name)
        alice = api.list_audit(q="ALICE", limit=50, offset=0, user=admin, session=s)
        assert alice["total"] == 12, alice["total"]
        # limit is capped to bound the scan
        capped = api.list_audit(q="", limit=99999, offset=0, user=admin, session=s)
        assert capped["limit"] == 200, capped["limit"]
    print("test_audit_search_and_paging OK")


# --------------------------------------------------------------------------- #
# curated image catalog                                                        #
# --------------------------------------------------------------------------- #
def test_catalog_seeded_idempotently():
    seed.seed_base_image()
    seed.seed_base_image()  # second call must not duplicate
    by_name = {}
    with session_scope() as s:
        for b in s.exec(select(Image).where(Image.kind == "base")).all():
            by_name[b.name] = by_name.get(b.name, 0) + 1
    for entry in seed.CURATED_BASE_IMAGES:
        assert by_name.get(entry["name"]) == 1, f"{entry['name']}: {by_name.get(entry['name'])}"
        assert entry["source_url"].startswith("https://"), entry
    print(f"test_catalog_seeded_idempotently OK ({len(seed.CURATED_BASE_IMAGES)} curated bases)")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\nAll {len(fns)} wave-6 tests passed.")
