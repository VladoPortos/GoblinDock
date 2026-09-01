# UI, Accessibility, and Onboarding Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose required deployment settings, align UI/API semantics, remove misleading actions/statuses, support keyboard and narrow-screen use, and make the starter template deployable after first connection setup.

**Architecture:** Keep the dependency-free React/IIFE frontend and existing REST state shape. Add small exported pure helpers for dependency-free Node regression tests, extend only admin serializers with operational connection fields, and use structural CSS classes for accessible responsive behavior. Backend authorization remains authoritative.

**Tech Stack:** Vanilla React UMD/IIFE JavaScript, CSS, FastAPI/SQLModel, Node built-in assertions, plain-Python `test_wave*.py` scripts, disposable in-app browser smoke tests.

**Spec:** `docs/superpowers/specs/2026-08-31-end-to-end-review-remediation-design.md`

## Global Constraints

- The current easy first-admin setup remains unchanged; no setup token, prompt, or restriction.
- Connection value `0` means unlimited in backend and UI.
- SSH/snippet details are admin-only and never enter public connection serialization.
- Canceled and failed remain distinct; only failed uses error presentation.
- Native semantic controls and visible focus are required; pointer behavior remains available.
- The responsive breakpoint remains 760px and introduces no new frontend dependency.
- Backend authorization/validation remains authoritative for every UI control.
- Production changes require a failing regression test or explicit failing browser acceptance check first.

## Dependency

Complete the deployment plan's cloud-init preflight before releasing the connection-form work. The form exposes the settings that preflight requires.

---

### Task 1: Round-trip operational connection fields and correct limit copy

**Files:**
- Modify: `app/serialize.py:368-414`
- Modify: `app/models.py:52-72`
- Modify: `app/api.py:1688-1720,2292-2332`
- Modify: `web/manage.js:240-341`
- Create: `tests/test_wave39.py`
- Create: `tests/test_wave39_ui.js`

**Interfaces:**
- Admin `connection_dict()` adds `sshHost`, `sshUser`, and `sshKeyPath`; public dict includes none of the host/token/storage/SSH fields.
- Exposes `window.ConnectionUI.connectionDraft(conn)` and `connectionPayload(draft, editing)` used by the actual form and Node test.

- [ ] **Step 1: Write failing serializer tests.** Full admin state round-trips port, snippet storage, SSH host/user/key path; public state has none of them.
- [ ] **Step 2: Write failing Node helper/copy tests.** Assert a draft round-trips exact snake-case payload and source contains `0 = unlimited` but not `0 = inherit global`.

```javascript
const draft = ConnectionUI.connectionDraft({
  port: 9443, snippetStorage: 'snippets', sshHost: 'ssh.example',
  sshUser: 'automation', sshKeyPath: '/run/secrets/pve_key'
});
const payload = ConnectionUI.connectionPayload(draft, true);
assert.equal(payload.port, 9443);
assert.equal(payload.snippet_storage, 'snippets');
assert.equal(payload.ssh_key_path, '/run/secrets/pve_key');
```

- [ ] **Step 3: Run `python tests/test_wave39.py` and `node tests/test_wave39_ui.js`; confirm missing keys/helpers and stale copy.**
- [ ] **Step 4: Implement shared mapping and fields.** Render API port, snippet storage, SSH host, SSH user, SSH key path; use `/run/secrets/pve_key` as placeholder, not forced value; include all create/edit payload fields and numeric nonnegative limits. Correct stale model/API comments only—do not change zero behavior.
- [ ] **Step 5: Run Python/Node tests and `node --check web/manage.js`; commit as `feat: expose connection delivery settings in admin UI`.**

### Task 2: Add validated image-checksum create/edit support

**Files:**
- Modify: `app/api.py:134-143,1274-1291,2707-2739`
- Modify: `app/serialize.py:246-255`
- Modify: `web/images.js:45-63`
- Modify: `tests/test_wave39.py`
- Modify: `tests/test_wave39_ui.js`

**Interfaces:**
- Produces `_clean_checksum(value: str) -> str`: blank or lowercase hexadecimal with length 32/40/64/96/128; otherwise HTTP 400.
- `base_image_dict()` returns raw checksum or empty string; UI provides its own display fallback.
- Exposes `window.ImageUI.checksumMeta(value) -> {normalized, valid, algorithm, message}`.

- [ ] **Step 1: Write failing Python validation/serialization tests.** Blank passes, uppercase SHA-256 normalizes, invalid hex/length/prefixed digest returns 400, and raw values serialize.
- [ ] **Step 2: Write failing Node tests.** Blank reports Optional; valid lengths report MD5/SHA-1/SHA-256/SHA-384/SHA-512; invalid digest sets `valid=false`.
- [ ] **Step 3: Run red tests and confirm helpers/field are absent.**
- [ ] **Step 4: Implement authoritative backend normalization in add/edit.** Add optional create/edit input, algorithm feedback, `aria-invalid`/`aria-describedby`, and prevent known-invalid client submission while retaining server authority.
- [ ] **Step 5: Run tests plus `node --check web/images.js`; commit as `feat: add validated base image checksums`.**

### Task 3: Serialize and honor template action capabilities

**Files:**
- Modify: `app/serialize.py:293-323`
- Modify: `web/extra.js:153-219`
- Modify: `tests/test_wave39.py`
- Modify: `tests/test_wave39_ui.js`

**Interfaces:**
- `template_dict()` adds `canEdit` and `canDelete`; delete requires authorization and no referencing deployment.
- Exposes `window.TemplateUI.templateActionFlags(template)`.

- [ ] **Step 1: Write failing owner/viewer/admin/referenced-template serializer tests.** Owner/admin can edit, non-owner cannot; referenced template cannot delete.
- [ ] **Step 2: Write failing Node flag tests and assert the rendered card omits unauthorized Edit/Delete controls while retaining Deploy.**
- [ ] **Step 3: Run red tests.**
- [ ] **Step 4: Add capabilities and gate controls.** Preserve backend ownership/reference checks and do not add Fork.
- [ ] **Step 5: Run tests plus `node --check web/extra.js`; commit as `feat: hide unauthorized template actions`.**

### Task 4: Preserve and render canceled jobs neutrally

**Files:**
- Modify: `app/serialize.py:41-43,174-243`
- Modify: `web/job.js:120-164`
- Modify: `web/history.js:62-85`
- Modify: `web/shell.js:104-150`
- Modify: `web/styles.css:235-255`
- Modify: `tests/test_wave39.py`
- Modify: `tests/test_wave39_ui.js`

**Interfaces:**
- `_UI_STATUS["canceled"] == "canceled"`; job brief/detail preserve raw status.
- Exposes `window.UI.jobPresentation(rawStatus)` returning exact label, badge class, dot class, and `failure` boolean.

- [ ] **Step 1: Write failing Python test.** Canceled brief/detail status equals canceled; failed remains error; raw status is present.
- [ ] **Step 2: Write failing Node test.** Canceled maps to `{label:'Canceled', badgeClass:'canceled', dotClass:'stopped', failure:false}` and failed maps to red failure.
- [ ] **Step 3: Run red tests.**
- [ ] **Step 4: Use the helper in job detail, History, and activity drawer.** Only raw failed renders failure banner/meter; add subdued `.badge.canceled` styling.
- [ ] **Step 5: Run tests/syntax; commit as `fix: present canceled jobs separately from failures`.**

### Task 5: Make sidebar and builder keyboard-accessible

**Files:**
- Modify: `web/shell.js:25-54`
- Modify: `web/builder.js:41-120`
- Modify: `web/styles.css:161-180,464-491`
- Modify: `tests/test_wave39_ui.js`

**Interfaces:**
- Sidebar destinations/collapse are native `button type="button"`; active page uses `aria-current="page"`.
- Placed blocks use `role="button"`, `tabIndex=0`, `aria-pressed`, and Enter/Space selection because they contain nested action buttons.
- Exposes `window.BuilderUI.activatePlacedBlock(event, select)` for shared runtime/test behavior.

- [ ] **Step 1: Extend the dependency-free fake React walker test.** Assert active sidebar button/current state, changing Collapse/Expand accessible name, keyboard Space invokes block selection/prevents default, icon-only actions have labels, and CSS has focus-visible/focus-within selectors.
- [ ] **Step 2: Run Node test and confirm div-only navigation/no keyboard handler.**
- [ ] **Step 3: Implement native semantics and focus styling.** Preserve pointer drag/drop; keep explicit move/duplicate/remove controls; reveal actions on focus-within as well as hover.
- [ ] **Step 4: Run Node tests and checks for shell/builder; perform keyboard-only browser acceptance from dashboard through saved template.**
- [ ] **Step 5: Commit as `fix: add keyboard access to navigation and builder`.**

### Task 6: Make narrow layouts usable at 760px

**Files:**
- Modify: `web/app.js:31-110`
- Modify: `web/shell.js:25-101`
- Modify: `web/icons.js:3-64`
- Modify: `web/builder.js:41-458`
- Modify: `web/dashboard.js:90-135`
- Modify: `web/vmdetail.js:337-428`
- Modify: `web/manage.js:124-630`
- Modify: `web/styles.css:117-205,331-340,454-491`
- Modify: `tests/test_wave39_ui.js`

**Interfaces:**
- `App` owns `mobileNavOpen`; `TopBar` opens it and navigation/scrim/Escape close it.
- Builder owns `mobilePanel: 'palette' | 'canvas' | 'inspector'`, default canvas.
- Adds structural classes `sidebar-scrim`, `mobile-nav-toggle`, `table-scroll`, `builder-workspace`, `builder-mobile-switcher`, `builder-palette`, `builder-canvas`, `builder-inspector`, `vm-detail-actions`, and `vm-detail-columns`.

- [ ] **Step 1: Write failing source-contract tests.** Require the new classes, the 760px media query, and `overflow-x:auto` on table scroll wrappers.
- [ ] **Step 2: Capture failing browser acceptance at 375x812.** Record sidebar consuming width, clipped Settings table, inaccessible builder panels/actions, and unstacked VM detail.
- [ ] **Step 3: Implement drawer/table/panel behavior.** Off-canvas sidebar with scrim/menu/Escape and no hidden focus targets; scroll wrappers for management/dashboard tables; builder panel switcher on narrow only; wrapping builder/action headers; stacked VM detail; reduced-motion guard.
- [ ] **Step 4: Run Node syntax/contracts.**
- [ ] **Step 5: Repeat browser acceptance at 375x812 and 760x900.** Dashboard Deploy remains visible; tables scroll inside cards; builder starts Canvas and all panel switches/Save work; VM details stack; Settings does not widen page.
- [ ] **Step 6: Commit as `fix: support narrow dashboard and builder layouts`.**

### Task 7: Backfill only the system starter template

**Files:**
- Modify: `app/seed.py:1237-1292`
- Modify: `app/api.py:1708-1727`
- Modify: `tests/test_wave39.py`

**Interfaces:**
- Produces `backfill_starter_template_location(session: Session) -> bool`.
- Selects exact `AI Dev Box` with `owner_id IS NULL`, first connection/network by ascending ID, changes only null connection, and leaves commit to caller.

- [ ] **Step 1: Write failing deterministic/system-only tests.** A user-owned same-name template is untouched; null system starter gets first connection/network; an existing operator location is never overwritten; endpoint adds default network before backfill.

```python
def test_starter_backfill_is_system_only():
    user_tpl, system_tpl, conn, net = _starter_fixture()
    assert seed.backfill_starter_template_location(_session()) is True
    assert _template(system_tpl).connection_id == conn
    assert _template(system_tpl).network_id == net
    assert _template(user_tpl).connection_id is None
```

- [ ] **Step 2: Run wave 39 and confirm helper is absent/current early return prevents backfill.**
- [ ] **Step 3: Implement helper/order.** Seed template definition, ensure default networks, then backfill. Invoke after `add_connection()` creates its default DHCP network. Preserve non-null connection/network choices.
- [ ] **Step 4: Run wave 39 and existing seed waves; commit as `fix: backfill system starter template location`.**

### Task 8: UI/onboarding verification

**Files:**
- Review: all files changed in Tasks 1-7

- [ ] **Step 1: Run `python tests/test_wave39.py` and `node tests/test_wave39_ui.js`.**
- [ ] **Step 2: Run `node --check` for every authored `web/*.js`, Python compileall, and `git diff --check`.**
- [ ] **Step 3: Run the complete browser acceptance at desktop, 760x900, and 375x812, including keyboard-only sidebar/builder use.**
- [ ] **Step 4: Inspect public `/state` to confirm no SSH/snippet details leak and inspect all visible template actions/status labels.**
- [ ] **Step 5: Commit only review-approved corrections with focused messages.**
