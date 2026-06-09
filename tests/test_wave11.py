"""Wave 11 — live state push: statebus signal + SSE stream wiring.

Run (Linux/WSL/CI):   GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave11.py
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave11-test.sqlite3")
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", os.path.join(tempfile.gettempdir(), "gd-data-test"))

from app.db import init_db, session_scope  # noqa: E402

init_db()


def test_statebus_bump_monotonic():
    from app import statebus
    v0 = statebus.version()
    statebus.bump()
    assert statebus.version() == v0 + 1
    statebus.bump(); statebus.bump()
    assert statebus.version() == v0 + 3
    print("test_statebus_bump_monotonic OK")


def test_statebus_thread_safe():
    from app import statebus
    v0 = statebus.version()

    def worker():
        for _ in range(1000):
            statebus.bump()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statebus.version() == v0 + 8000, statebus.version()
    print("test_statebus_thread_safe OK")


def test_state_stream_route_registered():
    from app.api import router
    paths = {r.path for r in router.routes}
    assert "/api/state/stream" in paths, sorted(paths)
    print("test_state_stream_route_registered OK")


def test_progress_bumps_statebus():
    from app import statebus
    from app.models import Job
    from app.worker import JobCtx
    with session_scope() as s:
        j = Job(type="deploy", title="bump-test", status="running")
        s.add(j); s.flush()
        jid = j.id
    v0 = statebus.version()
    JobCtx(jid).progress(42, "Phase 1 of 5 · Test")
    assert statebus.version() > v0, "progress() must bump statebus"
    print("test_progress_bumps_statebus OK")


def test_deploy_bumps_statebus():
    from app import api, statebus
    from app.models import User
    from app.seed import seed_blocks
    # minimal connection + base image + template (mirrors wave10 helpers)
    from app.models import Connection, Image, Network, Template
    seed_blocks()
    with session_scope() as s:
        u = User(email="w11-deploy@example.com", name="U",
                 password_hash="x", role="user")
        s.add(u); s.flush(); uid = u.id
        c = Connection(name="px-w11", host="127.0.0.1", token_id="t@pve!x", node="pve")
        s.add(c); s.flush()
        img = Image(kind="base", name="b-w11", os_family="ubuntu",
                    source_url="https://example.invalid/img.img", build_status="ready")
        s.add(img); s.flush()
        net = Network(connection_id=c.id, name="lan", mode="dhcp")
        s.add(net); s.flush()
        t = Template(name="t-w11", recipe_json="[]", base_image_id=img.id,
                     connection_id=c.id, network_id=net.id, public=True)
        s.add(t); s.flush(); tid = t.id
    v0 = statebus.version()
    with session_scope() as s:
        api.deploy(api.DeployBody(templateId=tid, name="w11-vm"),
                   user=s.get(User, uid), session=s)
    assert statebus.version() > v0, "deploy() must bump statebus"
    print("test_deploy_bumps_statebus OK")


def test_state_stream_emits_frame():
    """The SSE generator emits a `state` frame on the first tick. We pull a single
    frame via __anext__ (yielded BEFORE the 1s sleep), so the test is instant."""
    import asyncio
    from app import api, statebus
    from app.models import User

    class _Req:
        async def is_disconnected(self):
            return False

    async def _first_frame():
        statebus.bump()  # ensure version() != the generator's -1 sentinel
        user = User(email="w11-sse@example.com", name="U", password_hash="x", role="admin")
        resp = await api.state_stream(_Req(), user=user)
        try:
            return await resp.body_iterator.__anext__()
        finally:
            await resp.body_iterator.aclose()

    frame = asyncio.run(_first_frame())
    text = frame.decode() if isinstance(frame, (bytes, bytearray)) else frame
    assert text.startswith("event: state"), text
    assert '"v"' in text, text
    print("test_state_stream_emits_frame OK")


if __name__ == "__main__":
    test_statebus_bump_monotonic()
    test_statebus_thread_safe()
    test_state_stream_route_registered()
    test_state_stream_emits_frame()
    test_progress_bumps_statebus()
    test_deploy_bumps_statebus()
    print("\nALL WAVE 11 UNIT TESTS PASSED")
