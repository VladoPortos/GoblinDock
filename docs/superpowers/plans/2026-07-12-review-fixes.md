# Security and State-Invariant Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the reviewed secret-authorization, provisioning, recovery, validation, transaction, and deletion defects with direct regression coverage.

**Architecture:** Preserve the FastAPI + SQLModel + single worker-thread design. Add small shared validators/guards, make deployment admission a single locked transaction, and pass deployment-owner authorization explicitly into worker secret resolution.

**Tech Stack:** Python 3.12, FastAPI, SQLModel/SQLite, plain-Python `test_wave*.py` scripts, WSL test runtime.

## Global Constraints

- Global secrets and variables resolve only for admin deployment owners.
- Referenced resources are rejected with HTTP 409; no implicit cascade.
- Public-template trust behavior remains unchanged.
- No schema migration or multi-process support.
- Production changes require a failing regression test first.

---

### Task 1: Secret authorization and rebuild ownership

**Files:**
- Modify: `app/worker.py:145-308,417-605`
- Modify: `app/api.py:485-527`
- Create: `tests/test_wave36.py`

**Interfaces:**
- Produces: `_secret_lookup_factory(owner_id, sink=None, *, allow_global=False)` and `_owner_secret_context(owner_id) -> tuple[int | None, bool]`.
- Consumes: `Deployment.owner_id`, `User.role`, existing secret/variable models.

- [ ] **Step 1: Write failing tests** proving a normal owner cannot resolve global values, an admin owner can, `/state` does not enumerate global values to normal users, and an admin-triggered rebuild uses `dep.owner_id` rather than `job.created_by`.
- [ ] **Step 2: Run** `GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave36.py` and confirm authorization assertions fail against current behavior.
- [ ] **Step 3: Implement minimal authorization plumbing.** Add an explicit `allow_global` keyword to lookup; obtain the deployment owner's role once per deploy; pass the owner ID through SSH-key, cloud-init, sensitive-input, and Ansible paths; filter `/state` metadata consistently.
- [ ] **Step 4: Re-run the wave and confirm the Task 1 tests pass.**

### Task 2: Worker provisioning safety

**Files:**
- Modify: `app/worker.py:293-308,437-487`
- Modify: `tests/test_wave36.py`

**Interfaces:**
- Produces: `_clamp_resource(requested: int, limit: int) -> int` where zero means unlimited.

- [ ] **Step 1: Add failing tests** proving a create-call collision performs no destroy, zero CPU/RAM/disk ceilings remain unlimited, nonzero limits clamp, and `run_playbook()` startup exceptions propagate.
- [ ] **Step 2: Run the new wave and verify all four tests fail for the intended reasons.**
- [ ] **Step 3: Implement minimal fixes:** set a `create_submitted` flag only after `create_vm_import()` returns; clean up only when true; centralize zero-as-unlimited clamping; raise `RuntimeError` from Ansible startup failures.
- [ ] **Step 4: Re-run the wave and existing worker-focused waves 15, 18, 25, and 26.**

### Task 3: Atomic deployment admission and crash recovery

**Files:**
- Modify: `app/api.py:152-273,700-759`
- Modify: `app/worker.py:1018-1042`
- Modify: `tests/test_wave36.py`

**Interfaces:**
- Produces: process-local `_deploy_lock`; `allocate_ip(..., commit: bool = False)` is not exposed—allocation joins the caller transaction and flushes.

- [ ] **Step 1: Add failing tests** proving exhausted pools leave no deployment/job/allocation, concurrent requests cannot exceed a one-VM quota, and orphan rebuild/destroy jobs retain IP allocations while orphan initial deploys release them.
- [ ] **Step 2: Run the wave and verify the expected partial-state and recovery failures.**
- [ ] **Step 3: Implement one locked transaction:** move quota checking inside `_deploy_lock`, use `session.flush()` for deployment ID and IP reservation, create job/audit before the sole commit, and rollback on exceptions. Restrict startup IP release to `job.type == "deploy"`.
- [ ] **Step 4: Re-run the wave and existing network/job waves 2, 4, 15, and 18.**

### Task 4: Recipe validation and reference-safe deletion

**Files:**
- Modify: `app/api.py:1352-1430,1433-1443,2270-2285,2379-2392,2608-2620,2661-2675`
- Modify: `app/recipes.py:133-146,230-319,432-437`
- Modify: `app/serialize.py:258-323`
- Modify: `tests/test_wave36.py`

**Interfaces:**
- Produces: `_validate_recipe(recipe: list, session: Session, user: User) -> None`; `_template_uses_block(template, key) -> bool`.

- [ ] **Step 1: Add failing tests** for malformed section/block rejection, defensive legacy serialization, and HTTP 409 deletion guards for used templates, blocks, images, connections, and networks.
- [ ] **Step 2: Run the wave and confirm validation/deletion tests fail against current behavior.**
- [ ] **Step 3: Implement strict write validation and defensive read helpers.** Validate recipe structure and block visibility on save/edit; make recipe iteration skip malformed legacy entries; add dependency queries/scans before each destructive delete.
- [ ] **Step 4: Re-run the wave and existing template/security waves 9, 10, 14, 17, 18, and 26.**

### Task 5: Full verification and review

**Files:**
- Review: `app/api.py`, `app/worker.py`, `app/recipes.py`, `app/serialize.py`, `tests/test_wave36.py`

- [ ] **Step 1: Run all direct wave tests** with a fresh Python process per file.
- [ ] **Step 2: Run** `node --check web/*.js` and Python `compileall`.
- [ ] **Step 3: Run `git diff --check`, inspect the complete diff, and confirm no unrelated changes.**
- [ ] **Step 4: Commit the implementation with a focused message.**

## Self-review

- All nine reviewed findings map to Tasks 1-4.
- Function signatures are consistent across tasks.
- No schema or public-template redesign is introduced.
- The test commands match the repository's documented direct-script convention.
