# Contributing to Straticate

## Branching model

Two permanent branches:

| Branch | Role | Rules |
| --- | --- | --- |
| `main` | Production. Always represents a formally released version. | No direct development, no direct pushes, no force pushes, no deletion. Only formal release PRs merge here. Every merge receives an annotated semver tag (`v0.1.0`, …). Feature branches **never** target `main`. |
| `dev` | Integration. The next release under development. | No direct pushes, no force pushes, no deletion. All feature PRs target `dev`. CI must pass before merge. |

### Branch protection

Configured on GitHub, and verified against the API on 2026-08-26:

| | `main` | `dev` |
| --- | --- | --- |
| Pull request required | yes | yes |
| Required checks (strict) | `backend`, `frontend` | `backend`, `frontend` |
| Linear history required | no | yes |
| Force pushes / deletion | blocked | blocked |
| Applies to admins | **yes** | no |

Two of those are easy to misread. The `e2e` job runs on every pull request but
is **not** a required check on either branch, so a red `e2e` does not block a
merge — read it before you merge anyway. And `main` does not require linear
history, which means GitHub would permit a merge commit there; the project's
rule against one (below) is a convention this repository chooses, not something
the protection enforces.

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
Release PRs into `main` are the one exception — they rebase-merge; see
[Release process](#release-process).

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
feature branches → dev → release/vX.Y.Z → PR into main → tag vX.Y.Z → Release workflow
```

1. When `dev` reaches a release milestone, prepare the release (version bumps,
   changelog, docs) — directly on `dev` via a numbered feature, or on a
   `release/vX.Y.Z` branch cut from `dev` if stabilization needs isolation.
   Work on a release branch follows the same rules as any other: numbered
   feature branches, PRs, no direct pushes.
2. Open a release PR `dev` (or `release/vX.Y.Z`) → `main`.
3. **Rebase-merge it.** Not a merge commit, and not a squash — the reason is
   below.
4. Create an **annotated** tag on `main`'s new tip and push it:

   ```sh
   git switch main && git pull
   git tag -a vX.Y.Z -m "Straticate vX.Y.Z"
   git push origin vX.Y.Z
   ```

5. Pushing the tag runs [`.github/workflows/release.yml`](.github/workflows/release.yml),
   which publishes the GitHub Release. It refuses to publish unless the tag is
   annotated, the tagged commit is reachable from `main`, and the tag,
   `backend/pyproject.toml` and the `## [X.Y.Z]` heading in
   [CHANGELOG.md](CHANGELOG.md) all name the same version. The release notes
   are that changelog section verbatim — write it before you tag.
6. Any release-branch fixes are reconciled back into `dev`.

### Why release PRs rebase-merge

`main` began as a single commit that is an ancestor of `dev`, and no merge
commit ever joins the two, so `merge-base(main, dev)` does not advance when a
release lands. Under a squash merge the whole release collapses into one commit
that shares no ancestry with the commits it came from, and the *next* release
PR three-way-merges from that same stale base — every line the previous release
touched that the next one also touches comes back as a conflict to resolve by
hand.

A rebase merge replays `dev`'s commits onto `main` instead. They arrive with new
SHAs, but at the next release `git rebase` recognises them by patch-id and skips
them, so only genuinely new commits replay and the merge stays clean.

Two consequences worth knowing before you meet them:

- **The GitHub PR diff for a release PR looks enormous** — it is computed
  against that stale merge base, so it shows everything since the project
  began. That is cosmetic. Review the release from `git log dev ^main`.
- **The tag does not point at any commit on `dev`.** Rebasing rewrites the
  SHAs, so `main`'s tip has the same tree as `dev`'s tip but a different hash,
  and `git describe` on `dev` will not find the tag.
