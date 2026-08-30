# [064] Stem-audio retry + player hygiene

Branch: `064-stem-retry-hygiene`
Status: PR OPEN
Dependencies: 048, 052
PR: #91

## Objective

A failed **stem-audio download** is recoverable in place: the engine gains
`retryFailedStems()`, which re-fetches only the stems whose bytes never
arrived — leaving every loaded stem's buffer, gain node and running source
untouched — and the player offers a "Try again" beside the alert that reaches
it. This is the half of the retry story feature 048 deliberately deferred
("Widening retry to engine errors needs an engine reload path"). Four small
recorded debts in the same files ride along.

## Scope

### The engine reload path

- **`audio/engine.ts` — `retryFailedStems(): Promise<void>`** on
  `StemPlayerEngine`. It is a no-op unless the engine is live and some entry
  is in `error`; those entries flip to `loading` (their `error` cleared) and
  are re-fetched through the existing `loadStemAudio` + `decodeAudioData`
  path, with a fresh `AbortController` folded into the existing `loadAbort`
  handling so `dispose()` still cancels whole-file transfers. Guarded by the
  existing `loadGeneration`, so a superseding `load()` or a `dispose()`
  orphans the retry silently. **Never rejects**, exactly like `load()`: a
  stem that fails again lands back on its own entry and in the snapshot's
  `error`.
- **Loaded stems are never touched.** No buffer is dropped, no gain node is
  rebuilt, nothing is re-decoded. Recovered gain nodes are wired **in stem
  order**, so the mixer's node order stays the result's stem order however
  the network interleaved.
- **`publishLoadOutcome()`** — `load()`'s tail (status, `durationSeconds`,
  `loadError`, `applyGains`, `notify`) extracted into one private method that
  both paths call, rather than duplicated. A retry that recomputed the
  duration differently from a load would give the same decoded stems two
  different timelines.
- **A recovered stem joins mid-flight, once.** If the transport is playing,
  the retry ends in exactly one `startSources(currentTime())` — the same
  one-generation rebuild `setLoopRegion` makes while playing, under the same
  `try/catch → transportError`. Under a loop region that position is the
  *wrapped* one, and `startSources` reapplies the region's flags, so the new
  stem wraps with the rest from its first sample.

### The UI

- **`components/StemPlayer.tsx`** — the loaded body's engine-error alert gains
  a "Try again" button (class `stem-player-retry`, mirroring 048's), rendered
  **only** when `snapshot.stems.some((stem) => stem.status === 'error')`. A
  transport failure (an autoplay rejection, a context the browser closed) has
  nothing to re-fetch and `play()` is its remedy, so it gets no button. No
  stem name or count appears anywhere (AGENTS.md principle 6).
- **`components/StemPlayer.css`** — one rule suppressing the focus outline on
  the region (see the focus rider below); the button reuses 048's existing
  `.stem-player-retry` rule.

### Hygiene riders

1. **C5 — the orphaned 048 focus handoff.** 048 recorded "clicking 'Try
   again' drops keyboard focus [to `<body>`]" as an accepted trade-off, for
   whoever rebuilt this UI. Both retry buttons now move focus onto the player
   region *before* the state flip that unmounts them: `tabIndex={-1}` on the
   `<section>` plus a ref and `focus()` in a shared `keepFocusInPlayer`
   callback. The section is not a tab stop, so nothing is added to the tab
   order, and the outline is suppressed so the handoff is invisible.
2. **C8 — the scrub/lookahead invariant.** `DEFAULT_LOOKAHEAD_SECONDS` and
   `DEFAULT_SCRUB_FADE_SECONDS` are exported, and one engine test pins
   `fade < lookahead`. That inequality is what 052 recorded as unpinned: the
   motionless-click-stays-silent property holds only while the release's
   `stop(now + fade)` lands before the grain's `start(now + lookahead)`.
3. **C11 — two jsdom `getContext` warnings.** Root cause found: the peak
   computations are chunked and therefore async, so in
   `recovers from a dropped result fetch when the user tries again` the lanes
   painted *after* `afterEach` restored `getContext`, past the installed fake
   canvas and into jsdom's unimplemented one. Fixed with the same trailing
   `await act(async () => {})` settle `renderReady` already ends with. The
   suite is now silent.
4. **B7 — `channels: 1` coverage.** 041 recorded "no test covers a
   `channels: 1` stem through `StemPlayer` or `ExportPanel`… missing coverage
   rather than a known defect". Both suites' `stem()`/`resultOver()` helpers
   take a `channels` parameter (default `2`, so every existing test is
   unchanged), and a single mono stem now goes through the player (one lane,
   one canvas, one mute, one solo, one fader, plus play and seek) and through
   the export panel (one checkbox, the single-file wording, the export URL,
   and the deselect-everything guard).

## Out of scope

- `StemTimeline.tsx`/`.css`, `TimelineLane.tsx` and `frontend/e2e/**` —
  feature 067's, concurrently. **One exception, unavoidable:** adding
  `retryFailedStems` to the `StemPlayerEngine` interface makes
  `StemTimeline.test.tsx`'s inert `bufferEngine()` literal incomplete, so it
  gained the one line `retryFailedStems: () => Promise.resolve(),`. Nothing
  else in that file was touched.
- The session hoist (065). The result fetch and engine ownership stay exactly
  where they are; 065 builds on the `publishLoadOutcome` extraction.
- The zipper ramp (C9, deferred), and the whole backend.

## Expected modules/files

- `frontend/src/audio/engine.ts`, `frontend/src/audio/engine.test.ts`
- `frontend/src/components/StemPlayer.tsx`, `.css`, `.test.tsx`
- `frontend/src/components/ExportPanel.test.tsx`
- `frontend/src/components/StemTimeline.test.tsx` (one line — see above)
- `docs/features/064-stem-retry-hygiene.md` (this file)
- `ROADMAP.md` (own ledger row only)

## Acceptance criteria

- [x] `retryFailedStems()` re-fetches **only** the failed entries, recomputes
      `status`/`durationSeconds`/`loadError`, and clears the alert when every
      stem recovers. **Proved to fail first — output below.**
- [x] Loaded stems keep their buffer object, their gain node and their
      connection: asserted by identity, with `disconnectCount === 0`.
- [x] A retry with nothing failed makes no request at all and publishes no
      new snapshot (asserted by loader-call list and snapshot identity).
- [x] A retry never rejects: a stem that fails again re-reports on its entry
      and in `snapshot.error`, and the stems that loaded stay playable.
- [x] A retry superseded by `load()` publishes nothing — same generation
      guard, same contract `load()` has. A retry after `dispose()` is a
      no-op.
- [x] A recovered stem joins a playing mix in **one** new generation, sharing
      one `when` and one offset with every other stem, at the position the
      transport had reached — including the wrapped position under a loop
      region, with the region's flags reapplied.
- [x] The player renders "Try again" exactly when some stem is in `error`,
      and not for a pure transport failure; clicking it calls the engine
      once, without disposing or reloading it. End-to-end over the real
      engine: a stem that 404s once and then loads recovers, the alert goes,
      and the working stem's gain node is untouched.
- [x] Neither retry button drops focus to `<body>`; both leave it on the
      player region (asserted on `document.activeElement`).
- [x] One engine test pins `DEFAULT_SCRUB_FADE_SECONDS <
      DEFAULT_LOOKAHEAD_SECONDS`.
- [x] `npm test` runs with no jsdom `getContext` warning.
- [x] A `channels: 1` single-stem result is covered through `StemPlayer` and
      `ExportPanel`.
- [x] All five frontend gates green: `format:check`, `lint`, `typecheck`,
      `test` (41 files, 1025 tests), `build`.

## Required tests

`frontend/src/audio/engine.test.ts`, `StemAudioEngine retrying a failed stem`:

- `recovers a stem whose audio failed, and recomputes the mix`
- `leaves every stem that already loaded exactly as it was`
- `does nothing at all when no stem failed`
- `asks only for the stems that failed`
- `re-reports a stem that fails again, without rejecting`
- `joins a recovered stem to a mix that is already playing`
- `joins at the wrapped position when a loop region is running`
- `publishes nothing when a load supersedes the retry`
- `ignores a retry after disposal`

`frontend/src/audio/engine.test.ts`, `StemAudioEngine scheduling defaults`:

- `keeps the scrub fade shorter than the scheduling lookahead`

`frontend/src/components/StemPlayer.test.tsx`, `StemPlayer stem-audio retry`:

- `offers a retry beside the alert when a stem’s audio failed`
- `offers none for a transport failure, which has nothing to re-fetch`
- `asks the engine to re-fetch the failed stems, without reloading`
- `keeps focus in the player when the stem retry unmounts`
- `keeps focus in the player when the result retry unmounts (feature 048’s handoff)`
- `recovers the stem over the real engine, keeping the mix intact`

`frontend/src/components/StemPlayer.test.tsx`,
`StemPlayer with a single mono stem`:

- `renders one of everything, derived from the result`
- `plays and seeks it exactly as it does a stereo mix`

`frontend/src/components/ExportPanel.test.tsx`,
`ExportPanel with a single mono stem`:

- `renders one checkbox and offers it as a single audio file`
- `exports it with no stems parameter, everything being selected`
- `disables export when its only stem is deselected`

## Notes / decisions

### Proved to fail first

The engine method did not exist before this feature, so "assert the current
behaviour" and "show the new test failing" are the same run — but the test
file also imports the two newly exported defaults, and a plain revert would
have failed at *import* rather than at the behaviour. So the fail-first run
used the pre-feature `engine.ts` **with only the two `export` keywords
added**, leaving `retryFailedStems` absent:

```text
× recovers a stem whose audio failed, and recomputes the mix
× leaves every stem that already loaded exactly as it was
× does nothing at all when no stem failed
× asks only for the stems that failed
× re-reports a stem that fails again, without rejecting
× joins a recovered stem to a mix that is already playing
× joins at the wrapped position when a loop region is running
× publishes nothing when a load supersedes the retry
× ignores a retry after disposal
TypeError: engine.retryFailedStems is not a function
Tests  9 failed | 102 passed (111)
```

`keeps the scrub fade shorter than the scheduling lookahead` passed in that
run, as it must: it pins a relationship the defaults already satisfy, and it
exists so the *next* change to either number is noticed.

The component tests were proved the same way, against the pre-feature
`StemPlayer.tsx` with the new test file in place:

```text
× offers a retry beside the alert when a stem’s audio failed
× asks the engine to re-fetch the failed stems, without reloading
× keeps focus in the player when the stem retry unmounts
× keeps focus in the player when the result retry unmounts (feature 048’s handoff)
× recovers the stem over the real engine, keeping the mix intact
TestingLibraryElementError: Unable to find an accessible element with the
  role "button" and name "Try again"
AssertionError: expected <body>…</body> to be <section …>…</section>
Tests  5 failed | 82 passed (87)
```

That second assertion is C5 exactly: without the handoff, focus is on
`<body>`. (`offers none for a transport failure` passed against the old code
for the trivial reason that no such button existed at all — it is a guard for
the new one, not a regression test for the old.)

### Why the retry is additive rather than a reload

The obvious implementation is "call `load()` again with the same sources".
It is wrong for the case that matters: `load()` tears the graph down, so a
user who is *listening* to three of four stems would lose the mix, the
levels, the mutes and the playhead in order to recover the fourth. The engine
already models each stem as an independent entry with its own buffer and gain
node, so recovery is naturally per-entry. What that buys — and what the tests
hold — is that a retry changes nothing about a stem that already worked.

The one thing it cannot avoid is the transport rebuild: an
`AudioBufferSourceNode` is single-use, so the recovered stem can only join by
starting a new generation for everybody. That is the same cost `seek` and
`setLoopRegion` pay, it happens once, and it happens at the position the
clock reports — so what the user hears is a rebuild, not a jump.

### Why the button is conditional and 048's is not

048 deliberately shows its button for every result-fetch error shape,
including definitionally-futile ones, because branching would have added a
second axis of error classification. This one is conditional for a different
reason: the condition is not "which error is worth retrying" but "is there
anything to retry at all". `snapshot.error` carries a transport failure
*or* a load failure (the transport one shadows), and a transport failure has
no failed download behind it — the button would call `retryFailedStems()`,
which would return without making a request, and the user would be offered a
control that does nothing observable. `snapshot.stems.some(… === 'error')` is
the honest test, and it is one field, not a classification.

### Known limitations

- **No per-stem retry.** One button re-fetches every failed stem. With two
  failures and one file genuinely gone, a click re-downloads both. Naming
  stems in the UI would also cross AGENTS.md principle 6, so a per-lane
  control would need its own design.
- **No backoff, no retry limit.** Clicking repeatedly against a stem that is
  really gone re-issues the request every time. That matches 048's result
  retry, which has the same property.
- **The alert stays up during the retry.** Flipping the failed entries to
  `loading` deliberately leaves `status` and `loadError` alone, so the mix
  stays playable and keeps explaining itself; what changes is that the button
  disappears until the attempt answers. There is no "retrying…" wording — the
  lane goes from "Unavailable" to an empty lane, which is quiet feedback.
- **A retry cannot fix a stem that failed to *decode* on a context the
  browser has since closed**; that lands as `transportError` from the graph
  wiring and is reported, not recovered.

### Noticed, not touched

- `docs/features/048-result-fetch-retry.md`'s "Known Limitations" now
  describes two things this feature fixed (the missing stem-audio retry, and
  the focus drop). Merged feature docs are historical records, so it was left
  as written rather than edited; this document is the answer to it.
- `ROADMAP.md`'s "After v0.1.0" prose bullet about the unrecoverable result
  fetch is still stale — 048 noted the same thing and left it. Still outside
  any one feature's file scope.
