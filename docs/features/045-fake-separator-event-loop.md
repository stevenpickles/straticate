# [045] The fake separator must not block the event loop

Branch: `045-fake-separator-event-loop`
Status: PLANNED
Dependencies: 041, 044

## Objective

Stop `FakeSeparator` stalling the backend's event loop while it filters a chunk.

## The evidence

Feature 044 set out to stabilise the Playwright tier and concluded the tier was
never the problem. `FakeSeparator._run_chunks` filters every chunk **inline on
the event loop** — four stems by two channels of pure-Python comb filtering —
and the only yield is the `await asyncio.sleep(self._chunk_delay_seconds)`
*after* all of that work is done. While it runs, nothing is served: not REST,
not the feature 013 WebSocket hub, not progress delivery, not a new TCP accept.

044 measured it by opening a new TCP connection per sample — what the Vite proxy
does — and timing `GET /api/v1/health` around a real twelve-chunk job:

| CPU contention | idle p50 | during-job p95 | during-job max |
| --- | --- | --- | --- |
| 1.0x | 3 ms | 367 ms | 367 ms |
| 4.5x | 6 ms | 2,290 ms | 3,429 ms |
| 17.2x | 6 ms | 4,042 ms | 8,059 ms |

Two properties of that table are why this went unrecognised for three waves.
The stall **scales linearly with contention** — extrapolated to the ~40x a
developer machine reaches with several agents and a GPU job, one chunk stalls
the backend for about 20 s, which is Playwright's `expect` budget. And the
**median request is unaffected** (p50 stays 6 ms; roughly one probe in four
stalls, about one per chunk), which is precisely the shape of a failure that
passes on re-run and in isolation.

It also explains why all three historical flakes were in `separation.spec.ts`:
that is the only spec that drives a job through the fake chunk loop while still
talking to the backend.

## Why this is a real defect and not a test-only concern

- **`TorchSeparator` already does it correctly.** It offloads every blocking
  span with `asyncio.to_thread` — `_place_on_device`, `_run_chunks`,
  `_finish_stems` — and the skeleton documents that callbacks then arrive from
  the worker thread. So the pattern to follow already exists in this codebase,
  one directory away, and this feature is bringing the fake path in line rather
  than inventing anything.
- **AGENTS.md principle 4 and ARCHITECTURE.md §14 forbid it.** Features 022 and
  025 offload their blocking work for exactly this reason, and
  `api/system.py`'s storage endpoint offloads a single `stat` on the same
  argument.
- **The fake separators ship.** `fake-vocals-001` and `fake-standard-001` are
  catalog entries a user can select, so a user running a fake job stalls the
  whole application — including the progress they are watching.

## Scope

- Move the per-chunk filtering off the event loop, following
  `TorchSeparator`'s existing `asyncio.to_thread` pattern.
- **Preserve the observable behaviour of the fake engine exactly.** Its output
  is a fixture that tests pin; the stems must not change. Verify that, do not
  assume it.
- **Preserve cancellation responsiveness and progress semantics.** The current
  `await asyncio.sleep(...)` is doing double duty — its comment says it is
  awaited even at `0.0` because it yields, so a cancel lands before the next
  chunk and progress is observable. Whatever replaces the inline work must keep
  both properties. Per-chunk cancellation granularity is fine; that is what the
  torch path has.
- Note that `run.last_chunk_seconds` and the timing in `_RunState` are measured
  around the filtering; check whether moving it changes what they report, and
  say so either way.
- **Reproduce 044's probe** before and after. Its harness and method are in
  `docs/features/044-e2e-stability.md`; the acceptance criterion is that the
  during-job p95 comes down to the idle range, at more than one contention
  level.

## Out of scope

- Making the fake separator faster, or changing its audio.
- Exposing `chunk_seconds` / `chunk_delay_seconds` as settings. 044 established
  they are constructor keyword arguments with no `config.py` exposure; that is
  worth knowing, and it is a different change.
- The E2E tier. 044 has just landed there and its diagnostics are what will
  demonstrate this fix from the outside; do not modify `frontend/e2e/**`.
- `StemPlayer`'s inability to recover from a failed result fetch — a real
  finding 044 recorded, and a separate feature.

## Acceptance criteria

- [ ] The per-chunk work no longer runs on the event loop
- [ ] 044's probe shows during-job p95 in the idle range at two or more
      contention levels, with before/after numbers reported
- [ ] Fake stem output is unchanged, demonstrated rather than asserted
- [ ] Cancellation still lands within one chunk, and progress is still observable
- [ ] New tests are mutation-verified: each must fail against the unfixed code
- [ ] All gates green

## Required tests

A test that the chunk work does not occupy the loop — the natural form is to
run a job and concurrently await something cheap on the loop, asserting the
loop stayed responsive. That test must fail against today's implementation;
demonstrate that it does.
