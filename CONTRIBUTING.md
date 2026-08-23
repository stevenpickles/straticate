# Contributing to Straticate

## Branching model

Two permanent branches:

| Branch | Role | Rules |
| --- | --- | --- |
| `main` | Production. Always represents a formally released version. | No direct development, no direct pushes, no force pushes, no deletion. Only formal release PRs merge here. Every merge receives an annotated semver tag (`v0.1.0`, …). Feature branches **never** target `main`. |
| `dev` | Integration. The next release under development. | No direct pushes, no force pushes, no deletion. All feature PRs target `dev`. CI must pass before merge. |

### Branch protection

Intended protection (documented here; configured in GitHub settings as
permissions allow):

- `main`: require PRs, require CI, require up-to-date branch, no force pushes,
  no deletions, stricter than `dev`.
- `dev`: require PRs, require CI (`backend` and `frontend` checks once feature
  004 lands), no force pushes, no deletions.

## Numbered feature branches

Every development unit gets a sequential, zero-padded, never-reused number:

```text
branch:  NNN-short-description        e.g. 008-job-manager
PR title: [NNN] Feature Name          e.g. [008] Implement asynchronous job manager
docs:    docs/features/NNN-*.md
```

Workflow:

1. Check [ROADMAP.md](ROADMAP.md): the feature must exist in the ledger with
   its dependencies MERGED (or explicitly contract-only). Allocate the next
   number if it doesn't exist — numbers are monotonically increasing.
2. Branch from up-to-date `dev`: `git switch dev && git pull && git switch -c NNN-short-description`.
3. Implement **only** that feature. Noticed an unrelated issue? Open a new
   numbered feature instead of fixing it in-branch.
4. Update `docs/features/NNN-*.md` and the ROADMAP ledger row in the same PR.
5. Run all quality checks (see [DEVELOPMENT.md](DEVELOPMENT.md)).
6. Open a PR targeting `dev`.

## Pull request requirements

PR body template:

```markdown
## Feature
[NNN] Feature Name

## Summary

## Changes

## Testing

## Acceptance Criteria
- [x] ...

## Known Limitations
```

Before merge, all of the following hold:

- build succeeds; tests pass; formatting, linting, and type checking pass
- acceptance criteria are satisfied
- relevant documentation is updated (including the ROADMAP ledger row)
- changes remain within feature scope

Merge style: **squash merge** into `dev`, commit title `[NNN] Feature Name`.

## Definition of Done

A numbered feature is complete only when: acceptance criteria met · tests exist
where appropriate and pass · formatting/linting/type-checking pass · public
interfaces documented · docs updated · no unrelated changes · PR targets `dev`
· ledger entry updated. **Working code alone is not done.**

## Shared contracts

API request/response shapes and WebSocket event payloads are shared contracts,
owned by the Pydantic schemas in `backend/src/straticate/schemas/` and the
generated OpenAPI document. Rules:

- Contracts are established (merged into `dev`) **before** dependent parallel
  work begins on both sides.
- Two branches must never independently redefine the same contract; features
  touching `schemas/` serialize with each other.
- Frontend work may proceed against documented contracts, generated types, and
  mocks — it does not wait for backend implementations.

## Release process

```text
feature branches → dev → release preparation → PR into main → tag vX.Y.Z
```

1. When `dev` reaches a release milestone, prepare the release (version bumps,
   changelog, docs) — directly on `dev` via a numbered feature, or on a
   `release/X.Y.Z` branch cut from `dev` if stabilization needs isolation.
2. Open a release PR `dev` (or `release/X.Y.Z`) → `main`.
3. After merge, create an **annotated** tag on `main`: `git tag -a vX.Y.Z -m "Straticate vX.Y.Z"`.
4. Any release-branch fixes are reconciled back into `dev`.
