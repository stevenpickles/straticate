# [043] Release preparation for v0.1.0

Branch: `043-release-preparation`
Status: PLANNED
Dependencies: 038, 042

## Objective

Everything that must be true before the `dev → main` release PR for **v0.1.0**.

Deliberately **not** a CI workflow. The project owner chose the smallest thing
that makes a real release, and `CONTRIBUTING.md` already documents the manual
`dev → main` → annotated-tag flow. Release automation would be new CI surface to
maintain for a first release; it can come later if the manual flow proves
annoying.

## Scope

- **`CHANGELOG.md`**, which the release process in `CONTRIBUTING.md` already
  assumes exists and which does not. Write the 0.1.0 entry from the feature
  ledger — unusually good source material, since every feature has a numbered
  doc with acceptance criteria. Write it for **users**, not as a commit log:
  what the application can now do, what it needs, and what it cannot do.
- **Version bump.** `backend/pyproject.toml` `0.1.0.dev0` → `0.1.0`. Feature 029
  single-sourced `__version__` from package metadata, so that is one edit and
  `/api/v1/version` follows automatically — **verify** that rather than assuming
  it, and note that 029 added a drift test which must still pass.
  `frontend/package.json` is `0.0.0`; decide whether the frontend carries the
  same version, and say why.
- **Honest limitations, in the release notes.** This project has recorded them
  carefully throughout; the release is where a user meets them. At minimum: the
  `vocals` mode has no fast tier (~0.3× real time on CPU); Demucs loses bass on
  wide-separation stereo mixes, with mono fold-down as the verified workaround
  (028's known limitations, feature 041); model weights carry their own licences
  and one shipped model is **research-use-only**; job records are in memory and
  do not survive a restart; nothing prunes job outputs or export artifacts.
- **Verify the release works from a clean checkout.** Clone or `git clean` into
  a fresh directory, follow the README exactly as written, and separate
  something. Report what you did, not what should have happened. This is the
  step that catches documentation which only works on the machine it was written
  on.
- Update the ROADMAP: M3's table, and "Current state" to describe a released
  project rather than one in flight.

## Out of scope

- **Creating the release PR, merging to `main`, or tagging.** Those belong to the
  project owner (`AGENTS.md`: never push to `main`). Prepare everything and hand
  over a `dev` that is ready.
- A GitHub Actions release workflow — explicitly deferred.
- Publishing to PyPI or npm; building distributable artifacts.
- Any feature work. If the clean-checkout run finds a bug, **report it** — do not
  fix it inside release preparation.

## Acceptance criteria

- [ ] `CHANGELOG.md` exists with a user-facing 0.1.0 entry
- [ ] Versions bumped; `/api/v1/version` reports `0.1.0`; 029's drift test passes
- [ ] Known limitations stated where a user will meet them
- [ ] A clean checkout was followed end to end and what happened is reported
- [ ] ROADMAP reflects a project at release rather than in flight
- [ ] All gates green
