<!-- Thanks for contributing to GoblinDock! -->

## What does this change?

<!-- A short description of the change and the motivation. Link any related issue. -->

Closes #

## Checklist

- [ ] The change is focused and described above
- [ ] `for t in tests/test_wave*.py; do GOBLINDOCK_DEV=1 .venv/bin/python "$t"; done` passes
- [ ] `node --check web/*.js` passes (frontend is vanilla `React.createElement`, no build step)
- [ ] No secrets, Proxmox tokens, or private/homelab IPs added to code, tests, or docs
- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
