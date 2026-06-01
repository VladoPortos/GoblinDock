# Security Policy

GoblinDock talks to your Proxmox cluster and stores API tokens and secrets, so we take
security seriously and appreciate responsible disclosure.

## Supported versions

The latest release and the `main` branch receive security fixes. Older versions do not.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's [private vulnerability reporting](https://github.com/VladoPortos/GoblinDock/security/advisories/new)
(the **Security → Report a vulnerability** button on the repository). Include:

- a description of the issue and its impact,
- steps to reproduce (a proof of concept if you have one),
- affected version / commit.

We aim to acknowledge a report within a few days and to ship a fix or mitigation as
quickly as is practical, crediting you unless you prefer to stay anonymous.

## Scope notes

GoblinDock is designed to sit **behind a TLS-terminating reverse proxy on a trusted
network**, not directly on the public internet. It ships with CSRF protection, RBAC,
per-account lockout, an SSRF allowlist on image downloads, at-rest encryption of stored
secrets, and a Proxmox VMID guard window. Findings that assume the app is exposed raw to
the internet are still welcome, but please note this intended deployment model.
