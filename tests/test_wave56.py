"""Inventory request isolation, completed TTL, staged image refresh and pinning."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GOBLINDOCK_DEV", "1")
os.environ["GOBLINDOCK_DB"] = os.path.join(tempfile.mkdtemp(prefix="gd-wave56-"), "test.sqlite3")
from app import api, worker
from app.db import init_db, session_scope
from app.models import Connection, Deployment, Image, Job, User
from app.proxmox import base_disk_filename
from fastapi import HTTPException
from starlette.requests import Request

init_db()


class InventoryTests(unittest.TestCase):
    def fixture(self):
        with session_scope() as s:
            suffix = os.urandom(5).hex()
            user = User(name="admin", email=suffix+"@example.test", role="admin", password_hash="unused")
            conn = Connection(name=suffix, host="pve.test", token_id="a@pve!b", node="old")
            img = Image(kind="base", name=suffix, source_url="https://example.test/releases/20260901/image.qcow2")
            s.add(user); s.add(conn); s.add(img); s.flush()
            s.add(Deployment(name=suffix, owner_id=user.id, connection_id=conn.id, vmid=8800, node="old", status="running"))
            return user.id, conn.id, img.id

    def test_cold_state_never_constructs_remote_clients(self):
        uid, cid, _ = self.fixture()
        with patch.object(api, "Proxmox", side_effect=AssertionError("request contacted Proxmox")) as remote, session_scope() as s:
            result = api.state(Request({"type":"http", "headers":[], "session":{}}), s.get(User,uid), s)
            connection = next(c for c in result["CONNECTIONS"] if c["connId"] == cid)
            self.assertIn("inventory", connection)
            self.assertTrue(connection["inventory"]["stale"])
            self.assertEqual(remote.call_count, 0)

    def test_bounded_coalesced_probes_publish_completion_time_and_preserve_last_good(self):
        self.assertIsNotNone(importlib.util.find_spec("app.inventory"), "background inventory is missing")
        from app.inventory import InventoryCache
        entered = threading.Event(); release = threading.Event()
        calls = []
        def probe(conn):
            calls.append(conn.id); entered.set(); release.wait(2)
            return {"status":"online", "version":"9", "vms":{8800:{"vmid":8800,"node":"new","status":"running"}}}
        cache = InventoryCache(probe=probe, ttl=0.15, max_workers=1)
        conn = Connection(id=1,name="one",host="pve",token_id="a")
        try:
            self.assertTrue(cache.refresh(conn)); self.assertTrue(entered.wait(1))
            time.sleep(0.18)  # probe takes longer than TTL; completion starts the TTL
            self.assertFalse(cache.refresh(conn))
            self.assertFalse(cache.refresh(Connection(id=2,name="two",host="pve",token_id="a")))
            release.set()
            for _ in range(100):
                if cache.snapshot(1)["completedAt"]: break
                time.sleep(.01)
            self.assertFalse(cache.snapshot(1)["stale"])
            self.assertFalse(cache.refresh(conn))
            self.assertEqual(calls,[1])
            cache.probe = lambda _: (_ for _ in ()).throw(RuntimeError("offline"))
            self.assertTrue(cache.refresh(conn,force=True))
            for _ in range(100):
                if cache.snapshot(1)["error"]: break
                time.sleep(.01)
            self.assertTrue(cache.snapshot(1)["stale"])
            self.assertEqual(cache.snapshot(1)["vms"][8800]["node"],"new")
            self.assertFalse(cache.refresh(Connection(id=3,name="disabled",host="pve",token_id="a",disabled=True),force=True))
        finally:
            release.set(); cache.stop()

    def test_force_sync_is_durable_and_pin_rejects_moving_or_unverified_sources(self):
        uid,cid,iid = self.fixture()
        with session_scope() as s:
            result = api.sync_image(iid,api.SyncBody(connectionId=cid,force_refresh=True),s.get(User,uid),s)
            cfg = json.loads(s.get(Job,result["jobId"]).context_json)
            self.assertTrue(cfg.get("force_refresh"), "explicit refresh was discarded")
            for body in ({"pin":True,"immutable":True}, {"pin":True,"immutable":True,"source_url":"https://example.test/current/image.qcow2","checksum":"a"*64}):
                with self.assertRaises(HTTPException):
                    api.edit_image(iid,api.BaseImageEditBody(**body),s.get(User,uid),s)
            api.edit_image(iid,api.BaseImageEditBody(pin=True,immutable=True,checksum="b"*64),s.get(User,uid),s)
            from app.serialize import base_image_dict
            self.assertTrue(base_image_dict(s.get(Image,iid))["pinned"])

    def test_connection_edit_invalidates_completed_and_inflight_snapshots(self):
        from app import inventory
        entered=threading.Event(); release=threading.Event()
        def probe(conn):
            entered.set(); release.wait(2)
            return {"status":"online","version":"old","vms":{}}
        cache=inventory.InventoryCache(probe=probe,ttl=60)
        uid,cid,_=self.fixture()
        try:
            with session_scope() as s:
                conn=Connection(**s.get(Connection,cid).model_dump())
            cache.refresh(conn); self.assertTrue(entered.wait(1))
            with patch.object(inventory,"_cache",cache), session_scope() as s:
                api.edit_connection(cid,api.ConnEditBody(disabled=True),s.get(User,uid),s)
            release.set()
            for _ in range(100):
                if cid not in cache._inflight: break
                time.sleep(.01)
            self.assertIsNone(cache.snapshot(cid)["completedAt"],"old in-flight probe overwrote the invalidated connection")
            cache.probe=lambda c:{"status":"online","version":"new","vms":{}}
            self.assertTrue(cache.refresh(conn),"invalidated connection incorrectly retained the old TTL")
        finally:
            release.set(); cache.stop()

    def test_refresh_keeps_old_volume_and_publishes_validated_new_identity(self):
        uid,cid,iid = self.fixture()
        from types import SimpleNamespace
        with session_scope() as s:
            conn = Connection(**s.get(Connection,cid).model_dump())
            url = s.get(Image,iid).source_url
        old = base_disk_filename(url)
        volumes = {old}; downloads = []
        class PX:
            iso_storage="local"
            def storage_has_volume(self,name,node=None): return name in volumes
            def download_url(self,name,url,**kwargs): downloads.append(name); volumes.add(name); return "UPID:test"
            def wait_task(self,*args,**kwargs): pass
            def delete_storage_volume(self,name,**kwargs): volumes.discard(name)
        px=PX(); px.conn=conn
        ctx=SimpleNamespace(log=lambda *a:None,cancelled=lambda:False,phase_note=lambda *a:None)
        fresh = worker._ensure_base_disk(ctx,px,"old",{"src_url":url,"force_refresh":True})
        self.assertNotEqual(fresh,old,"refresh reused existing bytes")
        self.assertIn(old,volumes)
        self.assertEqual(downloads,[fresh])
        from app.image_cache import cache_metadata
        meta=cache_metadata(conn,"old",url)
        self.assertEqual(meta["filename"],fresh)
        self.assertTrue(meta["downloadedAt"])
        self.assertIsNone(meta["validatedAt"])
        self.assertEqual(worker._ensure_base_disk(ctx,px,"old",{"src_url":url}),fresh)
        def failure(*a,**kw): raise RuntimeError("checksum mismatch")
        px.wait_task=failure
        with self.assertRaises(RuntimeError): worker._ensure_base_disk(ctx,px,"old",{"src_url":url,"force_refresh":True})
        self.assertIn(fresh,volumes)
        self.assertEqual(cache_metadata(conn,"old",url)["filename"],fresh)

    def test_unverified_existing_volume_is_preserved_until_replacement_is_validated(self):
        uid,cid,iid = self.fixture()
        from types import SimpleNamespace
        with session_scope() as s:
            conn=Connection(**s.get(Connection,cid).model_dump())
            url=s.get(Image,iid).source_url
        original=base_disk_filename(url,"c"*64,"sha256")
        volumes={original}
        class PX:
            iso_storage="local"
            def storage_has_volume(self,name,node=None): return name in volumes
            def download_url(self,name,url,**kwargs): volumes.add(name); return "UPID:test"
            def wait_task(self,*args,**kwargs): pass
            def delete_storage_volume(self,name,**kwargs): volumes.discard(name)
        px=PX(); px.conn=conn
        ctx=SimpleNamespace(log=lambda *a:None,cancelled=lambda:False,phase_note=lambda *a:None)
        chosen=worker._ensure_base_disk(ctx,px,"old",{"src_url":url,"checksum":"c"*64,"checksum_algorithm":"sha256"})
        self.assertIn(original,volumes,"a pre-existing volume may be used outside this cache")
        self.assertNotEqual(chosen,original)
        from app.image_cache import cache_metadata
        self.assertTrue(cache_metadata(conn,"old",url,"c"*64,"sha256")["validatedAt"])


if __name__ == "__main__": unittest.main()
