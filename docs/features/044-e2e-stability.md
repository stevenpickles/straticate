# [044] Playwright tier stability under load

Branch: `044-e2e-stability`
Status: PR OPEN
Dependencies: 030

## Objective

Make the Playwright tier trustworthy when the machine is busy, so a failure
means a defect rather than a coincidence.

## Scope

- Diagnose the three 033–042 flakes under deliberate, **measured** contention
  rather than by guessing which wait loses.
- Change what a failure reports, where the report was hiding the cause.
- Quantify before and after, with the number of runs and the load each run
  actually experienced.

## Out of scope

Changing application behaviour to suit a test — including the defect this
feature found. Adding retries as a substitute for diagnosis. Broadening
coverage. `backend/**` and `frontend/src/**` belong to feature 041 in parallel.

## Expected modules/files

- `frontend/e2e/app.ts` — failed-API-request recording
- `frontend/e2e/separation.spec.ts` — the stem-player assertion, and the
  recorder for the block's own page
- `frontend/playwright.config.ts` — the recorded reasoning for no local retries
- `DEVELOPMENT.md` — two concurrent `uv run`s in one worktree
- `docs/features/044-e2e-stability.md`, `ROADMAP.md`

## Required tests

The tier is the test. What this feature added is verified by *mutation*
(below): the result fetch was aborted, and both new diagnostics were confirmed
to name the cause. Sixteen full tier runs across four measured contention
levels are the evidence for everything else.

## Result, up front

**The tier was not flaky. The application stalls, and the tier was reporting
it.**

`FakeSeparator._run_chunks` filters every chunk **inline on the event loop**
(`backend/src/straticate/inference/fake.py`). The only yield is the
`await asyncio.sleep(chunk_delay_seconds)` that comes *after* all four stems
and both channels of that chunk have been filtered — so for the whole of the
filtering the backend serves nothing: not REST, not the feature 013 WebSocket
hub, not progress delivery.

Measured directly, by opening a **new TCP connection per sample** — which is
what the Vite dev-server proxy does — and timing `GET /api/v1/health` before,
during and after one real twelve-chunk job:

| CPU contention | idle p50 | during-job p50 | during-job p95 | during-job max |
| --- | --- | --- | --- | --- |
| 1.0x (quiet) | 3 ms | 301 ms | 367 ms | **367 ms** |
| 4.5x | 6 ms | 6 ms | 2 290 ms | **3 429 ms** |
| 17.2x | 6 ms | 6 ms | 4 042 ms | **8 059 ms** |

Two things in that table are the whole diagnosis.

**The stall scales linearly with contention.** 0.37 s quiet, 3.4 s at 4.5x,
8.1 s at 17x. Extrapolated to the ~40x a developer machine reaches with several
agents, a GPU job and the backend suite at once, a single chunk stalls the
backend for around 20 seconds — which is exactly `expect`'s budget in
`playwright.config.ts`. Nothing about the tier had to change for the tier to
start failing; the machine only had to get busier.

**The median request is unaffected.** At 4.5x the p50 during the job is still
6 ms and only 11 of 39 probes stall — roughly one per chunk. So most requests
are fine and an unlucky one waits seconds. That is precisely the shape of a
failure that passes on re-run, passes in isolation, and looks like a flaky
test.

It also explains the one fact the brief flagged as the most suggestive: **all
three historical flakes were in `separation.spec.ts`**, and that is the only
spec that drives a job through the fake separator's chunk loop while continuing
to talk to the backend. Under this diagnosis that is not a coincidence, it is
the prediction.

The real separators do not have this defect: `TorchSeparator._separate`
offloads every blocking span with `asyncio.to_thread`. It is confined to the
fake path — which is the path the entire E2E tier runs on, and the only one a
fresh checkout can run at all (feature 032).

**This is an application defect, in `backend/src/straticate/inference/`, and it
is not fixed here.** The brief puts "changing application behaviour to suit a
test" out of scope, `inference/` belongs to feature 041 concurrently, and the
fix is a separate numbered feature. It is a violation of AGENTS.md principle 4
in spirit — inference must not block the request path — and it is worth more as
a recorded, quantified defect than as a tier tuned to survive it.

### Why the tier cannot mitigate it from its own side

Checked before concluding "report, do not patch", because a preference and a
conclusion are different things:

- `fake_separator_builder` takes `chunk_seconds` and `chunk_delay_seconds` as
  Python keyword arguments with defaults (`inference/registry.py`). Nothing in
  `config.py` exposes either as a setting, and only in-process test code
  injects a tuned builder — so the E2E backend, which is a subprocess, gets
  `chunk_seconds=5.0` and there is no environment variable, flag or fixture
  choice that changes it.
- The blocking span is set by the **chunk size**, not by the audio length. A
  shorter fixture would reduce how many times the stall happens without
  shortening the stall itself, so it buys nothing that matters.

There is therefore no in-scope change that makes the tier stop losing to this.

## What this feature does change

Nothing about what the suite *waits for*. No timeout was raised, no retry was
added, no sleep was introduced. What changed is what a failure **tells you**.

### 1. Failed `/api` requests are a first-class signal

`app.ts` gains `recordFailedApiRequests(page)` and
`attachFailedApiRequests(failures, info)`. Every page records the `/api`
requests the browser could not complete, and a test that fails attaches them.
The `page` fixture gets it automatically; `separation.spec.ts` owns its page
(`browser.newPage()`) so it asks for one explicitly.

This exists because of what the first captured failure actually looked like.
The test said:

```text
Locator: getByRole('region', { name: 'Stem player' }).locator('.stem-player-stem-name')
- Expected: ["vocals","drums","bass","other"]
+ Received: []
43 × locator resolved to 0 elements
```

…and the cause was in the dev server's stderr, which nobody had thought to
read:

```text
[vite] http proxy error: /api/v1/jobs/{id}/result
Error: connect ETIMEDOUT 127.0.0.1:8223
```

An hour went into finding that. The report now carries it:

```text
attachment: failed API requests
GET /api/v1/jobs/01M0Y3TM59HS84FPDCX7X4M4CN/result — net::ERR_TIMED_OUT
```

### 2. The stem-player assertion names its own error state

`StemPlayer` fetches `GET /jobs/{id}/result` once, in a `useEffect`, with no
retry: it renders the stems or it renders an error, and there is no third
outcome. When that fetch is dropped, the stem list is simply empty, and the
old assertion could only report the emptiness. The test now waits for the fetch
to reach *either* outcome and asserts on the error state first, so a dropped
result reads as

```text
Error: the stem player could not load the separation result
+ ["Something went wrong. Please try again."]
```

It costs nothing when the fetch succeeds.

### Mutation-tested

Both were verified by reintroducing the failure rather than by inspection — the
result fetch was aborted with `route.abort('timedout')` in a scratch edit, the
spec re-run, and both outputs above are verbatim from that run. The mutation was
reverted; `git diff` carries no trace of it.

## Method — and why the numbers can be trusted

Every claim here is measured under load that was **verified per run**, not
assumed. "Under load" is a claim; "under a measured mean of 12.5x" is evidence.

- A fixed CPU workload was calibrated with the deliberate load off: **478 ms**.
- A sampler ran that workload every ~8 s throughout, logging elapsed time.
- Each tier run stamped its start and end, and is reported against the
  contention samples **inside its own window**.
- Contention was generated by *N* busy processes on a 22-core machine plus a
  continuously looping backend suite (906 tests, ~3 min 15 s quiet), the latter
  running from an **isolated copy of `backend/` with its own venv** — see
  *Discarded runs* for why that matters.

Levels: 0, 11, 22 and 44 hog processes, two runs each, both phases. A fifth
level (64 processes, ~30x) was collected for one run and then dropped: at that
point the load is the thing being tested rather than the tier, and no developer
machine looks like that.

### Discarded runs — nine of them, and none counted

Reported because an N nobody can defend is worth no more than a tier nobody
trusts.

1. **One run** whose backend was killed mid-suite at 21:56:34 by another
   agent's process-wide `taskkill /F /IM python.exe`. Every symptom after that
   timestamp is `ECONNREFUSED 127.0.0.1:8223`, and none of it is a flake.
2. **Eight runs** across two batches that raced each other for ports
   8223/5223. Stopping a batch had killed its wrapper but left the runner loop
   alive, so a second batch started alongside it; both wiped and regenerated
   the same fixture directory. The runner now frees those ports before each
   run, holds a PID lock, and is executed from a frozen copy so an edit cannot
   corrupt a run in flight.

A third contaminant was found and removed before it could affect anything: the
looping backend suite and the E2E backend were sharing one `.venv` with
different `uv` extras. That is a general trap, not a detail of this feature, and
it is written up in DEVELOPMENT.md (*Two `uv run`s in one worktree fight over
the environment*).

## Measured: before and after

Sixteen tier runs — eight before the change, eight after — across the same four
contention levels, each reported against the load it actually experienced.

**Before** (7/8 passed):

| level | wall | mean slowdown | result |
| --- | --- | --- | --- |
| h0 | 84 s | 1.2x | **FAIL** — dropped `GET /jobs/{id}/result` |
| h0 | 71 s | 1.2x | pass |
| h11 | 94 s | 2.2x | pass |
| h11 | 109 s | 2.9x | pass |
| h22 | 294 s | 6.6x | pass |
| h22 | 220 s | 5.7x | pass |
| h44 | 510 s | 12.5x | pass |
| h44 | 446 s | 14.7x | pass |

The single failure is worth reading carefully, because it is not what the brief
predicted. It happened at **1.2x — the quietest level, not the loudest** — and
the tier survived 14.7x untouched. So the *observed* failure was not caused by
slowdown at all; it was a request that never reached the backend. No timeout
change could have helped it, which is why none was made.

Across all sixteen runs, `connect ETIMEDOUT` at the proxy appeared three times,
all within one ~100-second window, all at the quietest level and never at any
higher one. Whatever produced it, it is not load-driven, and this feature has
**not** established its cause. The event-loop stall is a plausible mechanism —
a backend that is not accepting cannot answer — but the stall at 1.2x is
0.37 s, far short of a connect timeout, so the honest answer is that it remains
unexplained. It is recorded rather than attributed.

**After** (8/8 passed):

| level | wall | mean slowdown | result |
| --- | --- | --- | --- |
| h0 | 63 s | 1.1x | pass |
| h0 | 62 s | 1.1x | pass |
| h11 | 99 s | 2.7x | pass |
| h11 | 91 s | 2.4x | pass |
| h22 | 234 s | 6.0x | pass |
| h22 | 277 s | 6.9x | pass |
| h44 | 531 s | 15.5x | pass |
| h44 | 508 s | 11.2x | pass |

**8/8 after against 7/8 before is not evidence that this feature improved
stability, and it is not offered as any.** Nothing here changes what the suite
waits for, so nothing here can change whether a run passes. The honest reading
is the one the logs give: `connect ETIMEDOUT` at the proxy appeared three times
in the before phase and **zero** times in the after phase, at every level,
which says the before-phase failure was a transient of the machine during one
~100-second window rather than something a code change removed. One failure in
sixteen runs is far too few to compare rates with, and this document does not
try.

What the after phase does establish is a negative that matters: across eight
runs spanning 1.1x to 15.5x, the added recording and the extra assertion cost
nothing measurable and broke nothing. Median wall clock 234 s after against
220 s before, at comparable contention — inside the run-to-run spread of a
machine under this much load.

The residual failure mode is the application's, it is still there, and this
feature is not permitted to fix it.

## Retries: why there are still none locally

The brief asked for the reasoning, including what a retry could hide. It has a
casualty to point at now, so this is demonstrated rather than asserted.

**A local retry would have deleted this feature's finding.** The three flakes
that earned 044 a number were the application stalling its own event loop. Give
the tier one retry and each of them becomes a green second attempt, the report
says `1 flaky`, and the stall reaches the release with the tier's blessing —
which is exactly the mechanism by which a real regression slips past a tier
nobody trusts. The tier found a genuine product defect *precisely because*
nobody had given it a retry.

The distinction that matters is failure class. CI keeps its single retry, which
predates this feature: a runner stall while a server boots is infrastructure,
and it is uncorrelated with what the application does. A backend that stops
answering for eight seconds is the application, and it must not be retried away.
An intermittent product bug and an intermittent test look identical in a report
— so the tier is set up to make the first one visible rather than survivable.

## Acceptance criteria

- [x] **The cause is diagnosed and stated, not guessed at** — measured with a
      probe, quantified at three contention levels, and confirmed against the
      source. The mechanism predicts the one fact the brief found most
      suggestive (all three flakes in one spec).
- [x] **No fixed sleeps introduced; 030's grep still comes back clean** —
      `grep -rn "waitForTimeout\|setTimeout\|sleep" frontend/e2e frontend/playwright.config.ts`
      finds only the prose that says there are none.
- [ ] **The tier passes repeatedly under deliberate contention, with the number
      of runs reported** — *the count is reported; the criterion is **not met**,
      and is not deliverable by this feature.* Sixteen runs: 7/8 before, 8/8
      after, spanning a measured 1.1x–15.5x. But nothing here changes what the
      suite waits for, so 8/8 is not a result this feature produced, and the
      failure mode that produced the one failure is an application defect this
      feature is not permitted to fix. Marking it met would take a retry, and a
      retry would hide the defect. It becomes deliverable when the stall is
      fixed.
- [x] **Wall-clock cost in CI is unchanged or better** — the change adds one
      `requestfailed` listener per page, one `expect.poll` that resolves on its
      first evaluation when the fetch succeeded, and — on failure only — one
      text attachment. Nothing on the passing path is new work. Quiet full runs
      either side, all 24 tests: **1 min 36 s** before, **58 s** and **1.1 min**
      after, on a machine whose background load differed between them. Under
      the load ramp, median 220 s before against 234 s after at comparable
      contention, inside the run-to-run spread. The defensible claim is that
      there is no mechanism by which it could cost more — not that it got
      faster.
- [x] **If retries are used at all, the reasoning is recorded** — none were
      added; the reasoning for *not* adding them is above, and is now also in
      `playwright.config.ts` beside the setting.

## Findings — not fixed here

1. **`FakeSeparator` blocks the event loop** (above). Application defect,
   `backend/src/straticate/inference/fake.py`, quantified at 0.37 s / 3.4 s /
   8.1 s per chunk at 1x / 4.5x / 17x contention. Worth its own numbered
   feature; `_run_chunks` wants the `asyncio.to_thread` treatment
   `TorchSeparator._separate` already gives its blocking spans.
2. **`StemPlayer` cannot recover from a failed result fetch.** One `useEffect`,
   one `getSeparationResult`, no retry and no way to ask again — a single
   dropped request leaves the inspect phase permanently showing "Something went
   wrong. Please try again." with no control that tries again. Not a regression
   and not caused by anything here; the tier now names it when it happens.
3. **Three unexplained `connect ETIMEDOUT` failures** at the dev-server proxy,
   all at the quietest contention level (above). Recorded, not attributed.
