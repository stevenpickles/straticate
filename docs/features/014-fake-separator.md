# [014] Separator interface + FakeSeparator

Branch: `014-fake-separator`
Status: PR OPEN
Dependencies: 012
PR: #15

## Objective

The replaceable-inference-backend seam exists. `Separator` is the one
abstraction every present and future inference backend implements, and
`FakeSeparator` is a complete implementation of it that behaves like a real
chunk-based model — deterministic real-time progress, cooperative
cancellation, fake model/GPU statistics, and real playable placeholder stems —
so the whole application can be built and CI-tested with no CUDA, no model
downloads and no ML infrastructure. This is the milestone that unblocks M1.

## Scope

- `backend/src/straticate/inference/base.py` — the `Separator` protocol,
  `SeparatorInfo` (model descriptor), `SeparationProgress` /
  `ProgressCallback`, `StageCallback`, and the telemetry snapshot types
  `SeparatorRuntimeStats` / `DeviceStats` / `ProcessingStats`.
- `backend/src/straticate/inference/fake.py` — `FakeSeparator`, the built-in
  fake model descriptors, and `FakeDeviceProfile`.
- `backend/src/straticate/inference/executor.py` — `SeparatorJobExecutor`, the
  adapter that makes any separator a `JobExecutor`.
- `backend/src/straticate/inference/layout.py` — where a job's stems live.
- `backend/src/straticate/inference/pcm.py` — planar 16-bit PCM buffers,
  FFmpeg decoding, WAV writing.
- `ARCHITECTURE.md` §7/§8 corrected to match the implemented contract.

## Out of scope

WebSocket transport (013), telemetry event publishing (019), job REST
endpoints (015), the model catalog service (010), result serving/export
(021/022), any real ML model or PyTorch dependency, frontend.

## Expected modules/files

- `backend/src/straticate/inference/{__init__,base,fake,executor,layout,pcm}.py`
- `backend/tests/audio_fixtures.py` · `test_inference_fake.py` ·
  `test_inference_executor.py`
- `docs/features/014-fake-separator.md` · `ARCHITECTURE.md` · `ROADMAP.md`

## Acceptance criteria

- [x] `Separator` is architecture-agnostic: nothing in the package's public
      surface mentions PyTorch, tensors, segment/FFT sizes or a network
      family; no new runtime dependency was added.
- [x] `SeparatorInfo` reports display name, architecture, version, separation
      mode, stem list and sample rate, and projects onto the contract
      `ModelInfo` for feature 019.
- [x] `FakeSeparator` derives its chunk count from the audio duration and a
      configurable chunk length, and reports progress after every chunk.
- [x] Deterministic: identical input and settings produce identical chunk
      counts and byte-identical stems (and the audio does not even depend on
      the chunk length).
- [x] Cooperative cancellation is checked every chunk and before every written
      stem; a cancelled run leaves no stem — complete or partial — behind.
- [x] Stems are real, playable 16-bit WAVs derived from the source, one per
      stem name of the requested mode (2 for `vocals`, 4 for
      `standard_stems`) — nothing is hardcoded to two stems.
- [x] Fake runtime statistics (pretend VRAM allocated/peak, utilization,
      temperature, chunk timings, RTF) are exposed through
      `runtime_stats()`; this feature publishes no events.
- [x] `SeparationResult` carries the stem list plus `processing_seconds` and
      `realtime_factor`.
- [x] `SeparatorJobExecutor` drives stages in order, forwards progress into
      `JobContext.report_progress`, passes the context's token through, and
      returns the result; a full job through a real `JobManager` completes,
      and cancelling through the manager cancels the job.
- [x] Fake models are consistent with `models/catalog.json` (a test asserts
      it); the catalog service itself is untouched.
- [x] `ruff format --check` · `ruff check` · `pyright` (strict) · `pytest`
      all green.

## Required tests

`backend/tests/test_inference_fake.py` and `test_inference_executor.py`; WAV
fixtures are generated at test time with stdlib `wave`
(`backend/tests/audio_fixtures.py`) — no audio binaries are committed. Every
separator under test runs with `chunk_delay_seconds=0.0` and
`model_load_seconds=0.0`, and all coordination is `asyncio.Event`-gated (or a
single `sleep(0)` loop tick) — no sleeps as synchronization.

Covered: exact stem list per mode (2 / 4) with valid, non-silent WAVs of the
expected duration, sample rate and channel count; mono/22.05 kHz input
resampled to the model rate; stems mutually distinct and distinct from the
source; monotonic chunk-grained progress ending at `chunks_total` with
`fraction == 1.0`; chunk count follows the configured chunk length;
determinism across runs and across chunk lengths; cancellation mid-run raising
`JobCancelled` promptly at the next chunk boundary and removing stale/partial
files; cancellation before the first chunk; `processing_seconds` /
`realtime_factor` populated and consistent; `runtime_stats()` (`None` before
the first run, monotonic peak memory, live stage, contract projections,
CPU-only variant, configurable device profile); mode mismatch and undecodable
input as `ApplicationError`s; one-separation-at-a-time guard; PCM decode
rejection and >2-channel downmix; layout helpers including stem-name
rejection; catalog consistency; executor stage sequence, full manager runs for
both modes, progress events, off-loop (worker-thread) progress marshalling,
manager cancellation, and error-code preservation.

## Notes / decisions

### The `Separator` contract

```python
class Separator(Protocol):
    @property
    def info(self) -> SeparatorInfo: ...
    def runtime_stats(self) -> SeparatorRuntimeStats | None: ...
    async def separate(
        self,
        input_path: Path,
        configuration: SeparationConfiguration,
        progress_callback: ProgressCallback,
        cancellation_token: CancellationToken,
        *,
        job_id: str,
        output_dir: Path,
        stage_callback: StageCallback | None = None,
    ) -> SeparationResult: ...
```

Three deliberate additions to the signature sketched in ARCHITECTURE.md §7
(updated in this PR):

1. **`job_id` / `output_dir` are keyword-only inputs.** `SeparationResult`
   requires a `job_id`, and stems must be written somewhere — but neither
   belongs in the user-facing `SeparationConfiguration` contract, and the
   on-disk layout must stay the application's decision rather than the
   separator's. Passing them in keeps separators pure and the layout in one
   place (`inference/layout.py`).
2. **Stages are a separate callback, not a field of the progress report.**
   §7's sketch put `stage` inside the progress callback, which forces a
   progress event for every stage change (with a meaningless chunk count) and
   makes stage transitions during decoding awkward. A dedicated
   `StageCallback` maps one-to-one onto `JobContext.set_stage`, keeps
   `SeparationProgress` aligned one-to-one with
   `JobContext.report_progress`, and — crucially — lets the **separator**
   announce stages, so the job's stage history only ever claims work that was
   really done.
3. **`info` / `runtime_stats()` are part of the protocol.** Features 015, 019
   and 021 all need "which model ran, what is it doing, how is it going", and
   putting it on the seam means a real separator (026) cannot forget it.

`ProgressCallback` receives an immutable `SeparationProgress`
(`chunks_completed`, `chunks_total`, `audio_processed_seconds`,
`audio_total_seconds`, plus a derived `fraction`) — exactly the arguments of
`JobContext.report_progress`. Separators report *every* chunk and never
throttle; throttling for the wire (~4 Hz) is the job manager's job.

Concurrency: one separation at a time per instance (`separate` raises
`RuntimeError` if re-entered), which matches the scheduler's "one GPU = one
active job" policy and makes `runtime_stats()` unambiguous. `separate` is
awaited on the job manager's loop, so a separator that does real compute
offloads it itself — the executor adapter already marshals off-loop callbacks
back onto the loop, so 026 does not have to care.

### The placeholder-stem transform

The source is decoded with FFmpeg to the model's native sample rate
(44.1 kHz for the fake models), keeping the source channel count capped at
stereo. Each stem is then the source through a **feed-forward comb filter**:

```text
y_i[n] = g_i * (0.6 * x[n] + 0.4 * s_i * x[n - D_i])

g_i = 0.9 * 0.85**i              per-stem gain (also level-distinguishes stems)
s_i = +1 if i even else -1       polarity of the reflection
D_i = round(sample_rate / (110 Hz * 2**i))
```

Why this and not something fancier — the point is a *predictable, verifiable*
fixture, not separation:

- **Audibly distinct.** Comb notches an octave apart per stem, plus distinct
  levels; a human can tell the stems apart in the player and a test can tell
  them apart by SHA-256.
- **Never silent.** The coefficients sum to 1, so for any sinusoid the output
  amplitude stays in `[0.2, 1.0] × g_i` of the input's — a stem of non-silent
  audio is always non-silent, which is what makes the "non-silent" assertions
  in the tests reliable rather than lucky.
- **Never clipping.** `|y| ≤ g_i·|x| ≤ 0.9` full scale.
- **Chunk-independent.** Filter state (the trailing `D_i` samples) is carried
  across chunk boundaries, so changing `chunk_seconds` changes the progress
  granularity and nothing else. A test asserts byte-identical output for two
  very different chunk lengths.
- **Cheap and dependency-free.** One multiply-add per sample per stem over
  `array("h")` slices — no numpy, no PyTorch. Measured ~5× real time for four
  stems of 44.1 kHz stereo on a development machine, which is both fast enough
  for the M1 demo and slow enough for progress to be visibly real.

Stages the fake really performs, in order: `decoding` (FFmpeg) →
`loading_model` (a simulated, configurable pause) → `separating` (the chunk
loop) → `post_processing` (a 10 ms fade on both ends of every stem, so
playback never starts with a click) → `encoding` (one WAV per stem). The
manager sets `preparing` before the executor runs.

Cost note: the fake holds the decoded source and every stem in memory
(`(1 + stem_count) ×` the decoded size) — fine for a local development tool,
and explicitly not the model a real streaming separator should copy.

### Output directory layout (for feature 021)

```text
{data_dir}/jobs/{job_id}/stems/{stem}.wav
```

`data_dir` is `Settings.data_dir` — the same root `AudioStore` writes uploads
under (`{data_dir}/audio/{audio_id}/original{ext}`), so uploads and job
outputs sit side by side. `inference/layout.py` is the single definition:
`job_output_dir()`, `job_stems_dir()`, `stem_path()`. `stem_path()` rejects
anything that is not a valid stem name (`^[a-z][a-z0-9_]*$`, the pattern from
the manifest schema), and `SeparatorInfo` enforces the same pattern on
construction, so path traversal through a stem name is impossible by
construction.

Stems are written to `{stem}.wav.part` and renamed into place, and any failed
or cancelled run deletes both forms — a reader never sees a truncated file
presented as a finished stem. Like the audio registry, job records are
in-memory only, so files left by a previous process are orphaned (a known
limitation until a persistent registry exists).

### The fake-stats accessor (for feature 019)

`separator.runtime_stats()` returns `SeparatorRuntimeStats | None` (`None`
before the first run; the last run's snapshot remains readable afterwards):

```python
SeparatorRuntimeStats(
    job_id: str,
    model: SeparatorInfo,          # .to_model_info()        -> ModelInfo
    device: DeviceStats | None,    # .to_gpu_metrics()       -> GpuMetrics
    processing: ProcessingStats,   # .to_processing_metrics()-> ProcessingMetrics
)
```

Building a `RuntimeMetricsEvent` is three projections and nothing else — this
feature deliberately publishes no events (019 owns telemetry, 013 owns
transport). `SeparatorJobExecutor.separator` exposes the instance for the
sampler to poll.

The fake's numbers are fabricated but internally consistent: allocation grows
with the stem count and wobbles deterministically with the chunk index, peak
never decreases, utilization and temperature vary deterministically, chunk
timings and elapsed time are real, and RTF is computed from real elapsed time
and the audio actually processed. The default `FakeDeviceProfile` describes an
honestly fake backend (`fake:0` / `backend="fake"`, a legal open-set value per
§10) rather than impersonating a GPU; pass a custom profile to exercise
CUDA-shaped UI. `device=None` reports no device block at all (the "running on
CPU" shape).

### What a real separator (026) must implement

Everything in the protocol, and nothing else:

1. `info` — a `SeparatorInfo` consistent with the model's `models/catalog.json`
   entry (`stems` and `sample_rate` in particular; a test pattern for this
   exists).
2. `separate(...)` — decode to the model's native rate (`inference/pcm.py`
   already does this, or stream it), announce the stages it really performs
   through `stage_callback`, process **real chunks** and call
   `progress_callback` after each, call `cancellation_token.raise_if_cancelled()`
   between chunks, write one WAV per stem into `output_dir` via a `.part`
   rename (cleaning up on failure), and return a `SeparationResult` with
   `processing_seconds` and `realtime_factor`.
3. `runtime_stats()` — real `torch.cuda` memory figures and optional NVML
   utilization/temperature in `DeviceStats`, real chunk/timing figures in
   `ProcessingStats`. NVML stays optional.
4. Offload the compute (worker thread/subprocess) and keep the event loop
   free; callbacks may be invoked from that thread — the executor marshals
   them.
5. Raise `JobCancelled` on cancellation, `ApplicationError` for expected
   failures (the code survives into the job record and the `job_failed`
   event), anything else for unexpected ones.
6. Keep PyTorch, tensors, segment/overlap/FFT parameters and the architecture
   name *inside* the implementation module. Per-model tuning belongs in the
   catalog's `default_inference_parameters`, which is opaque to everyone else.

### Errors introduced

- `audio_decode_failed` (422) — the input could not be decoded to PCM.
- `separation_mode_mismatch` (400) — the separator was handed a configuration
  for a mode it does not serve (a wiring bug; will be prevented by 010/015
  resolution, and is checked here so it fails loudly rather than silently
  producing the wrong stems).

### Known limitations

- The fake keeps whole stems in memory and does its arithmetic in pure Python;
  a real separator must stream.
- No persistent registry: job output directories are orphaned after a restart.
- Stems are 16-bit WAV. Higher-precision output formats (WAV PCM24, float32,
  FLAC) are feature 022's export path.
