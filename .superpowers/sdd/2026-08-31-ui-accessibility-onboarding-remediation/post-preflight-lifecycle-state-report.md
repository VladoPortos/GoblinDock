# Post-preflight lifecycle-state report

Base: `7b984685f101350e229b69065410f511e9c72c1a` (clean tracked linked worktree)

Reviewer round 1 baseline: `c8b87b7c05726a7ecddc87f733aa6f0133e15ad6`

## Reviewer round 1 — power/lifecycle admission serialization

- A bounded, reference-counted per-deployment lock registry now gives direct power
  actions and rebuild/destroy admission one synchronization boundary. Idle entries are
  reclaimed, and the registry guard is never held while acquiring or owning a
  deployment lock, so unrelated deployment IDs proceed independently.
- Start/stop/restart retain the ownership lookup before locking, end that initial read
  transaction, then re-read ownership and lifecycle state under the deployment lock.
  The lock remains held across Proxmox request submission, task completion, audit,
  commit, state notification, and response construction.
- Rebuild/destroy retain the ownership lookup before locking, then hold the same
  deployment lock across the active-job check and committed job admission. They acquire
  the existing global lifecycle admission lock only after the deployment lock. A
  lifecycle request waiting on a power action therefore cannot hold the global lock and
  stall a different deployment, while the existing quota/IP allocation serialization
  remains intact.
- The required order is ownership lookup, deployment lock, then (for lifecycle
  admission only) global lifecycle lock. Direct power actions never acquire the global
  lifecycle lock, and deploy admission never acquires a deployment lock, leaving no
  reverse-order cycle.

### Round 1 strict TDD evidence

- RED: the new deterministic Wave 36 barrier test failed because both same-deployment
  lifecycle contenders could complete while a power request was blocked in
  `wait_task`; the database could already contain lifecycle work before the power
  boundary released.
- GREEN: while a power action owns its deployment lock, concurrent rebuild and destroy
  remain uncommitted. After release they observe sequential state: exactly one admits
  and the other returns HTTP 409. The reverse-order rebuild and destroy cases commit
  first and force the waiting power action to return HTTP 409 without submitting to
  Proxmox. A lifecycle admission for a different deployment completes during the
  blocked power wait, and registry-entry reclamation is asserted after each scenario.
- Focused Wave 10, Wave 36, Wave 37, and Wave 39 Python suites passed. Both rendered UI
  suites passed unchanged.
- Fresh verification passed all `39/39` Python wave scripts in separate interpreter
  processes, both `2/2` UI behavior suites, all `20/20` JavaScript syntax checks, and
  Python compilation. `git diff --check` passed apart from informational repository
  LF-to-CRLF notices.
- Setup and disclosure scans passed: setup fields remain exactly `email`, `name`, and
  `password`; setup-token and disclosure regression sentinels are absent from authored
  application/UI code. The full Wave 39 run also exercised real token-free first-admin
  setup and authenticated non-admin state redaction.

Reviewer round 2 baseline: `3588effa875acb5ee8c10df58349e09b60cfaa2a`

## Reviewer round 2 — snapshot mutation/lifecycle serialization

- Snapshot create, delete, and rollback now perform ownership authorization before
  locking, end the initial read transaction, then re-read the deployment under the same
  per-deployment lock used by power and lifecycle admission. Cleanup-pending and every
  active queued/running/waiting deploy/rebuild/destroy job are rejected before
  connection, VMID, or Proxmox snapshot work.
- Each snapshot mutation holds the deployment lock through request submission, all
  task waits (including rollback auto-start), audit, commit, state notification, and
  response construction. Exceptions leave the context and reclaim idle registry
  entries. The read-only snapshot-list endpoint remains outside mutation admission.
- The detail UI passes the shared `working`/`cleanup_pending` predicate into the
  snapshot surface. Locked detail still fetches and renders existing snapshot names and
  metadata, but omits Take snapshot, Roll back, and Delete. Running/stopped detail keeps
  all three controls.
- The bounded endpoint audit found no remaining direct VM mutation route beyond power,
  rebuild/destroy admission, and these three snapshot writers.

### Round 2 strict TDD evidence

- RED: Wave 36 reached the patched Proxmox constructor instead of returning HTTP 409
  for a queued deploy during snapshot create. Wave 39's rendered working-detail fixture
  still contained snapshot mutation controls.
- GREEN: a 27-case snapshot matrix (three mutations × three lifecycle job types ×
  queued/running/waiting) rejects before Proxmox. All three cleanup cases reject, all
  three unauthorized cases fail before the deployment lock, and rejected paths reclaim
  the registry entry.
- For each snapshot mutation, deterministic registered-waiter tests block both rebuild
  and destroy until snapshot Proxmox work and commit finish. The reverse ordering holds
  each lifecycle admission at its commit boundary, registers the snapshot waiter, then
  proves the snapshot observes the committed job and returns HTTP 409 without Proxmox.
  All cases assert lock reclamation. A blocked snapshot on one deployment does not
  prevent lifecycle admission for another deployment.
- Round 1 rendezvous tests now observe the real registry owner/waiter count before their
  events fire, removing the pre-registration scheduling gap. Reverse-order power cases
  now also assert registry reclamation after HTTP 409.
- Focused Waves 16, 35, 36, 37, and 39 Python passed, covering legacy snapshot
  create/list/delete/rollback and rollback auto-start as well as lifecycle/detail state.
  Both rendered UI suites passed.
- Fresh verification passed `39/39` Python wave scripts, `2/2` UI behavior suites,
  `20/20` JavaScript syntax checks, Python compilation, diff validation, and the setup
  and disclosure scans.

Reviewer round 3 baseline: `9735304ebc8b1a5ff12b4112309fc1ae24554949`

## Reviewer round 3 — serial-console setup/lifecycle serialization

- Serial-console WebSocket origin, session, and ownership authorization still completes
  before any deployment lock. Missing, unauthenticated, cross-origin, and non-owner
  handshakes all close with the existing 4403 behavior and never enter the lock, so no
  deployment-existence side channel was introduced.
- After authorization, `asyncio.to_thread` performs guarded synchronous preparation so
  waiting for the per-deployment `threading.Lock` cannot block the event loop. Under
  that lock a fresh database session re-reads the user, deployment, connection, VMID,
  cleanup state, and active lifecycle state before touching Proxmox.
- Cleanup-pending or an active queued/running/waiting deploy/rebuild/destroy rejects
  before `ensure_serial`. The lock covers persistent `ensure_serial` configuration and
  the dependent serial termproxy preparation, then releases before the live WebSocket
  connection and proxy pump. Only detached URL, TLS/header options, ticket, and proxy
  user data cross that boundary. VNC/transient proxy behavior was not expanded.
- The shared detail lock predicate now removes Console launch and any already-open
  console surface for `working` and `cleanup_pending`; ordinary running/stopped detail
  continues to expose Console.

### Round 3 strict TDD evidence

- RED: with a queued deploy, the serial WebSocket reached the patched `ensure_serial`;
  the rendered working-detail fixture also still exposed Console.
- GREEN: the serial guard rejects all nine lifecycle type/status combinations plus
  cleanup before `ensure_serial`. Authorization-order cases prove the lock is never
  entered for an invalid origin/session/owner or missing deployment.
- Deterministic registry-waiter tests cover serial setup before rebuild and destroy,
  plus rebuild/destroy held at their uncommitted audit boundary before serial setup.
  The first ordering blocks lifecycle admission until serial preparation returns; the
  reverse ordering makes serial preparation observe the committed job and skip
  `ensure_serial`. An unrelated deployment proceeds independently, setup exceptions
  reclaim the registry entry, and a scheduled event-loop ticker remains responsive
  while synchronous serial setup waits in its worker thread.
- The snapshot forward-order regression now releases Proxmox `wait_task`, blocks after
  `record_audit` has staged its row but before commit, and proves rebuild/destroy remain
  blocked until that audit/commit boundary releases. Registry reclamation remains
  asserted after every case.
- Focused Waves 16, 35, 36, 37, 38, and 39 Python passed, including existing live
  serial/VNC revocation and coordinated-close coverage. Both rendered UI suites passed.
- Fresh verification passed `39/39` Python wave scripts, `2/2` UI behavior suites,
  `20/20` JavaScript syntax checks, Python compilation, diff validation, and setup and
  disclosure scans.

## Scope and outcome

- Direct start, stop, and restart now perform the existing ownership lookup first,
  preserve the cleanup guard, then reject an active queued/running/waiting
  deploy/rebuild/destroy job before VMID, connection, or Proxmox work.
- VM detail now prefers an active lifecycle job over a newer terminal history row,
  exposes that job ID, derives an effective `working` state for active work, and keeps
  `cleanup_pending` authoritative.
- Detail serialization returns the persisted exact error for `error` and
  `cleanup_pending` and never probes Proxmox for effective `working`, `error`, or
  `cleanup_pending` states. Ordinary running and stopped detail still performs the
  existing live/config probe.
- Dashboard serialization defensively keeps `cleanup_pending` plus its error when an
  active-job map is stale, without a misleading working overlay or job chip.
- One shared UI predicate defines `working` and `cleanup_pending` as lifecycle-locked.
  VM detail renders `Cleanup pending` and its exact error while omitting VM power and
  delete controls. It also omits snapshot mutations and console launch while retaining
  snapshot viewing. Normal running/stopped detail controls remain unchanged.
- Dashboard table rows and cards omit selection and lifecycle actions for locked VMs.
  Select-all, selected count, bulk confirmation copy, and bulk execution use only VMs
  that are currently unlocked; execution re-filters a mixed/stale selection.
- Existing responsive structural classes, native Delete naming, normal navigation,
  job navigation, benign detail/configuration presentation, and Task 5/6 behavior were
  retained. No schema change, migration, or unrelated refactor was introduced.

## TDD evidence

### Backend RED

- Wave 36's new 27-case direct-action matrix (three actions × three lifecycle job
  types × queued/running/waiting) failed with HTTP 400 `VM not provisioned`; this proved
  active work was checked after VMID/connection admission.
- Wave 37's cleanup-detail regression failed with observed calls
  `['construct', 'current', 'config']`; this proved `cleanup_pending` was live-probed.
- The same Wave 37 group covers exact cleanup/error propagation, active-job precedence
  over a newer terminal row, effective working/no-probe behavior, ordinary
  running/stopped probes, and cleanup precedence over a stale active map.

### Rendered UI RED

- Wave 39 UI failed at the real dashboard render boundary with
  `Working locked row must not be selectable` (`1 !== 0`). The harness renders the
  actual Dashboard and VM Detail components rather than inspecting source strings.

### GREEN

- Focused Waves 36, 37, and 39 Python passed.
- Focused Wave 37 and Wave 39 rendered UI suites passed.
- The rendered regressions cover locked table rows and cards, normal row/card actions,
  eligible select-all/count, stale-selection execution filtering, cleanup label/error,
  locked detail controls, normal running/stopped controls, and the shared predicate.

## Fresh verification

- Python wave scripts: `39/39` passed in separate `.venv` interpreter processes with
  `GOBLINDOCK_DEV=1`.
- UI behavior suites: `2/2` passed (`test_wave37_ui.js`, `test_wave39_ui.js`).
- JavaScript syntax: `20/20` passed (18 authored `web/*.js` files plus both UI suites).
- `.venv\Scripts\python.exe -m compileall -q app tests`: passed.
- `git diff --check`: passed; the only line-ending diagnostics were Git's existing
  informational LF-to-CRLF notices.
- Setup scans passed: `SetupBody` remains exactly `email`, `name`, `password`; the real
  token-free first-admin setup regression passed.
- Disclosure scans passed: the authenticated non-admin `/api/state` redaction regression
  passed, and known sensitive regression sentinels were absent from `app/` and `web/`.
- Changed-path audit before this report found only the intended API, serializer, shared
  UI predicate, dashboard, detail, and Wave 36/37/39 test files.

## Constraints and residuals

- No browser, server, page, screenshot, Playwright, CUA, or in-app navigation tooling
  was used. Rendered component/state/event tests were the required acceptance path.
- The implementation intentionally preserves the existing lifecycle admission model,
  global admission semantics, and active-job query. The new in-process per-deployment
  serialization matches the supported single-worker architecture and does not claim a
  cross-process distributed lock.
- No known functional, test, syntax, compilation, setup, disclosure, whitespace, or
  scope residual remains within this fix.

Initial requested commit message: `fix: lock VM actions during lifecycle work`.

Reviewer round 1 requested commit message:
`fix: serialize VM power and lifecycle admission`.

Reviewer round 2 requested commit message:
`fix: guard snapshot mutations during lifecycle work`.

Reviewer round 3 requested commit message:
`fix: guard serial console setup during lifecycle work`.
