# Deployment Reliability Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make queued deployments immutable, require delivery of every requested configuration phase, preserve VM/IP ownership through failures, and coordinate all Proxmox lifecycle tasks.

**Architecture:** Admission stores a Fernet-encrypted execution plan containing the accepted recipe and referenced block definitions. The single worker consumes only that snapshot, persists post-boot waits, and reconciles deletion using tri-state Proxmox ground truth. Existing SQLite and single-worker architecture remain unchanged.

**Tech Stack:** Python 3.12, FastAPI, SQLModel/SQLite, proxmoxer, Paramiko, plain-Python `test_wave*.py` scripts.

**Spec:** `docs/superpowers/specs/2026-08-31-end-to-end-review-remediation-design.md`

## Global Constraints

- The current easy first-admin setup remains unchanged; do not modify `auth_setup()` or add a bootstrap token.
- SQLite, one Uvicorn worker, and one in-process job worker remain the deployment model.
- Every requested cloud-init and Ansible phase must complete before a job succeeds.
- VMID and static-IP ownership are released only after confirmed VM absence.
- New jobs must execute the definition accepted at admission, not later template/block edits.
- No external queue, database replacement, or broad `api.py`/`worker.py` rewrite.
- Production changes require a failing regression test before implementation.

---

### Task 1: Persist immutable encrypted execution plans

**Files:**
- Create: `app/execution_plan.py`
- Modify: `app/models.py:159-208`
- Modify: `app/db.py:91-127`
- Modify: `app/api.py:268-276,718-775,818-841`
- Modify: `app/worker.py:145-160,295-335,444-639`
- Create: `tests/test_wave37.py`
- Modify: `tests/test_wave25.py`
- Modify: `tests/test_wave36.py`

**Interfaces:**
- Produces `build_execution_plan(session, template, deployment_owner_id, deploy_inputs_json) -> dict`.
- Produces `seal_execution_plan(plan: dict) -> str`, `open_execution_plan(ciphertext: str) -> dict`, and `materialize_execution_plan(plan) -> tuple[list[dict], dict[str, Block]]`.
- Adds `Job.execution_plan_enc: str`, `Job.waiting_since: datetime | None`, and `Deployment.cleanup_last_attempt_at: datetime | None` with idempotent migrations.
- Changes `_run_ansible_phase(..., blocks: dict[str, Block], ...)` so compilation never reloads current block rows.

- [ ] **Step 1: Write the failing snapshot test.** Create the fixture with one mutable recipe/block and assert the admitted encrypted plan retains the original command and accepted ask answer after both database rows change:

```python
def test_execution_plan_is_encrypted_and_immutable():
    uid, template_id, block_key = _mk_plan_fixture(command="old-command")
    result = _deploy(template_id, uid, {"0.0": {"hostname": "accepted-host"}})
    with session_scope() as s:
        job = s.get(Job, result["jobId"])
        assert job.execution_plan_enc
        assert "old-command" not in job.execution_plan_enc
        plan = execution_plan.open_execution_plan(job.execution_plan_enc)
        assert plan["recipe"][0]["blocks"][0]["inputs"]["command"] == "old-command"
        assert plan["deploy_inputs"] == {"0.0": {"hostname": "accepted-host"}}
        s.get(Template, template_id).recipe_json = '[{"blocks":[]}]'
        s.exec(select(Block).where(Block.key == block_key)).one().ansible_template = "changed"
    recipe, blocks = _load_materialized_job_plan(result["jobId"])
    assert recipe[0]["blocks"][0]["inputs"]["hostname"] == "accepted-host"
    assert "old-command" in blocks[block_key].ansible_template
```

- [ ] **Step 2: Run the new wave and confirm red.** Run `GOBLINDOCK_DEV=1 .venv/Scripts/python.exe tests/test_wave37.py`; expect missing `execution_plan_enc`/`app.execution_plan`.
- [ ] **Step 3: Add storage and strict codec.** Use canonical JSON plus existing `security.encrypt()`/`decrypt(strict=True)`. Validate `version == 1`, list recipe, dict blocks, block keys/templates/schema, owner IDs, and dict deploy inputs; raise only `ValueError("invalid execution plan")` for malformed ciphertext/content.
- [ ] **Step 4: Snapshot in deploy and rebuild transactions.** Build and seal the plan before the sole commit. Rebuild snapshots the current template plus persisted accepted deploy answers. New jobs may never have blank `execution_plan_enc`; a compatibility loader may snapshot legacy already-queued jobs once.
- [ ] **Step 5: Consume the plan exclusively in the worker.** Replace worker reads of `Template.recipe_json`, `_blocks_by_key()`, and `dep.deploy_inputs_json` with the materialized plan. Pass the captured map to cloud-init compilation, Ansible compilation, sensitive-input collection, and phase detection.
- [ ] **Step 6: Prove non-disclosure and green.** Assert `job_detail()` contains neither ciphertext nor captured commands; run waves 25, 36, and 37.
- [ ] **Step 7: Commit.** Commit `app/execution_plan.py`, models/migration/API/worker changes and tests as `feat: snapshot immutable deployment execution plans`.

### Task 2: Preflight required cloud-init delivery before VM creation

**Files:**
- Modify: `app/proxmox.py:175-238,445-535`
- Modify: `app/worker.py:338-639`
- Modify: `tests/test_wave37.py`

**Interfaces:**
- Produces `Proxmox.validate_snippet_volume(volid: str, node: str | None = None) -> None`.
- Produces `_preflight_deploy_cloud_init(...) -> DeployPreflight`, containing prepared config, managed key, credential, and snippet volume.

- [ ] **Step 1: Write failing ordering/fallback tests.** Assert a recipe deployment with empty `ssh_key_path` raises before `create_vm_import`; a recipe-free deployment may use native `ciuser`; and required snippet calls occur in `upload, validate, create` order.

```python
def test_recipe_without_ssh_key_fails_before_vm_creation():
    job_id = _mk_worker_job(recipe=_ansible_recipe(), ssh_key_path="")
    calls = []
    with _fake_proxmox(create=lambda *_: calls.append("create")):
        _expect_runtime("ssh_key_path", lambda: worker._execute(job_id))
    assert calls == []

def test_required_snippet_precedes_create():
    calls = _run_required_snippet_job()
    assert calls[:3] == ["upload", "validate", "create"]
```

- [ ] **Step 2: Run wave 37 and confirm current code creates before upload or silently falls back.**
- [ ] **Step 3: Validate delivery.** Reject missing/unreadable/unloadable key paths. Parse only normalized `storage:snippets/file`, verify active storage advertises snippets, and require the exact volume to appear through the Proxmox storage API.
- [ ] **Step 4: Reorder `_run_deploy`.** Choose/guard and persist VMID, prepare/upload/validate required cloud-config, ensure the base disk, then call `create_vm_import`. A nonempty effective recipe or generated console credential requires a snippet. Native fallback is allowed only when neither is required.
- [ ] **Step 5: Preserve collision safety.** If create raises before returning a UPID, remove the uploaded snippet and clear the selected VMID without destroying it. After accepted task submission, retain identity for reconciliation.
- [ ] **Step 6: Run waves 25, 36, and 37; commit as `fix: preflight cloud-init delivery before VM creation`.**

### Task 3: Reconcile ownership against tri-state Proxmox truth

**Files:**
- Modify: `app/worker.py:854-908,951-1074`
- Modify: `app/serialize.py:100-170`
- Modify: `tests/test_wave2.py`
- Modify: `tests/test_wave15.py`
- Modify: `tests/test_wave25.py`
- Modify: `tests/test_wave36.py`
- Modify: `tests/test_wave37.py`

**Interfaces:**
- Produces constants `VM_PRESENT`, `VM_ABSENT`, `VM_UNKNOWN`.
- Produces `_probe_vm_presence(px, vmid, node) -> tuple[str, str]` and `_retry_cleanup_pending(now=None) -> None`.
- Uses deployment status `cleanup_pending` and persisted `cleanup_last_attempt_at`.

- [ ] **Step 1: Write failing truth/recovery tests.** Cover present, absent, and inventory-error probes; failed deploy with present/unknown VM retains allocation; failed cancellation becomes cleanup-pending; and retry deletes only after confirmed absence.

```python
def test_failed_cancellation_keeps_ambiguous_vm_identity():
    dep_id, job_id = _mk_canceled_deploy(vmid=8102, allocated=True)
    with _presence("unknown"):
        worker._reconcile_canceled_job(job_id)
    dep = _deployment(dep_id)
    assert dep.status == "cleanup_pending"
    assert dep.vmid == 8102
    assert _allocation_count(dep_id) == 1
```

- [ ] **Step 2: Run focused waves and confirm current failure/cancel/restart paths release or hide resources.**
- [ ] **Step 3: Implement the invariant.** `vmid is None` is absent; successful inventory is present/absent; missing client or any Proxmox error is unknown. Release IP/delete snippet only on absent. Present/unknown ordinary failures remain visible `error`; ambiguous canceled deploy/destroy becomes `cleanup_pending`.
- [ ] **Step 4: Retry safely.** Stamp `cleanup_last_attempt_at` before external work, skip attempts newer than 60 seconds, and delete deployment/allocation only after later confirmed absence.
- [ ] **Step 5: Make restart recovery probe outside its DB transaction.** Mark only raw `running` jobs interrupted, then reconcile collected IDs. Never treat `waiting` as orphaned work.
- [ ] **Step 6: Serialize cleanup state visibly.** Preserve VMID/IP/error and avoid overwriting `cleanup_pending` with a live status probe. Invoke cleanup retry from the idle loop.
- [ ] **Step 7: Run waves 2, 15, 25, 36, and 37; commit as `fix: retain VM ownership until absence is confirmed`.**

### Task 4: Persist and resume post-boot IP waits

**Files:**
- Modify: `app/worker.py:604-639,776-851,1023-1093`
- Modify: `app/api.py` active-job, cancel, dismiss, and deletion queries
- Modify: `app/serialize.py` job/active-state mappings
- Modify: `tests/test_wave37.py`

**Interfaces:**
- Produces `WAITING_TIMEOUT = timedelta(minutes=30)`, `JobDeferred`, `_defer_for_guest_ip(ctx)`, `_poll_waiting_jobs(now=None) -> bool`, and `_resume_waiting_ansible(job_id, ip) -> None`.
- Adds raw `Job.status == "waiting"`, serialized as working/active rather than terminal.

- [ ] **Step 1: Write failing wait/resume tests.** Assert missing IP plus Ansible yields `waiting` with no `finished_at`; a later IP runs only the captured Ansible plan; 31-minute timeout fails while preserving VM/IP; cancellation reconciles; restart leaves waiting untouched.

```python
def test_missing_ip_defers_instead_of_succeeding():
    job_id, dep_id = _mk_started_job(ansible=True)
    with _guest_ips([None]):
        worker._execute(job_id)
    assert _job(job_id).status == "waiting"
    assert _job(job_id).finished_at is None
    assert _deployment(dep_id).status == "working"
```

- [ ] **Step 2: Run wave 37 and observe current false success.**
- [ ] **Step 3: Add deferred completion.** Persist VM facts, set status/waiting timestamp/phase, and raise `JobDeferred`; catch it before generic failure/success handling.
- [ ] **Step 4: Poll one oldest waiting job only when no queued job exists.** Handle cancel, deadline, no-IP, and IP-found branches. Resume Ansible from `execution_plan_enc`, persist facts, then succeed.
- [ ] **Step 5: Update every active-state boundary.** Include waiting in cancel, active-by-deployment, widget, connection deletion, lifecycle admission, and serialization queries; exclude it from terminal retention.
- [ ] **Step 6: Run waves 10, 31, and 37; commit as `feat: resume durable post-boot Ansible waits`.**

### Task 5: Serialize lifecycle admission and await every Proxmox task

**Files:**
- Modify: `app/api.py:706-857`
- Modify: `app/worker.py:660-735`
- Modify: `tests/test_wave36.py`
- Modify: `tests/test_wave37.py`

**Interfaces:**
- Renames `_deploy_lock` to `_lifecycle_admission_lock`.
- Produces `_ACTIVE_LIFECYCLE_STATUSES = ("queued", "running", "waiting")`, `_LIFECYCLE_TYPES`, and `_active_lifecycle_job(session, deployment_id) -> Job | None`.

- [ ] **Step 1: Write failing duplicate/conflict tests.** Two sequential and two synchronized destroy calls produce one job and return its ID; rebuild/destroy conflicts return 409 for every active status; cleanup-pending rejects lifecycle actions.
- [ ] **Step 2: Write failing UPID tests.** For start/stop/restart assert `wait_task(upid, timeout=120)` is called and non-OK/timeout becomes HTTP 502. For rebuild/destroy assert every stop UPID is awaited and no fixed sleep is used.
- [ ] **Step 3: Run waves 36/37 and confirm duplicate rows and ignored UPIDs.**
- [ ] **Step 4: Use one admission lock through final commit.** Duplicate destroy returns its existing active destroy; rebuild rejects all active lifecycle work; destroy rejects active deploy/rebuild; cleanup-pending rejects direct/destructive operations.
- [ ] **Step 5: Await all tasks.** Direct actions call `wait_task`; worker stop phases inspect state, submit stop only when needed, and await with cancellation. Preserve idempotent destroy only when Task 3 returns confirmed absent.
- [ ] **Step 6: Run waves 36 and 37; commit as `fix: coordinate lifecycle admission and Proxmox tasks`.**

### Task 6: Deployment-plan verification

**Files:**
- Review: all files changed in Tasks 1-5

- [ ] **Step 1: Run waves 2, 10, 15, 25, 31, 36, and 37 in fresh processes.**
- [ ] **Step 2: Run `python -m compileall -q app tests` and `git diff --check`.**
- [ ] **Step 3: Inspect the complete plan-range diff for mutable template reads, unconditional IP release, ignored UPIDs, and leaked `execution_plan_enc`.**
- [ ] **Step 4: Commit only any review-approved corrections with focused messages.**
