# Task 5 report — keyboard access for navigation and builder

## Outcome

Sidebar destinations and the Collapse/Expand control are native non-submitting buttons.
The active destination exposes `aria-current="page"`, collapsed destinations retain
accessible names, and keyboard focus has a visible outline.

Builder palette entries are native buttons while retaining drag/drop. Placed blocks
remain composite containers for their nested actions and now expose `role="button"`,
`tabIndex=0`, `aria-pressed`, and shared Enter/Space activation through
`window.BuilderUI.activatePlacedBlock`. Space prevents page scrolling; events from
nested controls do not select the parent block. Move, duplicate, remove, password,
custom-input removal, secret insertion, and generated-playbook close controls have
accessible names. Block actions remain visible during `focus-within` as well as hover
or selection.

The existing pointer-sized code-preview target and palette/dropzone drag/drop handlers
remain available. Decorative grip icons no longer masquerade as controls.

## Automated verification

- Focused `node tests/test_wave39_ui.js`: passed.
- All UI Node suites (`test_wave37_ui.js`, `test_wave39_ui.js`): passed.
- Syntax checks for all 18 authored `web/*.js` files: passed.
- All 39 `tests/test_wave*.py` scripts in fresh `.venv` processes with
  `GOBLINDOCK_DEV=1`: passed.
- `python -m compileall -q app tests`: passed.
- `git diff --check`: passed.
- Targeted source audit confirmed native sidebar/palette controls, active and collapsed
  navigation names, placed-block semantics and shared handlers, nested-action guards,
  retained pointer drag/drop, visible focus, and focus-within action disclosure.

## Deliberate acceptance exception and residuals

Live browser acceptance was deliberately skipped. Prior Codex browser-process attempts
crashed reproducibly at 18:12:05 and 18:34:01, so this task used automated headless
verification only and did not launch, navigate, inspect, or capture any browser/server
session. Keyboard-only browser acceptance therefore remains the sole unexecuted plan
check; no automated-test or source-audit residual was found.

Requested commit message: `fix: add keyboard access to navigation and builder`.
