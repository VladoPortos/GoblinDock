# Task 6 report — narrow layouts at 760px

## Outcome

The application now owns a closed-by-default mobile navigation drawer. The narrow
top bar exposes a native menu button targeting the stable labelled primary navigation;
the native scrim, Escape, and all route changes converge on the same close path.
Scrim and Escape closes restore focus to the menu toggle. Closed narrow navigation
uses `display: none`, open navigation ignores the desktop collapsed preference, and
the desktop collapse footer is hidden at the narrow breakpoint.

The builder now owns a Canvas-default mobile panel state and renders a narrow-only
Palette/Canvas/Inspector tab switcher. All three panes stay mounted, while CSS removes
inactive narrow panes from layout and focus order with `display: none`. Save remains in
the wrapping builder header outside the panes. Existing pointer drag/drop, placed-block
Enter/Space selection, focus disclosure, Move down/up, duplicate, remove, and all other
Task 5 action behavior remains covered unchanged.

Dashboard and the Secrets, Variables, Networks, Users, Audit, and Backups management
tables now have internal horizontal scroll wrappers. Settings keeps Connections as the
default and its Add connection path, while its six-section selector and both connection
form grids adapt at 375px. VM detail actions wrap, the two content columns stack, and
Delete VM is a named native non-submitting button.

The single effective 760px cascade now follows every affected base rule. Reduced-motion
coverage follows the responsive rules and also overrides inline animation declarations.
No first-admin, authentication, API, backend, model, data, dependency, or redesign work
was included.

## TDD evidence

- Added rendered-tree, state-transition, source-contract, and CSS-order assertions to
  `tests/test_wave39_ui.js` before production changes.
- RED: focused Wave 39 UI failed at the first intended missing behavior with
  `AssertionError: App must own closed-by-default mobile navigation state` against
  exact base `046099f9e2253d7381ef1a9590b12923dc9e2e64`.
- GREEN: focused Wave 39 UI passed after the minimal implementation.
- Existing Task 5 navigation, placed-block keyboard, nested action, pointer drag/drop,
  move-down dispatch/reorder, focus-visible, and focus-within assertions remain present
  and passing.

## Automated verification

- Focused `node tests/test_wave39_ui.js`: passed.
- Both UI Node suites (`test_wave37_ui.js`, `test_wave39_ui.js`): passed.
- Syntax checks for all 18 authored `web/*.js` files and both UI test files: passed.
- All 39 `tests/test_wave*.py` scripts in fresh `.venv` processes with
  `GOBLINDOCK_DEV=1`: passed.
- `.venv\Scripts\python.exe -m compileall -q app tests`: passed.
- `git diff --check`: passed.
- Targeted source audit confirmed the single close path and focus restoration, stable
  navigation target/labels, mobile `display` hiding, expanded open drawer, menu icon,
  semantic Canvas-default builder panels, retained Save/Deploy and Task 5 controls,
  seven table wrappers, Settings/default connection path and responsive grids, VM
  classes/Delete label, one late 760px media block, and reduced-motion inline override.

## Deliberate acceptance exception and residuals

Live browser acceptance was deliberately skipped under the binding Task 6 safety
constraint. Earlier browser-process attempts crashed Codex at 18:12:05 and 18:34:01,
so this task did not launch a server or use a browser, page navigation, screenshots,
Playwright, CUA, browser tabs, or development logs. The requested dependency-free
rendered/state/source/CSS/syntax verification replaced the 375x812 and 760x900 live
checks. Those live viewport checks remain the sole acceptance residual; no automated
test or source-audit residual was found.

Requested commit message: `fix: support narrow dashboard and builder layouts`.
