"""Preflight is read-only; recovery preserves ownership, identity and snapshot trust."""
import json
import os
import sys
import tempfile
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GOBLINDOCK_DEV", "1")
os.environ["GOBLINDOCK_DB"] = os.path.join(tempfile.mkdtemp(prefix="gd-wave59-"), "test.sqlite3")
os.environ.setdefault("GOBLINDOCK_DATA_DIR", "/tmp/gd-wave59-data")
from fastapi import HTTPException
from sqlmodel import select
from app import api, operations
from app.db import init_db, session_scope
from app.execution_plan import build_execution_plan, seal_execution_plan
from app.models import Audit, Block, Connection, Deployment, Image, IpAllocation, Job, Network, Secret, Template, User
from app.security import encrypt
from app.template_ops import RebuildBody
init_db()

def expect(code, call):
    try: call()
    except HTTPException as exc:
        assert exc.status_code == code, exc.detail
        return exc
    raise AssertionError(f"expected HTTP {code}")

def fixture(s, *, phase="cloudinit"):
    tag=os.urandom(5).hex()
    u=User(email=tag+"@example.com",name="owner",password_hash="x",role="admin")
    c=Connection(name=tag,host="unused",token_id="unused",node="pve",storage="local-lvm",iso_storage="local",ssh_key_path=__file__)
    i=Image(name=tag,source_url="https://example.com/image.qcow2")
    s.add(u);s.add(c);s.add(i);s.flush()
    n=Network(name=tag,connection_id=c.id,mode="static",subnet_cidr="10.59.0.0/24",range_start="10.59.0.2",range_end="10.59.0.4",gateway="10.59.0.1")
    b=Block(key="c-"+tag,name=tag,kind="custom",builtin=False,owner_id=u.id,phase=phase,
            cloudinit_template="echo {password}",ansible_template="- name: configured\n  ansible.builtin.debug:\n    msg: '{password}'",
            input_schema_json='[{"name":"password","type":"secret"}]')
    sec=Secret(scope="user",owner_id=u.id,name="TOKEN",value_enc=encrypt("PRIVATE-CREDENTIAL"))
    s.add(n);s.add(b);s.add(sec);s.flush()
    t=Template(name=tag,owner_id=u.id,connection_id=c.id,network_id=n.id,base_image_id=i.id,
        recipe_json=json.dumps([{"blocks":[{"ref":b.key,"inputs":{"password":"{{ secrets.TOKEN }}"}}]}]))
    s.add(t);s.flush()
    d=Deployment(name=tag,owner_id=u.id,connection_id=c.id,network_id=n.id,image_id=i.id,template_id=t.id,vmid=8059,node="pve",status="error")
    s.add(d);s.flush()
    plan=seal_execution_plan(build_execution_plan(s,t,u.id,"{}"))
    j=Job(type="deploy",deployment_id=d.id,connection_id=c.id,status="failed",execution_plan_enc=plan,
        context_json='{"ansible_state":"executing"}',create_state="accepted")
    s.add(j);s.commit()
    return u,c,n,t,d,j,b,sec

class FakeProxmox:
    def __init__(self, conn): pass
    def list_cluster_guests(self): return [{"vmid":8059,"node":"pve","type":"qemu"}]
    def find_vm_node(self, vmid, node=None): return "migrated"
    def pick_node(self): return "pve"
    def vm_current(self, vmid, node=None): return {"status":"running","name":"guest"}
    def node_status(self,node=None): return {"memory":{"free":16*1024**3}}
    def storage_status(self,node=None):
        return [{"storage":"local-lvm","content":"images","avail":100*1024**3,"active":1,"enabled":1},
                {"storage":"local","content":"import,snippets,iso","avail":100*1024**3,"active":1,"enabled":1}]
    def storage_volumes(self,node=None,content="import"): return set()

def test_current_rebuild_preflight_does_not_allocate_or_touch_secret_usage():
    with session_scope() as s:
        u,c,n,t,d,j,b,secret=fixture(s)
        with patch.object(operations,"Proxmox",FakeProxmox), patch.object(api,"allocate_ip",side_effect=AssertionError("preflight allocated IP")):
            report=operations.rebuild_preflight(d.id,RebuildBody(mode="current"),u,s)
        assert report["ok"],report
        assert not s.exec(select(IpAllocation).where(IpAllocation.deployment_id==d.id)).all()
        s.refresh(secret); assert secret.last_used is None
        assert "PRIVATE-CREDENTIAL" not in json.dumps(report)
        assert d.status=="error" and len(s.exec(select(Job).where(Job.deployment_id==d.id)).all())==1

def test_preflight_missing_secret_and_storage_capacity_fail_actionably():
    with session_scope() as s:
        u,c,n,t,d,j,b,secret=fixture(s)
        s.delete(secret);s.commit()
        with patch.object(operations,"Proxmox",FakeProxmox):
            result=operations.rebuild_preflight(d.id,RebuildBody(mode="current"),u,s)
        assert not result["ok"]
        assert any(x["name"]=="Recipe" and x["status"]=="fail" for x in result["checks"])
        class Full(FakeProxmox):
            def storage_status(self,node=None):
                return [{**row,"avail":1} for row in super().storage_status(node)]
        with patch.object(operations,"Proxmox",Full):
            result=operations.rebuild_preflight(d.id,RebuildBody(mode="current"),u,s)
        assert any(x["name"]=="Disk capacity" and x["status"]=="fail" for x in result["checks"])

def test_reconciliation_default_reads_without_mutating_status_or_audit():
    with session_scope() as s:
        u,c,n,t,d,j,b,secret=fixture(s)
        d.status="cleanup_pending";d.cleanup_origin="deploy";s.add(d);s.commit()
        before=len(s.exec(select(Audit)).all())
        with patch.object(operations,"Proxmox",FakeProxmox):
            result=operations.reconcile(d.id,operations.ReconcileBody(),u,s)
        assert result["presence"]=="present"
        s.refresh(d)
        assert d.node=="pve" and d.status=="cleanup_pending" and d.cleanup_origin=="deploy"
        assert len(s.exec(select(Audit)).all())==before

def test_retry_requires_trusted_snapshot_and_confirmed_identity():
    with session_scope() as s:
        u,c,n,t,d,j,b,secret=fixture(s,phase="ansible")
        body=operations.RetryBody(acknowledgeReplay=True)
        j.create_state="submitting";s.add(j);s.commit()
        expect(409,lambda:operations.retry_configuration(d.id,body,u,s))
        j.create_state="accepted";j.execution_plan_enc="corrupt";s.add(j);s.commit()
        expect(409,lambda:operations.retry_configuration(d.id,body,u,s))
        j.execution_plan_enc=seal_execution_plan(build_execution_plan(s,t,u.id,"{}"));s.add(j)
        u.role="user";s.add(u);s.commit()
        expect(403,lambda:operations.retry_configuration(d.id,body,u,s))

def test_recovery_scoped_and_retry_reuses_prior_plan_without_current_template():
    with session_scope() as s:
        u,c,n,t,d,j,b,secret=fixture(s,phase="ansible")
        other=User(email=os.urandom(5).hex()+"@x",name="other",password_hash="x")
        s.add(other);s.commit()
        assert operations.recovery_list(other,s)["items"]==[]
        expect(403,lambda:operations.retry_configuration(d.id,operations.RetryBody(acknowledgeReplay=True),other,s))
        expected=j.execution_plan_enc
        t.recipe_json="[]";s.add(t);s.commit()
        result=operations.retry_configuration(d.id,operations.RetryBody(acknowledgeReplay=True),u,s)
        retry=s.get(Job,result["jobId"])
        assert retry.execution_plan_enc==expected and retry.type=="configure"
        expect(409,lambda:operations.retry_configuration(d.id,operations.RetryBody(acknowledgeReplay=True),u,s))

def test_ambiguous_identity_survives_history_retention_until_explicit_confirmation():
    with session_scope() as s:
        u,c,n,t,d,j,b,secret=fixture(s)
        d.identity_state="submitting";s.add(d);s.delete(j);s.commit()
        row=next(row for row in operations.recovery_list(u,s)["items"] if row["depId"]==d.id)
        assert row["uncertainIdentity"] and row["jobId"] is None
        with patch.object(operations,"Proxmox",FakeProxmox):
            result=operations.reconcile(d.id,operations.ReconcileBody(),u,s)
            assert result["requiresIdentityConfirmation"]
            s.refresh(d);assert d.identity_state=="submitting"
            operations.reconcile(d.id,operations.ReconcileBody(confirmIdentity=True),u,s)
        s.refresh(d);assert d.identity_state=="accepted" and d.node=="migrated"

if __name__=="__main__":
    tests=[v for k,v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests: test();print("PASS",test.__name__)
    print(f"{len(tests)} wave59 tests passed")
