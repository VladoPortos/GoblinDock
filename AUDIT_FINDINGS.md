# Outstanding audit findings

These findings were confirmed during the 2026-08-31 end-to-end review but were
intentionally left for a later remediation pass. They are not release claims or
theoretical hardening ideas; each has a reachable application path.

## Important — public custom templates can reference a deployer's secrets

Cross-owner public templates may execute author-controlled custom block code while
secret references are resolved in the deployer's scope (`app/api.py`, `app/worker.py`,
`app/recipes.py`). A malicious public template can embed a secret reference directly
in its hidden block source and send the resolved value elsewhere when another user
deploys it.

Recommended fix: reject hidden secret references in cross-owner block source and
non-sensitive inputs, or require an explicit admin-reviewed/trusted publishing model
before custom code may execute for another user.

## Important — literal sensitive inputs remain in plaintext database fields

Literal password/secret template values can remain in `Template.recipe_json`, and
ask-on-deploy answers are stored in `Deployment.deploy_inputs_json` (`app/models.py`,
`app/api.py`, `app/serialize.py`). They therefore appear in the SQLite database and
its backups even though the execution-plan snapshot is encrypted.

Recommended fix: require template credentials to use Secret references, migrate
legacy literals into encrypted Secret records, and store rebuild-required deployment
answers in an encrypted column with an upgrade migration that clears plaintext rows.

## Important — select answers are not checked against configured options

`app/api.py` verifies that an ask-on-deploy select answer is a string but does not
require it to be one of the block schema's options. A crafted API request can persist
and execute an option the template author never configured.

Recommended fix: retain the complete field schema during deploy-input validation and
reject select values not exactly present in its normalized `options` list.

## Important — account menu focus does not enter the open menu

The portalled menu in `web/ui.js` leaves keyboard focus on its trigger. Tab order can
traverse unrelated page controls before reaching the newly opened menu.

Recommended fix: expose `aria-haspopup`/`aria-expanded`, focus the first enabled item,
support ArrowUp/ArrowDown/Home/End/Escape, and restore trigger focus on close.

## Important — activity drawer lacks modal focus management

The activity overlay in `web/shell.js` does not move or trap focus, close on Escape,
advertise dialog semantics, or restore focus to its opener. Keyboard users can move
through background controls while the drawer is open.

Recommended fix: implement dialog focus entry/trapping, Escape dismissal, and opener
focus restoration with regression coverage.

## Important — unavailable Proxmox connections need an explicit disabled state

An administrator may intentionally stop using a Proxmox source because it is
offline, under maintenance, retired, or otherwise unavailable. Connection settings
currently need a persistent enabled/disabled control so this situation is not
treated as an endless connection failure.

When a Proxmox connection is disabled:

- Keep its connection configuration and known VM records so it can be re-enabled
  without reconfiguration or data loss.
- Hide all VMs associated with that connection from normal VM inventory and
  dashboard views.
- Stop background inventory polling and other automatic operations against that
  connection.
- Do not offer the disabled connection or its nodes as targets for new operations.
- Make the disabled state visible in Settings, with a clear way to enable the
  connection again and refresh its inventory.

The disabled state must be distinct from a connection that is enabled but
temporarily unreachable. Re-enabling should not silently delete or duplicate its
previously known VMs; the first successful refresh should reconcile them.

Recommended fix: add a persisted enabled flag to Proxmox connections, enforce it in
polling and operation scheduling, and exclude associated VM records from normal
inventory responses while preserving them in the database.

## Important — missing upstream VMs need a local-only cleanup path

A VM may be deleted directly in Proxmox while GoblinDock still has a database record
for it. GoblinDock then shows the stale VM as offline. Using the normal Delete action
launches a Proxmox deletion for a VM that no longer exists, which fails and leaves
the stale local record in place.

The VM actions need an explicit **Clean up** or **Delete locally only** option with
these semantics:

- Never send a delete request to Proxmox; remove only GoblinDock's local VM record
  and the local relationships that prevent that record from disappearing.
- Keep the existing normal Delete action for VMs that still exist: delete upstream
  first and remove the local record only after upstream deletion succeeds.
- Clearly warn that local-only deletion does not delete a real VM from Proxmox.
- If Proxmox is reachable and confirms the VM is missing, present cleanup as the
  natural recovery action.
- If the Proxmox source is disabled or unreachable, still permit local-only cleanup
  when explicitly confirmed, but explain that GoblinDock cannot verify whether the
  VM still exists upstream.
- Record the local cleanup in activity/audit history so an administrator can tell it
  apart from a successful Proxmox deletion.

Recommended fix: add a dedicated local-only deletion endpoint and guarded UI action
rather than overloading normal deletion or treating every Proxmox error as proof that
the VM is gone. Add coverage for a confirmed upstream 404/not-found response, an
unreachable source, a disabled source, and a VM that still exists upstream.
