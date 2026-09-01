# Task 1 report — migrate placed `b-ssh` recipes before pruning

## Implementation

- Added `_migrate_b_ssh_recipe(recipe: list[dict]) -> tuple[list[dict], bool]` in `app/seed.py`.
- The migration deep-copies the recipe and rewrites only well-formed placement dictionaries with exact `ref == "b-ssh"` and dictionary inputs.
- The placement reference changes to `b-user`; all placement, section, and unrelated metadata remains untouched.
- Inputs map to the unified contract exactly: legacy `sudo` becomes `nopasswd`; `sudoers` is enabled; the legacy sudo group, shell, home, password, public key, and SSH password-login values are retained/mapped.
- Ask values are filtered to valid `b-user` inputs and legacy `sudo` is renamed to `nopasswd`, preserving order.
- `seed_blocks()` migrates every persisted template in a dedicated transaction and commits repaired JSON before the separate obsolete-built-in pruning transaction. It does not depend on the legacy `Block` row still existing.

## TDD evidence

RED, before production changes:

```text
wsl.exe -e bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && . ../../.venv/bin/activate && GOBLINDOCK_DEV=1 pytest -q tests/test_wave38.py"
1 failed: expected placed["ref"] == "b-user", got "b-ssh"
```

GREEN, after the minimal implementation:

```text
wsl.exe -e bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && . ../../.venv/bin/activate && GOBLINDOCK_DEV=1 pytest -q tests/test_wave38.py"
1 passed (with the repository's existing passlib deprecation warning)
```

## Verification

```text
wsl.exe -e bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && . ../../.venv/bin/activate && GOBLINDOCK_DEV=1 pytest -q tests/test_wave10.py tests/test_wave17.py tests/test_wave24.py tests/test_wave33.py tests/test_wave38.py"
38 passed, 2 existing warnings

wsl.exe -e bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && . ../../.venv/bin/activate && python tests/test_wave38.py"
ALL WAVE 38 UNIT TESTS PASSED

wsl.exe -e bash -lc "cd /mnt/e/goblindock/.worktrees/end-to-end-review-remediation && . ../../.venv/bin/activate && ruff check app/seed.py tests/test_wave38.py"
All checks passed!

git diff --check
clean (only Git's line-ending warning for app/seed.py)
```

## Files

- Modified: `app/seed.py`
- Added: `tests/test_wave38.py`
- Waves 24 and 33 were run as required; their existing regression files required no edits.

## Idempotence audit

- A persisted template with no `b-ssh` `Block` row and one with the row present are both migrated.
- The legacy row is pruned after migration, while both templates retain repaired `b-user` placements.
- Placement IDs/names/metadata, section metadata, and unrelated placements are asserted unchanged.
- A second `seed_blocks()` call leaves both serialized recipes byte-for-byte unchanged.
- Already-migrated placements, malformed JSON/non-list recipes, malformed sections/blocks, and non-dictionary inputs are not rewritten.

## Self-review

- No first-admin setup or unrelated seed behavior changed.
- Migration is scoped to exact legacy references and does not touch custom blocks.
- The persisted-template test exercises the real database and seed path rather than source-text inspection or mocks.
- Commit created as `fix: migrate legacy ssh block placements before pruning`.
