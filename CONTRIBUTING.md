# Contributing to GoblinDock

Thanks for your interest in improving GoblinDock! This is a small, pragmatic codebase —
contributions of all sizes are welcome.

## Project layout

- **`app/`** — FastAPI backend (`api.py`), a threaded job worker (`worker.py`), the
  Proxmox client (`proxmox.py`), SQLModel tables (`models.py`), and an APScheduler-based
  periodic task runner (`scheduler.py`). One SQLite file is the whole datastore.
- **`web/`** — the SPA. Plain `React.createElement` (aliased `h`) in vanilla `.js` files
  with **no build step**; React is vendored under `web/vendor/`.
- **`tests/`** — plain-Python test scripts (`test_wave*.py`), run directly (no pytest).

## Dev setup

```bash
# 1. create a venv and install the hash-locked deps
uv venv
uv pip install --require-hashes -r requirements.txt

# 2. run the test suite (each file is a standalone script)
for t in tests/test_wave*.py; do GOBLINDOCK_DEV=1 .venv/bin/python "$t"; done

# 3. syntax-check the frontend
for f in web/*.js; do node --check "$f"; done

# 4. run the app locally (auto-seeds a dev Proxmox connection if configured)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

## Conventions

- **Frontend:** match the surrounding `React.createElement` style — no JSX, no bundler,
  no new runtime dependencies. UI primitives live in `web/ui.js`.
- **Database migrations:** SQLite columns are added by hand in `app/db.py` `_migrate()`
  (no Alembic). Adding a model column? Add it to both `models.py` and the `adds` dict.
- **Dependencies:** edit `requirements.in`, then regenerate the lock with
  `uv pip compile requirements.in -o requirements.txt --generate-hashes`. The repo
  installs with `--require-hashes`, so every dependency must be pinned and hashed.
- **Tests:** add a `tests/test_*.py` with plain `assert`s and a `__main__` runner that
  prints `… OK`. Cover new backend logic; the CI runs every `test_wave*.py`.
- **Commits:** use [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:` …).

## Before opening a PR

- The CI (compile · unit tests · JS syntax · CodeQL · Trivy) must pass.
- Never commit secrets, Proxmox API tokens, or private/homelab IPs — not in code, tests,
  docs, or fixtures.
- Open an issue first for anything large so we can agree on the approach.

By contributing you agree that your contributions are licensed under the project's
[Apache 2.0 License](LICENSE).
