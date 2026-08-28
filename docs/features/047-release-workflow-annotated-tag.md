# [047] Release workflow: ask GitHub whether the tag is annotated

Branch: `hotfix/release-workflow-annotated-tag`
Status: PR OPEN
Dependencies: 046
PR: #… (when open)

## Objective

`release.yml`'s first guard can pass. As shipped in 046 it could not: it asked
the checkout whether the tag was annotated, and `actions/checkout` has already
destroyed that information by the time any step runs.

## What went wrong

v0.1.0 was tagged correctly — the Git API reports `refs/tags/v0.1.0` as
`object.type: "tag"`, a real tag object (`52e1285`) with a tagger and a message,
targeting `ea063ae`. The Release workflow ran and failed anyway:

```
##[error]v0.1.0 is a lightweight tag (commit).
```

When `actions/checkout` checks out a tag ref it writes the local
`refs/tags/<tag>` pointing **straight at the commit**, discarding the
annotation. So `git cat-file -t "$GITHUB_REF_NAME"` answers `commit` on the
runner for every tag, annotated or not. The guard tested a property that no
correct tag could exhibit — it was not a strict guard, it was a guaranteed
failure, and it would have rejected v0.1.1 and every release after it.

046 recorded this step as verified. It was verified in the wrong environment:
`git cat-file -t` was exercised in a scratch repository, where it distinguishes
the two kinds perfectly. Nothing about that test touched the thing that breaks
it, and 046's own document called the three git-level guards "first exercised
for real by the v0.1.0 tag" — which is exactly what happened, one failure later
than was useful.

## Scope

Replaces the check with the Git API, which reports what the remote actually
stores rather than what the checkout left behind:

```sh
ref=$(gh api "repos/$GITHUB_REPOSITORY/git/ref/tags/$GITHUB_REF_NAME" --jq '.object.type + " " + .object.sha')
kind=${ref%% *}
```

The failure message is unchanged. The step now also prints the tagger and
message from the tag object, which is the provenance the rule exists to
protect and which `for-each-ref` could not have shown either.

## Out of scope

- The other three guards. Guard 2 uses `$GITHUB_SHA` and an explicit refspec,
  guards 3 and 4 read files out of the checkout; none depends on tag-ref
  identity.
- Publishing v0.1.0, which is the tag's job once this is on `main`.

## Expected modules/files

- `.github/workflows/release.yml` — one step
- `ROADMAP.md`, `docs/features/047-release-workflow-annotated-tag.md`

## Acceptance criteria

- [x] An annotated tag is recognised as annotated on a GitHub runner
- [x] A lightweight tag is still refused, with the same message
- [x] The step prints the tagger and message of the tag object
- [x] No other step changes

## Required tests

Verified against the live API before opening the PR — including, deliberately,
repositories whose tags are of each kind, since the bug was that the wrong
environment was tested:

| Case | Source | Result |
| --- | --- | --- |
| Annotated | `git/git` `v2.47.0` | `kind=tag`; tagger *Junio C Hamano 2024-10-06*, message *Git 2.47* |
| Annotated | `python/cpython` `v3.13.0` | `kind=tag`; tagger *Thomas Wouters 2024-10-07*, message *Python 3.13.0* |
| **Lightweight** | `cli/cli` `v2.63.0` | `kind=commit` → refused, and the tag-object call 404s as it should |
| Parsing, one space | stub | `kind=tag` |
| Parsing, two spaces | stub | `kind=tag` — `${ref%% *}` / `${ref##* }` are whitespace-tolerant |
| Parsing, `commit` | stub | refused |

`release_notes.py` is unchanged and its 046 results stand.

## Notes / decisions

- **The lesson is about where a guard is exercised, not which command it uses.**
  Every one of 046's script-level cases was tested in the environment it runs
  in; the three git-level ones were not, and the only one whose behaviour the
  runner changes is the one that broke. A CI guard is only tested by CI.
- **Why the API and not a corrective `git fetch`.** Re-fetching
  `+refs/tags/X:refs/tags/X` would probably restore the tag object, but
  "probably" is what produced this bug. The API call is the one that was
  actually observed working, against tags of both kinds.
- **This branch is a hotfix into `main`, which no document describes.**
  `CONTRIBUTING.md` and `AGENTS.md` now cover feature branches into `dev` and
  release branches, but not a fix that must reach `main` without waiting for
  the next release. Deliberately not settled here — a hotfix policy is a
  process decision, not a workflow fix, and inventing one inside a hotfix is
  how the last inconsistency happened. Fold it into the `dev` reconciliation.
