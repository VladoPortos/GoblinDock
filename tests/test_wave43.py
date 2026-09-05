"""Wave 43 — final scheduler, network, and packaging regressions."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
from datetime import timedelta

from sqlmodel import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("GOBLINDOCK_DEV", "1")
_DB = os.path.join(tempfile.gettempdir(), "gd-wave43-test.sqlite3")
for _ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + _ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB

from app import api, worker  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Connection, Deployment, Job, Network, utcnow  # noqa: E402

init_db()


def test_static_network_context_uses_family_specific_proxmox_keys():
    saved_allocate = api.allocate_ip
    try:
        api.allocate_ip = lambda *_args, **_kwargs: "2001:db8:43::20"
        ipv6 = Network(
            name="v6", mode="static", subnet_cidr="2001:db8:43::/64",
            gateway="2001:db8:43::1", range_start="2001:db8:43::20",
            range_end="2001:db8:43::40", bridge="vmbr0",
        )
        v6 = api._network_ctx(None, ipv6, 43)
        assert v6["ipconfig0"] == "ip6=2001:db8:43::20/64,gw6=2001:db8:43::1"
        assert "ip=" not in v6["ipconfig0"] and ",gw=" not in v6["ipconfig0"]

        api.allocate_ip = lambda *_args, **_kwargs: "192.0.2.43"
        ipv4 = Network(
            name="v4", mode="static", subnet_cidr="192.0.2.0/24",
            gateway="192.0.2.1", range_start="192.0.2.43",
            range_end="192.0.2.80", bridge="vmbr0",
        )
        v4 = api._network_ctx(None, ipv4, 44)
        assert v4["ipconfig0"] == "ip=192.0.2.43/24,gw=192.0.2.1"
    finally:
        api.allocate_ip = saved_allocate


def _waiting_fixture() -> int:
    with session_scope() as session:
        conn = Connection(name="wave43-pve", host="127.0.0.1", token_id="u@p!t")
        session.add(conn)
        session.flush()
        dep = Deployment(
            name="wave43-vm", connection_id=conn.id, vmid=8043,
            node="pve", status="working",
        )
        session.add(dep)
        session.flush()
        job = Job(
            type="deploy", status="waiting", deployment_id=dep.id,
            connection_id=conn.id, waiting_since=utcnow() - timedelta(minutes=31),
        )
        session.add(job)
        session.flush()
        return job.id


def test_expired_wait_probes_ready_guest_before_timing_out():
    job_id = _waiting_fixture()
    resumed = []
    timed_out = []
    saved_px = worker.Proxmox
    saved_resume = worker._resume_waiting_ansible
    saved_timeout = worker._timeout_waiting_job

    class _Px:
        def __init__(self, _conn): pass
        def agent_ipv4(self, vmid, node):
            assert (vmid, node) == (8043, "pve")
            return "192.0.2.143"

    try:
        worker.Proxmox = _Px
        worker._resume_waiting_ansible = lambda jid, ip: resumed.append((jid, ip))
        worker._timeout_waiting_job = lambda jid: timed_out.append(jid)
        worker._poll_waiting_job(job_id, utcnow())
    finally:
        worker.Proxmox = saved_px
        worker._resume_waiting_ansible = saved_resume
        worker._timeout_waiting_job = saved_timeout

    assert resumed == [(job_id, "192.0.2.143")]
    assert timed_out == []


def test_waiting_poll_does_not_refuse_ready_work_because_queue_is_nonempty():
    with session_scope() as session:
        waiting = session.exec(select(Job).where(Job.status == "waiting")).first()
        assert waiting is not None
        waiting_id = waiting.id
        queued = Job(type="image_sync", status="queued")
        session.add(queued)
    visited = []
    saved_poll = worker._poll_waiting_job
    try:
        worker._poll_waiting_job = lambda job_id, _now: visited.append(job_id)
        assert worker._poll_waiting_jobs() is True
    finally:
        worker._poll_waiting_job = saved_poll
    assert visited == [waiting_id]


def test_waiting_poller_runs_while_main_worker_is_busy():
    execute_started = threading.Event()
    release_execute = threading.Event()
    waiting_polled = threading.Event()
    claimed = {"done": False}
    saved_claim = worker._claim_next_job
    saved_execute = worker._execute
    saved_poll = worker._poll_waiting_jobs

    def claim():
        if claimed["done"]:
            return None
        claimed["done"] = True
        return 43

    def execute(_job_id):
        execute_started.set()
        assert release_execute.wait(timeout=3)

    def poll():
        waiting_polled.set()
        return True

    worker._claim_next_job = claim
    worker._execute = execute
    worker._poll_waiting_jobs = poll
    worker._stop.clear()
    main = threading.Thread(target=worker._loop)
    waits = threading.Thread(target=worker._waiting_loop)
    try:
        main.start()
        assert execute_started.wait(timeout=2)
        waits.start()
        assert waiting_polled.wait(timeout=2), "waiting poller stalled behind queued execution"
    finally:
        worker._stop.set()
        release_execute.set()
        main.join(timeout=3)
        waits.join(timeout=3)
        worker._claim_next_job = saved_claim
        worker._execute = saved_execute
        worker._poll_waiting_jobs = saved_poll
    assert not main.is_alive() and not waits.is_alive()


def test_worker_lifecycle_starts_and_stops_both_threads():
    main_started = threading.Event()
    waiting_started = threading.Event()
    saved_main_loop = worker._loop
    saved_waiting_loop = worker._waiting_loop
    saved_main_thread = worker._worker_thread
    saved_waiting_thread = worker._waiting_thread

    def main_loop():
        main_started.set()
        worker._stop.wait(3)

    def waiting_loop():
        waiting_started.set()
        worker._stop.wait(3)

    worker._loop = main_loop
    worker._waiting_loop = waiting_loop
    worker._worker_thread = None
    worker._waiting_thread = None
    try:
        worker.start_worker()
        assert main_started.wait(timeout=2)
        assert waiting_started.wait(timeout=2)
        active_main = worker._worker_thread
        active_waiting = worker._waiting_thread
        worker.stop_worker(join_timeout=3)
        assert active_main is not None and not active_main.is_alive()
        assert active_waiting is not None and not active_waiting.is_alive()
    finally:
        worker._stop.set()
        worker._loop = saved_main_loop
        worker._waiting_loop = saved_waiting_loop
        worker._worker_thread = saved_main_thread
        worker._waiting_thread = saved_waiting_thread
        worker._stop.clear()


if __name__ == "__main__":
    test_static_network_context_uses_family_specific_proxmox_keys()
    test_expired_wait_probes_ready_guest_before_timing_out()
    test_waiting_poll_does_not_refuse_ready_work_because_queue_is_nonempty()
    test_waiting_poller_runs_while_main_worker_is_busy()
    test_worker_lifecycle_starts_and_stops_both_threads()
    print("\nALL WAVE 43 UNIT TESTS PASSED")
