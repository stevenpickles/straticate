# [045] The fake separator must not block the event loop

Branch: `045-fake-separator-event-loop`
Status: PR OPEN
Dependencies: 041, 044
PR: #60

## Objective

Stop `FakeSeparator` stalling the backend's event loop while it filters a chunk.

## Result, up front

**Fixed, and not by the change the brief expected.**

The brief's instruction was to follow `TorchSeparator`, which offloads each
blocking span with a single `asyncio.to_thread`. That was implemented first and
**measured, it does almost nothing**: a health probe on a fresh TCP connection
during a real twelve-chunk job went from a 271 ms 95th percentile to 266 ms.

The reason is the GIL, and it is the difference between the two engines rather
than between the two patterns. `TorchSeparator`'s blocking span is a torch
forward pass, and torch's kernels *release the GIL*, so the loop's thread can run
while the worker computes. `FakeSeparator`'s blocking span is pure-Python
arithmetic, which holds the GIL for its whole duration. A thread that holds the
GIL for 250 ms leaves the event loop as unserved as a chunk that never left it —
the loop needs the GIL to answer anything, and an interpreter that offers it back
every 5 ms (`sys.getswitchinterval`) still loses badly when the waiter is a
selector that has to be woken first.

So the unit of work is a **block of frames**, not a chunk. `_filter_block` is
dispatched with `asyncio.to_thread` once per `FILTER_BLOCK_FRAMES` (1024 frames,
~23 ms of audio, ~1 ms of compute), and it is the *return* to the event loop
between blocks — not the GIL's preemption during one — that gets requests served.
That is the same shape as `stereo.apply_stereo_handling_async`, one module away,
which already awaits one thread hop per fold block.

The four shapes, measured against a standalone asyncio server with one TCP
connection per sample, over six chunks of the real per-chunk workload:

| work shape | during-job p50 | during-job p95 |
| --- | --- | --- |
| inline on the loop (before) | 1 183 ms | 1 183 ms |
| one thread hop per chunk | 81 ms | 130 ms |
| blocks, on the loop (`await asyncio.sleep(0)` between) | 16 ms | 72 ms |
| **blocks, in threads** | **16 ms** | **29 ms** |

— against an idle p95 of 27 ms on the same client. Blocking *on the loop* is the
same size of unit and is still two to three times worse, because a request needs
several loop passes to accept, read and answer and it only gets one per block.

## 044's probe, before and after

044's method exactly: a **new TCP connection per sample** (what the Vite proxy
does), timing `GET /api/v1/health` before, during and after one real twelve-chunk
`fake-standard-001` job over 60 s of audio, with contention from *N* busy
processes and a fixed CPU workload sampled throughout to measure what the machine
was actually doing.

Runs are **paired and interleaved** — before and after back to back at each
level, same fixture, same probe. Every row carries the contention its own run
experienced, measured rather than assumed; see *Method, and the runs that were
discarded* below for why that mattered.

| hogs | phase | measured slowdown | idle p50/p95 | during p50 | **during p95** | during max | samples > 100 ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | before | 1.9x | 6.0 / 18.6 ms | 336.8 ms | **490.6 ms** | 540.1 ms | 12 of 20 |
| 0 | **after** | 1.6x | 6.2 / 18.8 ms | 10.0 ms | **20.0 ms** | 20.9 ms | **0 of 60** |
| 11 | before | 2.2x | 4.2 / 5.5 ms | 469.4 ms | **598.9 ms** | 916.1 ms | 12 of 20 |
| 11 | **after** | 2.0x¹ | 6.1 / 8.1 ms | 10.5 ms | **31.3 ms** | 46.9 ms | **0 of 74** |
| 22 | before | 8.1x | 5.7 / 9.8 ms | 6.4 ms | **3 014.4 ms** | 5 452.9 ms | 12 of 20 |
| 22 | **after** | 6.6x | 5.3 / 17.7 ms | 11.4 ms | **72.5 ms** | 152.8 ms | **5 of 112** |
| 44 | before | 9.0x | 5.7 / 11.1 ms | 6.1 ms | **2 757.4 ms** | 2 965.5 ms | 15 of 59 |
| 44 | **after** | 5.0x | 5.7 / 7.7 ms | 13.8 ms | **110.4 ms** | 188.5 ms | 8 of 136 |
| 44 | before (2) | 4.7x | 5.8 / 7.5 ms | 5.9 ms | **1 172.2 ms** | 1 423.3 ms | 16 of 59 |
| 44 | **after (2)** | 3.6x | 5.9 / 7.6 ms | 12.9 ms | **103.2 ms** | 152.5 ms | 9 of 136 |

¹ this run's calibration baseline came back at 57 ms against 28 ms for the run
it is paired with, so its reported 1.1x understates the load it actually saw;
~2.0x is the like-for-like figure. The cause was not established.

**"12 of 20" is the signature of the defect**: twelve chunks, twelve stalls, one
per chunk, identically at 1x, 2x and 6.6x. At 44 hogs it is 15 and 16 of 59 — the
sampler gets fewer samples in because each stalled one costs seconds, so the
per-chunk accounting stops being exact, but the shape is the same. After the
change it is 0 at 1x and 2x, and 5, 8 and 9 out of 112 to 136 at the three
high-contention runs.

At 1x the during-job p95 (20.0 ms) is inside the idle band (18.8 ms) — the
criterion, met. At 2x it is 31 ms against an 8 ms idle p95, at 6.6x 73 ms against
18 ms, and at 5x/3.6x about 105 ms against 8 ms: **not** in the idle band at
those levels, and reported as such. What it is instead is 19x, 42x and 25x
better, and — the important part — no longer scaling like a chunk. The stall
grew from 490 ms to 3 014 ms across the range; the residual grows from 20 ms to
110 ms. What remains is the block's own compute stretched by contention (a 1 ms
block at 5x is ~5 ms, and a fresh connection needs several loop passes through
an oversubscribed scheduler), not a chunk-long stall.

The 44-hog level is reported as **two pairs** because the first pair's two runs
disagreed with each other about how loaded the machine was (9.0x against 5.0x at
the same nominal hog count). The second pair is closer (4.7x against 3.6x) and
says the same thing, which is the reason both are printed rather than the
friendlier one. Note also that the after runs consistently measure *less*
contention than the before runs they are paired with: the ordering across the
session is monotonically decreasing (9.0 → 5.0 → 4.7 → 3.6), which looks like the
machine settling over twenty minutes rather than anything either version does.

### Method, and the runs that were discarded

The harness starts *N* busy processes and a backend, times `GET /api/v1/health`
on a fresh connection every 100 ms through an idle window, the whole job and a
tail window, and runs a fixed CPU workload every 4 s throughout so each run
reports the contention it actually saw against a baseline calibrated with the
deliberate load off.

**Three 44-hog runs were discarded, and the reason is worth recording because
the conclusion it produced was the opposite of the truth.** A crashed 44-hog run
leaked its whole batch of hogs. Three things had to be wrong at once:

1. On Windows a uv venv's `python.exe` is a **trampoline** that spawns the real
   interpreter as a child, so `Popen.terminate()` killed the launcher and left
   the process doing the work alive and reparented.
2. The spawns sat *outside* the harness's `try`, so a failure part-way through
   never reached the cleanup at all.
3. Even with the cleanup fixed, it only runs if the process reaches it — and the
   shell running the harness was killed from outside on one attempt, which is
   how the second batch escaped.

Ninety orphans (44 hogs, their 44 real interpreters, and a backend) ran for 90
minutes on a 22-core machine, and everything measured in that window is
worthless.

**What that pair appeared to show is the point.** Run under the leak, the 44-hog
comparison read p95 6 589 ms before against **8 093 ms after** — the fix looking
*worse* than the defect, which would have been a real and publishable finding
about `asyncio.to_thread` degrading under oversubscription. Re-run clean, twice,
it reads 2 757 ms → 110 ms and 1 172 ms → 103 ms. The instrumentation is the only
reason the first version was caught: the two runs recorded `mean_slowdown` of
3.9x and 7.6x for the *same* nominal hog count, and calibration baselines of
193 ms and 103 ms against a true quiet 31 ms. A pair that disagrees with itself
by 2x is not a comparison, so it was thrown away rather than explained.

The fix, in the harness, is three-layered to match the three failures: every
spawn moved inside the `try`; cleanup is `taskkill /PID <pid> /T /F` per process
so the tree dies with the launcher; and every hog carries a
`STRATICATE_PROBE_HOG` marker in its command line, so each run begins and ends by
sweeping for surviving hogs **by that marker** and prints how many it found. That
last one is the only layer that survives the harness being killed. Every kill is
by PID — an image-wide `taskkill` is what damaged 044's experiment earlier in
this wave.

Which runs were affected: the 44-hog runs, all discarded and re-run, and the
first engine-cost benchmark, whose numbers were corrected (see below — the cost
it reported, +15%, turned out to be noise). The 0/11/22 pairs, the work-shape
comparison and the block-size latency sweep all predate the leak and are clean.
The mutation runs and the byte-identical comparison ran during it and are
pass/fail and hash comparisons that contention cannot change; one side effect is
that the loop-latency figures quoted in the new test module's docstrings were
measured at ~18x, which is a harsher condition than intended and a better
robustness argument than quiet numbers would have been.

## The audio is unchanged — demonstrated

Not asserted against a golden hash. The pre-045 module was loaded out of
`origin/dev` **alongside** the current one and both were run over the same
inputs, comparing the SHA-256 of every stem file:

```text
standard 60s   chunk=5     as_is stems=4 IDENTICAL  bass.wav=f755a0b6859a885e
standard 60s   chunk=1.7   as_is stems=4 IDENTICAL  bass.wav=f755a0b6859a885e
standard 1.3s  chunk=5     as_is stems=4 IDENTICAL  bass.wav=164f094ac9830942
standard 1.3s  chunk=0.3   as_is stems=4 IDENTICAL  bass.wav=164f094ac9830942
standard 60s   chunk=5     mono  stems=4 IDENTICAL  bass.wav=9ce0374bb53039e0
vocals   60s   chunk=5     as_is stems=2 IDENTICAL  instrumental.wav=f5d96cae3c4d74f0
vocals   1.3s  chunk=0.05  as_is stems=2 IDENTICAL  instrumental.wav=0cca10ab095a8d3f
vocals   1.3s  chunk=0.3   mono  stems=2 IDENTICAL  instrumental.wav=609ab5d8c01d4304

all cases identical
```

Both models, chunk lengths that are and are not multiples of the block, chunks
shorter than a block, and both `stereo_handling` values. A separate sweep over
block sizes 8192/4096/2048/1024/512 also produced identical stems at every size.

There is a reason it cannot be otherwise, and it is the same reason the chunk
length has never mattered: `_CombFilter` carries its state across whatever
boundary it is given, which `test_output_is_independent_of_the_chunk_length`
already pins.

## What the block size costs, and how it was chosen

By measurement. The 044 probe was run against the same job at four block sizes:

| block frames | during-job p95 |
| --- | --- |
| 8192 | 53 ms |
| 4096 | 41 ms |
| 2048 | 28 ms |
| **1024** | **20 ms** (idle: 18 ms) |

1024 is where it reaches the idle band, so that is the size. Nothing smaller was
probed — there was nothing left for it to buy.

**What it costs the engine is not resolvable above the noise on this hardware,**
and an earlier draft of this document said otherwise. The first benchmark ran
under the leaked hogs above and reported a tidy monotonic +15% for 1024 and +36%
for 512. Re-run clean, five runs per configuration on the same 60 s four-stem
job, quiet and in process:

| configuration | median | range |
| --- | --- | --- |
| inline (pre-045) | 4.12 s | 3.66 – 4.15 s |
| blocks of 8192 | 3.40 s | 3.33 – 3.94 s |
| blocks of 4096 | 3.78 s | 3.49 – 4.12 s |
| blocks of 2048 | 4.05 s | 3.68 – 4.20 s |
| **blocks of 1024** | **4.38 s** | 3.66 – 4.46 s |
| blocks of 512 | 4.35 s | 4.14 – 4.64 s |

The ranges overlap almost entirely, 2048 and 1024 are out of order, and 8192
measures *faster* than the code it replaced. A whole-job measurement including
the simulated per-chunk delay (median of three) put it at 4.25 s before against
4.33 s after. The defensible statement is "a few percent at most, and possibly
none" — not the clean 15% the contaminated run appeared to show.

## `run.last_chunk_seconds` and `_RunState`

Checked, as the brief asked, and the answer is **the semantics are unchanged and
the numbers grow slightly**.

The bracket is where it was: `chunk_started` is stamped before the chunk's work
and `run.last_chunk_seconds` is taken after the `await asyncio.sleep(...)`, so it
still measures "one whole chunk, including the simulated delay", and
`chunk_seconds_total`, `mean_chunk_seconds`, `audio_processed_seconds`,
`chunks_completed` and the RTF are all computed from it exactly as before. What
changed is what falls inside the bracket: the filtering now includes one thread
hop per block, so a chunk reports a little more wall clock than it used to, and
the RTF a little less. Both remain true statements about real elapsed time —
nothing here reports a timer.

Measured on the same 60 s four-stem job, quiet, in process, median of three:
`processing_seconds` 4.25 s before against 4.33 s after, `mean_chunk_seconds`
330.7 ms against 338.6 ms, RTF 14.12 against 13.86. About 2%, which is inside
this machine's run-to-run spread — so the honest reading is that these numbers
did not move.

One property is **not** preserved and is worth stating: `runtime_stats()` is
called from the event loop while `_filter_block` mutates the stem planes on a
worker thread. That is safe for the same reason `TorchSeparator.RunState`
documents — every field it reads is a single `int`/`float` store and a snapshot
may legally be a chunk stale — and it is stronger here, because the fields
`runtime_stats` reads are written by the *loop*, not by the worker.

## Cancellation and progress

Unchanged, deliberately. The chunk *loop* stays on the event loop: the token is
checked at the top of each chunk, the run bookkeeping and `_report` run where
they always did, and the `await asyncio.sleep(self._chunk_delay_seconds)` is
still there doing its documented double duty. Per-chunk granularity is what
ARCHITECTURE.md §7 specifies and what the torch path has, so no per-block token
check was added.

The existing tests are the evidence and all still pass unchanged:
`test_cancellation_mid_run_raises_and_leaves_no_outputs` (cancel requested from
the progress callback lands at the very next chunk boundary — `chunks_completed
== 1` of 5), `test_cancellation_removes_stale_and_partial_stem_files`,
`test_cancellation_before_the_first_chunk_is_observed`, and
`test_progress_is_chunk_grained_monotonic_and_ends_complete`.

## Expected modules/files

- `backend/src/straticate/inference/fake.py` — `FILTER_BLOCK_FRAMES`,
  `_filter_block`, the dispatch in `_run_chunks`, and the *Threading* section of
  the module docstring
- `backend/tests/test_inference_fake_event_loop.py` — new
- `docs/features/045-fake-separator-event-loop.md`, `ROADMAP.md`

## Required tests, and the mutation that proves them

Three tests, deliberately different in kind: two structural and timing-free (so
they cannot be flaky), one behavioural (the form the brief asks for).

**Every one was run against the unfixed code.** The mutation replaces only the
loop body, so the module still imports and the tests fail on their own merits
rather than on a collection error.

Mutation 1 — the pre-045 inline filtering:

```text
E  AssertionError: the per-chunk filtering ran on the event loop thread, so the
   backend served nothing for the length of every chunk (feature 044's stall)
E  AssertionError: the largest unit of filtering was 44100 frames against a
   1024-frame block: the event loop waits that long for its turn, however many
   threads the work is handed to
E  AssertionError: the event loop was unavailable for 205.6 ms (p99; peak
   205.6 ms) during the chunk loop, against a mean chunk of 182.3 ms — the
   per-chunk filtering is blocking the loop
FAILED test_chunk_filtering_does_not_run_on_the_event_loop_thread
FAILED test_the_loop_gets_its_turn_back_between_blocks_within_a_chunk
FAILED test_the_loop_stays_responsive_while_chunks_are_filtered
3 failed
```

Mutation 2 — **the near-miss**: one `asyncio.to_thread` per chunk, the shape the
brief pointed at and the shape that measurably does not fix the stall.

```text
E  AssertionError: the largest unit of filtering was 44100 frames against a
   1024-frame block: the event loop waits that long for its turn, however many
   threads the work is handed to
FAILED test_the_loop_gets_its_turn_back_between_blocks_within_a_chunk
1 failed, 2 passed
```

That is exactly why the third test exists: "off the loop" and "the loop stayed
responsive to a spinning waiter" both pass for a version whose external latency
is unimproved, and only the block-size assertion catches it. Both mutations were
reverted; `git diff` carries no trace of them.

## Acceptance criteria

- [x] **The per-chunk work no longer runs on the event loop** — `_filter_block`
      is dispatched with `asyncio.to_thread`, and
      `test_chunk_filtering_does_not_run_on_the_event_loop_thread` asserts the
      calling thread is not the loop's.
- [x] **044's probe shows during-job p95 in the idle range at two or more
      contention levels, with before/after numbers reported** — met at 1x
      (20.0 ms against an 18.8 ms idle p95) and at 2x (31.3 ms against 8.1 ms
      idle, and **zero** samples over 100 ms against twelve before). Reported
      honestly for the rest: at 6.6x and 5x the p95 is 73 ms and 110 ms against
      idle p95s of 18 ms and 8 ms, so **near** the idle band rather than inside
      it — while being 42x and 25x better than before, and no longer scaling
      like a chunk. Four contention levels, six pairs, all numbers above.
- [x] **Fake stem output is unchanged, demonstrated rather than asserted** —
      eight configurations run through both implementations side by side, all
      byte-identical (above).
- [x] **Cancellation still lands within one chunk, and progress is still
      observable** — the chunk loop, its token check and its progress report
      never left the event loop; the four existing tests that pin this pass
      unchanged.
- [x] **New tests are mutation-verified: each must fail against the unfixed
      code** — all three fail against the inline code, output above; one of them
      additionally fails against the near-miss.
- [x] **All gates green** — `ruff format --check`, `ruff check`, `pyright`
      (0 errors), `pytest` (backend). No frontend file was touched.

## Notes / decisions

1. **`TorchSeparator`'s pattern is not portable to pure-Python work.** This is
   the finding worth carrying forward: `asyncio.to_thread` buys responsiveness
   only when the callee releases the GIL. Anything in this codebase that offloads
   pure-Python compute in one hop has the same problem — `stereo.fold_blocks`
   already blocks for this reason (0.14 s per block), and future work should
   size its blocks by the latency it owes the loop, not only by memory. A
   reviewer reading `_run_chunks` and asking "why not one `to_thread` like the
   torch skeleton?" will find the measurement in the module docstring rather
   than an opinion.
2. **A measurement harness that spawns processes needs a marker, not just a
   cleanup path.** The leak above cost three runs and produced a confident wrong
   answer before it was caught. The generalisable part: a `finally` protects
   against your code failing, not against your process being killed, so anything
   that starts background load should be findable and killable by something it
   stamped on itself.
3. **Not touched, and deliberately.** `_apply_fades` also runs on the loop, but
   it is ~3.5 k iterations once per job (sub-millisecond) rather than per chunk.
   `chunk_seconds` / `chunk_delay_seconds` remain constructor keyword arguments
   with no `config.py` exposure — 044 established that and the brief puts it out
   of scope. `frontend/e2e/**` and `playwright.config.ts` are untouched.
4. **044's third acceptance criterion is now deliverable.** 044 marked "the tier
   passes repeatedly under deliberate contention" **not met**, saying it "becomes
   deliverable when the stall is fixed". The stall is fixed; re-running that
   criterion is 044's ledger to close, not this feature's, and no E2E file was
   touched here.
5. **`StemPlayer` cannot recover from a failed result fetch** (044's finding 2)
   is still open and still needs its own number.
