"""Lifecycle recovery regression tests; all external effects are mocked."""
import os
import sys
import tempfile
import unittest
import subprocess
from unittest.mock import patch, Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['GOBLINDOCK_DEV'] = '1'
os.environ['GOBLINDOCK_DB'] = os.path.join(tempfile.mkdtemp(prefix='gd-wave58-'), 'test.db')
os.environ['GOBLINDOCK_DATA_DIR'] = tempfile.mkdtemp(prefix='gd-wave58-data-')
from app import worker
from app.db import init_db, session_scope
from app.models import Connection, Deployment, Job
init_db()

class RecoveryTests(unittest.TestCase):
    def fixture(self, disabled=False, status='waiting'):
        with session_scope() as s:
            c = Connection(name='test-' + os.urandom(4).hex(), host='example.invalid', token_id='test@pve!test', disabled=disabled)
            s.add(c); s.flush()
            d = Deployment(name='vm', connection_id=c.id, vmid=8810, status='error')
            s.add(d); s.flush()
            j = Job(type='deploy', connection_id=c.id, deployment_id=d.id, status=status,
                    execution_plan_enc='fixture')
            s.add(j); s.flush()
            return c.id, d.id, j.id

    def test_disabled_background_source_is_never_contacted(self):
        _, did, jid = self.fixture(disabled=True)
        with session_scope() as s:
            d=s.get(Deployment,did);d.status='running';s.add(d)
        with patch.object(worker, 'Proxmox') as px:
            px.return_value.agent_ipv4.return_value = None
            worker._reconcile_ips()
            worker._poll_waiting_job(jid, worker.utcnow())
            worker._resume_waiting_ansible(jid, '10.0.0.2')
        px.assert_not_called()

    def test_cleanup_stops_running_vm_before_delete(self):
        px = Mock()
        px.find_vm_node.return_value = 'migrated'
        px.vm_current.return_value = {'status': 'running'}
        with patch.object(worker, '_px_for_conn', return_value=px):
            worker._best_effort_destroy(999999, 8810, 'old')
        names = [c[0] for c in px.method_calls]
        self.assertLess(names.index('stop'), names.index('destroy'))
        self.assertLess(names.index('wait_task'), names.index('destroy'))
        px.destroy.assert_called_once_with(8810, node='migrated')

    def test_first_boot_marker_is_conditional_and_pipefail_enabled(self):
        import yaml
        cfg = yaml.safe_load(worker._deploy_cloud_config('vm', [], ['false | true']))
        script = cfg['write_files'][0]['content']
        self.assertIn('pipefail', script)
        commands = str(cfg['runcmd'])
        self.assertIn('goblindock-recipe-result', commands)
        self.assertNotIn('touch /run/goblindock-ready', commands)

    def test_uncertain_create_keeps_identity_and_allocation(self):
        _, did, jid = self.fixture(status='failed')
        with session_scope() as s:
            j = s.get(Job, jid); j.create_state = 'submitting'; s.add(j)
        with patch.object(worker, '_px_for_conn') as px, patch.object(worker, '_best_effort_destroy') as destroy:
            worker._reconcile_failed_job(jid)
            worker._reconcile_canceled_job(jid)
        px.assert_not_called(); destroy.assert_not_called()
        with session_scope() as s:
            self.assertEqual(s.get(Deployment, did).vmid, 8810)
            self.assertEqual(s.get(Deployment, did).status, 'error')

    def test_resume_is_durably_running_before_script_and_never_replayed(self):
        _, _, jid = self.fixture()
        def execute(*args, **kwargs):
            with session_scope() as s:
                self.assertEqual(s.get(Job, jid).status, 'running')
            raise RuntimeError('simulated process interruption')
        with patch.object(worker, 'open_execution_plan', return_value={'owner_id':None}), \
             patch.object(worker, 'materialize_execution_plan', return_value=([],{})), \
             patch.object(worker, 'has_ansible_blocks', return_value=True), \
             patch.object(worker, '_managed_keypair', return_value=('private','public')), \
             patch.object(worker, '_run_ansible_phase', side_effect=execute) as run:
            with self.assertRaisesRegex(RuntimeError, 'interruption'):
                worker._resume_waiting_ansible(jid, '10.0.0.2')
            with patch.object(worker, '_reconcile_failed_job'):
                worker._recover_orphans()
            worker._resume_waiting_ansible(jid, '10.0.0.2')
            self.assertEqual(run.call_count, 1)

    def test_failed_pipeline_writes_failure_marker_and_fails_boot_wrapper(self):
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load(worker._deploy_cloud_config('vm', [], ['false | true']))
        temp = Path(tempfile.mkdtemp(prefix='gd-firstboot-'))
        recipe, marker = temp/'recipe.sh', temp/'result'
        recipe.write_text(cfg['write_files'][0]['content'])
        outer = cfg['runcmd'][1][2].replace('/opt/goblindock-recipe.sh', str(recipe)).replace('/var/lib/goblindock-recipe-result', str(marker))
        result = subprocess.run(['/bin/bash','-c',outer],capture_output=True,text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text().strip(), str(result.returncode))
        self.assertFalse(recipe.exists())

    def test_schema_version_and_upgrade_are_idempotent(self):
        from app.db import engine, SCHEMA_VERSION
        with engine.begin() as conn:
            conn.exec_driver_sql('ALTER TABLE jobs DROP COLUMN remote_task')
            conn.exec_driver_sql('PRAGMA user_version=0')
        init_db(); init_db()
        with engine.connect() as conn:
            self.assertEqual(conn.exec_driver_sql('PRAGMA user_version').scalar(),SCHEMA_VERSION)
            self.assertIn('remote_task', {r[1] for r in conn.exec_driver_sql('PRAGMA table_info(jobs)')})

    def test_rebuild_cancel_after_create_stays_in_recovery(self):
        _, did, jid = self.fixture(status='canceled')
        with session_scope() as s:
            j=s.get(Job,jid);j.type='rebuild';j.create_state='accepted';s.add(j)
        with patch.object(worker,'_px_for_conn'), patch.object(worker,'_probe_vm_presence',return_value=(worker.VM_PRESENT,'present')):
            worker._reconcile_canceled_job(jid)
        with session_scope() as s:
            self.assertEqual(s.get(Deployment,did).status,'error')

    def test_running_remote_task_blocks_cleanup_even_when_vm_absent(self):
        _, did, jid = self.fixture(status='failed')
        with session_scope() as s:
            j=s.get(Job,jid);j.remote_task='UPID:pve:1:2:3:qmcreate:8810:test:';j.remote_node='pve';s.add(j)
        px=Mock();px.task_status.return_value={'status':'running'}
        with patch.object(worker,'_px_for_conn',return_value=px),patch.object(worker,'_probe_vm_presence') as probe:
            worker._reconcile_failed_job(jid)
        probe.assert_not_called()
        with session_scope() as s:
            self.assertEqual(s.get(Deployment,did).identity_state,'submitting')

    def test_failed_staged_preflight_never_overwrites_old_vm_snippet(self):
        cid, did, jid = self.fixture(status='failed')
        with session_scope() as s:
            c=Connection(**s.get(Connection,cid).model_dump())
            d=Deployment(**s.get(Deployment,did).model_dump())
        px=Mock();px.validate_snippet_volume.side_effect=RuntimeError('storage unavailable')
        with patch.object(worker,'_managed_keypair',return_value=('private','public')), \
             patch.object(worker,'auto_root_password_enabled',return_value=False), \
             patch.object(worker,'write_snippet_over_ssh',return_value='local:snippets/gd-preflight-test.yml') as write, \
             patch.object(worker,'delete_snippet_over_ssh') as delete:
            with self.assertRaisesRegex(RuntimeError,'storage unavailable'):
                worker._preflight_deploy_cloud_init(Mock(),px,c,d,'pve',8810,{},['true'],'',False,
                                                   snippet_name='gd-preflight-test.yml')
        self.assertEqual(write.call_args.args[1],'gd-preflight-test.yml')
        self.assertEqual(delete.call_args.args[1],'gd-preflight-test.yml')

    def test_destroy_timeout_cannot_be_swallowed_by_registry_absence(self):
        _, _, jid = self.fixture(status='running')
        with session_scope() as s:
            j=Job(**s.get(Job,jid).model_dump())
        px=Mock();px.destroy.return_value='UPID:pve:1:2:3:qmdestroy:8810:test:'
        px.pick_node.return_value='pve'
        px.wait_task.side_effect=TimeoutError('task still running')
        with patch.object(worker,'Proxmox',return_value=px), \
             patch.object(worker,'_stop_vm_for_lifecycle',return_value=True), \
             patch.object(worker,'_vm_exists',return_value=False):
            with self.assertRaisesRegex(TimeoutError,'still running'):
                worker._run_destroy(worker.JobCtx(jid),j)

    def test_explicit_cleanup_retry_admits_cleanup_pending(self):
        from app import operations
        from app.models import User
        from app.db import engine
        from sqlmodel import Session
        _, did, _ = self.fixture(status='failed')
        with session_scope() as s:
            d=s.get(Deployment,did);d.status='cleanup_pending';s.add(d)
        with Session(engine) as s:
            result=operations.retry_cleanup(did,User(id=999,name='admin',role='admin'),s)
            self.assertEqual(s.get(Job,result['jobId']).type,'destroy')

    def test_ownership_confirmation_cannot_clear_a_running_task(self):
        from app import operations
        from app.models import User
        from app.db import engine
        from sqlmodel import Session
        from fastapi import HTTPException
        _, did, jid = self.fixture(status='failed')
        with session_scope() as s:
            d=s.get(Deployment,did);d.identity_state='submitting';s.add(d)
            j=s.get(Job,jid);j.remote_task='UPID:pve:1:2:3:qmcreate:8810:test:';s.add(j)
        px=Mock();px.find_vm_node.return_value='pve';px.vm_current.return_value={'status':'running'}
        px.task_status.return_value={'status':'running'}
        with Session(engine) as s, patch.object(operations,'Proxmox',return_value=px):
            with self.assertRaises(HTTPException) as result:
                operations.reconcile(did,operations.ReconcileBody(confirmIdentity=True),User(id=999,role='admin'),s)
            self.assertEqual(result.exception.status_code,409)
            self.assertEqual(s.get(Deployment,did).identity_state,'submitting')

    def test_canceled_cleanup_timeout_retains_identity_and_task(self):
        _, did, jid = self.fixture(status='canceled')
        px=Mock();px.find_vm_node.return_value='pve';px.vm_current.return_value={'status':'stopped'}
        px.destroy.return_value='UPID:pve:1:2:3:qmdestroy:8810:test:'
        px.wait_task.side_effect=TimeoutError('still running')
        with patch.object(worker,'_px_for_conn',return_value=px),patch.object(worker,'_probe_vm_presence') as probe:
            worker._reconcile_canceled_job(jid)
        probe.assert_not_called()
        with session_scope() as s:
            self.assertEqual(s.get(Deployment,did).identity_state,'submitting')
            self.assertEqual(s.get(Job,jid).remote_task,px.destroy.return_value)

if __name__ == '__main__':
    unittest.main()
