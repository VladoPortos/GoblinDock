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
