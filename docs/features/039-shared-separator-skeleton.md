# [039] Shared separator skeleton

Branch: `039-shared-separator-skeleton`
Status: PR OPEN
Dependencies: 026, 028
PR: #48

## Objective

Stop paying for every fix twice. The two real separators shared 49% of their
lines; they now share one run skeleton, with the chunk loop and stem assembly as
the only architecture-specific holes.

## The measurement

Measured with `difflib.SequenceMatcher` over the two files' lines, counting
byte-identical lines and reporting them against the longer file — the same way
the brief measured the baseline.

| | roformer/separator.py | demucs/separator.py | identical | largest block |
| --- | --- | --- | --- | --- |
| before (`dev`, 8c42ce7) | 1,140 lines | 1,591 lines | **781 / 1,591 (49.1%)** | **154 lines** |
| after | 575 lines | 1,024 lines | **260 / 1,024 (25.4%)** | **17 lines** |

The 154-line block (`separate` → `_separate` → `_check_mode` → `_decode` →
`_place_on_device`) is gone, as are the 61/50/39/38-line blocks the brief named:
`NvmlProbe`, the audio-conversion section, `device_stats`,
`_resolve_torch_device`, `_encode`, `_discard_outputs`, `_realtime_factor`.

What the residual 260 lines are is worth stating, because the number alone
overstates them: they are blank lines, `"""` delimiters, shared *import lines*
(both modules import `torch`, `Tensor`, `Path`, `SeparatorInfo`, …) and two
docstring paragraphs that describe the same contract — the manifest's
`default_inference_parameters` block — for two different parameter classes.
Ignoring blank and punctuation-only lines, 179 of 838 meaningful lines (21.4%)
coincide. The largest remaining block is 17 lines, and it is a docstring plus
the four-line preamble of `from_catalog`. There is no code left to extract
without merging two genuinely different parameter schemas.

Reproduce with:

```python
import difflib
a = open("backend/src/straticate/inference/roformer/separator.py").read().splitlines()
b = open("backend/src/straticate/inference/demucs/separator.py").read().splitlines()
m = difflib.SequenceMatcher(None, a, b, autojunk=False)
print(sum(x.size for x in m.get_matching_blocks()), max(len(a), len(b)))
```

## What moved

Four new modules under `backend/src/straticate/inference/`:

| module | holds | torch? |
| --- | --- | --- |
| `torch_separator.py` | `TorchSeparator` (the run skeleton), `RunState`, `announce` / `realtime_factor` / `discard_outputs` | yes |
| `torch_device.py` | `resolve_torch_device`, `cuda_namespace`, `reset_peak_memory`, `device_stats`, `NvmlProbe` | yes |
| `torch_audio.py` | the PCM ↔ tensor bridge: `pcm_to_tensor`, `tensor_to_pcm`, `to_source_channels` | yes |
| `model_errors.py` | the three construction-time envelopes: `model_weights_missing` (409), `model_weights_invalid` (500), `model_parameters_invalid` (500) | **no** |

`inference/torch_device.py` is the name feature 028 predicted for this in its
own document; the comment above its copy of those five definitions is now spent
and has been removed.

`model_errors.py` was not in the brief's list, and it is deliberately small. The
brief requires the `model_weights_missing` (409) envelope to stay
byte-identical across backends; the surest way to keep two things identical is
for there to be one of them. The same argument covers `model_weights_invalid`
and `model_parameters_invalid`, which had the same 500 envelope written twice.

### The two holes

`TorchSeparator` is an ABC with exactly two abstract methods:

- **`_run_chunks(source, run, progress_callback, cancellation_token, device)`**
  — the chunk loop, returning the network's estimates as a CPU tensor in the
  network's own layout.
- **`_finish_stems(estimates, source)`** — one `PcmAudio` per advertised stem,
  in advertised order.

`_run_chunks` is documented in `torch_separator.py` as **the extension point
feature 038 will work inside**, with the contract an implementation must keep
spelled out (announce the denominator before the first chunk; check
cancellation *between* chunks, not inside one; update the five `RunState`
counters and call `_report` after each chunk). It is given the whole loop rather
than a per-chunk callback precisely because 038's job — bounding the
accumulator, whose peak today grows linearly with track length — is a decision
about the loop, not about a chunk.

## Bit-identical output

Both models, both devices, same input file, same catalog parameters, `dev`
(8c42ce7) versus this branch. SHA-256 of each written stem WAV:

Input (6.0 s stereo, 44.1 kHz):
`2274d359e5b3cd38f3f41f2a28c050b302d7ad4f6fa14fc4cc1d23b64670f258`

| model | device | stem | SHA-256 (identical on `dev` and on this branch) |
| --- | --- | --- | --- |
| `vocals-hq-001` | cpu | vocals | `1e48cce9da1556801f9b296ac6e18daaee3d08530cb52f78c24cf8ae8ac33502` |
| `vocals-hq-001` | cpu | instrumental | `306c7f89c2526f50365f6ee0a457cb1c2344f5094541f0abe397e6ad2b6b3421` |
| `standard-stems-001` | cpu | vocals | `af539baaea16e5501522c90fc77758b1a1b260a9c22220ba43cb0309c57eba71` |
| `standard-stems-001` | cpu | drums | `79b60d708e1dbfcef0cfe8f9d4e0e8fca111d2a44496e6a2565a4588e5a23c92` |
| `standard-stems-001` | cpu | bass | `0b45089ca03a03b932c55de0eb7d849ab8c387e0f9ba7848ec91e398dc03f4c4` |
| `standard-stems-001` | cpu | other | `a06134461f061c372a74e65cf2d876d1fc7878304749f8cd7143f101f33ff75a` |
| `vocals-hq-001` | cuda:0 | vocals | `28300e91b22da4cc06dc93512cb85063f692b7005db9370402c964cb033d6cde` |
| `vocals-hq-001` | cuda:0 | instrumental | `f8d95a5677860c4c002b9f83125fbd72b9385dc9757c08e781be2ef0a89edd63` |
| `standard-stems-001` | cuda:0 | vocals | `7992cb5b73f49f3decebd25b6fb2278800993d0cd71967cd995f648ef2462d57` |
| `standard-stems-001` | cuda:0 | drums | `7eb2d33af59616b993fd1483163526e5fc4e82972e57ab3b32bdbd4718678081` |
| `standard-stems-001` | cuda:0 | bass | `317d5bac752385afb46b554a4b82568352f543ba3b0e9f81529cfaaed2178092` |
| `standard-stems-001` | cuda:0 | other | `1863a697e8a9404b13a72edfb0a40373d9571cee47f7df2fca76647b50836ac1` |

Measured on an RTX 4060 Laptop GPU with `torch 2.13.0+cu130` (CPU rows on
`2.13.0+cpu`), real installed weights, both source trees on `PYTHONPATH` in turn
so the *only* variable was the Straticate source. CUDA and CPU digests differ
from each other, as they always have — that is float arithmetic on two devices,
not this feature.

Those digests are a one-off measurement against `dev` and cannot be a test (a
float hash is not portable across platforms or torch builds). The portable
regression each suite gained instead is
`test_a_second_run_of_one_separator_is_byte_identical_to_the_first`: a separator
is cached per model for the life of the process, so the plausible way this
refactor could go wrong is state outliving a run and quietly changing the
*second* job's audio. Nothing announces that; only the bytes do.

## Tests: what moved, and why

**No test's intent was rewritten.** Three kinds of change, all listed:

1. **Moved with the code they cover.** Feature 026's CUDA/NVML block (and
   feature 028's verbatim copy of it) patched `separator_module.cuda_namespace`,
   `_NVML`, `atexit` and `importlib` — the seam 026 added specifically so the
   telemetry path could be exercised on a CPU-only host. That code now lives in
   `inference/torch_device.py`, so the tests moved to
   `tests/test_inference_torch_device.py`: **one copy instead of two**, patching
   that module's globals, with every assertion and every explanatory docstring
   unchanged. Same for the PCM round-trip test, now
   `tests/test_inference_torch_audio.py`.
   - Moved: `test_device_stats_report_the_devices_real_memory_figures`,
     `test_device_stats_are_absent_on_cpu`,
     `test_reset_peak_memory_only_touches_cuda`,
     `test_a_reset_restarts_the_high_water_mark_from_the_resident_allocation`,
     `test_nvml_is_initialised_once_and_not_per_sample`,
     `test_nvml_shuts_down_at_teardown`,
     `test_a_missing_nvml_binding_costs_one_failed_import`,
     `test_a_driver_failure_mid_job_does_not_break_the_snapshot`,
     `test_planar_conversion_round_trips_through_the_pcm_module`.
2. **Stayed, patch target retargeted.** Two tests are about a whole *run* and
   need a real separator, so they stayed in both backends' suites; the module
   whose global they patch moved from the backend to the skeleton.
   - `test_the_peak_is_reset_once_per_run_not_once_per_device` — patches
     `torch_separator.reset_peak_memory` (026's review finding: the peak is
     reset per **run**, not per device placement). Assertion unchanged:
     `resets == ["cpu", "cpu"]`.
   - `test_a_failure_removes_every_stem_it_had_already_written` — patches
     `torch_separator.write_wav`, because `_encode` is the skeleton's now.
     Assertions unchanged.
3. **Added.** `test_a_second_run_of_one_separator_is_byte_identical_to_the_first`
   in both suites; two device-resolution tests and a channel-folding test in the
   new shared files.

`tests/test_torch_optional.py` needed two data updates, both of which keep it
proving what it proved: `NvmlProbe` left the two backend packages' lazy-export
lists (it is not re-exported from them any more — it lives in `torch_device`,
which nothing outside the backends imports), and the three new torch-importing
modules joined `BACKEND_MODULES`, whose stated invariant is "every Straticate
module that imports torch at module scope".

**No seam failed to survive the extraction.** The one that needed care was
`cuda_namespace()`; it survived because it is still a module-level function that
`device_stats` calls by name, in a module a test can patch — the only thing that
changed is which module.

## Preserved deliberately

- `separator_unavailable` (501) and `model_weights_missing` (409) envelopes —
  the 501 lives in `registry.py` and was not touched; the 409 is now defined
  once, so the two backends cannot drift.
- `runtime_stats()` as a cheap, non-blocking snapshot: three allocator queries,
  a cached device-property lookup and two NVML queries against a handle
  initialised once per process.
- NVML strictly optional, initialised at most once, shutdown at `atexit`.
- CUDA peak memory reset **per run**, in `TorchSeparator._separate`.
- The PR #45 `_place_on_device` fix: `_loaded_device` is set to `None` *before*
  the move, so a partial `.to()` cannot wedge a cached separator. Now one copy;
  both backends' regression tests still exercise it through their own class.
- Per-model caching and the `threading.Lock` arrangement in `registry.py` —
  untouched.
- Demucs' name-based stem mapping, `_check_sources` and `_check_sample_rate` —
  untouched, and still called in the same order relative to the weights check.
- Construction order in both `__init__`s: the `ffmpeg_timeout_seconds`
  `ValueError` first (it is `super().__init__`'s first statement), then each
  backend's own checks in their original order.

## Acceptance criteria

- [x] Duplication substantially eliminated, measured with `difflib` and reported
      before/after: **781/1,591 (49.1%) → 260/1,024 (25.4%)**, largest identical
      block **154 → 17** lines
- [x] Both separators produce **bit-identical** output to `dev` — proved by
      SHA-256 per stem, both models, on **CPU and CUDA**
- [x] 026's and 028's test seams still work; no test's intent was rewritten
      (every move and retarget listed above)
- [x] Integration tier passes for both backends: **9 passed** on `cuda:0`
      (verified `torch 2.13.0+cu130`, `torch.cuda.is_available() == True`, run
      through the CUDA interpreter directly rather than `uv run --extra torch`),
      **7 passed, 2 skipped** on CPU (the two `gpu`-marked tests)
- [x] `_run_chunks` documented as feature 038's extension point
- [x] Backend gates green, suite clean under `-W error` (**823 passed** on the
      CPU environment; **821 passed, 2 skipped** on the CUDA one, the two skips
      being the "a CUDA device on a CPU-only host" tests, which skip when the
      host *has* CUDA); frontend gates green (no frontend file changed)

## Notes / decisions

- **For feature 038.** `_run_chunks` is yours. Its contract is documented on
  `TorchSeparator._run_chunks`; keep it, and the skeleton and both backends stay
  correct. Two observations recorded rather than acted on, because they are
  memory work and therefore 038's: RoFormer allocates its `weights` accumulator
  at the full `(stems, channels, samples)` shape when a `(samples,)` vector
  would do (the same value is broadcast across every stem and channel), and both
  backends hold the whole mixture *and* the whole accumulator on the device for
  the length of the run. Neither was changed here — this feature's output had to
  hash the same.
- **A third architecture is now two methods, a parameters dataclass and a
  loader**, not another copy of the lifecycle.
- The `RunState` fields are written by a worker thread and read by
  `runtime_stats()` on the event loop. That was already true; it is now stated in
  one docstring instead of implied in two.
