# End-to-End Review Remediation Design

## Goal

Correct the confirmed security, deployment, recovery, persistence, validation, and
user-interface defects from the August 2026 end-to-end review without replacing
GoblinDock's SQLite database, single background worker, or Proxmox integration model.

## Approved product decisions

- The current easy first-admin setup remains unchanged. Production setup does not gain a
  bootstrap token, extra prompt, or local-only restriction.
- GoblinDock remains a single-Uvicorn-worker application with one in-process job worker.
- SQLite remains the source of truth. No message broker, external queue, or new service is
  introduced.
- A job executes the template definition accepted when that job is queued. Later template
  or block edits affect later jobs only.
- A deployment is successful only after every requested cloud-init and Ansible phase has
  completed.
- GoblinDock retains a VM record and static-IP reservation until it confirms that the VM
  no longer exists.
- Connection limit value `0` continues to mean unlimited. The UI and stale comments will
  be corrected to match that established behavior.
- Public templates remain deployable across owners, but the template author's literal
  password or secret values must never cross that ownership boundary.
- The work stays focused on confirmed review findings. There is no broad breakup of
  `app/api.py`, `app/worker.py`, or the frontend.

## Architecture

The repair is divided into independently testable units that share the existing database
and worker. Deployment admission creates an immutable encrypted execution plan. The worker
uses that plan, persists any wait that must survive its current call stack, and reconciles
cleanup against Proxmox ground truth. Security and data-integrity fixes remain small,
purpose-specific changes around the existing flows. Frontend work exposes capabilities the
backend already supports and corrects misleading or inaccessible controls.

## Immutable execution plans

Add `execution_plan_enc` to `Job`, with the same idempotent SQLite column-migration pattern
used for other post-release fields. The value is Fernet-encrypted with the existing
application key and is never returned by API serializers.

Deployment and rebuild admission build the plan inside the same serialized transaction that
creates the job. It contains:

- the accepted recipe structure;
- the referenced blocks' phase, input schema, cloud-init template, and Ansible template;
- the sensitive-field names needed for cross-owner validation and later masking;
- the template owner and deployment owner identifiers;
- the already-validated ask-on-deploy answers needed for compilation.

The plan intentionally does not contain resolved values of `{{ secrets.* }}` or
`{{ vars.* }}` references. Those continue to resolve in the deployment owner's scope when
the worker executes. Literal values already stored in a private or self-owned template are
protected in the encrypted plan rather than duplicated in plaintext job context.

The worker must not reload mutable `Template.recipe_json` or current `Block` rows for a job
that has a plan. A compatibility path may build a plan for legacy queued jobs that predate
the column, but newly admitted jobs always require one.

## Public-template sensitive values and legacy migration

Before built-in pruning, seeding migrates every placed `b-ssh` reference to `b-user` using a
deterministic input map:

- `user`, `password`, `public_key`, and `ssh_password_login` retain their values;
- `shell` becomes `/bin/bash` and `home` remains empty;
- `groups` becomes `["sudo"]`, `sudoers` becomes true, and `nopasswd` receives the legacy
  `sudo` boolean, preserving group membership while honoring the old field's stated
  passwordless-sudo intent.

This migration is keyed by the recipe reference and therefore also repairs databases where
the `b-ssh` block row was already pruned by v2.5 or v2.6.

Saving a public template rejects a non-empty literal in an input whose schema type is
`password` or `secret`. Acceptable sensitive values are:

- an input marked ask-on-deploy; or
- a syntactically valid deployer-scoped `{{ secrets.NAME }}` reference.

Cross-owner deployment repeats the validation against the immutable execution plan. This
second check covers unsafe templates created by older versions or direct database imports.
Unknown or missing block references are rejected for cross-owner deployment. When a
non-owner views an unknown legacy block, serialization masks all of that placement's
non-empty inputs rather than risking plaintext disclosure.

## Cloud-init delivery preflight

The supported arbitrary recipe-delivery mechanism remains a node-side cloud-init snippet.
Before Proxmox creates/imports a VM, the worker:

1. selects and guards the VMID;
2. determines whether the execution plan needs cloud-init/Ansible bootstrap support;
3. builds the complete cloud-config, including the managed SSH key, guest agent, Python,
   console credential hash, and cloud-init blocks;
4. uploads the snippet using the configured node SSH key and snippet storage;
5. aborts without creating a VM if upload or delivery validation fails.

The native Proxmox cloud-init fallback remains available only for a plain deployment that
does not request recipe phases or a generated console credential. Requested recipe work is
never silently omitted.

The connection form exposes API port, snippet storage, SSH host, SSH user, and SSH key path.
The default key-path hint is `/run/secrets/pve_key`, matching the shipped Compose mount. The
backend remains authoritative: it reports an actionable preflight error if the path is
missing, unreadable, or cannot upload to the selected storage.

If a snippet was uploaded but later provisioning fails before a stable VM is created, the
normal reconciliation path also removes that known snippet when it is safe to do so.

## Post-boot waiting and resumption

If an execution plan contains Ansible blocks and neither a static address nor the guest
agent yields an address during the initial wait, the job enters raw status `waiting` and the
deployment remains `working`. The job phase explicitly says that it is waiting for the guest
IP; it is not marked successful or failed.

The idle worker periodically checks waiting jobs without blocking other queued jobs. When an
address appears, it resumes only the pending Ansible phase from the encrypted execution plan,
then persists the VM facts and completes the job. Cancellation while waiting follows the
same confirmed-cleanup rules as cancellation during provisioning.

A waiting job has a 30-minute deadline measured from entry into `waiting`. When that
deadline expires, it fails with a clear configuration error while preserving the surviving
VM record and any static-IP reservation. A rebuild creates a new execution plan and retries
the full deployment.

Worker startup treats `waiting` as durable work, not as an interrupted `running` job.

## VM ownership, cleanup, and restart recovery

Introduce deployment status `cleanup_pending`. It means GoblinDock attempted to remove a
partially created or canceled VM but could not confirm absence. While in this state:

- the deployment remains visible;
- its VMID, connection, node, and IP allocation remain owned;
- conflicting lifecycle requests are rejected;
- the idle worker retries cleanup at most once per minute;
- operators see the last cleanup error.

For failure, cancellation, and worker restart, reconciliation follows the same invariant:

1. no assigned VMID means no VM exists, so the unused allocation may be released;
2. a confirmed-absent VM permits snippet removal, deployment deletion where appropriate,
   and IP release;
3. a confirmed-present VM remains recorded with its allocation;
4. an unavailable or ambiguous Proxmox response fails closed as "possibly present" and
   retains ownership.

An ordinary failed deploy that leaves a VM present remains an error deployment rather than
being hidden. A canceled deploy whose cleanup cannot be confirmed becomes
`cleanup_pending`; it is deleted only after a later confirmation of absence.

Restart recovery probes the actual VM before deciding whether an interrupted deploy owns a
live resource. It does not release allocations merely because the job type was `deploy`.

## Lifecycle task coordination

Deployment admission's existing process lock becomes the shared lifecycle-admission lock.
Deploy, rebuild, and destroy check for queued, running, or waiting work on the same
deployment before inserting another job.

- duplicate destroy requests return the existing active destroy job;
- rebuild is rejected while any lifecycle operation is active;
- destroy is rejected while deploy/rebuild is active;
- cleanup-pending deployments cannot start another destructive operation.

Direct start, stop, and restart endpoints wait for the returned Proxmox UPID and return a
502 response when the task finishes unsuccessfully or times out. Worker rebuild/destroy
paths also wait for the stop UPID. Fixed sleeps are removed. Destroy remains idempotent when
Proxmox confirms that the VM is already absent.

## Console revocation and pump lifecycle

Console authorization remains unchanged at handshake, then gains a periodic authorization
task. Every three seconds it re-reads:

- the user and disabled/deleted state;
- the session epoch captured at handshake;
- the deployment and current ownership;
- the user's current role for admin access.

Revocation closes both the browser and Proxmox sockets before any later frame is relayed.
The two directional pump tasks and authorization task run under first-completion semantics:
when any side ends, the remaining tasks are canceled and both sockets are closed. This also
prevents a closed Proxmox console from leaving the browser frozen on an open WebSocket.

The first-admin setup and logout behavior are outside this change.

## Atomic login lockout

Persistent failed-login counting uses one atomic SQLite update rather than a Python
read-modify-write. The statement increments the counter and sets `locked_until` when the
threshold is reached without allowing concurrent stale writes to reduce the count.

The existing per-email/IP in-memory throttle, password policy, response text, successful
login reset, and lock duration remain unchanged.

## Atomic backups

`backup_now()` writes to a unique temporary filename outside the published backup glob. It
then:

1. closes the SQLite destination cleanly;
2. verifies `PRAGMA quick_check` returns `ok`;
3. applies owner-only permissions;
4. flushes the file and performs a best-effort directory flush where supported;
5. atomically replaces the final timestamped backup path;
6. rotates only after publication.

Every exception removes the temporary file. A failed backup never appears in listing or
rotation and never replaces a previously valid file.

## Static-network validation and allocation

A static pool requires both range endpoints. Validation rejects malformed addresses,
mixed address families, reversed/out-of-subnet ranges, the subnet's network address, the
IPv4 broadcast address, and the configured gateway.

The allocator defensively skips reserved addresses even for legacy rows that predate the
write-time validation. Static configuration omits the `gw` fragment when an intentionally
gateway-less network is used. Missing or exhausted static ranges produce an admission error
and never fall back to DHCP.

Serialized capacity reflects the usable validated range rather than treating an incomplete
static row like a full DHCP subnet.

## Backup, connection, image, and template UI

The admin connection dialog round-trips the new/existing operational fields without exposing
them to non-admin connection serializers. Its resource-limit copy says `0 = unlimited`.

Custom base-image create/edit dialogs include an optional checksum input, display the
inferred algorithm, provide basic client-side format feedback, and continue to rely on the
backend for authoritative validation.

Template serialization adds `canEdit` and `canDelete`. Public-template cards hide actions a
viewer cannot perform; backend ownership checks remain mandatory. No new Fork feature is
required by this repair.

Job serialization preserves `canceled` separately from `error`. Job detail and History
render a neutral Canceled state and reserve Failed for raw `failed` jobs.

## Accessibility and responsive layout

Sidebar destinations and collapse use native buttons or links, have visible focus, and mark
the active destination with `aria-current="page"`.

Builder palette items and placed blocks are keyboard focusable and support Enter/Space for
their primary action. Block actions become visible on `:focus-within` as well as hover.
Existing pointer drag/drop remains available; keyboard users can use the existing explicit
move/duplicate/remove controls after selecting a block.

Below the existing narrow-screen breakpoint:

- the sidebar becomes an overlay/drawer instead of consuming fixed content width;
- management tables receive an explicit horizontal scroll container;
- builder palette and inspector become switchable panels so the canvas retains usable
  width;
- primary actions remain reachable without clipped horizontal content.

## Starter-template onboarding

On startup and after the first connection is added, the system-owned `AI Dev Box` starter
template is backfilled only when its connection is missing. The backfill selects the first
available connection and a compatible default network. It never overwrites a non-null
operator selection or modifies a user-owned template with a coincidentally similar name.

## Error handling and observability

- Capability/preflight failures occur before VM creation whenever possible and name the
  missing connection setting.
- Waiting, cleanup-pending, canceled, and failed remain distinct raw states.
- External Proxmox ambiguity preserves ownership rather than assuming deletion.
- Public-template validation returns a concrete 400/409 response identifying the unsafe
  block input without echoing its value.
- Backup errors identify the failed operation but never expose database contents or secrets.
- Existing job/event logging remains redacted using the execution plan's sensitive metadata.

## Test strategy

Each repair starts with a failing regression test following the repository's direct-script
wave convention.

### Deployment and recovery tests

- a no-SSH-key recipe deploy fails before VM creation and never reports success;
- a plain recipe-free deploy may use the native fallback;
- missing DHCP IP enters waiting, later resumes Ansible, and survives restart;
- waiting timeout fails without losing the VM/IP identity;
- ordinary failure and restart retain a live VM's allocation;
- failed cancellation cleanup leaves a visible cleanup-pending deployment;
- confirmed cleanup removes the deployment and releases the allocation;
- queued jobs execute their admitted recipe/block snapshot after later edits;
- duplicate lifecycle requests create at most one active job;
- every start/stop/reboot/rebuild/destroy path observes Proxmox task exit status.

### Security and migration tests

- v2.4-style `b-ssh` recipes migrate to `b-user` without losing inputs;
- non-owner serialization never returns literals from missing/legacy blocks;
- cross-owner deployment rejects literal password and secret inputs;
- ask-on-deploy and deployer-scoped secret references continue to work;
- open serial and VNC sockets close after disable, delete, password reset, role change, or
  ownership loss;
- one closed pump direction closes the complete console;
- five synchronized login failures lock the account.

### Persistence, validation, and UI tests

- injected backup failure leaves no file under the published glob;
- a published backup passes SQLite quick-check and rotation;
- incomplete and reserved static ranges are rejected;
- the allocator skips reserved addresses on legacy data;
- zero limits display and behave as unlimited;
- canceled jobs render distinctly;
- unauthorized template actions are absent;
- checksum and SSH/snippet fields round-trip;
- keyboard-only navigation and builder selection work;
- dashboard, builder, VM detail, and Settings remain usable at narrow viewport sizes;
- adding the first connection backfills only the system starter template.

After focused red/green cycles, run every `tests/test_wave*.py` in a fresh process, all
authored JavaScript syntax checks, Python byte-compilation, both Compose configurations, a
Docker image build, and a disposable runtime/browser smoke test.

## Rollout and compatibility

- Database changes are additive and idempotent.
- Existing jobs without an encrypted plan use the compatibility path; new jobs always have
  one.
- Existing public templates remain stored, but unsafe cross-owner deployment is blocked
  until their owner replaces literal sensitive values.
- Legacy incomplete static networks remain visible to admins but cannot allocate unsafe
  addresses.
- Existing setup URLs and first-admin behavior remain unchanged by explicit user decision.

## Scope exclusions

- No first-admin bootstrap token or setup-flow restriction.
- No external queue, broker, or multi-process worker support.
- No database replacement or migration framework.
- No redesign of Proxmox authentication.
- No general-purpose template version-history UI.
- No new public-template Fork workflow.
- No unrelated refactor of large backend/frontend files.
