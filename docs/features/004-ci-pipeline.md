# [004] CI pipeline

Branch: `004-ci-pipeline`
Status: PR OPEN
Dependencies: 002, 003
PR: #4

## Objective

Every PR and every push to `dev`/`main` runs the full backend and frontend
quality suites automatically; the `backend` and `frontend` checks become
required status checks on both protected branches.

## Scope

- `.github/workflows/ci.yml` with two parallel jobs on Ubuntu:
  - `backend`: FFmpeg install → `uv sync` → `ruff format --check` →
    `ruff check` → `pyright` → `pytest`
  - `frontend`: Node 24 + npm cache → `npm ci` → `format:check` → `lint` →
    `typecheck` → `vitest run` → `vite build`
- Concurrency cancellation of superseded PR runs
- This feature document

## Out of scope

- GPU/model integration tier (separate, manually-triggered workflow — a later
  feature)
- Path filtering (both jobs always run, keeping required checks simple)
- Release automation (Phase 10)

## Acceptance criteria

- [ ] Both jobs pass on this PR after 002 and 003 are merged into `dev`
- [ ] Normal CI requires no CUDA, no GPU, no model downloads
- [ ] `backend` and `frontend` are added as required status checks on `dev`
      and `main` branch protection after merge

## Required tests

The workflow itself is the test: both jobs must be green on this PR (after
rebasing on a `dev` that contains 002 and 003).

## Notes / decisions

- Must merge **after** 002 and 003; opened as a draft PR until then.
- Check job names (`backend`, `frontend`) are stable identifiers referenced by
  branch protection — renaming them is a breaking process change.
