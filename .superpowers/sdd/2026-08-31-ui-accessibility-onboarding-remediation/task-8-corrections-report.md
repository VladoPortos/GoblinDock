# Task 8 verification corrections report

Base: `dc9857e85309bf61fb1306f9c80263d9292e3d62` (Task 7, clean tracked worktree)

## Scope and outcome

- `.github/workflows/ci.yml` now runs both dependency-free UI behavior suites:
  `tests/test_wave37_ui.js` and `tests/test_wave39_ui.js`.
- CI syntax-checks one fail-closed array containing all 18 authored `web/*.js` files
  and both UI test files. The job exits if that expected 20-file contract drifts.
- `tests/test_wave39.py` now contains a focused CI workflow source-contract regression.
- Wave 39 now authenticates a real non-admin through the mounted ASGI application and
  requests the exact `/api/state` endpoint in-process. It exercises the actual session
  middleware, authentication dependency, route filtering, and serializers without
  launching a page or server.
- No application production code changed. The existing endpoint behavior already met
  the required public-state contract.

## Red/green evidence

### CI correction

The new focused workflow regression was run before editing CI on the exact Task 7 base.
It failed for the intended reason:

```text
AssertionError: CI workflow is missing: the complete 18 web + 2 UI-test
syntax-check list, the fail-closed 20-file guard, the syntax-check loop,
the Wave 37 UI behavior suite, the Wave 39 UI behavior suite
```

After the workflow correction, the focused test passed:

```text
FOCUSED TASK 8 CONTRACTS PASSED
```

### Authenticated public-state correction

The endpoint regression was also run before any workflow or application change. It was
green on `dc9857e`, so this is honestly recorded as a characterization/coverage addition,
not a fabricated red and not a production fix.

The regression creates realistic admin/viewer users, a connection populated with host,
token, storage, snippet, SSH, network-topology and limit data, a public template owned by
the admin, password/secret schema defaults and recipe inputs, global and viewer secrets,
global and viewer variables, an image, network, and public palette block. It logs in the
viewer through `/api/auth/login`, preserves the signed session cookie, then requests
`/api/state` through the mounted application.

The response assertions prove:

- connection host/port/token/TLS/storage/bridge/snippet/SSH keys and sentinel values are
  absent;
- `USERS` is empty;
- the non-owner viewer receives `canEdit: false` and `canDelete: false`;
- password and token recipe inputs are `********`, schema defaults and all sensitive
  sentinels are absent, the viewer secret is masked, and admin secret/variable metadata
  is tenant-filtered;
- allowed state remains functional: viewer identity, public target sizing, public network
  selection, deployable template location/base, built-in palette data, the viewer's
  masked secret metadata, and the viewer's non-secret variable remain available.

Only the external Proxmox probe cache is replaced with an empty test double; the mounted
HTTP path, login/session behavior, dependency resolution, database queries, route logic,
and serializers are real.

## Fresh verification

- Focused Wave 39 corrections: passed.
- `tests/test_wave39.py`: passed.
- `tests/test_wave37_ui.js`: passed.
- `tests/test_wave39_ui.js`: passed.
- Python waves: `39/39` scripts passed with `GOBLINDOCK_DEV=1`.
- JavaScript syntax: `20/20` passed (18 web scripts + 2 UI suites).
- Python compileall: `app` and `tests` passed.
- CI workflow source audit: passed.
- `git diff --check`: passed.

The first all-wave invocation omitted the repository's required test-mode environment and
stopped in Wave 0 while loading configuration because `GOBLINDOCK_SECRET_KEY` was unset.
The complete matrix was immediately rerun with the established `GOBLINDOCK_DEV=1` test
setting and all 39 waves passed. This was an invocation/setup failure, not a regression.

## Constraints and residuals

- No in-app browser, browser tab, Node REPL, Playwright, CUA, page navigation, screenshot,
  dev-log, or launched server tooling was used.
- The first-admin setup body remains exactly email/name/password; no setup token or API
  contract changed.
- Real computed viewport geometry and physical focus traversal remain unverified because
  of the recorded Codex browser-process crash constraint. Rendered component/state/event
  tests, CSS source/order contracts, both UI suites, and the authenticated in-process
  endpoint regression are the safe substitutes for this correction.
