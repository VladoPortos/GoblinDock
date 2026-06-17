"""Wave 34 — ultrareview follow-ups (gaps the cloud review found in batches B/C).

bug_001: edit_secret had no system-secret guard — an admin could rename
         GD_MANAGED_PRIVKEY off its reserved prefix (or overwrite its value) and then
         reveal the fleet SSH key. Now blocked, and a rename can't enter the reserved
         namespace either.
bug_002: _mask_recipe_passwords masked only `password`-typed inputs, but `secret`-typed
         fields can also hold literals (the SecretPicker is a plain text field) — and
         collect_sensitive_inputs already redacts both. Now both are masked for non-owners.
bug_004: edit_variable/edit_secret only enforce the resolver charset on an actual RENAME,
         so a value-only edit of a legacy non-conforming name isn't spuriously rejected.
bug_006: _clean_storage_id rejected `/` but accepted `..`; now `..` is rejected too.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave34.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave34-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from fastapi import HTTPException                 # noqa: E402
from sqlmodel import select                       # noqa: E402
from app.db import init_db, session_scope         # noqa: E402
from app import api                               # noqa: E402
from app import serialize as S                     # noqa: E402
from app.models import Block, Image, Secret, Template, User, Variable  # noqa: E402
from app.security import encrypt, hash_password    # noqa: E402

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
# bug_001 — edit_secret system-secret guard                                    #
# --------------------------------------------------------------------------- #
def test_edit_secret_blocks_managed_key_rename_and_overwrite():
    adm = _mk_user("w34-a1@x.io", role="admin")
    with session_scope() as s:
        sec = Secret(name="GD_MANAGED_PRIVKEY", value_enc=encrypt("-----PRIV-----"), scope="global")
        s.add(sec); s.flush(); sid = sec.id
    with session_scope() as s:
        _expect_http(403, lambda: api.edit_secret(sid, api.SecretEditBody(name="HIJACK"),
                                                  user=s.get(User, adm), session=s))
        _expect_http(403, lambda: api.edit_secret(sid, api.SecretEditBody(value="attacker-key"),
                                                  user=s.get(User, adm), session=s))
    with session_scope() as s:
        sec = s.get(Secret, sid)
        assert sec.name == "GD_MANAGED_PRIVKEY", "managed key must be untouched"
    print("test_edit_secret_blocks_managed_key_rename_and_overwrite OK")


def test_edit_secret_rejects_rename_into_reserved_prefix():
    adm = _mk_user("w34-a2@x.io", role="admin")
    with session_scope() as s:
        sec = Secret(name="NORMAL_W34", value_enc=encrypt("v"), scope="global")
        s.add(sec); s.flush(); sid = sec.id
    with session_scope() as s:
        _expect_http(400, lambda: api.edit_secret(sid, api.SecretEditBody(name="GD_MANAGED_EVIL"),
                                                  user=s.get(User, adm), session=s))
    print("test_edit_secret_rejects_rename_into_reserved_prefix OK")


# --------------------------------------------------------------------------- #
# bug_002 — secret-typed literals masked for non-owners                        #
# --------------------------------------------------------------------------- #
def test_template_dict_masks_secret_typed_literal_for_non_owner():
    owner = _mk_user("w34-own@x.io")
    other = _mk_user("w34-oth@x.io")
    with session_scope() as s:
        s.add(Block(key="b-sec34", name="Sec", phase="cloudinit",
                    input_schema_json=json.dumps([{"name": "authkey", "type": "secret", "default": ""}])))
        img = Image(kind="base", name="i-" + os.urandom(2).hex(),
                    source_url="https://e/x.img", build_status="ready")
        s.add(img); s.flush()
        t = Template(name="leaky", base_image_id=img.id, owner_id=owner, public=True,
                     recipe_json=json.dumps(
                         [{"blocks": [{"ref": "b-sec34", "inputs": {"authkey": "tskey-LITERAL-99"}}]}]))
        s.add(t); s.flush(); tid = t.id

    def _val(viewer_id):
        with session_scope() as s:
            d = S.template_dict(s, s.get(Template, tid), viewer=s.get(User, viewer_id))
            return d["recipe"][0]["blocks"][0]["inputs"]["authkey"]

    assert _val(other) == "********", "a literal in a secret-typed field must be masked for non-owners"
    assert _val(owner) == "tskey-LITERAL-99", "the owner still sees their own value"
    print("test_template_dict_masks_secret_typed_literal_for_non_owner OK")


# --------------------------------------------------------------------------- #
# bug_004 — value-only edit of a legacy non-conforming name is allowed         #
# --------------------------------------------------------------------------- #
def test_edit_variable_value_only_on_legacy_badname_ok():
    uid = _mk_user("w34-v1@x.io", role="admin")
    with session_scope() as s:   # legacy bad name, inserted directly (bypasses add_variable)
        v = Variable(name="MY-VAR", value="old", scope="global", created_by=uid)
        s.add(v); s.flush(); vid = v.id
    with session_scope() as s:   # value-only edit re-sends the existing name → must NOT 400
        api.edit_variable(vid, api.VariableBody(name="MY-VAR", value="new", scope="global"),
                          user=s.get(User, uid), session=s)
    with session_scope() as s:
        assert s.get(Variable, vid).value == "new"
        # but renaming to a NEW non-conforming name is still rejected
        _expect_http(400, lambda: api.edit_variable(
            vid, api.VariableBody(name="STILL-BAD", value="x", scope="global"),
            user=s.get(User, uid), session=s))
    print("test_edit_variable_value_only_on_legacy_badname_ok OK")


# --------------------------------------------------------------------------- #
# bug_006 — _clean_storage_id rejects '..'                                     #
# --------------------------------------------------------------------------- #
def test_clean_storage_id_rejects_dotdot():
    assert api._clean_storage_id("pve-dell", "node") == "pve-dell"
    assert api._clean_storage_id("local.test", "storage") == "local.test"
    assert api._clean_storage_id("", "node") == ""        # empty = auto/default, allowed
    for bad in ("..", "a..b", "..foo", "../../etc"):
        _expect_http(400, lambda b=bad: api._clean_storage_id(b, "node"))
    print("test_clean_storage_id_rejects_dotdot OK")


if __name__ == "__main__":
    test_edit_secret_blocks_managed_key_rename_and_overwrite()
    test_edit_secret_rejects_rename_into_reserved_prefix()
    test_template_dict_masks_secret_typed_literal_for_non_owner()
    test_edit_variable_value_only_on_legacy_badname_ok()
    test_clean_storage_id_rejects_dotdot()
    print("\nALL WAVE 34 UNIT TESTS PASSED")
