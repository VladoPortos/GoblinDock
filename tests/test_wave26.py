"""Wave 26 — review Batch B1: secret/credential confidentiality (the two QA Highs).

QA-H1: user templates were PUBLIC by default and /api/state served their full raw
recipe to every authenticated user, so a literal value typed into a `password`-typed
block input leaked in plaintext. Fix: private by default + mask password-typed inputs
for non-owners.

QA-H2: the job-log redaction vault was fed only by {{ secrets.NAME }} resolution, so
a LITERAL value typed into a password/secret-typed input was never redacted and could
appear in Ansible failure output. Fix: vault all password/secret-typed input values.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave26.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave26-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from sqlmodel import select                            # noqa: E402
from app.db import init_db, session_scope             # noqa: E402
from app import worker, recipes                        # noqa: E402
from app import serialize as S                         # noqa: E402
from app.models import Block, JobEvent, Job, Template, User  # noqa: E402
from app.security import hash_password                  # noqa: E402

init_db()


def _mk_user(email, role="user"):
    with session_scope() as s:
        u = User(email=email, name="U", password_hash=hash_password("StrongPass12!"), role=role)
        s.add(u); s.flush()
        return u.id


def _pw_block(key, phase="cloudinit"):
    return Block(key=key, name="PW", category="test", phase=phase,
                 ansible_template="- name: t\n  ansible.builtin.shell: echo {password_q}",
                 input_schema_json=json.dumps([{"name": "password", "type": "password", "default": ""}]))


def _recipe(ref, pw):
    return [{"blocks": [{"ref": ref, "inputs": {"password": pw}}]}]


# --------------------------------------------------------------------------- #
# QA-H1 — private by default + non-owner password masking                      #
# --------------------------------------------------------------------------- #
def test_template_public_defaults_false():
    from app import api
    assert api.TemplateBody(name="x").public is False, \
        "user-created templates must be PRIVATE by default"
    print("test_template_public_defaults_false OK")


def test_template_dict_masks_password_for_non_owner():
    from app.models import Image
    owner = _mk_user("w26-owner@x.io")
    other = _mk_user("w26-other@x.io")
    admin = _mk_user("w26-adm@x.io", role="admin")
    with session_scope() as s:
        s.add(_pw_block("b-pw26"))
        img = Image(kind="base", name="i-" + os.urandom(2).hex(),
                    source_url="https://e/x.img", build_status="ready")
        s.add(img); s.flush()
        t = Template(name="leaky", base_image_id=img.id, owner_id=owner, public=True,
                     recipe_json=json.dumps(_recipe("b-pw26", "leakMe123")))
        s.add(t); s.flush(); tid = t.id

    def _pw_value(viewer_id):
        with session_scope() as s:
            d = S.template_dict(s, s.get(Template, tid), viewer=s.get(User, viewer_id))
            return d["recipe"][0]["blocks"][0]["inputs"]["password"]

    assert _pw_value(other) == "********", "a non-owner must NOT see the literal password"
    assert _pw_value(owner) == "leakMe123", "the owner still sees their own value"
    assert _pw_value(admin) == "leakMe123", "admin sees all"
    print("test_template_dict_masks_password_for_non_owner OK")


# --------------------------------------------------------------------------- #
# QA-H2 — literal password/secret inputs enter the redaction vault             #
# --------------------------------------------------------------------------- #
def test_collect_sensitive_inputs_includes_password_literal():
    blk = _pw_block("b-pw26b", phase="ansible")
    vals = recipes.collect_sensitive_inputs(_recipe("b-pw26b", "s3cretLiteral"),
                                            {"b-pw26b": blk}, lambda ns, n: "")
    assert "s3cretLiteral" in vals, f"password literal must be collected for redaction, got {vals}"
    print("test_collect_sensitive_inputs_includes_password_literal OK")


def test_ansible_phase_redacts_literal_password_in_job_log():
    """A failed/echoed shell task that prints the literal password must NOT leave the
    plaintext in the persisted job log."""
    SECRET = "s3cretLiteral99"
    blk = _pw_block("b-pw26c", phase="ansible")
    with session_scope() as s:
        j = Job(type="deploy", status="running"); s.add(j); s.flush(); jid = j.id
    ctx = worker.JobCtx(jid)
    saved = {k: getattr(worker, k) for k in ("run_playbook", "_blocks_by_key", "compile_ansible")}

    def _fake_run(playbook, ip, user, key, on_line=None, cancelled=None, timeout=1200):
        on_line(f"FAILED! cmd: mysql -e \"CREATE USER x IDENTIFIED BY '{SECRET}'\"")
        return ("successful", 0)
    worker.run_playbook = _fake_run
    worker._blocks_by_key = lambda: {"b-pw26c": blk}
    worker.compile_ansible = lambda *a, **k: "- hosts: all\n  tasks: []"
    try:
        worker._run_ansible_phase(ctx, _recipe("b-pw26c", SECRET), None, "10.0.0.5", "KEY", "cfg")
    finally:
        for k, v in saved.items():
            setattr(worker, k, v)

    with session_scope() as s:
        logs = [e.line for e in s.exec(
            select(JobEvent).where(JobEvent.job_id == jid, JobEvent.kind == "log")).all()]
    assert logs, "expected a streamed log line"
    assert not any(SECRET in ln for ln in logs), \
        f"literal password leaked into job log: {[ln for ln in logs if SECRET in ln]}"
    assert any("***" in ln for ln in logs), "the secret should have been masked to ***"
    print("test_ansible_phase_redacts_literal_password_in_job_log OK")


if __name__ == "__main__":
    test_template_public_defaults_false()
    test_template_dict_masks_password_for_non_owner()
    test_collect_sensitive_inputs_includes_password_literal()
    test_ansible_phase_redacts_literal_password_in_job_log()
    print("\nALL WAVE 26 UNIT TESTS PASSED")
