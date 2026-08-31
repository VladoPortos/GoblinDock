# Task 4 report — atomic persistent login lockout

## Status

Implemented an atomic per-account failed-login update while preserving the existing
login/setup/logout API contracts, five-failure threshold, 15-minute lock duration,
disabled-user behavior, successful-login reset, SQLite engine, and single-worker model.

## Implementation

- Added `_record_account_login_failure(session, user_id, now) -> None`.
- The helper emits one SQLAlchemy `UPDATE users ... WHERE users.id = user_id`.
- `coalesce(users.failed_logins, 0) + 1` computes the next count in the database,
  avoiding request-session stale-object read/modify/write races.
- Two `case` expressions use that same next-count expression. Below the threshold the
  counter increments and the existing `locked_until` value is retained. At the fifth
  failure the counter resets to zero and `locked_until` becomes `now + 15 minutes` in
  the same statement.
- The statement has `synchronize_session=False`; the helper performs exactly one commit.
- The invalid-credential branch still records the same IP-throttle attempt and returns
  the same HTTP 401. Missing and disabled users still do not update an account row.

## Tests added (`tests/test_wave38.py`)

- `test_first_admin_setup_contract_is_unchanged` creates the first administrator by
  directly constructing only `SetupBody(email, name, password)`. It proves no bootstrap
  token or new production guard was added and asserts the established authenticated
  setup result/session.
- `test_five_concurrent_failures_lock_account` creates five threads, five distinct
  request IPs, and five separate `Session(engine)` instances. Patched password
  verification waits at a five-party barrier, ensuring every request has loaded the
  same pre-failure account state before any request can write. Every request must retain
  the existing 401 result, and the persisted lock deadline must be in the future.
- The concurrency test runs three rounds per invocation. Thread barrier and joins are
  bounded; a stuck attempt fails with a specific five-second assertion.

## Baseline evidence

The worktree was clean and already isolated at required base
`f301bec481ea6051ebd8e209ad620af273bdb015` on
`codex/end-to-end-review-remediation`.

Before test or production edits:

```powershell
$env:GOBLINDOCK_DEV='1'
& '.\.venv\Scripts\python.exe' tests/test_wave3.py
& '.\.venv\Scripts\python.exe' tests/test_wave30.py
& '.\.venv\Scripts\python.exe' tests/test_wave38.py
```

Result: all commands exited 0. Waves 3, 30, and 38 printed their pass sentinels.

## Characterization and RED evidence

The setup characterization was run alone before the production edit:

```powershell
$env:GOBLINDOCK_DEV='1'
& '.\.venv\Scripts\python.exe' -c "import runpy; ns = runpy.run_path('tests/test_wave38.py', run_name='wave38_characterization'); ns['test_first_admin_setup_contract_is_unchanged'](); print('test_first_admin_setup_contract_is_unchanged PASSED')"
```

Result: exit 0, `test_first_admin_setup_contract_is_unchanged PASSED`.

The new concurrent regression was then run alone against unchanged production code:

```powershell
$env:GOBLINDOCK_DEV='1'
& '.\.venv\Scripts\python.exe' -c "import runpy; ns = runpy.run_path('tests/test_wave38.py', run_name='wave38_red'); ns['test_five_concurrent_failures_lock_account']()"
```

Result: exit 1 at the intended persistent-state assertion:

```text
AssertionError: five concurrent failures left failed_logins=1 and locked_until=None
```

The complete Wave 38 command also exited 1 with the identical assertion. This proves
the pre-fix ORM read/modify/write path lost four of five updates rather than failing due
to a test typo, timeout, throttle response, or SQLite exception.

## GREEN and race-repetition evidence

Focused GREEN after the minimal production change:

```powershell
$env:GOBLINDOCK_DEV='1'
& '.\.venv\Scripts\python.exe' -c "import runpy; ns = runpy.run_path('tests/test_wave38.py', run_name='wave38_green'); ns['test_first_admin_setup_contract_is_unchanged'](); ns['test_five_concurrent_failures_lock_account'](); print('setup characterization + 3 concurrent lockout rounds PASSED')"
```

Result: exit 0 with the printed pass message.

Additional flake detection repeated the three-round regression ten times:

```powershell
$env:GOBLINDOCK_DEV='1'
& '.\.venv\Scripts\python.exe' -c "import runpy; ns = runpy.run_path('tests/test_wave38.py', run_name='wave38_repeat'); [ns['test_five_concurrent_failures_lock_account']() for _ in range(10)]; print('10 repetitions x 3 rounds x 5 concurrent failures PASSED')"
```

Result: exit 0, covering 30 synchronized rounds and 150 failed-login requests.

## Compatibility checks

A focused real-endpoint probe ran setup, four sequential failures, the fifth threshold
failure, a locked sixth attempt, successful login after an expired lock, disabled-user
login, and logout. It asserted:

- setup succeeds with only the three established body fields;
- failures one through four persist counts 1–4 without a lock;
- failure five returns 401, resets the count to zero, and creates a deadline between 14
  and 15 minutes in the future;
- the next attempt returns the established 429 while the lock is active;
- successful login clears both failed count and expired lock and stamps the session;
- disabled-user login remains 401 and does not increment or lock the account;
- logout returns `{"ok": true}` and clears the session.

The probe exited 0 and printed:

```text
setup, 5-failure threshold, 15-minute duration, reset, disabled-user, and logout checks PASSED
```

No endpoint model, route signature, response detail, setup code, logout code, success
branch, throttle limit, database configuration, or worker configuration changed.

## Required waves and static checks

Fresh final command group:

```powershell
$env:GOBLINDOCK_DEV='1'
& '.\.venv\Scripts\python.exe' tests/test_wave3.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& '.\.venv\Scripts\python.exe' tests/test_wave30.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& '.\.venv\Scripts\python.exe' tests/test_wave38.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& '.\.venv\Scripts\python.exe' -m compileall -q app tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
git diff --check
```

Result: exit 0. Waves 3, 30, and 38 printed their pass sentinels; compileall was silent;
diff check reported only Git's informational LF-to-CRLF working-copy warnings.

### Broader wave sweep

As an additional completion check, every `tests/test_wave*.py` script was run in numeric
order in a fresh process. Waves 0–28 passed under the Windows virtual environment.
Wave 29's POSIX owner-mode assertion reported `0666` rather than `0600` on Windows; the
same unchanged Wave 29 was rerun in the repository's documented Linux environment:

```powershell
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave29.py"
```

Result: exit 0, `ALL WAVE 29 UNIT TESTS PASSED`, including both backup and database
owner-only mode checks. Waves 30–38 then all exited 0 under the Windows environment.
The Windows Wave 29 result is a filesystem mode-bit platform artifact, not a product
failure or a login-lockout regression; no out-of-scope backup change was made.

## Self-review

- Mutation check: restoring the ORM assignment path makes the deterministic regression
  fail with the captured `failed_logins=1`/no-lock result.
- Every persisted failure transition derives from the row value held under SQLite's
  serialized write, so concurrent requests cannot overwrite another request's count.
- Both threshold effects are in the same statement; there is no observable state with a
  reset count but missing deadline, or a deadline with an unreset count.
- The helper receives the attempt time explicitly, keeping the deadline deterministic
  within each request and avoiding a database-specific datetime function.
- `synchronize_session=False` is safe because the invalid-login path raises immediately
  after the helper and does not inspect the stale request-scoped object. Commit expiry
  also prevents later accidental reliance on its loaded values.
- The active-lock check still runs before password verification and account failure
  recording. A disabled user still short-circuits account mutation. A successful login
  still uses the existing ORM reset and session-stamping path.
- The setup characterization runs first in Wave 38 before any helper creates users, so
  it exercises the genuine empty-database first-admin path.
- Only the required API file, Wave 38 tests, and this report are in scope.

## Files

- `app/api.py`
- `tests/test_wave38.py`
- `.superpowers/sdd/2026-08-31-security-data-integrity-remediation/task-4-report.md`

Commit message: `fix: update login lockout counters atomically`.
