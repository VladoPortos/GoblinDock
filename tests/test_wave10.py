"""Wave 10 — templates-only: base image + connection on templates (golden pointer dropped).

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave10.py
Run (Windows):        $env:GOBLINDOCK_DEV='1'; .venv\\Scripts\\python.exe tests\\test_wave10.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave10-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from app.db import engine, init_db, session_scope  # noqa: E402

init_db()


def _cols(table):
    with engine.begin() as conn:
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def _expect_http(code, fn):
    from fastapi import HTTPException
    try:
        fn()
    except HTTPException as e:
        assert e.status_code == code, (e.status_code, e.detail)
        return e
    raise AssertionError(f"expected HTTPException {code}")


def _mk_user(email, role="user"):
    from app.models import User
    from app.security import hash_password
    with session_scope() as s:
        u = User(email=email, name="U", password_hash=hash_password("StrongPass12!"), role=role)
        s.add(u); s.flush()
        return u.id


def _mk_conn_base_net():
    """connection + base image + network on the connection; returns (conn_id, base_id, net_id)."""
    from app.models import Connection, Image, Network
    with session_scope() as s:
        c = Connection(name="px-t10-" + os.urandom(3).hex(), host="127.0.0.1",
                       token_id="t@pve!x", node="pve")
        s.add(c); s.flush()
        img = Image(kind="base", name="b-ubuntu-" + os.urandom(2).hex(), os_family="ubuntu",
                    source_url="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
                    build_status="ready")
        s.add(img); s.flush()
        net = Network(connection_id=c.id, name="lan", mode="dhcp")
        s.add(net); s.flush()
        return c.id, img.id, net.id


def test_schema_templates_only():
    tcols = _cols("templates")
    assert {"base_image_id", "connection_id"} <= tcols, tcols
    assert "golden_image_id" not in tcols, tcols
    print("test_schema_templates_only OK")


def test_template_refs_validation():
    from app import api
    from app.models import Template, User
    from sqlmodel import select
    uid = _mk_user("t10-crud@example.com")
    conn_id, base_id, net_id = _mk_conn_base_net()
    with session_scope() as s:
        user = s.get(User, uid)
        api.save_template(api.TemplateBody(
            name="web10", description="d", recipe=[],
            baseImageId=base_id, connectionId=conn_id, networkId=net_id),
            user=user, session=s)
    with session_scope() as s:
        t = s.exec(select(Template).where(Template.name == "web10")).first()
        assert t and t.base_image_id == base_id and t.connection_id == conn_id and t.network_id == net_id
    with session_scope() as s:
        user = s.get(User, uid)
        # baseImageId must reference a BASE image
        _expect_http(400, lambda: api.save_template(api.TemplateBody(
            name="bad", recipe=[], baseImageId=999999), user=user, session=s))
        # network requires a connection
        _expect_http(400, lambda: api.save_template(api.TemplateBody(
            name="bad2", recipe=[], networkId=net_id), user=user, session=s))
    # network on a different connection → 400
    conn2, _b2, net2 = _mk_conn_base_net()
    with session_scope() as s:
        user = s.get(User, uid)
        _expect_http(400, lambda: api.save_template(api.TemplateBody(
            name="bad3", recipe=[], baseImageId=base_id, connectionId=conn_id,
            networkId=net2), user=user, session=s))
    print("test_template_refs_validation OK")


def _mk_template10(base_id, conn_id, net_id=None, ask=True, cpu=1, ram=2, disk=20):
    from app.models import Template
    recipe = [{"id": "s-os", "name": "OS Setup", "blocks": [
        {"ref": "b-hostname", "name": "Set Hostname",
         "inputs": {"hostname": ""}, **({"ask": ["hostname"]} if ask else {})},
    ]}]
    with session_scope() as s:
        t = Template(name="t10-" + os.urandom(3).hex(), recipe_json=json.dumps(recipe),
                     base_image_id=base_id, connection_id=conn_id, network_id=net_id,
                     default_cpu=cpu, default_ram=ram, default_disk=disk, public=True)
        s.add(t); s.flush()
        return t.id


def test_deploy_templates_only():
    from app import api
    from app.models import Deployment, Job, User
    from app.seed import seed_blocks
    from sqlmodel import select
    seed_blocks()
    uid = _mk_user("t10-deploy@example.com")
    conn_id, base_id, net_id = _mk_conn_base_net()
    tid = _mk_template10(base_id, conn_id, net_id, cpu=1, ram=2, disk=25)

    def _deploy(**kw):
        body = api.DeployBody(**kw)
        with session_scope() as s:
            return api.deploy(body, user=s.get(User, uid), session=s)

    # sizes default from the template; image_id set to the base image; ctx carries src_url
    r = _deploy(templateId=tid, name="t10-a", deployInputs={"0.0": {"hostname": "h1"}})
    assert r["ok"]
    with session_scope() as s:
        d = s.exec(select(Deployment).where(Deployment.name == "t10-a")).first()
        assert d.image_id == base_id and d.template_id == tid
        assert (d.cpu, d.ram, d.disk) == (1, 2, 25), (d.cpu, d.ram, d.disk)
        job = s.exec(select(Job).where(Job.deployment_id == d.id)).first()
        ctx = json.loads(job.context_json)
        assert ctx.get("src_url"), ctx
        assert "src_vmid" not in ctx, ctx

    # explicit size override respected (server clamps still apply)
    r2 = _deploy(templateId=tid, name="t10-b", cpu=1, ram=1, disk=30,
                 deployInputs={"0.0": {"hostname": "h2"}})
    assert r2["ok"]
    with session_scope() as s:
        d2 = s.exec(select(Deployment).where(Deployment.name == "t10-b")).first()
        assert (d2.ram, d2.disk) == (1, 30), (d2.ram, d2.disk)

    # template without base image / connection → 400
    t_nobase = _mk_template10(None, conn_id)
    _expect_http(400, lambda: _deploy(templateId=t_nobase, name="t10-c",
                                      deployInputs={"0.0": {"hostname": "h"}}))
    t_noconn = _mk_template10(base_id, None)
    _expect_http(400, lambda: _deploy(templateId=t_noconn, name="t10-d",
                                      deployInputs={"0.0": {"hostname": "h"}}))
    # missing template id → pydantic ValidationError
    try:
        api.DeployBody(name="t10-e")
        raise AssertionError("expected ValidationError")
    except Exception as e:
        assert "templateId" in str(e), e
    print("test_deploy_templates_only OK")


def test_legacy_rebuild_guard():
    from app import api
    from app.models import Deployment, User
    uid = _mk_user("t10-rb@example.com")
    conn_id, base_id, net_id = _mk_conn_base_net()
    with session_scope() as s:
        d = Deployment(name="legacy-rb", owner_id=uid, connection_id=conn_id,
                       vmid=8042, status="running", template_id=None)
        s.add(d); s.flush(); dep_id = d.id
    with session_scope() as s:
        _expect_http(400, lambda: api.vm_rebuild(dep_id, user=s.get(User, uid), session=s))
    print("test_legacy_rebuild_guard OK")


def test_seed_template_wiring():
    from app.seed import run_all_seeds
    from app.models import Template
    from sqlmodel import select
    run_all_seeds()
    with session_scope() as s:
        t = s.exec(select(Template).where(Template.name == "AI Dev Box")).first()
        assert t is not None
        assert t.base_image_id is not None, "AI Dev Box should wire to the seeded ubuntu base image"
        rec = json.loads(t.recipe_json)
        assert rec[0]["blocks"][0].get("ask") == ["hostname"], rec[0]["blocks"][0]
    print("test_seed_template_wiring OK")


def test_cached_images_endpoint():
    from app import api
    from app.models import Image, User
    from app.proxmox import base_disk_filename
    uid = _mk_user("t10-cache@example.com")
    conn_id, base_id, net_id = _mk_conn_base_net()
    with session_scope() as s:
        src = s.get(Image, base_id).source_url
        blank = Image(kind="base", name="no-url-cache", os_family="ubuntu",
                      source_url="", build_status="ready")
        s.add(blank); s.flush(); blank_id = blank.id
    # unknown connection → 404
    with session_scope() as s:
        _expect_http(404, lambda: api.cached_images(999999, user=s.get(User, uid), session=s))
    # stub Proxmox: exactly base_id's file is present on the node
    class _StubPx:
        def __init__(self, conn): pass
        def storage_volumes(self, node=None, content="import"):
            return {f"local:import/{base_disk_filename(src)}"}
        def iso_volume_path(self, filename):
            return f"local:import/{filename}"
    orig = api.Proxmox
    api.Proxmox = _StubPx
    try:
        with session_scope() as s:
            out = api.cached_images(conn_id, user=s.get(User, uid), session=s)
        assert out["online"] is True
        assert out["cached"][str(base_id)] is True
        assert str(blank_id) not in out["cached"], "blank source_url must be omitted"
        assert all(isinstance(v, bool) for v in out["cached"].values())
    finally:
        api.Proxmox = orig
    # unreachable node → online False, HTTP 200 (no exception)
    class _DownPx:
        def __init__(self, conn): pass
        def storage_volumes(self, node=None, content="import"):
            raise RuntimeError("connection refused")
    api.Proxmox = _DownPx
    try:
        with session_scope() as s:
            out = api.cached_images(conn_id, user=s.get(User, uid), session=s)
        assert out == {"online": False, "cached": {}}
    finally:
        api.Proxmox = orig
    print("test_cached_images_endpoint OK")


def test_sync_endpoint():
    from app import api
    from app.models import Image, Job, User
    uid = _mk_user("t10-sync@example.com")
    conn_id, base_id, net_id = _mk_conn_base_net()
    with session_scope() as s:
        user = s.get(User, uid)
        _expect_http(404, lambda: api.sync_image(999999, api.SyncBody(connectionId=conn_id), user=user, session=s))
        g = Image(kind="golden", name="legacy-sync-g", os_family="ubuntu")
        s.add(g); s.flush()
        _expect_http(400, lambda: api.sync_image(g.id, api.SyncBody(connectionId=conn_id), user=user, session=s))
        b = Image(kind="base", name="no-url-sync", os_family="ubuntu", source_url="", build_status="ready")
        s.add(b); s.flush()
        _expect_http(400, lambda: api.sync_image(b.id, api.SyncBody(connectionId=conn_id), user=user, session=s))
        _expect_http(404, lambda: api.sync_image(base_id, api.SyncBody(connectionId=999999), user=user, session=s))
        r = api.sync_image(base_id, api.SyncBody(connectionId=conn_id), user=user, session=s)
        assert r["ok"] and r["jobId"]
        job_id = r["jobId"]
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job.type == "image_sync" and job.image_id == base_id and job.connection_id == conn_id
        ctx = json.loads(job.context_json)
        assert ctx["src_url"] and "checksum" in ctx and "checksum_algorithm" in ctx
    from app.worker import _DISPATCH
    assert "image_sync" in _DISPATCH
    from app import serialize as S
    with session_scope() as s:
        brief = S.job_brief(s, s.get(Job, job_id))
        assert brief["imageId"] == base_id
    with session_scope() as s:
        detail = S.job_detail(s, s.get(Job, job_id))
        assert detail["phases"] == ["Prepare image"], detail["phases"]
    # the failure handler must be scoped to legacy image_build jobs — a failed
    # image_sync must never flip a base image's build_status (guard regression check)
    import inspect
    from app import worker as W
    src = inspect.getsource(W._execute)
    assert 'job.type == "image_build"' in src, "image_id failure handler lost its type guard"
    print("test_sync_endpoint OK")


def test_wait_task_timeout_stops_task():
    from app.models import Connection
    from app.proxmox import Proxmox, ProxmoxError
    px = Proxmox(Connection(name="stub", host="127.0.0.1", token_id="t@pve!x", node="pve"))
    calls = {"deleted": 0, "polls": 0}

    class _Status:
        @staticmethod
        def get():
            calls["polls"] += 1
            return {"status": "running"}

    class _Task:
        status = _Status()
        @staticmethod
        def delete():
            calls["deleted"] += 1

    class _Tasks:
        def __call__(self, upid):
            return _Task()

    class _Node:
        tasks = _Tasks()

    class _Nodes:
        def __call__(self, node):
            return _Node()

    class _Api:
        nodes = _Nodes()

    px.api = _Api()
    try:
        px.wait_task("UPID:stub", node="pve", timeout=0.2)
        raise AssertionError("expected ProxmoxError timeout")
    except ProxmoxError as e:
        assert "timed out" in str(e), e
    assert calls["deleted"] == 1, "timeout must stop the orphaned node task"
    assert calls["polls"] >= 1
    print("test_wait_task_timeout_stops_task OK")


def test_job_detail_waiting_for():
    from app import serialize as S
    from app.models import Job, User
    from sqlmodel import select
    # neutralise any running/queued leftovers from earlier tests in the shared DB
    with session_scope() as s:
        for j in s.exec(select(Job).where(Job.status.in_(("running", "queued")))).all():
            j.status = "succeeded"
            s.add(j)

    # create two users: uid_a (owner of the running job) and uid_b (unrelated non-admin)
    uid_a = _mk_user("t10-wf-a@example.com")
    uid_b = _mk_user("t10-wf-b@example.com")
    uid_adm = _mk_user("t10-wf-adm@example.com", role="admin")

    with session_scope() as s:
        running = Job(type="image_sync", title="Syncing Big Image → pve",
                      status="running", created_by=uid_a)
        queued = Job(type="deploy", title="Deploying wf-test", status="queued")
        s.add(running); s.add(queued); s.flush()
        rid, qid = running.id, queued.id

    # viewer = owner of the running job → full title revealed
    with session_scope() as s:
        viewer_a = s.get(User, uid_a)
        d = S.job_detail(s, s.get(Job, qid), include_log=False, viewer=viewer_a)
        assert d.get("waitingFor") == "Syncing Big Image → pve", d.get("waitingFor")

    # viewer = unrelated non-admin → generic placeholder
    with session_scope() as s:
        viewer_b = s.get(User, uid_b)
        d = S.job_detail(s, s.get(Job, qid), include_log=False, viewer=viewer_b)
        assert d.get("waitingFor") == "another job", d.get("waitingFor")

    # viewer = admin → full title revealed
    with session_scope() as s:
        viewer_adm = s.get(User, uid_adm)
        d = S.job_detail(s, s.get(Job, qid), include_log=False, viewer=viewer_adm)
        assert d.get("waitingFor") == "Syncing Big Image → pve", d.get("waitingFor")

    # viewer omitted (None) → safe generic default
    with session_scope() as s:
        d = S.job_detail(s, s.get(Job, qid), include_log=False)
        assert d.get("waitingFor") == "another job", d.get("waitingFor")

    # running job's own detail → waitingFor is None (unchanged)
    with session_scope() as s:
        d2 = S.job_detail(s, s.get(Job, rid), include_log=False)
        assert d2.get("waitingFor") is None, d2.get("waitingFor")

    # cleanup
    with session_scope() as s:
        for jid in (rid, qid):
            j = s.get(Job, jid)
            j.status = "succeeded"
            s.add(j)
    print("test_job_detail_waiting_for OK")


if __name__ == "__main__":
    test_schema_templates_only()
    test_template_refs_validation()
    test_deploy_templates_only()
    test_legacy_rebuild_guard()
    test_seed_template_wiring()
    test_cached_images_endpoint()
    test_sync_endpoint()
    test_wait_task_timeout_stops_task()
    test_job_detail_waiting_for()
    print("\nALL WAVE 10 UNIT TESTS PASSED")
