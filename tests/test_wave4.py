"""Wave 4 — network redaction, image-state mapping, network validation, quotas, log cap.

Run: GOBLINDOCK_SECRET_KEY=<64hex> .venv/bin/python tests/test_wave4.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DB = "/tmp/gd-wave4-test.sqlite3"
for ext in ("", "-wal", "-shm"):
    try:
        os.remove(_DB + ext)
    except OSError:
        pass
os.environ["GOBLINDOCK_DB"] = _DB
os.environ.setdefault("GOBLINDOCK_DATA_DIR", "/tmp/gd-data-test")

from app.db import init_db, session_scope          # noqa: E402
from app import serialize as S, api                 # noqa: E402
from app.config import settings                     # noqa: E402
from app.models import Network, Image, Deployment, User, Job, JobEvent  # noqa: E402
from fastapi import HTTPException                    # noqa: E402

init_db()


def test_network_redaction():
    with session_scope() as s:
        n = Network(connection_id=1, name="lab", mode="static", bridge="vmbr9", vlan=50,
                    subnet_cidr="10.0.50.0/24", gateway="10.0.50.1",
                    range_start="10.0.50.10", range_end="10.0.50.20", dns="1.1.1.1")
        s.add(n)
    with session_scope() as s:
        from sqlmodel import select
        n = s.exec(select(Network)).first()
        pub = S.network_dict(s, n, {1: "conn"}, public=True)
        adm = S.network_dict(s, n, {1: "conn"}, public=False)
    secret_fields = {"bridge", "vlan", "subnet", "gateway", "rangeStart", "rangeEnd", "dns", "conn"}
    assert not (secret_fields & set(pub)), f"public leaks: {secret_fields & set(pub)}"
    assert {"name", "netId", "connId", "mode"} <= set(pub)        # enough to pick one
    assert secret_fields <= set(adm), "admin must still see full topology"
    print("test_network_redaction OK")


def test_image_state_mapping():
    # Base images are always created build_status="ready" (add_base_image + the seed
    # catalog), so base_image_dict no longer serializes a state — only goldens have one.
    with session_scope() as s:
        for st in ("building", "importing", "ready", "failed", "none"):
            s.add(Image(kind="golden", name=f"g-{st}", build_status=st))
    from sqlmodel import select
    with session_scope() as s:
        gmap = {i.name: S.golden_image_dict(s, i)["state"]
                for i in s.exec(select(Image).where(Image.kind == "golden")).all()}
        assert "state" not in S.base_image_dict(Image(kind="base", name="b", build_status="ready"))
    assert gmap == {"g-building": "building", "g-importing": "building", "g-ready": "ready",
                    "g-failed": "failed", "g-none": "none"}, gmap
    print("test_image_state_mapping OK")


def _nb(**kw):
    base = dict(connectionId=1, name="n", mode="static", bridge="vmbr0", vlan=None,
                subnet_cidr="10.0.50.0/24", gateway="10.0.50.1",
                range_start="10.0.50.10", range_end="10.0.50.20", dns="")
    base.update(kw)
    return api.NetworkBody(**base)


def test_validate_network_body():
    api._validate_network_body(_nb())                       # valid → no raise
    api._validate_network_body(_nb(mode="dhcp", subnet_cidr=""))  # dhcp skips checks
    bad = [
        _nb(subnet_cidr="not-a-cidr"),
        _nb(gateway="10.0.99.1"),                # outside subnet
        _nb(range_start="10.0.50.20", range_end="10.0.50.10"),   # reversed
        _nb(range_end="10.0.99.20"),             # range outside subnet
        _nb(vlan=5000),                          # vlan out of range
        _nb(dns="1.1.1.1, notanip"),
    ]
    for b in bad:
        try:
            api._validate_network_body(b)
            assert False, f"expected reject for {b}"
        except HTTPException as e:
            assert e.status_code == 400
    print("test_validate_network_body OK")


def test_enforce_quota():
    with session_scope() as s:
        s.add(User(id=10, email="q@x", name="Q", password_hash="x", role="user"))
        s.add(User(id=11, email="a@x", name="A", password_hash="x", role="admin"))
        s.add(Deployment(name="d1", owner_id=10))
        s.add(Deployment(name="d2", owner_id=10))
    settings.max_vms_per_user = 2
    with session_scope() as s:
        u = s.get(User, 10)
        admin = s.get(User, 11)
        try:
            api._enforce_quota(s, u, "vm")
            assert False, "quota should be exceeded"
        except HTTPException as e:
            assert e.status_code == 429
        api._enforce_quota(s, admin, "vm")   # admins are exempt → no raise
    settings.max_vms_per_user = 0
    with session_scope() as s:
        api._enforce_quota(s, s.get(User, 10), "vm")  # 0 = unlimited → no raise
    print("test_enforce_quota OK")


def test_job_detail_log_cap():
    with session_scope() as s:
        s.add(Job(id=99, type="deploy", status="running"))
    with session_scope() as s:
        for i in range(10):
            s.add(JobEvent(job_id=99, kind="log", line=f"line {i}"))
    with session_scope() as s:
        job = s.get(Job, 99)
        full = S.job_detail(s, job, include_log=True)
        none = S.job_detail(s, job, include_log=False)
        capped = S.job_detail(s, job, include_log=True, log_limit=3)
    assert len(full["log"]) == 10
    assert none["log"] == [], "stream frames after the first must skip the log"
    assert len(capped["log"]) == 3 and capped["log"][-1]["text"] == "line 9", capped["log"]
    print("test_job_detail_log_cap OK")


if __name__ == "__main__":
    test_network_redaction()
    test_image_state_mapping()
    test_validate_network_body()
    test_enforce_quota()
    test_job_detail_log_cap()
    print("\nALL WAVE 4 UNIT TESTS PASSED")
