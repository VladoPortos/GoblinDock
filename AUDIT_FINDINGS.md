# Audit findings — remediation record

The 2026-08-31 end-to-end review left seven confirmed findings for a later
remediation pass. All seven are now fixed on this branch; each entry names the
enforcement point and the regression wave that pins the behaviour.

## Fixed — public custom templates can reference a deployer's secrets

Cross-owner admission now fails closed on any `{{ secrets.* }}` /
`{{ variable.* }}` reference in author-controlled text: block source templates,
non-sensitive schema defaults, and non-sensitive stored inputs
(`reject_cross_owner_hidden_references` in `app/recipes.py`, enforced at the
execution-plan chokepoint in `app/api.py`, covering deploy and rebuild). The
two legitimate carriers are untouched: a sensitive stored input holding exactly
one full deployer secret reference, and the deployer's own deploy-time answers.
Regression: `tests/test_wave48.py`.

## Fixed — literal sensitive inputs remain in plaintext database fields

Sensitive (password/secret) template inputs must now be ask-on-deploy or a
deployer secret reference for every template, not just public ones; startup
migrates legacy literals into the owner's encrypted Secret store and rewrites
the recipe to reference them (`migrate_template_literal_secrets` in
`app/seed.py`). Ask-on-deploy answers moved to the Fernet-encrypted
`deployments.deploy_inputs_enc` column; the upgrade migration encrypts existing
rows, blanks the plaintext, then drops the legacy column (`app/db.py`). Rebuild
and the worker's legacy-plan path decrypt fail-closed. Regression:
`tests/test_wave49.py`.

## Fixed — select answers are not checked against configured options

`_validate_deploy_inputs` retains the full field schema and rejects a select
answer that is not exactly one of the block's normalized `options`
(`app/api.py`). Regression: `tests/test_wave46.py`.

## Fixed — account menu focus does not enter the open menu

`UI.Menu` exposes `aria-haspopup`/`aria-expanded`/`aria-controls` on its
trigger, focuses the first enabled item on open, supports
ArrowUp/ArrowDown/Home/End/Escape, and restores trigger focus on close
(`web/ui.js`). Regression: `tests/test_wave39_ui.js`.

## Fixed — activity drawer lacks modal focus management

The activity drawer is a real `role="dialog"` with `aria-modal`, labelled
title, initial focus on its close control, a Tab/Shift+Tab focus trap, Escape
dismissal, and opener focus restoration (`web/shell.js`). Regression:
`tests/test_wave39_ui.js`.

## Fixed — unavailable Proxmox connections need an explicit disabled state

Connections carry a persisted `disabled` flag (Settings toggle, audited as
`connection.enable`/`connection.disable`), distinct from enabled-but-
unreachable. Disabling keeps the connection config and all VM records; its VMs
leave normal inventory and dashboards, nothing polls it (/state probes,
capacity, cached-image listings, worker reconciliation), and it is refused as
a target for deploys, rebuilds, destroys, power actions, consoles, snapshots,
image syncs, and template locations. A job still queued when its source is
disabled fails with a clear message instead of contacting it. Re-enabling
reconciles the previously known VMs without deleting or duplicating them.
Regression: `tests/test_wave50.py`.

## Fixed — missing upstream VMs need a local-only cleanup path

`POST /deployments/{id}/cleanup_local` (UI: "Clean up (local only)" on the
dashboard row menu and VM detail page, behind an explicit warning dialog)
removes only GoblinDock's record and its IP reservation — it never sends a
delete to Proxmox. When the source is reachable it first runs a read-only
inventory probe and refuses if the VM still exists (the normal Delete stays
upstream-first); on a disabled or unreachable source the cleanup proceeds when
confirmed, flagged as unverified. Every cleanup writes a `vm.cleanup_local`
audit entry an operator can tell apart from a completed upstream destroy.
Regression: `tests/test_wave50.py`.
