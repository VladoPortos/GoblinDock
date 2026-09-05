"""Read-only preflight and explicit, owner-scoped recovery operations."""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select
from . import api, statebus
from .db import get_session
from .deps import current_user
from .execution_plan import open_execution_plan, materialize_execution_plan
from .models import Connection, Deployment, Image, Job, Network, Secret, Template, User, Variable
from .proxmox import Proxmox
from .recipes import compile_ansible, compile_cloudinit, has_ansible_blocks
from .security import decrypt
from .template_ops import RebuildBody, prepare_rebuild, read_only_network_ctx

router = APIRouter(prefix='/api')


def _read_only_lookup(session, owner_id):
    """Resolve the deployment owner's references without recording secret usage."""
    owner = session.get(User, owner_id) if owner_id else None
    allow_global = bool(owner and owner.role == 'admin')
    def lookup(namespace, name):
        model = Variable if namespace == 'variable' else Secret
        row = session.exec(select(model).where(model.scope == 'user', model.owner_id == owner_id,
                                               model.name == name).order_by(model.id)).first() if owner_id else None
        if row is None and allow_global:
            row = session.exec(select(model).where(model.scope == 'global', model.name == name)
                               .order_by(model.id)).first()
        if row is None:
            raise ValueError('required reference is unavailable')
        value = row.value if namespace == 'variable' else decrypt(row.value_enc, strict=True)
        if not value:
            raise ValueError('required reference is empty')
        return value
    return lookup


def _report(session, conn, plan_enc, cfg, dep=None):
    """Check without allocating IPs, uploading snippets, or changing the guest."""
    checks = []
    def add(name, ok, detail):
        checks.append({'name': name, 'status': 'pass' if ok else 'fail', 'detail': detail})
    add('Location', bool(conn and not conn.disabled),
        'Connection enabled' if conn and not conn.disabled else 'Choose an enabled connection')
    source = urlsplit(cfg.get('src_url') or '')
    valid_source = source.scheme == 'https' and bool(source.hostname)
    add('Base image', valid_source, 'HTTPS image source is captured' if valid_source else 'A valid HTTPS image source is required')
    plan = open_execution_plan(plan_enc)
    recipe, blocks = materialize_execution_plan(plan)
    try:
        api._assert_trusted_ansible_blocks(session, recipe, blocks)
        lookup = _read_only_lookup(session, plan['owner_id'])
        commands = compile_cloudinit(recipe, blocks, lookup)
        compile_ansible(recipe, blocks, lookup)
        add('Recipe', True, 'Captured recipe and inputs validated')
    except Exception:
        commands = []
        add('Recipe', False, 'A recipe input or secret reference could not be resolved')
    needs_snippet = has_ansible_blocks(recipe, blocks) or any(c.strip() not in ('', 'set -e') for c in commands)
    if needs_snippet:
        add('Cloud-init delivery', bool(conn and conn.ssh_key_path and os.path.isfile(conn.ssh_key_path)
                                       and os.access(conn.ssh_key_path, os.R_OK)),
            'A readable SSH key is required; the worker verifies snippet delivery before creating or removing a VM')
    if conn and not conn.disabled:
        try:
            px = Proxmox(conn)
            guests = px.list_cluster_guests()
            node = px.find_vm_node(dep.vmid, dep.node) if dep and dep.vmid else px.pick_node()
            node = node or conn.node or px.pick_node()
            px.node_status(node)
            add('Node reachability', True, 'Selected node responds to status queries')
            add('Cluster inventory', True, 'Complete guest inventory is available')
            if not dep:
                used = {int(g['vmid']) for g in guests}
                add('VM identifier', any(i not in used for i in range(api.settings.vmid_min, api.settings.vmid_max + 1)),
                    'A free cluster-wide VM identifier is required')
            stores = {x.get('storage'): x for x in px.storage_status(node)}
            for label, storage, content in [('VM storage', conn.storage, 'images'), ('Image storage', conn.iso_storage, 'import')]:
                row = stores.get(storage)
                supported = bool(row and content in str(row.get('content', '')).split(','))
                add(label, bool(row and row.get('active', 1) and row.get('enabled', 1) and supported),
                    f'Storage must be active and support {content} on the selected node')
            disk_store = stores.get(conn.storage) or {}
            available = disk_store.get('avail')
            needed = int(cfg.get('disk', 20)) * 1024 ** 3
            add('Disk capacity', isinstance(available, (int, float)) and available >= needed,
                f'At least {cfg.get("disk", 20)} GB free VM storage is required')
            from .image_cache import active_filename
            filename = active_filename(conn, node, cfg.get('src_url', ''), cfg.get('checksum', ''), cfg.get('checksum_algorithm', ''))
            cached = any(str(volume).endswith('/' + filename) for volume in px.storage_volumes(node=node))
            add('Image cache', True, 'Matching image is cached' if cached else 'Image download and checksum validation will run before VM removal or creation')
            if needs_snippet:
                row = stores.get(conn.snippet_storage)
                add('Snippet storage', bool(row and 'snippets' in str(row.get('content', ''))),
                    'Storage must support snippets on the selected node')
        except Exception:
            add('Proxmox readiness', False, 'Could not verify cluster inventory and node storage; check connection permissions and availability')
    return {'ok': all(c['status'] == 'pass' for c in checks), 'checks': checks,
            'note': 'A point-in-time check. The worker validates image and cloud-init delivery again before destructive work.'}


@router.post('/preflight/deploy')
def deploy_preflight(body: api.DeployBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    tpl = session.get(Template, body.templateId)
    if not tpl or not (tpl.public or tpl.owner_id == user.id or user.role == 'admin'):
        raise HTTPException(404, 'template not found')
    api._enforce_quota(session, user, 'vm')
    answers = api._validate_deploy_inputs(session, tpl, body.deployInputs)
    plan = api._build_admitted_execution_plan(session, tpl, user.id, answers)
    base = session.get(Image, tpl.base_image_id)
    conn = session.get(Connection, tpl.connection_id)
    cpu = body.cpu if body.cpu is not None else tpl.default_cpu
    ram = body.ram if body.ram is not None else tpl.default_ram
    disk = body.disk if body.disk is not None else tpl.default_disk
    if conn:
        cpu = min(cpu, conn.max_cores) if conn.max_cores else cpu
        ram = max(1, min(ram, conn.max_ram_mb // 1024)) if conn.max_ram_mb else ram
        disk = min(disk, conn.max_disk_gb) if conn.max_disk_gb else disk
    cfg = {'src_url': base.source_url if base and base.kind == 'base' else '',
           'checksum': base.checksum if base else '', 'checksum_algorithm': api._checksum_algo(base.checksum or '') if base else '',
           'cpu': cpu, 'ram': ram, 'disk': disk}
    report = _report(session, session.get(Connection, tpl.connection_id), plan, cfg)
    if tpl.network_id:
        net = session.get(Network, tpl.network_id)
        valid = bool(net and net.connection_id == tpl.connection_id)
        if valid:
            try:
                cfg.update(read_only_network_ctx(session, net))
            except HTTPException:
                valid = False
        report['checks'].append({'name': 'Network', 'status': 'pass' if valid else 'fail',
                                 'detail': 'Network has a valid mapping and available address' if valid else 'Network mapping or static IP pool is invalid or exhausted'})
        report['ok'] = report['ok'] and valid
    return report


@router.post('/deployments/{dep_id}/preflight')
def rebuild_preflight(dep_id: int, body: RebuildBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    dep = api._owned_deployment(session, dep_id, user)
    plan, context, _ = prepare_rebuild(session, dep, user, body.mode, body.deployInputs, read_only=True)
    return _report(session, session.get(Connection, dep.connection_id), plan, json.loads(context), dep)


@router.get('/recovery')
def recovery_list(user: User = Depends(current_user), session: Session = Depends(get_session)):
    query = select(Deployment).where(Deployment.status.in_(('error', 'cleanup_pending')))
    if user.role != 'admin':
        query = query.where(Deployment.owner_id == user.id)
    rows = []
    for dep in session.exec(query).all():
        job = session.exec(select(Job).where(Job.deployment_id == dep.id).order_by(Job.id.desc())).first()
        rows.append({'depId': dep.id, 'name': dep.name, 'vmid': dep.vmid, 'node': dep.node,
                     'status': dep.status, 'error': dep.error, 'jobId': job.id if job else None,
                     'phase': job.phase if job else '', 'remoteTask': job.remote_task if job else '',
                     'remoteNode': job.remote_node if job else '',
                     'uncertainIdentity': dep.identity_state == 'submitting' or bool(job and job.create_state == 'submitting')})
    return {'items': rows}


class ReconcileBody(BaseModel):
    confirmIdentity: bool = False


@router.post('/deployments/{dep_id}/reconcile')
def reconcile(dep_id: int, body: ReconcileBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    with api._deployment_operation_lock(dep_id):
        dep = api._owned_deployment(session, dep_id, user)
        if api._active_lifecycle_job(session, dep.id):
            raise HTTPException(409, 'VM operation is still active')
        conn = session.get(Connection, dep.connection_id)
        if not conn:
            raise HTTPException(409, 'connection is missing')
        api._reject_disabled_connection(conn)
        job = session.exec(select(Job).where(Job.deployment_id == dep.id).order_by(Job.id.desc())).first()
        uncertain = dep.identity_state == 'submitting' or bool(job and job.create_state == 'submitting')
        px = Proxmox(conn)
        node = px.find_vm_node(dep.vmid, dep.node) if dep.vmid else None
        if not node:
            return {'presence': 'absent', 'detail': 'No VM is currently visible. The local identity is retained; use local cleanup only after checking for pending Proxmox tasks.'}
        live = px.vm_current(dep.vmid, node)
        if uncertain and not body.confirmIdentity:
            return {'presence': 'present', 'requiresIdentityConfirmation': True, 'node': node,
                    'name': live.get('name', ''), 'detail': 'Confirm in Proxmox that this VM belongs to this deployment before enabling recovery actions.'}
        if not body.confirmIdentity:
            return {'presence': 'present', 'node': node, 'status': live.get('status', 'unknown'),
                    'detail': 'VM located. Local status and identity have not been changed.'}
        if uncertain:
            if job and job.remote_task:
                try:
                    task = px.task_status(job.remote_task, job.remote_node)
                except Exception:
                    raise HTTPException(409, 'Remote task outcome is still unavailable; inspect Proxmox before retrying')
                if task.get('status') != 'stopped':
                    raise HTTPException(409, 'Remote task is still running; wait for it to stop before confirming recovery')
            if job:
                job.create_state = 'accepted'
                session.add(job)
            dep.identity_state = 'accepted'
        dep.node = node
        # Preserve configuration failures until an explicit retry succeeds.
        if dep.status == 'cleanup_pending':
            dep.status = 'error'
            dep.cleanup_origin = None
        dep.error = dep.error or 'VM located; choose a recovery action'
        session.add(dep)
        api.record_audit(session, user, 'reconcile', 'deployment', dep.id, dep.name)
        session.commit(); statebus.bump()
        return {'presence': 'present', 'node': node, 'status': live.get('status', 'unknown'),
                'detail': 'VM located. Review the job log, then retry configuration, rebuild, or delete.'}


class RetryBody(BaseModel):
    acknowledgeReplay: bool = False


@router.post('/deployments/{dep_id}/retry-cleanup')
def retry_cleanup(dep_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    with api._deployment_operation_lock(dep_id), api._lifecycle_admission_lock:
        dep = api._owned_deployment(session, dep_id, user)
        if dep.identity_state == 'submitting':
            raise HTTPException(409, 'Confirm the remote task outcome and VM ownership in Recovery first')
        if api._active_lifecycle_job(session, dep.id):
            raise HTTPException(409, 'VM operation is still active')
        if dep.status == 'cleanup_pending':
            dep.status = 'error'
            session.add(dep)
        try:
            return api._vm_destroy_transaction(dep_id, user, session)
        except Exception:
            session.rollback()
            raise


@router.post('/deployments/{dep_id}/retry-configuration')
def retry_configuration(dep_id: int, body: RetryBody, user: User = Depends(current_user), session: Session = Depends(get_session)):
    if not body.acknowledgeReplay:
        raise HTTPException(400, 'Acknowledge that captured post-boot scripts will run again')
    with api._deployment_operation_lock(dep_id):
        dep = api._owned_deployment(session, dep_id, user)
        api._reject_cleanup_pending(dep)
        if api._active_lifecycle_job(session, dep.id):
            raise HTTPException(409, 'VM operation is still active')
        conn = session.get(Connection, dep.connection_id)
        if not conn or not dep.vmid:
            raise HTTPException(409, 'VM identity or connection is missing')
        api._reject_disabled_connection(conn)
        prior = session.exec(select(Job).where(Job.deployment_id == dep.id,
            Job.type.in_(('deploy', 'rebuild', 'configure'))).order_by(Job.id.desc())).first()
        if (not prior or not prior.execution_plan_enc or prior.create_state == 'submitting'
                or dep.identity_state in ('submitting', 'rejected')):
            raise HTTPException(409, 'Captured plan or confirmed VM identity is unavailable')
        try:
            plan = open_execution_plan(prior.execution_plan_enc)
        except ValueError:
            raise HTTPException(409, 'Captured execution plan is corrupt or cannot be decrypted')
        if plan['owner_id'] != dep.owner_id:
            raise HTTPException(409, 'Captured plan owner mismatch')
        recipe, blocks = materialize_execution_plan(plan)
        api._assert_trusted_ansible_blocks(session, recipe, blocks)
        if not has_ansible_blocks(recipe, blocks):
            raise HTTPException(409, 'This recipe has no post-boot configuration; use rebuild for first-boot changes')
        job = Job(type='configure', title=f'Retrying configuration for {dep.name}',
                  deployment_id=dep.id, connection_id=conn.id, created_by=user.id,
                  context_json=prior.context_json, execution_plan_enc=prior.execution_plan_enc,
                  create_state='accepted')
        dep.status = 'working'; dep.error = ''
        session.add(job); session.add(dep)
        api.record_audit(session, user, 'retry-configuration', 'deployment', dep.id, dep.name)
        session.commit(); session.refresh(job); statebus.bump()
        return {'ok': True, 'jobId': job.id}
