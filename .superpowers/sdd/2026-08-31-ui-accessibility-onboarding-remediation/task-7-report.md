# Task 7 report — system starter location backfill

## Outcome

The system-owned exact-name `AI Dev Box` starter now receives a deterministic,
compatible connection/network location after default networks exist. The helper selects
the first system starter, connection, and compatible network by ascending database ID.
A user-owned same-name template neither suppresses system seeding nor gets mutated.

Existing non-null connection choices remain authoritative and untouched for null,
compatible, mismatched, or dangling networks. A missing connection with an existing
network is filled only when both the network and its owning connection resolve. A fully
missing location is assigned only after both selected rows resolve, so there are no
partial writes.

The helper owns no flush or commit, including SQLAlchemy autoflush on repeated calls;
it returns true exactly for an in-session mutation and is idempotent. Startup seeds the
location-null definition, creates any missing default networks, then backfills inside a
caller-owned committing session. Connection creation persists its connection, creates
the default network through the unchanged `default_network_for()` path, invokes the
helper, and explicitly commits the backfill.

`SetupBody`, `/auth/setup`, the CSRF exemption, and the easy token-free first-admin flow
remain unchanged. Public serialization, models, deploy fallback behavior, and default
network selection semantics were not changed.

## TDD evidence

- Added focused real-session Wave 39 coverage before production edits for all four
  connection/network null combinations; compatible ordered selection with a distracting
  lower-ID network on another connection; dangling and mismatched preservation;
  user/system same-name isolation; no partial writes; no flush/commit; rollback
  ownership; double-call idempotence; seed ordering/persistence; real connection-route
  persistence; and the token-free setup contract.
- RED on exact base `164ed7da5869629de4ebe73a3c62027923227bdd`: Wave 39 failed with
  `AttributeError: module 'app.seed' has no attribute 'backfill_starter_template_location'`.
- Independent RED: the existing name-only `seed_templates()` early return left zero
  system-owned starters when a user-owned `AI Dev Box` existed.
- The initial GREEN attempt exposed an implicit autoflush on the second helper call
  (`flush=1`, `commit=0`). The helper reads were minimally placed under
  `session.no_autoflush`; focused Wave 39 then passed.

## Automated verification

- Focused `tests/test_wave39.py`: passed.
- Seed/deploy Wave 10 and setup/security Wave 38: passed.
- All 39 `tests/test_wave*.py` files in fresh `.venv` processes with
  `GOBLINDOCK_DEV=1`: passed.
- Both UI suites, `test_wave37_ui.js` and `test_wave39_ui.js`: passed.
- Syntax checks for all 18 authored top-level `web/*.js` files: passed.
- `.venv\Scripts\python.exe -m compileall -q app tests`: passed.
- `git diff --check`: passed.
- Targeted scans confirmed the exact three-field `SetupBody`, unchanged `/auth/setup`
  and CSRF exemption, and no diff to `app/main.py`, `app/models.py`, or
  `app/serialize.py`.

## Residuals

No automated-test, syntax, compilation, whitespace, setup-contract, or disclosure
residual was found. Browser/server/page tooling was not used, as required by the task.

Requested commit message: `fix: backfill system starter template location`.
