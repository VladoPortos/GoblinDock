"""Wave 33 — review Batch H (maintainability): Block.builtin is the single source of
truth for "is this one of GoblinDock's own built-in blocks?".

seed_blocks() pruned removed built-ins by `kind == "builtin"` but re-synced and gated
visibility by `.builtin` — two fields for one concept (DRY hazard: if they ever
diverge, prune and re-sync disagree). The prune now keys off `.builtin` like
everything else, so an orphaned built-in is pruned regardless of the descriptive
`kind` string, and a user's custom block is never touched.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave33.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave33-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from sqlmodel import select                       # noqa: E402
from app.db import init_db, session_scope         # noqa: E402
from app import seed                               # noqa: E402
from app.models import Block                       # noqa: E402

init_db()


def _add(key, builtin, kind):
    with session_scope() as s:
        s.add(Block(key=key, name=key, builtin=builtin, kind=kind))


def _exists(key):
    with session_scope() as s:
        return s.exec(select(Block).where(Block.key == key)).first() is not None


def test_prune_keys_off_builtin_not_kind():
    # three orphan blocks whose keys are NOT in the catalog:
    _add("b-zz-gone", builtin=True, kind="builtin")    # a removed built-in → must be pruned
    _add("b-zz-mine", builtin=False, kind="custom")    # a user's custom block → must be kept
    _add("b-zz-divergent", builtin=True, kind="custom")  # divergent: builtin=True wins → pruned
    seed.seed_blocks()
    assert not _exists("b-zz-gone"), "a removed built-in (builtin=True) must be pruned"
    assert _exists("b-zz-mine"), "a user's custom block (builtin=False) must never be pruned"
    assert not _exists("b-zz-divergent"), \
        "prune must key off .builtin: a builtin=True orphan is pruned regardless of kind"
    print("test_prune_keys_off_builtin_not_kind OK")


if __name__ == "__main__":
    test_prune_keys_off_builtin_not_kind()
    print("\nALL WAVE 33 UNIT TESTS PASSED")
