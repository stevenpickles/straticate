# [039] Shared separator skeleton

Branch: `039-shared-separator-skeleton`
Status: PLANNED
Dependencies: 026, 028

## Objective

Stop paying for every fix twice.

## The measurement

`inference/demucs/separator.py` and `inference/roformer/separator.py` are
**781 of 1,591 lines byte-identical — 49%** (measured with `difflib` after
feature 028's review fixes; the pre-fix figure was 766/1411). The largest
contiguous identical block is **154 lines**: `separate` → `_separate` →
`_check_mode` → `_decode` → `_place_on_device`. Then blocks of 61, 50, 39 and
38 lines: `NvmlProbe`, the audio-conversion section, `device_stats`,
`_resolve_torch_device`, `_encode`, `_discard_outputs`, `_realtime_factor`.

Feature 028's PR originally described this as "~200 lines of CUDA telemetry
helpers". It is roughly four times that, and it is not only telemetry — it is
the whole run lifecycle.

**It has already cost.** Review finding 3 on PR #45 (`_place_on_device` leaves
`_loaded_device` stale after a partially-failed `.to()`, wedging a cached
separator for the process lifetime) existed in *both* files and had to be fixed
in both, with two regression tests. That is the first bill; every future fix to
this skeleton costs the same until it is shared.

## Scope

Extract the shared skeleton, leaving **`_run_chunks` and `_finish_stems` as the
two architecture-specific holes** — those are where RoFormer and Demucs
genuinely differ (chunk loop and stem assembly). Everything else above is
common: run-state lifecycle, stage sequence, decode plumbing, device placement,
CUDA/NVML snapshotting, the PCM bridge, cleanup, and RTF computation.

## The constraint that made 028 leave it alone

**Feature 026's tests monkeypatch that module's globals.** Extracting naively
will break a seam that 026's suite depends on, and 028 deliberately did not
disturb it — correctly, in the middle of shipping a separator. Read
`backend/tests/test_roformer_separator.py` and the CUDA-double seam (026 added
a `cuda_namespace()` indirection specifically so the telemetry path could be
tested on a CPU-only host) **before** moving anything.

Both separators are covered by unit tests against synthetic checkpoints and by
an `integration` tier against real weights, so the safety net exists — but the
integration tier needs weights and a GPU, so run it deliberately rather than
assuming CI covers you.

## Out of scope

Changing what either separator produces. Any new architecture. Anything about
feature 038's memory work — though whoever does both should sequence them
deliberately, since 038 will touch the chunk loop that this feature is trying
to leave alone.

## Acceptance criteria

- [ ] Duplication between the two separators is substantially eliminated,
      measured the same way (`difflib`) and reported before/after
- [ ] Both separators produce **bit-identical** output to today for the same
      input and parameters — prove it, as 026 proved its vendored mel filters
      bit-identical to librosa
- [ ] 026's test seams still work; no test's *intent* was rewritten to
      accommodate the refactor
- [ ] The integration tier passes for both backends, on CPU and CUDA
- [ ] All gates green; suite clean under `-W error`

## Required tests

The existing suites are the test: they must pass unchanged in intent. Add a
duplication check only if it can be made meaningful rather than brittle.
