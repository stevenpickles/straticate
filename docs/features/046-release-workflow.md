# [046] Release workflow and release-process corrections

Branch: `046-release-workflow`
Status: MERGED
Dependencies: 043
PR: #65

## Objective

Pushing an annotated `vX.Y.Z` tag to `main` publishes the GitHub Release, with
the matching `CHANGELOG.md` section as its notes, and refuses to publish if the
tag, the packaged version and the changelog disagree. Before this feature the
repository had one workflow (`ci.yml`), triggered only by branch pushes and
pull requests: tagging `main` ran nothing, and `CHANGELOG.md`'s own
`[0.1.0]: …/releases/tag/v0.1.0` link pointed at a release that would never
exist.

## Scope

- `.github/workflows/release.yml` — `on: push: tags: ['v*']`, one job, four
  guards then `gh release create`.
- `.github/scripts/release_notes.py` — extracts one release's section from
  `CHANGELOG.md` and verifies the version against `backend/pyproject.toml`.
- `.github/workflows/ci.yml` — one trigger change, so release branches are
  tested (below).
- `CONTRIBUTING.md` — the release process, corrected (below).
- `ROADMAP.md` — this feature's ledger row.

### Why `ci.yml` changed

Its triggers were `branches: [dev, main]` for both `pull_request` and `push`,
so **a pull request into `release/vX.Y.Z` reported no checks at all** — the
release branch's content would have been tested for the first time by the
release PR into `main`. Adding `release/**` to both lists makes release-branch
work verified exactly like `dev` work, and this feature's own PR is the first
to rely on it. Tags are deliberately not added: the tag push publishes the
release, and points at a commit the pipeline has already passed on.

### What was wrong in `CONTRIBUTING.md`

1. **Branch protection was aspirational.** The section said "intended
   protection … as permissions allow" and listed `main` as requiring an
   up-to-date branch and being "stricter than `dev`". Checked against the API:
   `main` requires PRs, the strict `backend`/`frontend` checks, and
   `enforce_admins`, but **not** linear history — while `dev` **does** require
   linear history. So `main` is stricter in one respect and looser in another.
   Replaced with a table of what is actually configured.
2. **`e2e` was never mentioned.** It runs on every PR and is not a required
   check on either branch. Someone reading the old text would reasonably assume
   a red `e2e` blocks a merge. It does not.
3. **The release branch name lacked its `v`.** The document said
   `release/X.Y.Z`; the branch cut for this release is `release/v0.1.0`, and
   the tag is `vX.Y.Z`. Aligned on `release/vX.Y.Z`.
4. **No merge method was stated for release PRs**, while the same document
   states "squash merge" for feature PRs — so the obvious reading was that
   release PRs squash too, which is the one method that makes the *next*
   release painful. Now stated, with the reasoning.
5. **The tag step stopped at creating the tag** and never said to push it, which
   is the step that now actually publishes anything.

## Out of scope

- Cutting, merging or tagging the release. The workflow is the machinery; the
  project owner still opens the release PR, rebase-merges it and pushes the tag
  (`AGENTS.md`: never push to `main`).
- Release assets. See *Notes / decisions*.
- The `scripts/` and `testdata/` directories that 043 found are described in
  three documents but absent. Still absent; still not owned by anything.

## Expected modules/files

- `.github/workflows/release.yml` (new)
- `.github/scripts/release_notes.py` (new)
- `.github/workflows/ci.yml`
- `CONTRIBUTING.md`
- `ROADMAP.md`
- `docs/features/046-release-workflow.md` (new)

## Acceptance criteria

- [x] An annotated `v*` tag pushed to `main` publishes a GitHub Release titled
      `Straticate vX.Y.Z` whose body is that version's `CHANGELOG.md` section.
- [x] A **lightweight** tag is refused, naming the `git tag -a` command that
      would have been right.
- [x] A tag on a commit not reachable from `origin/main` is refused.
- [x] A tag whose version disagrees with `backend/pyproject.toml` is refused,
      printing both values.
- [x] A version with no `## [X.Y.Z]` changelog section, or an empty one, is
      refused.
- [x] The extracted notes exclude the heading itself, the file's preamble, any
      neighbouring release's section, and the trailing link-reference block.
- [x] `ci.yml` runs on `release/**` pull requests and pushes, so release-branch
      work is verified like any other.
- [x] `ci.yml` still does not run on tags, so a tag push does not re-run the
      pipeline that already passed on the release PR.

## Required tests

`release_notes.py` is CI-only tooling and is not part of either test suite —
neither `pytest` (it lives outside `backend/`) nor `vitest` would collect it.
It was exercised directly instead, and this is the record of that run:

| Case | Input | Result |
| --- | --- | --- |
| Real changelog | `--tag v0.1.0` against `CHANGELOG.md` | 224 lines; heading and `[0.1.0]:` link definition both excluded |
| First of several | `0.2.0`, sections for 0.2.0/0.1.0/0.0.1 | stops at the `## [0.1.0]` heading |
| Sandwiched | `0.1.0`, same file | body only, neither neighbour |
| Last of several | `0.0.1`, same file | body only, link-definition block dropped |
| Version mismatch | tag `v9.9.9`, pyproject `0.2.0` | exit 1, both values printed |
| No such section | tag `v0.3.0` | exit 1 |
| Missing `v` prefix | tag `0.2.0` | exit 1 |
| Empty section | heading immediately followed by the next heading | exit 1 |

The workflow's three git-level guards are asserted by the same commands they
run (`git cat-file -t`, `git merge-base --is-ancestor`) and are first exercised
for real by the v0.1.0 tag.

## Notes / decisions

- **No release assets.** Straticate is run from a checkout — `npm ci && npm run
  build`, `uv sync`, `python -m straticate` — and publishes no package or
  binary. A release carrying a stale or unbuildable artifact is worse than one
  carrying none. The `gh release create` step is where they would go.
- **The workflow does not re-verify CI on the tagged commit.** It could query
  the check runs for `$GITHUB_SHA` and require `backend`/`frontend` green, but
  a tag pushed promptly after the merge would race the `push: main` run of
  `ci.yml` and fail spuriously. The release PR's own required checks are the
  gate; this is deliberate, and the alternative is recorded here rather than
  rediscovered.
- **"Reachable from `main`", not "is `main`'s tip".** Rebase-merging means
  `main` accumulates real commits, so reachability is the meaningful test and it
  still permits tagging an earlier released commit. One consequence: `main`'s
  *intermediate* commits arrive as new SHAs that CI never ran on individually —
  only the tip is built, by `ci.yml`'s `push: main` trigger. Tag tips.
- **`release_notes.py` is deliberately dependency-free** (`tomllib` is standard
  library from 3.11). It runs before any toolchain is installed, so the job
  needs no `uv sync` and no `setup-node`.
- The em dash in `## [0.1.0] — 2026-08-26` is not parsed; the heading regex
  keys on the bracketed version and ignores everything after it, so changing
  the date separator cannot break the release.
