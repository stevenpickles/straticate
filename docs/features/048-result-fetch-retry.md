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
  - a retry's fetch that is superseded — job cleared via "Start another
    separation" and the same job re-tracked, giving the component a third,
    distinct in-flight fetch — does not apply its stale result when it
    resolves late; the effect's existing `current` cleanup flag, unchanged,
    is what protects this. Proved by mutation: with the cleanup emptied
    (`current` forced permanently `true`), the stale result renders and the
    test fails.

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

- [x] A failed **result fetch** renders the error message plus a "Try again"
      button; clicking it refetches and, on success, the stems render. This
      does not extend to a failed stem-audio download (engine load error) —
      see Known Limitations.
- [x] Result-fetch errors of every shape (network failure, 409
      `result_not_available`) get the button.
- [x] New tests cover retry-success and stale-fetch-superseded; the
      retry-success test was proven to fail against the unfixed code (see
      Notes below).
- [x] Feature doc + own ROADMAP row updated; all five frontend checks
      green (`format:check`, `lint`, `typecheck`, `test`, `build`).

## Known Limitations

- **The retry covers the result fetch only.** A failed stem-audio download
  (an engine load error, rendered through the same `.stem-player-error`
  paragraph once a result has loaded) still renders the verbatim defect
  wording — "Something went wrong. Please try again." — with no control that
  does anything: the "Try again" button lives only in the result-fetch error
  branch. Widening retry to engine errors needs an engine reload path, which
  is out of this feature's scope. The timeline work (050+) rebuilds this UI
  and should give this its own numbered fix rather than inherit this one's.

## Required tests

- `StemPlayer result-fetch retry (feature 048)` in
  `frontend/src/components/StemPlayer.test.tsx`:
  - `recovers from a dropped result fetch when the user tries again`
  - `offers "Try again" for a 409 result_not_available while the job is still running`
  - `does not apply a retry fetch that resolves after a newer fetch superseded it`

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
- **Accepted trade-off: "Try again" is shown for definitionally-futile
  errors too.** `job_not_found` and a `result_not_available` whose
  `detail.state` is `cancelled` or `failed` will never be fixed by retrying
  the same job — "Start another separation" is the real remedy for those.
  The button is kept unconditional across every error shape anyway, for
  simplicity: branching the body on which errors are worth retrying would
  add a second axis of error classification on top of `explainError`'s
  existing one, for a control that is merely useless (not harmful) in the
  futile cases.
- **Accepted trade-off: clicking "Try again" drops keyboard focus.** The
  button unmounts as soon as `result` leaves the `error` state (the body
  switches to the loading paragraph), which takes the focused element with
  it; the browser's default is to drop focus to `<body>` rather than move it
  anywhere meaningful. Not addressed here — noted for whoever rebuilds this
  UI in the 050 timeline work, who should decide where focus belongs across
  this transition.
