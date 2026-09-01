# Task 3 report — revoke and terminate live console bridges

## Status

Implemented periodic authorization for established serial and VNC bridges and replaced
the two-direction blocking gather with coordinated first-completion shutdown. Revoked
authorization and authorization-storage failures now close the browser with 4403, close
the Proxmox peer, cancel/gather all bridge tasks, and suppress frames surfaced after the
shared stopping state is set.

## Implementation

- Added `_CONSOLE_AUTH_INTERVAL_S = 3.0`.
- Added frozen `_ConsoleGrant(conn, deployment, user_id, session_epoch)`. The existing
  origin, session-epoch, owner/admin, deployment, and connection handshake guard still
  runs before WebSocket accept; it now returns the detached grant consumed by either
  console endpoint.
- Added `_console_grant_still_valid(grant)`. Every call opens a fresh `Session(engine)`
  and re-reads the user, disabled flag, current epoch, deployment, deployment owner, and
  current role. A missing row or any database exception returns false.
- Added repeat-safe `_close_console_pair(...)`, which attempts both browser and Proxmox
  closes even if either peer has already closed or raises.
- Changed `_pump_ws(..., grant)` to run browser-to-PVE, PVE-to-browser, and authorization
  monitor tasks under `asyncio.wait(..., FIRST_COMPLETED)`.
- Every direction sets the shared stopping event when it ends. The coordinator cancels
  all pending tasks, gathers every task with `return_exceptions=True`, and closes both
  sockets. Revocation sets stopping before pair close and retains browser close code 4403
  for the coordinator's idempotent final close.
- Both relay directions check stopping after their blocking receive/iteration and
  immediately before starting the peer send.
- Serial setup, VNC proxy/token setup, logout, and first-admin behavior were not changed.

## Tests added (`tests/test_wave38.py`)

The tests exercise the real `_pump_ws` coroutine for both serial (`prefer_bytes=False`)
and VNC (`prefer_bytes=True`) modes with real database rows and a test-only 0.01-second
authorization interval.

- disabled user;
- deleted user;
- session epoch increment;
- demotion of a non-owner admin;
- deployment ownership transfer;
- fresh-session database failure;
- PVE async iterator ending first;
- browser disconnecting first;
- frozen grant mutation rejection.

Every asynchronous start, close, and task-completion observation is independently
bounded. Revocation peers deliberately surface one buffered frame after cancellation;
assertions prove neither direction relays that frame after stopping is set. Every
revocation asserts browser 4403, both peer closes, pump completion, and empty relay
effects. First-completion cases assert both peers close and the pump finishes within
0.5 seconds.

## Baseline and RED evidence

The required baseline ran before test or production edits:

```powershell
$env:GOBLINDOCK_DEV='1'
& '.\.venv\Scripts\python.exe' tests/test_wave3.py
& '.\.venv\Scripts\python.exe' tests/test_wave32.py
& '.\.venv\Scripts\python.exe' tests/test_wave38.py
```

Result: all three commands exited 0 and printed their wave pass sentinels. Source tracing
then confirmed authorization existed only in `_ws_authorized_dep()` at handshake and
`_pump_ws()` awaited `asyncio.gather(browser_to_pve(), pve_to_browser())`.

After adding the bounded async tests, before production edits, the representative live
revocation command was:

```powershell
$env:GOBLINDOCK_DEV='1'
& '.\.venv\Scripts\python.exe' -c "import runpy; ns=runpy.run_path('tests/test_wave38.py'); ns['test_disabled_user_revokes_live_serial_and_vnc']()"
```

Result: exit 1 at the intended boundary:

```text
AssertionError: revoked live console was not closed within 0.5s
```

The representative first-completion command was:

```powershell
$env:GOBLINDOCK_DEV='1'
& '.\.venv\Scripts\python.exe' -c "import runpy; ns=runpy.run_path('tests/test_wave38.py'); ns['test_pve_iterator_ending_first_terminates_serial_and_vnc']()"
```

Result: exit 1 at the intended gather hang:

```text
AssertionError: pve first-completion left the opposite pump blocked
```

Both failed runs settled cleanly through the test helper's bounded forced cleanup; no
test process hung.

## GREEN and regression evidence

Focused Wave 38 after the minimal implementation, and again after strengthening the
test helpers to require the exact production interfaces:

```powershell
$env:GOBLINDOCK_DEV='1'
& '.\.venv\Scripts\python.exe' tests/test_wave38.py
```

Result on both runs: exit 0, `ALL WAVE 38 UNIT TESTS PASSED`.

Race repetition command:

```powershell
$env:GOBLINDOCK_DEV='1'
$runner = @'
import runpy
ns = runpy.run_path("tests/test_wave38.py")
names = [
    "test_disabled_user_revokes_live_serial_and_vnc",
    "test_deleted_user_revokes_live_serial_and_vnc",
    "test_epoch_change_revokes_live_serial_and_vnc",
    "test_admin_demotion_revokes_non_owner_live_serial_and_vnc",
    "test_ownership_transfer_revokes_live_serial_and_vnc",
    "test_console_authorization_db_error_fails_closed",
    "test_pve_iterator_ending_first_terminates_serial_and_vnc",
    "test_browser_disconnect_ending_first_terminates_serial_and_vnc",
]
for round_no in range(3):
    for name in names:
        ns[name]()
print("3 rounds x 8 live-console tests passed")
'@
& '.\.venv\Scripts\python.exe' -c $runner
```

Result: exit 0, `3 rounds x 8 live-console tests passed`.

Fresh required regression group:

```powershell
$env:GOBLINDOCK_DEV='1'
& '.\.venv\Scripts\python.exe' tests/test_wave3.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& '.\.venv\Scripts\python.exe' tests/test_wave32.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& '.\.venv\Scripts\python.exe' tests/test_wave38.py
```

Result: exit 0. Waves 3, 32, and 38 printed their pass sentinels. Repository search found
Wave 32's VNC proxy coverage as the only other directly affected console test; it is in
the required group above.

Static checks:

```powershell
git diff --check
& '.\.venv\Scripts\python.exe' -m compileall -q app tests/test_wave3.py tests/test_wave32.py tests/test_wave38.py
```

Result: exit 0; compileall was silent and diff check reported only Git's informational
LF-to-CRLF working-copy warnings. The environment does not install the optional `ruff`
module (`No module named ruff`), so it was not used as verification evidence.

## Close and post-revocation race audit

- The monitor writes close code 4403 and sets stopping before its first close await.
- A browser frame cannot cross after a cancellation-resistant `receive()` returns late:
  the stopping check is between payload selection and `pve.send()`.
- A PVE frame cannot cross after a cancellation-resistant `__anext__()` returns late:
  the stopping check is before byte conversion and the browser send.
- A send already begun before revocation is handled by concurrently closing both
  transports; no later received frame starts a new send after stopping.
- Every direction sets stopping in `finally`, including disconnect, normal iterator end,
  and swallowed transport errors.
- `FIRST_COMPLETED` observes any of the three tasks. The coordinator cancels pending
  tasks, gathers all tasks, and only then performs the final pair close.
- The authorization monitor exits immediately when another task sets stopping rather
  than sleeping for the remainder of the three-second interval.
- Pair close catches per-peer close errors, so an already-closed peer cannot prevent the
  other close. The auth monitor and coordinator may both call it safely.
- Endpoint `finally` blocks retain their pre-existing extra browser close. After a 4403
  pump close, that repeat close is caught and cannot replace the revocation path.

## Self-review

- Authorization uses only fresh database objects plus immutable grant identifiers; no
  request-scoped SQLModel object is reattached or trusted for current role/ownership.
- User absence, disabled state, epoch mismatch, deployment absence, demoted non-owner
  admin, transferred ownership, and database exceptions all converge on the same 4403
  fail-closed path.
- A demoted admin who is still the deployment owner remains authorized by the existing
  owner-or-admin rule; the regression deliberately uses a non-owner admin.
- A normal user whose deployment transfers away is revoked; an admin remains authorized,
  matching the existing handshake rule.
- The production interval remains exactly 3.0 seconds. Only tests patch it, and every
  helper restores it in `finally`.
- No setup, logout, credential, VNC token, proxy creation, or serial initialization logic
  changed.
- No unrelated production or test files changed.

## Files

- `app/api.py`
- `tests/test_wave38.py`
- `.superpowers/sdd/2026-08-31-security-data-integrity-remediation/task-3-report.md`

Commit message: `fix: revoke and terminate live console bridges`.
