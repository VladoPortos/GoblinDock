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

Focused commands:

```text
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_unknown_ref_less_block_masks_all_nonempty_inputs()'
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_public_blank_sensitive_fields_require_exact_ask_on_save_and_edit()'
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_cross_owner_blank_sensitive_without_ask_is_rejected_before_persistence()'
GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_cross_owner_malformed_snapshot_schemas_fail_before_persistence()'
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

## Fix Round 2

### Compatibility correction

- Added one shared `normalize_input_schema()` helper. It copies a list schema and turns
  the authoring-compatible omitted/null type into explicit `type: "text"`; it does not
  rewrite malformed field objects or explicit invalid types.
- Custom-block create and edit now normalize only after the authoritative linter accepts
  the schema, then persist the canonical schema.
- `build_execution_plan()` normalizes legacy stored schemas while constructing the
  detached block snapshot and before deriving sensitivity metadata and strictly
  validating the plan. The stored legacy `Block` row is not mutated.
- `seal_execution_plan()`, `open_execution_plan()`, and `_validate_plan()` remain strict.
  A directly supplied or authenticated snapshot that omits a type is still invalid.
- The cross-owner malformed-row matrix no longer treats a legacy stored omitted type as
  malformed. Missing/invalid names, misspelled types, and non-string types remain
  fail-closed cases with pre-persistence row-count assertions.

### Regressions

- `test_custom_block_create_and_edit_canonicalize_omitted_type` exercises the real API
  create and edit functions and asserts exact persisted schemas containing explicit
  text types.
- `test_same_owner_private_legacy_missing_type_schema_is_canonicalized_in_plan` directly
  inserts a legacy schema, deploys a same-owner private template, opens the sealed job
  snapshot, asserts the canonical schema, materializes it, and compiles `echo hello`.
- `test_authenticated_imported_plan_with_missing_type_is_rejected` proves both ordinary
  sealing and strict opening of independently authenticated ciphertext reject the
  missing-type snapshot.
- The existing malformed-schema admission test continues to assert unchanged
  Deployment/Job/IP counts for every remaining invalid schema.

### RED evidence

Exact commands run before production edits:

```text
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_custom_block_create_and_edit_canonicalize_omitted_type()'"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_same_owner_private_legacy_missing_type_schema_is_canonicalized_in_plan()'"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_authenticated_imported_plan_with_missing_type_is_rejected()'"
```

Exact results:

- create/edit exited 1 at the first persisted-schema equality assertion because the
  stored field had no `type` key;
- legacy deployment exited 1 with `HTTPException: 409: template execution plan is
  invalid`, originating in strict `_validate_plan()`;
- authenticated-import rejection exited 0, preserving the pre-existing fail-closed
  decoder behavior as a characterization control.

### Focused GREEN evidence

Exact complete commands (no abbreviated prefixes):

```text
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_custom_block_create_and_edit_canonicalize_omitted_type()'"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_same_owner_private_legacy_missing_type_schema_is_canonicalized_in_plan()'"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_authenticated_imported_plan_with_missing_type_is_rejected()'"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_cross_owner_malformed_snapshot_schemas_fail_before_persistence()'"
```

Result: every command exited 0 with no output.

### Required waves and static checks

Exact wave commands:

```text
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave6.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave9.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave10.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave26.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave34.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave36.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave37.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave38.py"
```

Exact results: all exited 0. Wave 6 printed `All 16 wave-6 tests passed.`; waves
9, 10, 26, 34, 36, 37, and 38 printed their `ALL WAVE ... UNIT TESTS PASSED`
sentinels. Wave 37 also printed its two expected, internally caught rebuild-abort
tracebacks before the pass sentinel.

Exact static commands:

```text
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && PYTHONPYCACHEPREFIX=/tmp/gd-task2-fix2-pycache /mnt/e/goblindock/.venv/bin/python -m compileall -q app tests"
git diff --check
rg -n "MALFORMED-SCHEMA-MUST-NOT-RUN|DO-NOT-ECHO|TOKEN-NOT-ECHO|AUTHOR-FALLBACK|IMPORTED-AUTHOR|UNKNOWN-BLOCK|DEPLOYER-PROVIDED|PRIVATE-AUTHOR|DEPLOY_PASSWORD|DEPLOY_TOKEN" app
```

Results: compileall exited 0 with no output; diff check exited 0 with only Git's
informational LF/CRLF warnings; production sentinel scan returned no matches (expected
`rg` exit 1).

### Non-disclosure, rollback, and self-review

- No new error includes a schema default, field value, deploy input, or secret. Strict
  imported-plan failures remain the fixed `invalid execution plan`/sanitized 409 path.
- The compatibility normalization operates on schema metadata only and never reads or
  copies a placed input value into an error.
- Missing-type authenticated plans are rejected before materialization. Explicit invalid
  types are not normalized, so misspellings and non-string types remain fail closed.
- The malformed cross-owner cases retain same-session before/after counts for
  Deployment, Job, and IpAllocation; wave 38 confirms all remain unchanged on rejection.
- The helper returns copied field dictionaries, avoiding mutation of API request objects
  or stored legacy rows. Only new API writes and sealed snapshots become canonical.
- The established linter remains the authority for accepted input names and types; no
  second type allowlist was introduced.
- Public-sensitive exact refs, deployer ask answers, unknown-block masking, and private
  author values remain covered and GREEN in wave 38. Easy first-admin setup is untouched.

### Fix Round 2 files

- `app/recipes.py`
- `app/api.py`
- `app/execution_plan.py`
- `tests/test_wave38.py`
- `.superpowers/sdd/2026-08-31-security-data-integrity-remediation/task-2-report.md`

## Fix Round 3

### Final-review Important addressed

`_validate_public_recipe_sensitive_inputs()` now runs the existing
`normalize_input_schema()` against the detached JSON-decoded stored schema before the
authoritative strict schema check and `validate_public_sensitive_inputs()`. This aligns
public create/edit with the accepted legacy implicit-text contract already used by
execution-plan construction.

The correction does not write the normalized schema back to the legacy `Block`, change
new block create/edit canonicalization, or normalize an authenticated execution plan.
Only omitted/null legacy field types become explicit text in the public validator's local
copy; blank, misspelled, unknown, and non-string explicit types remain invalid.

### TDD regressions and controls

- `test_public_create_and_edit_accept_legacy_omitted_text_type` directly inserts an
  owner-visible legacy custom block whose field omits `type`, then exercises real public
  create and edit, exact stored recipes, unchanged legacy schema, cross-owner deploy, and
  the sealed job snapshot's explicit `type: "text"`.
- `test_public_create_and_edit_accept_legacy_null_text_type` exercises the same API and
  snapshot path for the historically equivalent explicit `null` form.
- `test_public_legacy_explicit_invalid_types_remain_rejected_without_echo` proves
  `opaque`, `secrett`, list-shaped, and blank explicit types all return sanitized HTTP
  400 without inserting a Template.
- The cross-owner malformed-schema matrix now also includes explicit `opaque` and blank
  types and retains same-session Deployment/Job/IP rollback checks.
- Existing controls retained in the focused run cover password/secret literal and blank
  handling, unknown-block rejection and masking, and strict authenticated missing-type
  plan rejection.

### RED evidence

Exact commands run against unchanged production at `138cdb3` after adding only tests:

```text
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_public_create_and_edit_accept_legacy_omitted_text_type()'"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_public_create_and_edit_accept_legacy_null_text_type()'"
```

Both exited 1 at the real `api.save_template()` call. The omitted-type run ended with:

```text
fastapi.exceptions.HTTPException: 400: block 'c-w38-sensitive-aa4e32' is unavailable
```

The null-type run ended with:

```text
fastapi.exceptions.HTTPException: 400: block 'c-w38-sensitive-228685' is unavailable
```

The stack for both passed through `_validate_public_recipe_sensitive_inputs()` and
`validate_public_sensitive_inputs()`, confirming the missing local normalization was the
failure rather than test setup, visibility, or execution-plan decoding.

Pre-change security-control command:

```text
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_public_legacy_explicit_invalid_types_remain_rejected_without_echo(); t.test_authenticated_imported_plan_with_missing_type_is_rejected(); t.test_cross_owner_unknown_block_is_rejected_before_any_rows_are_inserted(); t.test_unknown_ref_less_block_masks_all_nonempty_inputs()'"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_cross_owner_malformed_snapshot_schemas_fail_before_persistence(); t.test_public_literal_is_rejected_without_echo(); t.test_public_blank_sensitive_fields_require_exact_ask_on_save_and_edit()'"
```

Both controls exited 0 with no output before the production edit.

### Focused GREEN evidence

Exact commands after the one production correction:

```text
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_public_create_and_edit_accept_legacy_omitted_text_type()'"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_public_create_and_edit_accept_legacy_null_text_type()'"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python -c 'from tests import test_wave38 as t; t.test_public_legacy_explicit_invalid_types_remain_rejected_without_echo(); t.test_public_literal_is_rejected_without_echo(); t.test_public_blank_sensitive_fields_require_exact_ask_on_save_and_edit(); t.test_cross_owner_malformed_snapshot_schemas_fail_before_persistence(); t.test_cross_owner_unknown_block_is_rejected_before_any_rows_are_inserted(); t.test_unknown_legacy_block_masks_all_nonempty_inputs(); t.test_unknown_ref_less_block_masks_all_nonempty_inputs(); t.test_authenticated_imported_plan_with_missing_type_is_rejected()'"
```

All three exited 0 with no output.

### Affected suite and required waves

Exact commands:

```text
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave9.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave10.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave26.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave34.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave36.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave37.py"
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && GOBLINDOCK_DEV=1 /mnt/e/goblindock/.venv/bin/python tests/test_wave38.py"
```

All exited 0. Waves 9, 10, 26, 34, 36, 37, and the full affected wave 38
printed their pass sentinels. Wave 37 also emitted its two expected, internally caught
rebuild-abort traces before `ALL WAVE 37 UNIT TESTS PASSED`.

### Compile, diff, and non-disclosure checks

Exact commands:

```text
wsl.exe --distribution Ubuntu-22.04 -- bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && PYTHONPYCACHEPREFIX=/tmp/gd-task2-fix3-pycache /mnt/e/goblindock/.venv/bin/python -m compileall -q app tests"
git diff --check
rg -n "PUBLIC-CREATE|PUBLIC-EDIT|INVALID-TYPE-LITERAL|MALFORMED-SCHEMA-MUST-NOT-RUN|DO-NOT-ECHO|TOKEN-NOT-ECHO|AUTHOR-FALLBACK|IMPORTED-AUTHOR|UNKNOWN-BLOCK|DEPLOYER-PROVIDED|PRIVATE-AUTHOR|DEPLOY_PASSWORD|DEPLOY_TOKEN" app
```

Compileall exited 0 with no output. Diff check exited 0 with only Git's informational
LF/CRLF warnings. The production literal scan returned no matches (expected `rg` exit 1).

### Non-disclosure, rollback, and self-review

- Public compatibility uses a detached decoded/copy-normalized schema. Exact assertions
  prove both omitted and null legacy `Block.input_schema_json` values remain unchanged
  after public create, edit, and cross-owner deployment.
- Recipe literals are stored exactly and appear in the accepted immutable plan, but no
  rejection detail echoes them. Invalid explicit-type tests assert both sanitized errors
  and unchanged Template counts.
- The existing authoritative `input_schema_problems(..., require_type=True)` still runs
  after normalization. Only `None`/omission takes the text default; `opaque`, `secrett`,
  list-shaped, and blank types never enter `schemas_by_ref`.
- Password/secret literals and blank non-ask values still fail the public boundary;
  cross-owner malformed plans still fail before Deployment/Job/IP insertion.
- Unknown references remain rejected in admission, and unknown/ref-less values remain
  fully masked for non-owners while empty values are preserved.
- `open_execution_plan()` was not modified and its independently authenticated
  missing-type regression remains GREEN. New custom-block create/edit canonicalization
  and private/same-owner plan behavior are unchanged.
- Mutation review: removing the new call fails both compatibility tests; normalizing the
  persisted row fails unchanged-schema assertions; weakening explicit-type or strict
  authenticated-plan validation fails the retained controls.

### Fix Round 3 files

- `app/api.py`
- `tests/test_wave38.py`
- `.superpowers/sdd/2026-08-31-security-data-integrity-remediation/task-2-report.md`
