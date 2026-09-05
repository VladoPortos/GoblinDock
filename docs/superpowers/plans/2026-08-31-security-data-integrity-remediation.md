# Security and Data-Integrity Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent cross-owner template secret disclosure, revoke live consoles, make login lockout concurrent-safe, publish only valid backups, and allocate only usable static addresses.

**Architecture:** Add focused recipe/network helpers around the existing API and serializer, migrate legacy recipes before seeding prunes metadata, periodically reauthorize open consoles, and make backup/network persistence fail closed. This plan consumes the immutable execution-plan format created by the deployment reliability plan.

**Tech Stack:** Python 3.12, FastAPI/Starlette WebSockets, SQLModel/SQLite, asyncio, ipaddress, plain-Python `test_wave*.py` scripts.

**Spec:** `docs/superpowers/specs/2026-08-31-end-to-end-review-remediation-design.md`

## Global Constraints

- The current easy first-admin setup remains exactly unchanged; `auth_setup()` still accepts only email, name, and password.
- Cross-owner public deployment may use ask-on-deploy values or deployer-scoped `{{ secrets.NAME }}` references, never the author's literal sensitive value.
- Unknown legacy block inputs fail closed for non-owner serialization/deployment.
- Console revocation applies to already-open serial and VNC sessions without changing handshake origin/ownership checks.
- Backups publish atomically only after SQLite integrity validation.
- Static mode never silently degrades to DHCP.
- SQLite, the single worker, and current setup/logout behavior remain unchanged.
- Production changes require a failing regression test first.

## Dependency

Complete Task 1 of `docs/superpowers/plans/2026-08-31-deployment-reliability-remediation.md` first. This plan consumes execution plan keys `recipe`, `blocks`, `sensitive_fields`, `deploy_inputs`, `template_owner_id`, and `deployment_owner_id`.

---

### Task 1: Migrate placed `b-ssh` recipes before pruning

**Files:**
- Modify: `app/seed.py:1125-1164`
- Create: `tests/test_wave38.py`
- Regression: `tests/test_wave24.py`
- Regression: `tests/test_wave33.py`

**Interfaces:**
- Produces `_migrate_b_ssh_recipe(recipe: list[dict]) -> tuple[list[dict], bool]`.
- `seed_blocks()` applies the migration to every `Template.recipe_json` before deleting obsolete built-ins.

- [ ] **Step 1: Write the failing migration/idempotence test.** Cover a template whose `b-ssh` row is already absent and one where it remains. Preserve placement IDs/names/metadata and map inputs exactly:

```python
def test_seed_migrates_b_ssh_before_pruning():
    tid = _legacy_template({
        "user": "alice", "password": "pw", "public_key": "ssh-ed25519 AAAA",
        "sudo": False, "ssh_password_login": True,
    }, ask=["password", "sudo"])
    seed.seed_blocks()
    placed = _placed(tid)
    assert placed["ref"] == "b-user"
    assert placed["inputs"] == {
        "user": "alice", "password": "pw", "public_key": "ssh-ed25519 AAAA",
        "ssh_password_login": True, "shell": "/bin/bash", "home": "",
        "groups": ["sudo"], "sudoers": True, "nopasswd": False,
    }
    assert placed["ask"] == ["password", "nopasswd"]
    first = _template_json(tid)
    seed.seed_blocks()
    assert _template_json(tid) == first
```

- [ ] **Step 2: Run wave 38 and confirm the reference remains `b-ssh`.**
- [ ] **Step 3: Implement a deep-copy migration.** Rewrite only well-formed placements with exact `ref == "b-ssh"`; map `sudo` to `nopasswd`, retain allowed asks, keep unrelated metadata, and persist changed JSON before pruning.
- [ ] **Step 4: Run waves 24, 33, and 38; commit as `fix: migrate legacy ssh block placements before pruning`.**

### Task 2: Enforce the public-template sensitive-value boundary

**Files:**
- Modify: `app/recipes.py:20-50,133-194`
- Modify: `app/api.py:636-775,1389-1493`
- Modify: `app/serialize.py:258-300`
- Modify: `app/execution_plan.py`
- Modify: `tests/test_wave38.py`

**Interfaces:**
- Produces `is_deployer_secret_ref(value: object) -> bool` using full-string `^\{\{\s*secrets\.[A-Za-z0-9_]+\s*\}\}$`.
- Produces `validate_public_sensitive_inputs(recipe, schemas_by_ref, *, deploy_inputs=None, cross_owner=False, reject_unknown=False) -> None`.
- Produces API `_validate_cross_owner_execution_plan(plan: dict) -> None` before any Deployment/Job/IP rows are inserted.

- [ ] **Step 1: Write failing save/deploy/masking tests.** Public save with literal password/secret returns 400 without echoing the value; ask-on-deploy and exact secret refs pass; imported unsafe/unknown plan returns 409 before persistence; owner/private behavior remains allowed; a missing block masks every nonempty input for non-owner state.

```python
def test_public_literal_is_rejected_without_echo():
    exc = _expect_http(400, lambda: _save_public(password="DO-NOT-ECHO"))
    assert "password" in exc.detail
    assert "DO-NOT-ECHO" not in exc.detail

def test_unknown_legacy_block_masks_all_nonempty_inputs():
    recipe = [{"blocks": [{"ref": "b-pruned", "inputs": {
        "password": "p", "note": "private", "empty": ""}}]}]
    assert S._mask_recipe_passwords(_session(), recipe)[0]["blocks"][0]["inputs"] == {
        "password": "********", "note": "********", "empty": ""}
```

- [ ] **Step 2: Run waves 26, 34, 36, and 38; confirm current save/deploy paths allow literals and unknown inputs leak.**
- [ ] **Step 3: Implement schema-aware validation.** Sensitive schema types are `password` and `secret`. Public save/edit permits blank ask fields or exact deployer secret refs. Cross-owner admission uses snapshot schemas, rejects unknown refs, and requires a supplied ask answer rather than falling back to stored author data. Errors name block/field only.
- [ ] **Step 4: Fail closed during serialization.** Known blocks mask schema-directed sensitive fields; unknown blocks mask every nonempty input for non-owner viewers.
- [ ] **Step 5: Run waves 9, 10, 26, 34, 36, and 38; commit as `fix: prevent public templates from carrying author secrets`.**

### Task 3: Revoke live consoles and terminate pumps together

**Files:**
- Modify: `app/api.py:1042-1268`
- Modify: `tests/test_wave38.py`
- Regression: `tests/test_wave3.py`
- Regression: `tests/test_wave32.py`

**Interfaces:**
- Produces frozen `_ConsoleGrant(conn, deployment, user_id, session_epoch)`.
- Produces `_console_grant_still_valid(grant) -> bool`, `_close_console_pair(...)`, and `_pump_ws(..., grant)`.
- Uses `_CONSOLE_AUTH_INTERVAL_S = 3.0`.

- [ ] **Step 1: Write failing live-revocation tests.** With interval temporarily `0.01`, establish each serial/VNC pump, then disable/delete the user, increment epoch, demote a non-owner admin, or transfer deployment ownership. Assert both sockets close and no later frame is relayed.
- [ ] **Step 2: Write failing first-completion tests.** PVE iterator ending first and browser disconnecting first must both finish `_pump_ws` and close the opposite side within 0.5 seconds.

```python
async def _assert_revoked(mutate, prefer_bytes):
    grant, browser, pve = _live_console_fixture()
    task = asyncio.create_task(api._pump_ws(browser, pve, prefer_bytes, grant))
    mutate(grant)
    await asyncio.wait_for(browser.closed.wait(), 0.5)
    await asyncio.wait_for(task, 0.5)
    assert browser.sent == []
```

- [ ] **Step 3: Run waves 3, 32, and 38; confirm no periodic check and hanging `gather`.**
- [ ] **Step 4: Implement fresh-session reauthorization every three seconds.** Re-read user, epoch, deployment, owner, and current role; any DB error fails closed. Check the stopping flag immediately before relaying frames.
- [ ] **Step 5: Run three tasks under `asyncio.wait(..., FIRST_COMPLETED)`.** On any completion, set stopping, cancel/gather pending tasks, and idempotently close both sockets. Revocation closes with 4403.
- [ ] **Step 6: Run waves 3, 32, and 38; commit as `fix: revoke and terminate live console bridges`.**

### Task 4: Update persistent login lockout atomically

**Files:**
- Modify: `app/api.py:433-468`
- Modify: `tests/test_wave38.py`

**Interfaces:**
- Produces `_record_account_login_failure(session, user_id, now) -> None` using one SQLAlchemy `UPDATE` with `case`/`coalesce`.
- Does not change login request/response, threshold, duration, success reset, or first-admin setup.

- [ ] **Step 1: Add a passing setup characterization and failing concurrency test.** The first test creates the first admin with only `SetupBody(email, name, password)`. The second uses five Sessions, distinct IPs, a barrier inside patched password verification, and asserts `locked_until > utcnow()`.

```python
def test_first_admin_setup_contract_is_unchanged():
    req = SimpleNamespace(session={})
    with Session(engine) as s:
        out = api.auth_setup(api.SetupBody(email="admin@example.com", name="Admin",
            password="StrongPass12!"), req, s)
    assert out["ok"] and req.session["uid"]

def test_five_concurrent_failures_lock_account():
    _run_synchronized_wrong_passwords(count=5, distinct_ips=True)
    assert ensure_utc(_user().locked_until) > utcnow()
```

- [ ] **Step 2: Run wave 38 and confirm setup passes while counter finishes below lock threshold.**
- [ ] **Step 3: Implement atomic update.** Increment with `coalesce(failed_logins, 0) + 1`; at threshold reset count to zero and set lock deadline in the same statement; disable session synchronization and commit once.
- [ ] **Step 4: Run waves 3, 30, and 38; commit as `fix: update login lockout counters atomically`.**

### Task 5: Publish verified SQLite backups atomically

**Files:**
- Modify: `app/backup.py:16-118`
- Modify: `tests/test_wave38.py`
- Regression: `tests/test_wave6.py`
- Regression: `tests/test_wave29.py`

**Interfaces:**
- Produces `_verify_sqlite_backup(path)`, `_flush_file(path)`, and best-effort `_flush_directory(path)`.
- Uses a private publication lock and a unique `.goblindock-backup-*.tmp` name outside `_GLOB`.

- [ ] **Step 1: Write failing fault-injection tests.** Patch verification and `os.replace` separately; each failure leaves published listing unchanged and no temp. A successful backup passes `PRAGMA quick_check`, retains 0600/0700 permissions, and rotation keeps the requested count.

```python
def test_publish_failure_leaves_no_advertised_or_temp_backup():
    before = {b["name"] for b in backup.list_backups()}
    with _patch(backup.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("publish"))):
        _expect_raises(OSError, lambda: backup.backup_now("injected"))
    assert {b["name"] for b in backup.list_backups()} == before
    assert not list(backup.backup_dir().glob(".goblindock-backup-*.tmp"))
```

- [ ] **Step 2: Run waves 6, 29, and 38; confirm current output opens the final glob name.**
- [ ] **Step 3: Implement the publication sequence.** `mkstemp`, close fd, SQLite online backup, close destination, exact `quick_check == ok`, chmod, fsync, capture size, `os.replace`, directory flush, then rotate. Always unlink remaining temp in `finally`; no raising operation that determines validity remains after replace.
- [ ] **Step 4: Run waves 6, 29, and 38; commit as `fix: publish verified sqlite backups atomically`.**

### Task 6: Validate and allocate only usable static addresses

**Files:**
- Create: `app/network_pool.py`
- Modify: `app/api.py:195-276,2367-2452`
- Modify: `app/serialize.py:417-439`
- Modify: `tests/test_wave38.py`
- Regression: `tests/test_wave4.py`
- Regression: `tests/test_wave15.py`
- Regression: `tests/test_wave36.py`

**Interfaces:**
- Produces `StaticPoolError`, frozen `StaticPool(network, start, end, gateway)` with `is_reserved`, `iter_usable`, and `usable_total`.
- Produces `parse_static_pool(subnet_cidr, range_start, range_end, gateway="") -> StaticPool`.

- [ ] **Step 1: Write failing validation/allocation tests.** Reject missing endpoint(s), mixed families, reversed/outside range, network, IPv4 broadcast, and gateway. For a directly inserted legacy row, allocator skips reserved addresses. Gatewayless `ipconfig0` omits `gw=`. Incomplete legacy capacity is zero and never DHCP.

```python
def test_static_pool_rejects_reserved_ranges():
    for body in (
        _nb(range_start="", range_end=""),
        _nb(range_start="10.0.50.0", range_end="10.0.50.10"),
        _nb(range_start="10.0.50.1", range_end="10.0.50.10"),
        _nb(range_start="10.0.50.250", range_end="10.0.50.255"),
    ):
        _expect_http(400, lambda body=body: api._validate_network_body(body))
```

- [ ] **Step 2: Run waves 4, 15, 36, and 38; confirm blank pools pass and reserved addresses allocate.**
- [ ] **Step 3: Implement parser/model.** Require both endpoints, same family, order/membership, in-subnet gateway; write validation rejects ranges containing reserved addresses. Defensive iterator skips network/broadcast/gateway in legacy data.
- [ ] **Step 4: Integrate fail-closed.** `_validate_network_body` translates `StaticPoolError` to 400; allocation raises 400 for malformed rows and 409 for exhaustion; `_network_ctx` always obtains static IP and omits absent gateway; serializer returns usable total or zero.
- [ ] **Step 5: Run waves 4, 15, 36, and 38; commit as `fix: validate and reserve usable static addresses`.**

### Task 7: Security/data verification

**Files:**
- Review: all files changed in Tasks 1-6

- [ ] **Step 1: Run waves 3, 4, 6, 9, 10, 15, 24, 26, 29, 30, 32, 33, 34, 36, and 38 in fresh processes.**
- [ ] **Step 2: Run `python -m compileall -q app tests` and `git diff --check`.**
- [ ] **Step 3: Inspect the complete plan-range diff for setup-flow changes, secret values in errors, stale console sessions, published temp backups, DHCP fallback, and reserved-address allocation.**
- [ ] **Step 4: Commit only review-approved corrections with focused messages.**
