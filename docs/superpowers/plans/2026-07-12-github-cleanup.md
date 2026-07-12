# GitHub Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve all open GitHub security findings and Dependabot pull requests, enable repository secret scanning, and leave `main` fully verified.

**Architecture:** Remove attacker-controlled content from connection-failure log records rather than attempting to escape arbitrary exception strings. Integrate each Dependabot commit unchanged, validate the combined dependency set locally, then push once and verify GitHub's security and workflow state.

**Tech Stack:** Python 3.12, FastAPI, SQLModel, buildless JavaScript, Docker, GitHub Actions, CodeQL, Dependabot.

## Global Constraints

- Do not expose connection hostnames, token data, or raw exception text in logs.
- Preserve the generic API error returned to clients.
- Keep Dependabot changes byte-for-byte equivalent to their reviewed pull-request diffs.
- Do not report completion until local tests and GitHub checks have finished successfully.

---

### Task 1: Prevent connection log injection

**Files:**
- Modify: `app/api.py:1741-1747,1829-1833`
- Modify: `tests/test_wave22.py`

**Interfaces:**
- Consumes: `probe_connection(ConnProbeBody, User, Session)` and `test_connection(int, User, Session)`.
- Produces: warning records containing only fixed text, numeric connection IDs, and exception class names.

- [ ] Add a regression test that submits CR/LF content through the probe host and raised exception and asserts neither value reaches the captured log record.
- [ ] Run `GOBLINDOCK_DEV=1 .venv/bin/python tests/test_wave22.py` and confirm the new assertion fails against the current logging call.
- [ ] Replace raw host, connection name, and exception logging with fixed context plus `type(e).__name__`.
- [ ] Rerun wave 22 and confirm it passes.

### Task 2: Integrate dependency updates

**Files:**
- Modify: `Dockerfile`
- Modify: `.github/workflows/*.yml`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: commits from Dependabot PRs 20, 23, and 24.
- Produces: updated pinned base image, actions, and Python dependency lock.

- [ ] Merge each Dependabot commit into the integration tree without rewriting its diff.
- [ ] Confirm `requirements.in` remains compatible with the regenerated lock.
- [ ] Install the hash-locked requirements in a clean environment to validate all artifacts and dependency resolution.

### Task 3: Verify, publish, and close GitHub findings

**Files:**
- Modify: GitHub repository security configuration.

**Interfaces:**
- Consumes: verified local `main` and GitHub REST/Actions APIs.
- Produces: updated `origin/main`, closed Dependabot PRs, closed CodeQL alert, and enabled secret scanning where supported.

- [ ] Run all `tests/test_wave*.py` files, JavaScript syntax checks, Python compilation, Docker build, and `git diff --check`.
- [ ] Commit the application fix and merge commits, then push `main`.
- [ ] Enable secret scanning and push protection through the repository API.
- [ ] Wait for CI, CodeQL, Trivy, Scorecard, and image publishing to complete.
- [ ] Confirm there are zero open Dependabot alerts, code-scanning alerts, secret-scanning alerts, ordinary issues, and Dependabot PRs.
