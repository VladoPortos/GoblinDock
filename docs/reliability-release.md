# Reliability and recovery update

This update keeps GoblinDock's single-container FastAPI, SQLite, worker, and React architecture.

## Test the PR image

GitHub Actions runs all Python waves, all JavaScript behavior suites, and JavaScript syntax checks. The PR image builds only after validation passes. CI starts the container with an empty database and checks its health endpoint, then uploads `goblindock-test-image-<PR number>` as an artifact (14-day retention).

Download and unzip that artifact from the PR's CI run, then load its image:

```sh
docker load -i goblindock-test-image.tar.gz
```

The image tag is `goblindock:pr-<PR number>`. Use that tag in your test Compose configuration. Keep your existing stable secret key when testing with a copy of existing data. Back up the database before trying the upgrade; schema migrations run at startup. This PR does not publish a `latest` image. Normal main, beta, and release-tag publishing now depends on the same validation workflow.

## Changed behavior

- Configuration and disk resize wait for Proxmox task success. A failed or uncertain resize prevents boot from being reported successful.
- VM allocation checks both QEMU and LXC across the cluster. Existing VM operations resolve a migrated node, and absence is confirmed against Proxmox's cluster VM registry before releasing ownership.
- Successful boot requires completed cloud-init and, when using a generated recipe snippet, its successful result marker. Agent IP availability alone is insufficient. Builtin installer pipelines now propagate download and intermediate failures.
- A lost create response retains the VM identifier and IP allocation. Recovery requires explicit ownership confirmation before further VM mutations. Canceled deployments stop running VMs before deletion.
- Resuming post-boot configuration commits a running state first. Interrupted scripts fail visibly after restart and are never automatically replayed.
- Disabled sources pause inventory, waiting-job probes, image sync, and cleanup access.

## New controls

- **Check readiness** in Deploy inspects recipe inputs, references, source, cluster inventory, storage, image cache, and network availability without allocating addresses or changing a VM. Rebuild offers the same report before proceeding. The worker prepares its image and cloud-init snippet before removing the old VM.
- **Rebuild** defaults to the captured original recipe and saved answers. Current-template rebuild requires fresh answers and shows changed categories. Stable placement IDs keep answers associated with the correct block when a template is reordered. Original snapshots survive job-history pruning. Legacy deployments without a valid original snapshot require an explicit current-template choice.
- **Recovery** lists failures, the last phase, and the last accepted Proxmox task. Reconcile reads the current remote facts. Ownership confirmation is explicit. Configuration retry replays only captured post-boot Ansible tasks; it does not rerun first-boot cloud-init. Rebuild and cleanup remain separate explicit actions.
- **ISOs** shows cache freshness and recorded download/validation dates. Refresh stages a new validated file and retains previous files. Pinning requires a checksum and an operator-confirmed immutable source URL. Historical caches have unknown download dates; retained versions consume storage and can be managed in Proxmox.
- **Template export/import** uses a versioned JSON bundle, stable block placements, and explicit local image/location/network mappings. Export omits stored credential answers, and imports do not download image URLs from bundles. Imported templates are private; custom Ansible blocks retain their admin trust requirement. Review custom script source before sharing it.

## Infrastructure requirements and validation limits

The Proxmox token needs propagated `VM.Audit` on `/vms`, the permissions for the selected VM/storage actions, and guest-agent execution access for readiness checks. Guests must provide cloud-init and the QEMU guest agent; generated snippets install the agent. Native cloud-init paths need an image with the agent already available. Recipe snippets require the configured SSH key and snippet-capable storage.

Automated tests mock Proxmox and execute installer failure cases with fake commands. UI checks use a separate local database. A real Proxmox deploy, migration, resize, and recovery cycle still need to be exercised with the test image in your lab.
