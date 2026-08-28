# [048] Stem player recovers from a failed result fetch

Branch: `048-result-fetch-retry`
Status: PR OPEN
Dependencies: 023
PR: —

## Objective

Fixes the known v0.1.0 defect recorded in ROADMAP.md ("After v0.1.0", first
bullet) and found by feature 044 (finding 2, deliberately not fixed there):
`StemPlayer` fetched `GET /jobs/{id}/result` exactly once, so a single
dropped request left the Inspect step permanently reading "Something went
wrong. Please try again." with no control that actually tried again. After
this feature, the error state offers a working "Try again" button that
refetches the result.

## Scope

- `frontend/src/components/StemPlayer.tsx`: added `attempt` state, bumped by
  a new `retryResult` callback; widened the result-fetch effect's dependency
  array from `[jobId]` to `[jobId, attempt]` so a bumped `attempt` reruns the
  fetch for the same job. Added a "Try again" button (class
  `stem-player-retry`) to the error branch, shown for every error shape —
  a 409 `result_not_available` while the job is still separating (retrying
  is the genuine remedy) as much as a transient network failure.
- `frontend/src/components/StemPlayer.css`: styled `.stem-player-retry`
  identically to the existing `.stem-player-restart` button, so the two
  secondary actions read as a visual pair.
- `frontend/src/components/StemPlayer.test.tsx`: added a
  `StemPlayer result-fetch retry (feature 048)` describe block:
  - a dropped result fetch (rejects once, then resolves) recovers when the
    user clicks "Try again", reaching the loaded/ready state;
  - a 409 `result_not_available` while the job is still running also gets
    the "Try again" button;
  - a retry's fetch that resolves *after* the job was cleared (via "Start
    another separation") does not apply its stale result — the effect's
    existing `current` cleanup flag, unchanged, is what protects this.

## Out of scope

- No timeline/waveform work, no other `StemPlayer` behavior changes, no
  audio engine changes, no e2e changes, no backend changes.
- The prose bullet in ROADMAP.md ("After v0.1.0") describing this exact
  defect was left as-is — the ledger table row is the source of truth this
  feature updates; pruning the now-stale prose bullet is a documentation
  nicety outside this feature's file scope, noted here for whoever next
  edits that section.

## Expected modules/files

- `frontend/src/components/StemPlayer.tsx`
- `frontend/src/components/StemPlayer.css`
- `frontend/src/components/StemPlayer.test.tsx`
- `docs/features/048-result-fetch-retry.md` (this file)
- `ROADMAP.md` (own ledger row only)

## Acceptance criteria

- [x] A failed result fetch renders the error message plus a "Try again"
      button; clicking it refetches and, on success, the stems render.
- [x] Errors of every shape (network failure, 409 `result_not_available`)
      get the button.
- [x] New tests cover retry-success and stale-fetch-superseded; the
      retry-success test was proven to fail against the unfixed code (see
      Notes below).
- [x] Feature doc + own ROADMAP row updated; all five frontend checks
      green (`format:check`, `lint`, `typecheck`, `test`, `build`).

## Required tests

- `StemPlayer result-fetch retry (feature 048)` in
  `frontend/src/components/StemPlayer.test.tsx`:
  - `recovers from a dropped result fetch when the user tries again`
  - `offers "Try again" for a 409 result_not_available while the job is still running`
  - `does not apply a retry fetch that resolves after the job was cleared`

## Notes / decisions

- The "reject once, then resolve" test double
  (`stubResultFetchQueue`/`deferred` helpers in the test file) follows the
  existing harness's convention of stubbing global `fetch` rather than
  mocking `getSeparationResult` directly, consistent with every other test
  in the file.
- Proved the main regression test fails first: `git stash push` on just
  `StemPlayer.tsx`/`StemPlayer.css` (keeping the new test file) and reran
  `npx vitest run src/components/StemPlayer.test.tsx`. All three new tests
  failed, the two that assert the button exists failing with
  `TestingLibraryElementError: Unable to find an accessible element with
  the role "button" and name "Try again"` — exactly the missing-control
  symptom this feature fixes. Restored the fix (`git stash pop`) and reran:
  all 44 tests in the file passed.
- A four-stem or a two-stem result renders through the same code either
  way; no stem name or count is hardcoded (AGENTS.md principle 6),
  unaffected by this change.
