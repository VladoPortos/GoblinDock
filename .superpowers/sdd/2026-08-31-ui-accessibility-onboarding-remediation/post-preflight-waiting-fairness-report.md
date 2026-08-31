# Post-preflight waiting fairness report

Base: `74e4bc50dea9e39b1cb602d97ff1addf7329bb32` (clean tracked linked worktree)

## Scope and outcome

- Replaced the one-row waiting poll with a short-session snapshot of all current
  waiting job IDs ordered by `waiting_since, id`.
- Added `_poll_waiting_job(job_id, poll_at)` to reload and detach each job,
  deployment, and connection independently before any Proxmox or Ansible work.
- Kept the existing cancellation, exact timeout, missing-target, no-IP,
  successful resume, and terminal resume-error transitions intact.
- A missing IP or failed IP probe now leaves only that row waiting; later rows in
  the same snapshot are still considered.
- An unexpected exception from one row is reported and isolated so later IDs are
  still processed.
- The worker checks for newly queued work before every later snapshot ID and
  yields immediately, preserving queued-job priority.
- Successful and failed rows leave `waiting`, so later polls cannot duplicate
  their Ansible resume.
- No schema, index, migration, cursor, worker-concurrency, or unrelated refactor
  was added. The supported architecture remains one worker.

## TDD evidence

Seven multi-wait regressions were added and run individually before the worker
changed. All seven failed for the intended head-of-line behavior:

- oldest missing IP / later ready stopped after probing only the oldest;
- oldest exact timeout / later ready never probed the ready row;
- three equal-age no-IP waits probed only one ID per call instead of all three in
  stable ID order;
- two ready waits required a second poll, exposing the missing same-call
  completion and duplicate-prevention contract;
- resume failure on A, IP-probe exception on A, and an unexpected timeout-row
  exception each prevented B from succeeding.

After the ordered snapshot and per-row helper were added, all seven passed.

Queued priority was a separate red/green cycle because the original one-row
implementation happened to stop after A. Against the first all-row
implementation, A enqueued work during its probe and B was incorrectly probed;
the test failed at the literal probe-order assertion. Adding the between-row
queued check made it pass while keeping the initial queued-work guard green.

## Fresh verification

- Focused `tests/test_wave37.py`: passed, including the existing cancellation
  race, exact-timeout, single-wait, initial queued-priority, restart, and
  lifecycle regressions plus all new multi-wait cases.
- Focused worker/test byte compilation: passed.
- Python wave scripts: `39/39` passed with `GOBLINDOCK_DEV=1`.
- JavaScript syntax contract: `20/20` passed (18 `web/*.js` files and both UI
  test scripts).
- UI behavior: Wave 37 and Wave 39 suites both passed.
- Python compileall for `app` and `tests`: passed.
- `git diff --check`: passed.

## Constraints and residuals

- No browser, server, page, screenshot, or in-app navigation tooling was used.
- The polling strategy intentionally snapshots all current waiting IDs once per
  idle poll and remains designed for the existing single-worker deployment.
- An unexpected per-row exception is printed and leaves any still-waiting row
  eligible for a later retry, matching the prior outer-loop retry behavior while
  no longer blocking the rest of the current snapshot.
- No known functional residual remains within the assigned fairness scope.
