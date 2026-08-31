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
  delete controls. Normal running/stopped detail controls remain unchanged.
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
