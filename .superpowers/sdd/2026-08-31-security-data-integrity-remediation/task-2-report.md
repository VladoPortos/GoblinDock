# Task 2 report — public-template sensitive-value boundary

## Status

Implemented the public save/edit and cross-owner deployment boundary with strict
schema-aware validation, immutable snapshot admission, fail-closed unknown-block
serialization, sanitized errors, and pre-persistence rollback guarantees.

## Implementation

- Added `recipes.is_deployer_secret_ref(value)` with a full-string
  `^\{\{\s*secrets\.[A-Za-z0-9_]+\s*\}\}$` match.
- Added `recipes.validate_public_sensitive_inputs(...)`:
  - sensitive schema types are exactly `password` and `secret`;
  - public stored values must be blank or an exact deployer-scoped secret reference;
  - cross-owner sensitive ask fields require an answer at the exact placement address;
  - deployer-provided ask answers may be literal;
  - missing schemas/refs fail closed when requested;
  - exceptions contain only block/field identity and generic remediation text.
- Public template create/edit now validates against the referenced block schemas before
  mutating or committing a `Template`.
- Expanded newly-built encrypted execution plans with `template_owner_id`,
  `deployment_owner_id`, and a schema-derived `sensitive_fields` map. Plan validation
  verifies that map against the immutable block snapshots. The existing `owner_id`
  alias and legacy five-key plan format remain accepted for worker/queued-plan
  compatibility.
- Added API `_validate_cross_owner_execution_plan(plan)` which reconstructs schemas only
  from the immutable block snapshots, rejects unknown refs, enforces deploy-answer
  provenance, and returns HTTP 409 without values.
- Deploy and rebuild now build, validate, and seal the execution plan before changing a
  deployment, creating a job, or allocating an IP. Unknown imported refs are translated
  to a sanitized 409.
- Non-owner serialization still masks schema-directed `password`/`secret` inputs for
  known blocks; unknown or unreadable block schemas now mask every truthy input.

## Tests added (`tests/test_wave38.py`)

- Public `password` and `secret` literals return 400 and are absent from error details.
- Blank ask-on-deploy sensitive fields and exact secret references save successfully;
  embedded/non-exact references fail.
- Public edit rejects literals while private save and same-owner imported deployment stay
  allowed.
- Cross-owner missing ask answers cannot fall back to stored author data.
- Directly imported public literals and unknown block refs return 409.
- Failed admission leaves `Deployment`, `Job`, and `IpAllocation` counts unchanged in the
  same live session.
- Literal deployer ask answers and exact deployer secret references pass cross-owner
  admission.
- Unknown legacy blocks mask all nonempty test inputs.

## RED evidence

Platform note: the worktree had no Windows `.venv`; the locked dependency set includes
Unix-only `uvloop`/`fcntl`. Tests were therefore run with the repository's documented WSL
Python 3.12 environment at `/mnt/e/goblindock/.venv/bin/python`.

Command group:

```text
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave26.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave34.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave36.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave38.py"
```

Result: waves 26, 34, and 36 passed. Wave 38 failed at the expected first boundary:

```text
AssertionError: expected HTTPException 400
```

The API had accepted the public `DO-NOT-ECHO` password.

Focused RED commands used the same WSL prefix and imported one wave-38 test per fresh
process, for example:

```text
... /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_cross_owner_missing_sensitive_ask_answer_cannot_fallback_to_author_value()'
... /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_cross_owner_imported_literal_is_rejected_before_any_rows_are_inserted()'
... /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_cross_owner_unknown_block_is_rejected_before_any_rows_are_inserted()'
... /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_unknown_legacy_block_masks_all_nonempty_inputs()'
```

Results, respectively: both unsafe known-block deployments failed because no 409 was
raised; the unknown ref escaped as `ValueError: invalid execution plan`; and the masking
literal assertion failed. These are the intended pre-fix failure modes.

## GREEN and regression evidence

Focused GREEN:

```text
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave38.py"
```

Result: exit 0, `ALL WAVE 38 UNIT TESTS PASSED`.

Required waves and the directly affected immutable-plan wave were run in fresh processes:

```text
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave9.py
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave10.py
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave26.py
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave34.py
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave36.py
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave37.py
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave38.py
```

Result: all seven scripts exited 0 and printed their `ALL WAVE ... PASSED` sentinel.
Wave 37 printed its expected internally-caught rebuild failure traces before its pass
sentinel.

Additional checks:

```text
PYTHONPYCACHEPREFIX=/tmp/gd-task2-pycache /mnt/e/goblindock/.venv/bin/python -m compileall -q app tests
git diff --check
rg -n "DO-NOT-ECHO|TOKEN-NOT-ECHO|AUTHOR-FALLBACK|IMPORTED-AUTHOR|UNKNOWN-BLOCK|DEPLOYER-PROVIDED|PRIVATE-AUTHOR" app
```

Result: compileall exit 0, diff check clean apart from Git's informational line-ending
warnings, and no production sentinel match. `PYTHONPYCACHEPREFIX` avoids a pre-existing
Docker-owned worktree `app/__pycache__` directory that WSL cannot replace.

## Files

- `app/recipes.py`
- `app/api.py`
- `app/serialize.py`
- `app/execution_plan.py`
- `tests/test_wave38.py`
- `.superpowers/sdd/2026-08-31-security-data-integrity-remediation/task-2-report.md`

## Non-disclosure and rollback audit

- Every new client-visible validation message is constructed only from a block reference,
  field name, or fixed generic text. No submitted/stored input value is interpolated.
- Exact secret-reference tests assert even the reference name is absent on rejection.
- Production-source search found none of the regression sentinel values.
- Cross-owner validation consumes `plan["blocks"][ref]["input_schema_json"]`, not current
  mutable block rows.
- New-deploy ordering is: visibility/base/connection checks, deploy-input validation,
  build snapshot, cross-owner validation, encryption, sizing/network selection, then
  `Deployment.flush`, IP allocation, `Job`, audit, and commit.
- Rebuild validates/seals before changing deployment status or creating a rebuild job.
- Live-session count assertions prove rejected admissions do not even leave uncommitted
  Deployment/Job/IP rows.

## Self-review

- No first-admin setup/authentication code changed.
- Public create and edit enforce the same boundary; changing an unsafe public template to
  private remains possible because private recipes retain owner behavior.
- Same-owner execution is deliberately exempt from the cross-owner boundary; private
  author literals continue to work.
- Sensitive ask answers are accepted only when present at their exact block address; a
  stored author value never satisfies the cross-owner requirement.
- Exact full-string matching prevents prefix/suffix text or invalid secret names from
  being treated as deployer references.
- Unknown refs fail before persistence and unknown schemas fail closed in serialization.
- New plan sensitivity metadata is recomputed and checked during plan validation, so it
  cannot omit a snapshot-sensitive field without invalidating the plan.
- Legacy encrypted plans remain readable, and the established worker owner lookup remains
  unchanged through the `owner_id == deployment_owner_id` invariant.
- No unrelated product behavior or files were changed.
