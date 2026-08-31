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

## Fix Round 1

### Findings addressed

1. Removed `_mask_recipe_passwords()`'s empty-ref-set early return. Missing and blank
   placement refs now take the same unknown-block branch as pruned refs: every truthy
   input is masked for a non-owner, empty strings are preserved, and owners retain their
   values.
2. Tightened sensitive blank handling. A blank/missing `password` or `secret` value is
   valid only when that exact field appears in the placement's `ask`; non-ask sensitive
   values require an exact deployer-scoped secret reference. Cross-owner ask fields still
   require an actual deployer answer, and literal deployer answers remain allowed.
3. Extracted `input_schema_problems()` from the existing block linter so one
   authoritative rule set validates field objects, names, duplicates, and allowed types.
   Public/admission/execution-plan callers use `require_type=True`; custom-block authoring
   retains its established omitted-type-as-text compatibility. Immutable plans now reject
   missing/invalid names, missing/non-string types, and unknown/misspelled types.

### Regressions added or corrected

- `tests/test_wave38.py`
  - real non-owner `template_dict()` coverage for missing and blank refs, including owner
    visibility and empty-string preservation;
  - public save coverage for blank `password` and `secret` without exact asks;
  - public edit coverage for blank password without ask;
  - cross-owner blank-without-ask 409 plus same-session Deployment/Job/IP counts;
  - authenticated API admission for five list-shaped malformed snapshot schemas:
    `secrett`, missing name, invalid name, missing type, and non-string type;
  - malformed-schema error non-disclosure and row-count invariants.
- `tests/test_wave10.py`: made the unrelated `b-user.public_key` field an exact deployer
  secret reference so the password-ask test remains a valid public template.
- `tests/test_wave37.py`: added explicit `text` types to the immutable-plan fixture's
  `command` and `hostname` fields.

### RED evidence

Each regression was executed in a fresh WSL process before production edits:

```text
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_unknown_ref_less_block_masks_all_nonempty_inputs()'
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_public_blank_sensitive_fields_require_exact_ask_on_save_and_edit()'
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_cross_owner_blank_sensitive_without_ask_is_rejected_before_persistence()'
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_cross_owner_malformed_snapshot_schemas_fail_before_persistence()'
```

Results:

- ref-less masking failed its literal expected-value assertion;
- public blank save failed with `AssertionError: expected HTTPException 400`;
- cross-owner blank admission failed with `AssertionError: expected HTTPException 409`;
- misspelled list-shaped schema admission failed with
  `AssertionError: expected HTTPException 409`.

These are the expected pre-fix failure modes. The existing public literal, exact-reference,
and deployer-answer tests stayed in the wave to guard allowed behavior.

### GREEN and compatibility evidence

Focused commands (same WSL worktree/runtime prefix as above):

```text
... -c 'from tests import test_wave38 as t; t.test_unknown_ref_less_block_masks_all_nonempty_inputs()'
... -c 'from tests import test_wave38 as t; t.test_public_blank_sensitive_fields_require_exact_ask_on_save_and_edit()'
... -c 'from tests import test_wave38 as t; t.test_cross_owner_blank_sensitive_without_ask_is_rejected_before_persistence()'
... -c 'from tests import test_wave38 as t; t.test_cross_owner_malformed_snapshot_schemas_fail_before_persistence()'
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave38.py
```

Result: focused tests exited 0; wave 38 printed `ALL WAVE 38 UNIT TESTS PASSED`.

Compatibility commands:

```text
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave6.py
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave9.py
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave10.py
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave26.py
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave34.py
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave36.py
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave37.py
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave38.py
```

Results: all scripts exited 0. Wave 6 reported all 16 tests passed; waves 9, 10, 26,
34, 36, 37, and 38 printed their pass sentinels. Wave 37 retained its expected
internally-caught rebuild failure traces before its pass sentinel.

Additional commands:

```text
PYTHONPYCACHEPREFIX=/tmp/gd-task2-fix1-pycache /mnt/e/goblindock/.venv/bin/python -m compileall -q app tests
git diff --check
rg -n "MALFORMED-SCHEMA-MUST-NOT-RUN|DO-NOT-ECHO|TOKEN-NOT-ECHO|AUTHOR-FALLBACK|IMPORTED-AUTHOR|UNKNOWN-BLOCK|DEPLOYER-PROVIDED|PRIVATE-AUTHOR" app
```

Results: compileall and diff check exited 0 (only Git's informational line-ending
warnings); the production sentinel scan returned no matches.

### Non-disclosure, rollback, and self-review

- Blank-sensitive failures use the existing fixed message format containing only block
  ref and field name; neither the other exact reference nor any input value is returned.
- Malformed snapshot schemas are rejected as generic invalid execution plans before
  `_validate_cross_owner_execution_plan()` can classify fields; no schema/default/input
  value reaches the client detail.
- All malformed-schema and blank-without-ask tests compare Deployment/Job/IP row counts
  in the same live session, proving no flushed or committed admission artifacts.
- Execution-plan schema validation and cross-owner admission both call the same strict
  shared validator; the public-save schema map also excludes anything it rejects.
- The shared allowed-type set remains the catalog/linter source of truth; no parallel
  security allowlist was introduced.
- Built-in schema audit found no missing names or types. Wave 6 confirms custom-block
  authoring compatibility, while corrected wave 37 fixtures document the stricter
  immutable snapshot contract.
- Private saves and same-owner execution remain outside the public/cross-owner sensitive
  value rule. Exact secret references and literal deployer ask answers remain GREEN.
- No setup/authentication behavior changed.

### Fix Round 1 files

- `app/recipes.py`
- `app/api.py`
- `app/execution_plan.py`
- `app/serialize.py`
- `tests/test_wave10.py`
- `tests/test_wave37.py`
- `tests/test_wave38.py`
- `.superpowers/sdd/2026-08-31-security-data-integrity-remediation/task-2-report.md`
