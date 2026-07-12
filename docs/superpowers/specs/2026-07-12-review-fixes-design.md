# Security and State-Invariant Review Fixes

## Goal

Correct the substantiated security, provisioning, recovery, validation, and deletion defects found in the July 2026 senior-engineer review without changing GoblinDock's deployment architecture.

## Approved policy decisions

- Global secrets are fail-closed: a non-admin deployment owner cannot resolve them. User-scoped secrets continue to resolve for their owner. Admin-owned deployments may resolve global secrets.
- Destructive CRUD operations reject deletion when a live deployment, template, or queued/running job still references the resource. There is no implicit cascade.
- Public cross-owner templates remain an explicitly accepted trust model; this change does not redesign template publication.

## Design

### Secret ownership

Worker-side secret and variable lookup receives an explicit `allow_global` flag. Deploy and rebuild determine that flag from the deployment owner's current role, not from the user who queued the job. SSH public-key lookup, cloud-init compilation, sensitive-input collection, and Ansible compilation all use the deployment owner. `/api/state` only lists global secrets and variables to admins, matching the values a viewer can actually consume.

### Provisioning safety

VM creation cleanup distinguishes failure before task submission from failure after Proxmox accepted the create operation. A pre-submission failure, including a VMID collision, never destroys the selected VMID.

Connection ceilings remain authoritative in the worker. A zero connection limit is unlimited; nonzero values clamp the request. The global defaults are not substituted after the API has selected a real connection.

An exception starting `ansible-runner` is a job failure, not a warning followed by success.

### Transactions and recovery

Deployment admission is serialized by an in-process lock, consistent with the documented single-Uvicorn-worker deployment model. Quota check, deployment insertion, IP reservation, job insertion, and audit insertion commit together. Static-IP allocation flushes rather than commits so pool exhaustion rolls back the whole operation.

Startup recovery releases IP allocations only for interrupted initial deploys. Interrupted rebuild and destroy jobs retain their allocation, matching normal cancel/failure reconciliation.

### Validation and referential integrity

Template recipes are validated as a list of object sections containing a list of object block placements with string `ref` values. Save and edit share the validator. Serializers and recipe helpers remain defensive against malformed legacy rows.

Deletion guards cover:

- templates referenced by deployments;
- blocks referenced from any stored template recipe;
- base images, connections, and networks referenced by templates;
- connections referenced by queued or running jobs.

The API returns HTTP 409 with a concrete dependency message. Existing deployment guards remain in place.

## Error handling

Expected validation, quota, pool-exhaustion, and dependency conflicts return 4xx responses and leave no partial database state. External provisioning failures fail the job and retain the existing reconciliation behavior. Cleanup remains best-effort only after GoblinDock has evidence that it submitted the resource-creation task.

## Tests

Add one new plain-Python wave test file following the repository's direct-script convention. Tests must first reproduce each defect, then pass after implementation:

- normal users cannot resolve or enumerate global secrets;
- admin rebuilds resolve the deployment owner's values;
- create-call collision does not destroy the colliding VMID;
- zero connection ceilings remain unlimited in the worker;
- Ansible startup errors fail the phase;
- crash recovery retains rebuild/destroy IPs;
- malformed public recipes are rejected and legacy malformed recipes serialize safely;
- exhausted static pools leave no deployment or job;
- concurrent admission cannot exceed quota;
- referenced resources return 409 on deletion.

Run the new wave directly during red/green cycles, then run every `tests/test_wave*.py`, JavaScript syntax checks, and Python compilation.

## Scope exclusions

- No Alembic or schema migration.
- No redesign of the public-template trust model.
- No multi-process worker support; the application remains explicitly single-worker.
- No broad breakup of `app/api.py` or `app/worker.py`.
