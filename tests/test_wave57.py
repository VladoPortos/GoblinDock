"""Versioned rebuild admission and portable template bundles (isolated SQLModel DB)."""
import copy
import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GOBLINDOCK_DEV", "1")
os.environ["GOBLINDOCK_DB"] = os.path.join(tempfile.mkdtemp(prefix="gd-wave57-"), "test.sqlite3")
os.environ.setdefault("GOBLINDOCK_DATA_DIR", "/tmp/gd-wave57-data")

from fastapi import HTTPException
from sqlmodel import select
from app import api, template_ops
from app import db
from app.db import init_db, session_scope
from app.execution_plan import open_execution_plan
from app.models import Block, Connection, Deployment, Image, Job, Network, Template, User
from app.recipes import ensure_placement_ids, merge_deploy_inputs

init_db()

def expect(code, call):
    try:
        call()
    except HTTPException as exc:
        assert exc.status_code == code, exc.detail
        return exc
    raise AssertionError(f"expected {code}")

def fixture(s):
    tag = os.urandom(5).hex()
    u = User(email=tag + "@example.com", name="owner", password_hash="unused", role="admin")
    c = Connection(name=tag, host="unused", token_id="unused", node="n1")
    i = Image(name=tag, source_url="https://example.com/original.qcow2", checksum="a" * 64)
    s.add(u); s.add(c); s.add(i); s.flush()
    n = Network(name=tag, connection_id=c.id, mode="dhcp", bridge="vmbr0")
    b = Block(key="custom-" + tag, name="Custom", kind="custom", builtin=False,
              owner_id=u.id, phase="cloudinit", cloudinit_template="echo {message}",
              input_schema_json=json.dumps([{"name":"message", "type":"text"},
                                             {"name":"password", "type":"secret"}]))
    s.add(n); s.add(b); s.flush()
    recipe = ensure_placement_ids([{"name":"Install", "blocks":[{"ref":b.key,
                    "inputs":{"message":"default", "password":"{{ secrets.DB }}"}, "ask":["message"]}]}])
    t = Template(name=tag, recipe_json=json.dumps(recipe), owner_id=u.id, public=False,
                 base_image_id=i.id, connection_id=c.id, network_id=n.id)
    s.add(t); s.commit()
    return u,c,i,n,b,t

def deploy(s,u,t):
    with patch.object(api, "_enforce_quota", return_value=None):
        result=api._deploy_transaction(api.DeployBody(templateId=t.id, deployInputs={"0.0":{"message":"accepted-secret-answer"}}),u,s)
    dep=s.get(Deployment,result["depId"])
    j=s.get(Job,result["jobId"]); j.status="succeeded"; s.add(j); s.commit()
    return dep,j

def test_original_survives_template_block_image_mutation_and_history_retention():
    with session_scope() as s:
        u,c,i,n,b,t=fixture(s); dep,j=deploy(s,u,t)
        saved=j.execution_plan_enc; ctx=j.context_json
        assert dep.original_execution_plan_enc == saved
        b.cloudinit_template="echo changed"; t.recipe_json="[]"; i.source_url="https://example.com/latest"
        s.add(b); s.add(t); s.add(i); s.delete(j); s.commit()
        result=api._vm_rebuild_transaction(dep.id,u,s)
        rebuilt=s.get(Job,result["jobId"])
        assert rebuilt.execution_plan_enc == saved
        assert json.loads(rebuilt.context_json)==json.loads(ctx)
        assert "accepted-secret-answer" not in dep.original_execution_plan_enc

def test_legacy_original_fails_and_current_requires_fresh_answers():
    with session_scope() as s:
        u,c,i,n,b,t=fixture(s)
        recipe=json.loads(t.recipe_json); recipe[0]["blocks"][0]["inputs"]["message"]=""
        t.recipe_json=json.dumps(recipe); s.add(t)
        dep=Deployment(name="legacy",owner_id=u.id,connection_id=c.id,template_id=t.id,image_id=i.id,network_id=n.id,status="stopped")
        s.add(dep); s.commit()
        expect(409,lambda:api._vm_rebuild_transaction(dep.id,u,s))
        expect(400,lambda:api._vm_rebuild_transaction(dep.id,u,s,template_ops.RebuildBody(mode="current")))
        result=api._vm_rebuild_transaction(dep.id,u,s,template_ops.RebuildBody(mode="current",deployInputs={recipe[0]["blocks"][0]["placementId"]:{"message":"new"}}))
        assert open_execution_plan(s.get(Job,result["jobId"]).execution_plan_enc)["recipe"][0]["blocks"][0]["inputs"]["message"]=="new"
        assert not dep.original_execution_plan_enc  # Never claim a current revision was original.

def test_stable_answers_follow_placement_on_reorder():
    recipe=ensure_placement_ids([{"blocks":[{"ref":"a","ask":["x"]},{"ref":"a","ask":["x"]}]}])
    pid=recipe[0]["blocks"][0]["placementId"]
    recipe[0]["blocks"].reverse()
    merged=merge_deploy_inputs(recipe,{pid:{"x":"first"}})
    assert merged[0]["blocks"][1]["inputs"]["x"]=="first"
    assert "inputs" not in merged[0]["blocks"][0]

def test_bundle_roundtrip_maps_fresh_blocks_and_never_exports_answers_or_credentials():
    with session_scope() as s:
        u,c,i,n,b,t=fixture(s); dep,j=deploy(s,u,t)
        bundle=template_ops.export_template(t.id,u,s)
        encoded=json.dumps(bundle)
        assert "accepted-secret-answer" not in encoded
        assert "token_id" not in encoded and "owner_id" not in encoded
        assert bundle["secretReferences"]==["DB"]
        result=template_ops.import_template(template_ops.ImportBody(bundle=bundle,connectionId=c.id,networkId=n.id,baseImageId=i.id),u,s)
        imported=s.get(Template,result["templateId"])
        ref=json.loads(imported.recipe_json)[0]["blocks"][0]["ref"]
        assert ref!=b.key
        assert s.exec(select(Block).where(Block.key==ref)).one().owner_id==u.id
        assert imported.connection_id==c.id and imported.network_id==n.id

def test_import_boundaries_no_url_creation_no_untrusted_ansible_no_partial_rows():
    with session_scope() as s:
        u,c,i,n,b,t=fixture(s)
        bundle=template_ops.export_template(t.id,u,s)
        u.role="user"; s.add(u); s.commit()
        bundle["customBlocks"][0]["phase"]="ansible"
        bundle["customBlocks"][0]["ansible_template"]="- local_action: shell id"
        before=len(s.exec(select(Block)).all())
        expect(403,lambda:template_ops.import_template(template_ops.ImportBody(bundle=bundle,connectionId=c.id,networkId=n.id,baseImageId=i.id),u,s))
        assert len(s.exec(select(Block)).all())==before
        bundle["customBlocks"][0]["phase"]="cloudinit"
        expect(400,lambda:template_ops.import_template(template_ops.ImportBody(bundle=bundle,connectionId=c.id,networkId=n.id,baseImageId=999999),u,s))
        other=User(email=os.urandom(5).hex()+"@x",name="other",password_hash="x")
        s.add(other); s.commit()
        expect(404,lambda:template_ops.export_template(t.id,other,s))

def test_preview_hides_answers_and_reports_revision_changes_without_admitting_job():
    with session_scope() as s:
        u,c,i,n,b,t=fixture(s); dep,j=deploy(s,u,t)
        assert template_ops.rebuild_plan(dep.id,u,s)["originalAvailable"] is True
        b.cloudinit_template="echo new {message}"; i.source_url="https://example.com/new.qcow2"
        s.add(b); s.add(i); s.commit()
        original_ciphertext=dep.original_execution_plan_enc
        preview=template_ops.rebuild_preview(dep.id,template_ops.RebuildBody(mode="current",deployInputs={"0.0":{"message":"new-secret-answer"}}),u,s)
        text=json.dumps(preview)
        assert "new-secret-answer" not in text and "accepted-secret-answer" not in text
        assert "Block definitions" in text and "Base image" in text
        assert s.get(Deployment,dep.id).original_execution_plan_enc==original_ciphertext
        assert len(s.exec(select(Job).where(Job.deployment_id==dep.id)).all())==1

def test_bundle_validation_rolls_back_custom_blocks_and_rejects_malformed_recipe():
    with session_scope() as s:
        u,c,i,n,b,t=fixture(s)
        original=template_ops.export_template(t.id,u,s)
        count=len(s.exec(select(Block)).all())
        bad=copy.deepcopy(original)
        bad["template"]["recipe"][0]["blocks"][0]["inputs"]["password"]="literal-credential"
        expect(400,lambda:template_ops.import_template(template_ops.ImportBody(bundle=bad,connectionId=c.id,networkId=n.id,baseImageId=i.id),u,s))
        assert len(s.exec(select(Block)).all())==count
        for recipe in ([None],[{"blocks":4}],[{"blocks":[{"ref":b.key,"deployAnswers":{"password":"x"}}]}]):
            bad=copy.deepcopy(original); bad["template"]["recipe"]=recipe
            expect(400,lambda:template_ops.import_template(template_ops.ImportBody(bundle=bad,connectionId=c.id,networkId=n.id,baseImageId=i.id),u,s))
        assert len(s.exec(select(Block)).all())==count

def test_export_redacts_legacy_sensitive_defaults_values_and_url_credentials():
    with session_scope() as s:
        u,c,i,n,b,t=fixture(s)
        schema=json.loads(b.input_schema_json); schema[1]["default"]="secret-default"
        b.input_schema_json=json.dumps(schema)
        recipe=json.loads(t.recipe_json); recipe[0]["blocks"][0]["inputs"]["password"]="secret-stored"
        recipe[0]["blocks"][0]["inputs"]["unknown-password"]="unknown-secret"
        t.recipe_json=json.dumps(recipe)
        i.source_url="https://username:password@example.com/image?token=credential#secret"
        s.add(b); s.add(t); s.add(i); s.commit()
        bundle=template_ops.export_template(t.id,u,s); text=json.dumps(bundle)
        for secret in ("secret-default","secret-stored","unknown-secret","username","credential"):
            assert secret not in text
        assert bundle["baseImage"]["source_url"]=="https://example.com/image"
        assert "password" in bundle["template"]["recipe"][0]["blocks"][0]["ask"]

def test_admitted_context_captures_base_image_identity_for_original_and_current():
    with session_scope() as s:
        u,c,i,n,b,t=fixture(s); dep,j=deploy(s,u,t)
        assert json.loads(j.context_json)["base_image_id"]==i.id
        _,ctx,_=template_ops.prepare_rebuild(s,dep,u,"current",{},read_only=True)
        assert json.loads(ctx)["base_image_id"]==i.id

def test_upgrade_backfills_only_earliest_successful_deploy_snapshot_and_is_idempotent():
    from sqlmodel import SQLModel, Session, create_engine
    from app.security import decrypt
    upgrade_engine=create_engine("sqlite:///"+os.path.join(tempfile.mkdtemp(prefix="gd-wave57-upgrade-"),"old.db"))
    SQLModel.metadata.create_all(upgrade_engine)
    with Session(upgrade_engine) as s:
        u,c,i,n,b,t=fixture(s)
        dep=Deployment(name="upgraded",owner_id=u.id,connection_id=c.id,template_id=t.id,image_id=i.id,network_id=n.id,status="running")
        s.add(dep);s.flush()
        accepted=api._build_admitted_execution_plan(s,t,u.id,'{"0.0":{"message":"original-answer"}}')
        admission={"src_url":"https://example.com/original.qcow2","cpu":1,"ram":2,"disk":20,"network_mode":"dhcp"}
        s.add(Job(type="rebuild",status="succeeded",deployment_id=dep.id,execution_plan_enc=accepted,
                  context_json=json.dumps({**admission,"src_url":"https://example.com/wrong-rebuild"})))
        s.add(Job(type="deploy",status="failed",deployment_id=dep.id,execution_plan_enc=accepted,context_json=json.dumps(admission)))
        s.add(Job(type="deploy",status="succeeded",deployment_id=dep.id,execution_plan_enc=accepted,
                  context_json=json.dumps({**admission,"ansible_state":"done","static_wait_started": "old", "rebuild_destroyed": True})))
        s.flush()
        s.add(Job(type="deploy",status="succeeded",deployment_id=dep.id,execution_plan_enc=accepted,
                  context_json=json.dumps({**admission,"src_url":"https://example.com/later-deploy"})))
        t.recipe_json="[]";b.cloudinit_template="echo changed";s.add(t);s.add(b)
        s.commit();dep_id=dep.id
    with upgrade_engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE deployments DROP COLUMN original_execution_plan_enc")
        conn.exec_driver_sql("ALTER TABLE deployments DROP COLUMN original_context_enc")
        conn.exec_driver_sql("PRAGMA user_version=0")
    with patch.object(db,"engine",upgrade_engine):
        db.init_db()
        with Session(upgrade_engine) as s:
            dep=s.get(Deployment,dep_id)
            assert dep.original_execution_plan_enc==accepted
            assert json.loads(decrypt(dep.original_context_enc,strict=True))==admission
            assert "original-answer" not in dep.original_execution_plan_enc
            encrypted_context=dep.original_context_enc
            for job in s.exec(select(Job).where(Job.deployment_id==dep_id)).all(): s.delete(job)
            s.commit()
        db.init_db()
        with Session(upgrade_engine) as s:
            dep=s.get(Deployment,dep_id)
            assert dep.original_execution_plan_enc==accepted and dep.original_context_enc==encrypted_context
            with patch.object(api,"_assert_trusted_ansible_blocks",return_value=None):
                plan,ctx,_=template_ops.prepare_rebuild(s,dep,s.get(User,dep.owner_id))
            assert plan==accepted and json.loads(ctx)==admission

def test_backfill_refuses_invalid_owner_missing_context_and_rebuild_only_history():
    from app.execution_plan import seal_execution_plan
    with session_scope() as s:
        u,c,i,n,b,t=fixture(s)
        plan=api._build_admitted_execution_plan(s,t,u.id,"{}")
        context=json.dumps({"src_url":"https://example.com/original.qcow2","cpu":1,"ram":2,"disk":20,"network_mode":"dhcp"})
        mismatched=open_execution_plan(plan)
        mismatched["owner_id"]=u.id+99999;mismatched["deployment_owner_id"]=u.id+99999
        mismatched=seal_execution_plan(mismatched)
        dep_ids=[]
        for index,(kind,cipher,ctx) in enumerate((("deploy","corrupt",context),("deploy",mismatched,context),("deploy",plan,"{}"),("rebuild",plan,context))):
            dep=Deployment(name="unavailable-"+str(index),owner_id=u.id,connection_id=c.id,template_id=t.id)
            s.add(dep);s.flush();dep_ids.append(dep.id)
            s.add(Job(type=kind,status="succeeded",deployment_id=dep.id,execution_plan_enc=cipher,context_json=ctx))
            # A later successful attempt must never replace an unprovable original.
            if kind=="deploy":
                s.add(Job(type="deploy",status="succeeded",deployment_id=dep.id,execution_plan_enc=plan,context_json=context))
        s.commit()
    db.init_db()
    with session_scope() as s:
        for dep_id in dep_ids:
            dep=s.get(Deployment,dep_id)
            assert not dep.original_execution_plan_enc and not dep.original_context_enc

if __name__ == "__main__":
    tests=[v for k,v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test(); print("PASS",test.__name__)
    print(f"{len(tests)} wave57 tests passed")
