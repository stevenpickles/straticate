# [026] Real separator — HQ vocals (Mel-Band RoFormer)

Branch: `026-roformer-separator`
Status: PR OPEN
Dependencies: 014, 018, 025
PR: #33

## Objective

Straticate performs a **real** vocal separation. A Mel-Band RoFormer runs behind
the existing `Separator` seam with real chunk-grained progress, real cooperative
cancellation and real device/memory telemetry — on CUDA when available, on CPU
otherwise — over weights installed by feature 025 and verified by SHA-256. This
is milestone **M2**.

## Scope

- **`backend/src/straticate/inference/roformer/`**
  - `vendor/` — a pinned copy of the Mel-Band RoFormer architecture with its
    `LICENSE` and a `README.md` recording provenance and every modification,
    plus `mel_filters.py` (Straticate's librosa-free mel filter bank).
  - `separator.py` — `RoFormerSeparator`, `RoFormerParameters`,
    `ROFORMER_ARCHITECTURE`. Decode → device → chunked overlap-add → residual →
    stems, with stages, progress, cancellation and `runtime_stats()`.
- **`inference/registry.py`** — `roformer_separator_builder`, the
  `InferenceParameterSource` seam, and `SeparatorRegistry.aget()`, which builds
  off the event loop.
- **`models/catalog.py`** — `default_inference_parameters` is now *retained* on
  `CatalogEntry` (still absent from the public `Model`) and reachable through
  `ModelCatalog.inference_parameters(model_id)`.
- **`jobs/resolution.py`** — `resolve_device(..., model=…)` consults
  `Model.capabilities`.
- **`api/jobs.py`** — awaits `registry.aget(model)` and resolves the device with
  the model.
- **`models/catalog.json`** — the `vocals-hq-001` entry: artifact (real URL,
  size and SHA-256), licensing, `quality_tier: high_quality`, and the
  checkpoint's hyperparameters as `default_inference_parameters`.
- **`backend/pyproject.toml`** — `torch` (pinned to the CPU wheel index),
  `numpy`, `einops`, `rotary-embedding-torch`, `beartype`; Ruff/Pyright
  exclusions for the vendored files; the `integration` / `gpu` pytest markers
  and `addopts = "-m 'not integration'"`.
- `ARCHITECTURE.md` §7/§9/§14, `DEVELOPMENT.md`, `docs/contracts/rest-api.md`,
  the ROADMAP row.

## Out of scope

- Features 027 (MDX fast tier) and 028 (4-stem). One architecture, one model.
- Any frontend source beyond the regenerated `api/generated/api.d.ts`.
- A model-management UI; changing which models a mode offers.
- `api/results.py`, `errors.py`, `conftest.py`, `main.py`'s logging wiring —
  feature 031 owns those.

## Expected modules/files

- `backend/src/straticate/inference/roformer/{__init__,separator}.py`
- `backend/src/straticate/inference/roformer/vendor/{__init__,attend,mel_band_roformer,mel_filters}.py`,
  `vendor/{LICENSE,README.md}`
- `backend/src/straticate/inference/{__init__,registry}.py`,
  `models/catalog.py`, `jobs/resolution.py`, `api/jobs.py`, `main.py`
- `backend/tests/{roformer_fixtures,test_roformer_separator,test_roformer_mel_filters,test_roformer_integration}.py`
  plus additions to `test_inference_registry.py`, `test_jobs_resolution.py`,
  `test_api_jobs.py`, and updates to `test_devices.py`, `test_model_catalog.py`,
  `test_model_installer.py`, `test_models_api.py`
- `models/catalog.json`, `backend/pyproject.toml`, `backend/uv.lock`
- `frontend/src/api/generated/api.d.ts` (descriptions only — see below)

## Acceptance criteria

- [x] A real vocal separation runs end to end through the existing job pipeline
      and produces genuinely separated stems (measured below).
- [x] The vendored checkpoint loads with **no missing or unexpected keys**.
- [x] Progress is real work (chunks completed / total), not a timer.
- [x] Cancellation stops the run promptly at a chunk boundary and leaves no
      partial stem.
- [x] `runtime_stats()` reports real memory figures on CUDA and `gpu: null` on
      CPU; NVML remains optional. *(The CUDA branch was not executed when this
      feature shipped; it was executed on real hardware on 2026-08-25 and
      passed — see "What was and was not validated".)*
- [x] Weights absent → `model_weights_missing` (409), not a crash.
- [x] A model whose `capabilities` exclude the resolved device is rejected at
      **create** time with `model_device_unsupported` (409), documented in
      `docs/contracts/rest-api.md`.
- [x] Separator construction does not block the event loop, proven by a test at
      the registry (`test_aget_builds_off_the_event_loop`) and through the API
      (`test_loading_a_separator_does_not_stall_the_application`).
- [x] Normal CI needs no GPU, no model download, and does not grow by
      gigabytes; every pre-existing test still passes.
- [x] The catalog entry's `sha256` is the real hash of the real file.
- [x] The licence of both the vendored code and the checkpoint is stated with
      evidence below.
- [x] `ruff format --check` · `ruff check` · `pyright` (strict) · `pytest`
      (also under `-W error`) green; frontend gates green.

## Required tests

- `test_roformer_mel_filters.py` — the vendored mel filter bank against band
  widths **read out of the real checkpoint**, so drift fails in normal CI with
  no weights present.
- `test_roformer_separator.py` — the full contract against a synthetic
  checkpoint built at test time (`roformer_fixtures.py`): stem list and playable
  WAVs, mixture reconstruction, mono handling, exact stage sequence,
  chunk-grained monotonic progress, chunk count following `chunk_size`, progress
  arriving from a worker thread, the loop staying responsive, cancellation
  mid-run and before the first chunk, cleanup after a mid-encode failure,
  `runtime_stats()` before/during/after, every error code, one-separation-at-a-
  time, and the catalog-parameter validation.
- `test_inference_registry.py` — the RoFormer builder configured purely from a
  catalog entry, `model_weights_missing`, `aget` off-loop / build-once /
  no-suspend-on-hit / no-negative-caching.
- `test_jobs_resolution.py` — every branch of the capability check.
- `test_api_jobs.py` — `model_device_unsupported` and `model_weights_missing` as
  create-time 409s, the same request succeeding once weights exist, and other
  routes served while a separator loads.
- `test_roformer_integration.py` — **deselected by default**: the real state
  dict loading with no missing/unexpected keys, the installed file re-hashed
  against the pinned digest, a real separation end to end, and the CUDA
  telemetry path (skipped without a GPU).

## Notes / decisions

### Licensing — what was verified, and how

**Vendored code: MIT.** Taken from
[`openmirlab/melband-roformer-infer`](https://github.com/openmirlab/melband-roformer-infer)
at tag `v0.1.5`, commit `fda5d8cb65403a04e2d143ecd130f508c2f8370f`. The GitHub
API reports `license.spdx_id: "MIT"`, the PyPI metadata for
`melband-roformer-infer 0.1.5` carries the full MIT text (Copyright (c) 2025
OpenMIRLab), and the repository's `LICENSE` was copied verbatim into
`vendor/LICENSE`. Upstream in turn adapts `lucidrains/BS-RoFormer` (MIT) and
Kimberley Jensen's Mel-Band-Roformer-Vocal-Model (MIT); both are named in
`vendor/README.md` and in the catalog entry's `licensing.attribution`.

**Checkpoint: MIT, relicensed from GPL-3.0.** `KimberleyJSN/melbandroformer` on
Hugging Face (`MelBandRoformer.ckpt`, "Kim Vocal 2"). The claim that it was
relicensed on 22 April 2026 was checked, not assumed:

| evidence | value |
| --- | --- |
| current model card (`.../raw/main/README.md`) | `license: mit` |
| model card at the previous revision `f45f9e3d…` (17 Jun 2025) | `license: gpl-3.0` |
| commit that changed it | `ac9b0614ab3cd7f77219e18ba494dfd93956c348`, by `KimberleyJSN`, **2026-04-22T12:30:27Z** |
| repo metadata tags | `license:mit` |

So the licence change is the author's own commit, on the stated date, in the
repository the weights are served from. Both licences are recorded in the
catalog entry (`code_license` / `weights_license`), which feature 025 surfaces
on `GET /models` *before* a user installs anything.

### The checkpoint's SHA-256, and how it was obtained

```text
87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e
913 106 900 bytes
```

Three independent confirmations:

1. The file was downloaded with `curl` from the pinned revision URL and hashed
   locally with `sha256sum`.
2. Hugging Face's `X-Linked-ETag` on that object — the git-LFS SHA-256 it stores
   — is the same string, as is `X-Linked-Size` for the byte count.
3. The catalog entry was then installed **through feature 025's own installer**,
   which verifies the pinned digest before publishing; it reported `installed`
   in 50 s with no error, so the pinned value is what a real install actually
   sees.

The URL pins the **revision**, not `main`
(`.../resolve/94a0e5de2622a4160198b158e6f8141296da887e/MelBandRoformer.ckpt`), so
a future upload to that repository cannot change what the digest is checked
against.

### Vendor, do not depend

A checkpoint only loads into code whose module structure and hyperparameters
match it exactly, so architecture code that a published checkpoint is pinned to
is *pinned source*. A routine `uv lock --upgrade` must not be able to rename a
submodule or reshape a layer. Upstream's own API is also folder-oriented — "every
WAV in `input_folder` produces …" — which can offer neither per-chunk progress
nor cooperative cancellation; what Straticate needs is the model class, and the
loop belongs here, next to the job contract. Depending on a high-level
separation library was rejected outright: they ship their own model registries
and downloaders, duplicating features 010 and 025.

`vendor/README.md` lists exactly what was copied and every modification. In
summary, five changes, each marked at its site:

1. **`librosa` replaced by `mel_filters.py`.** Upstream calls
   `librosa.filters.mel(...)` once, at construction. Depending on librosa for one
   pure function pulls numba, llvmlite, scipy, soundfile, pooch and friends — 25
   packages, ~90 MB of wheels, measured. `mel_filters.py` transcribes librosa's
   own implementation, restricted to the defaults this architecture uses, and was
   verified **bit-identical** to `librosa.filters.mel` across 245
   `(sample_rate, n_fft, n_mels)` combinations (`np.array_equal`, not
   `allclose`). This is the one modification that could silently break the
   checkpoint, so it has two guards: a normal-CI test pinning the 60 band widths
   the real checkpoint's layer shapes require, and the integration test that
   loads the real state dict.
2. **The deprecated SDPA backend selector.**
   `torch.backends.cuda.sdp_kernel(...)` → `torch.nn.attention.sdpa_kernel([...])`.
   Same backends per device class; only the API expressing it changed. It emits a
   `FutureWarning` otherwise, and the suite treats a warning as a finding.
3. **`print_once` → `logging`.** A server process does not print to stdout.
4. **An explicit rectangular window** on the constructor's one-off `torch.stft`
   bin-count probe, which torch warns about otherwise. The probe reads only
   `.shape[1]`, which no window can change.

5. **Docstring headers** in both files gained a `VENDORED CODE` banner pointing
   at `vendor/README.md`.

The vendored `attend.py` and `mel_band_roformer.py` are excluded from Ruff and
Pyright (`backend/pyproject.toml`) so the copy stays diffable against upstream.
`mel_filters.py` is Straticate's code and is **not** excluded.

### How the separation is shaped

Stages, all real: `decoding` (FFmpeg, via `inference/pcm.py` — the same decoder
the fake separator uses) → `loading_model` (the network moves onto the compute
device) → `separating` (the chunk loop) → `post_processing` (residual stem,
source channel layout, 16-bit quantization) → `encoding` (one WAV per stem,
`.part`-then-rename).

The chunk loop follows upstream's `demix_track` numerically — same 352 800-sample
window, same `num_overlap: 2`, same linear fade at each edge, same reflect-padded
borders, same division by accumulated window weight — but is reimplemented rather
than copied so that it can report progress, check the cancellation token between
windows, and run inside `asyncio.to_thread`. Each window is one forward pass
through a 228-million-parameter network, so `chunks_completed / chunks_total` is
a statement about work done.

**The second stem is a residual, and the catalog says which one.** The
checkpoint has `num_stems: 1` and emits vocals; `instrumental` is
`mixture - vocals`, computed in the float domain before either stem is quantized,
so the two reconstruct the mixture to within one LSB (there is a test).

The first version of this *derived* which stem was the residual — "it is the last
one advertised" — on the grounds that the catalog already states `stems` and
`num_stems` and a third statement is a third thing to disagree. **Review found
that reasoning wrong, and it was the most dangerous defect in the feature.**
`stems` has no ordering constraint in the manifest schema, and this architecture's
whole promise is that another checkpoint is a pure data edit; an entry written
`"stems": ["instrumental", "vocals"]` would have had the network's vocals written
to `instrumental.wav` and the residual to `vocals.wav`, with nothing anywhere
reporting a problem. Silently wrong audio is the worst thing this module can
produce.

So the residual is **named**, in
`default_inference_parameters.output.residual_stem`, and every other shape is
refused at construction with `model_parameters_invalid`: a residual implied but
not named, a residual named that the model does not advertise, and a residual
named for a network that emits every stem itself. The remaining stems map to the
network's outputs in advertised order — fully determined for a one-output model,
and for a multi-output one the same ordering contract the `advertised ==
produced` case already carries. A test separates the same audio under both stem
orders and asserts `vocals.wav` comes out byte-identical either way; it fails
against the derived version.

**A mono source gets mono stems.** The checkpoint is stereo-only, so a mono input
is duplicated for the network and folded back afterwards. The application never
learns that this particular model has an opinion about channels.

### Where per-model tuning lives

In the catalog, as ARCHITECTURE.md §9 always intended.
`default_inference_parameters` carries the checkpoint's hyperparameters plus its
chunking, and feature 010 dropped it on load — correct for the API, wrong for the
separator that needs it. It is now retained on `CatalogEntry` beside `artifact`,
under the same rule: private to the machinery, invisible to every response (a
test asserts it never appears on a `Model`).

The registry reaches it through a one-argument `InferenceParameterSource` lookup
rather than by importing the catalog service, so `inference/registry.py` still
knows nothing about `ModelCatalog`. Unknown keys in the block are **rejected**,
not ignored the way upstream's config loader ignores them: a key in
`models/catalog.json` is something a maintainer typed, and a silently ignored typo
is a model running with the wrong hyperparameters.

**Adding a second Mel-Band RoFormer checkpoint is a pure data edit** — a new
entry with its own `artifact` and `default_inference_parameters`, no code.

### Telemetry: two defects review found on the unrun CUDA path

Both were exactly where the "written and type-checked but unrun" caveat said to
look, and both are now covered by tests that run on a CPU-only host, through a
single seam (`cuda_namespace()`) that a double replaces.

**The peak memory figure was a previous job's.**
`torch.cuda.max_memory_allocated` is a per-device high-water mark that only an
explicit reset clears, and the reset sat in `_place_on_device`, which
early-returns when the network is already on the device it wants. So the first
CUDA job reset it and every later one inherited whatever the largest job before
it had reached: a ten-second track after a six-minute one would have reported the
six-minute track's peak as its own, under a docstring promising that every number
is measured. The reset is now per **run**, immediately before the chunk loop; it
resets to the *current* allocation, so the resident network still counts.

**NVML was initialised and shut down on every sample, on the event loop.**
`runtime_stats()` is polled directly on the loop by
`straticate.telemetry.sampler`, deliberately, because `inference/base.py`
promises "a cheap, non-blocking snapshot". An `nvmlInit()`/`nvmlShutdown()` pair
is tens of milliseconds of driver setup and teardown — once a second, for the
length of a job, in front of every WebSocket frame, job event and HTTP request
the loop owes somebody. `NvmlProbe` now loads and initialises the binding once,
caches the device handles, and leaves shutdown to `atexit`, which is how a
long-running NVML consumer is meant to behave; what remains per sample is two
driver queries. An absent binding costs one failed import per process rather than
one per sample, and a driver error mid-job empties the two optional fields
without touching anything else. NVML is still never a dependency.

### The two items feature 029 deferred to here

**Separator construction no longer runs on the event loop.**
`SeparatorRegistry.aget()` runs the builder in `asyncio.to_thread`, and the
build takes a **per-model** lock and re-checks the cache inside it: two
submissions racing for one model load it once and share the instance, which for a
228-million-parameter network is a gigabyte rather than a millisecond. Different
models still load in parallel. `get()` stays for synchronous callers, is
documented as blocking, and shares the same build-once path.

That lock is a `threading.Lock`, not an `asyncio.Lock` — review's finding. An
`asyncio.Lock` binds to whichever loop first *contends* for it and then stays in
the registry, so a build that fails leaves it bound with nothing cached, and the
next contended attempt from another loop (a synchronous `TestClient` block after
an async client on the same app, a second `asyncio.run`, the next test's
function-scoped loop) raised `RuntimeError: ... is bound to a different event
loop` instead of building. A thread lock is also what actually guards the build,
since the build already runs in a thread, and nothing is held across an `await`.
The regression test needs two racing callers per loop, because an *uncontended*
`asyncio.Lock` acquire returns on a fast path that never looks at the loop at
all — which is precisely why the defect survived the first round.

The `await` in `create_job` sits *before* `submit`, deliberately. What feature
019 needs to stay atomic is `submit` → sampler registration — the job ID does not
exist until `submit` returns, and a suspension between the two would let the
worker emit `job_started` before the sampler knew which separator to poll.
Resolution → submit has no such requirement.

**`Model.capabilities` is now consulted.** Two cases, treated differently on
purpose: an explicit `device_id` the model does not support is refused with
`model_device_unsupported` (409) rather than silently swapped, while a request
that pinned *no* device gets the first detected device the model does support
(still CUDA before CPU) and only errors when no detected device can run it at
all. "Let the backend pick" is a request to pick something that works. Absence in
`capabilities` counts as refusal: the set is open, so a backend the manifest does
not mention is one nobody has claimed the weights run on.

### `quality_options` still offers uninstalled models

Feature 025 left this open and asked 026 to decide with the case in hand. **Left
as it is.** Now that `POST /jobs` answers `model_weights_missing` (409) with the
model ID in `detail`, and `GET /models` already carries `installation.state`,
`requires_download` and `total_bytes` per model, a client has everything it needs
to render the high-quality tier with an "Install" affordance instead of hiding
it. Hiding a tier would make the product silently *less* capable on a fresh
checkout and would give the user no way to discover the model exists. The
decision properly belongs with the model-management UI, which is a later feature.

### Keeping CI lean

`torch` is now a runtime dependency (exactly as feature 018 anticipated: same
probe, same detector, same endpoint, no API change). The default PyPI wheel for
Linux bundles the CUDA runtime — gigabytes per CI run for a machine with no GPU —
so `backend/pyproject.toml` pins `torch` to PyTorch's CPU wheel index with
`explicit = true`, which leaves every other package on PyPI. DEVELOPMENT.md
documents the one command that installs a CUDA build instead.

The other four additions are deliberately small: `einops`,
`rotary-embedding-torch` and `beartype` are what the vendored file imports (all
pure Python, none of them a model registry or a downloader), and `numpy` is
declared because torch warns at *import* time without it, which the suite would
see as a finding under `-W error`. Replacing librosa with `mel_filters.py` avoided
25 further packages.

No test downloads anything. The real separator's plumbing is exercised against a
synthetic checkpoint built at test time — 8 mel bands, one layer, a 64-point STFT
at 8 kHz, ~20 000 random parameters, an 80 KB state dict — whose audio is
meaningless and whose control flow is identical.

### What was and was not validated

This host was **CPU-only** while this feature was developed:
`GET /api/v1/system/devices` returned one device, `cpu` /
`Intel64 Family 6 Model 170 Stepping 4`. It has a GPU now — see *Validated
later, on real hardware* below.

**Validated by running it:**

- The vendored architecture loads the real Kim Vocal 2 checkpoint with
  **`missing_keys == []` and `unexpected_keys == []`**, 228 202 852 parameters.
  This is the check that proves the vendoring matches the checkpoint.
- The installer downloaded the artifact from the pinned URL and verified the
  pinned SHA-256 (50 s, 913 106 900 bytes), then the integration test re-hashed
  the installed file and matched.
- A full job through the real HTTP API on a generated 20 s stereo mix: upload →
  `POST /jobs` (`201`, device resolved to `cpu`, model `vocals-hq-001`) →
  `decoding` → `separating` → `completed` → `GET /result` → both stems streamed
  from `GET /jobs/{id}/stems/{name}`.
- **Separation quality, measured.** A 20 s mixture was built from two *known*
  sources — a locally synthesised speech track (Windows SAPI; no third-party
  audio, nothing committed) and a generated bass/drum/guitar backing — and
  separated:

  | correlation | value |
  | --- | --- |
  | vocals stem ↔ true voice | **+0.993** |
  | vocals stem ↔ true backing | −0.001 |
  | instrumental stem ↔ true backing | **+1.000** |
  | instrumental stem ↔ true voice | +0.003 |
  | *mixture* ↔ true voice (for reference) | +0.231 |
  | vocals stem ↔ instrumental stem | +0.000 |

  The vocals stem is not a copy of anything: the mixture correlates with the
  voice at 0.23 and the extracted stem at 0.99.

- **CPU performance.** 10 s of audio in 49.3 s (**RTF 0.203**); 20 s of audio in
  69.5–74.2 s (**RTF 0.269–0.288**) on this laptop CPU. Roughly 3.5–5× slower
  than real time.
- Interestingly, a first attempt using a *synthetic* vowel-like tone as the
  "vocal" produced a near-silent vocals stem (RMS 0.0002). That is the model
  behaving correctly — it is trained to find human voices, not sawtooth vowels —
  and it is why the measurement above uses real synthesised speech.

**Not validated when this feature shipped — stated plainly:**

- **The CUDA path was never executed on real hardware.** No GPU was present.
  `torch.autocast`, the flash-attention backend selection, and the actual
  behaviour of the CUDA allocator are written and type-checked but **unrun**.
  `test_cuda_runtime_stats_report_real_memory` exists, carries
  `@pytest.mark.gpu`, and skipped. No GPU telemetry number in this document or
  anywhere else is measured; none has been fabricated.

  Review's two findings landed on exactly this path, which is the argument for
  saying so. What *can* be checked without a GPU now is: `device_stats()` against
  a `torch.cuda` double (device ID, name, allocated/peak/total, and `None` on
  CPU), the peak reset happening once per run, and the NVML lifecycle. Those are
  real tests in normal CI; they do not make the claim "it works on CUDA", and
  nothing here should be read as making it.

  **Superseded on 2026-08-25 — see *Validated later, on real hardware* below.**
- **NVML** (`utilization`, `temperature_celsius`) was never exercised against a
  real driver; the binding is not installed and never becomes a dependency. Its
  *lifecycle* — initialised once, handles cached, never shut down mid-job,
  absent-binding and driver-failure paths — is tested against a double.

  **Superseded on 2026-08-25**, with `nvidia-ml-py` — which is the package to
  install, and not `pynvml`; see below.
- **A real music file** was never separated — nothing copyrighted was
  downloaded. The quality figures above come from a synthesised voice over a
  generated backing, which is a real measurement of a real model but is not the
  same as a mastered recording.
- **Only this one checkpoint** was tried, at its own hyperparameters.

### Validated later, on real hardware (2026-08-25)

A GPU became available after this feature merged, so the two "unrun" items above
were run. **CUDA is verified.** NVIDIA GeForce RTX 4060 Laptop GPU (8,188 MiB,
driver 610.47 / CUDA 13.3) with `torch 2.13.0+cu130`; nothing in this feature
changed to make it work — swapping the wheel made `cuda:0` appear first and jobs
resolved to it, exactly as feature 018 designed and as this document assumed.

- The full integration tier passes **4/4**, including
  `test_cuda_runtime_stats_report_real_memory`, which had never executed.
- A job driven through the real HTTP API resolved to `cuda:0`, reported real
  chunk progress, and delivered a `runtime_metrics` event carrying genuine GPU
  figures over the WebSocket.
- **Performance**, 30 s clip: 100.5 s on CPU (RTF 0.299) against 6.7 s on
  `cuda:0` (RTF 4.496) — **~15× faster, and faster than real time**. The CPU
  figures earlier in this document stand; they are CPU figures and are labelled
  as such. A 3-minute track separates in about 40 s instead of 10 minutes, which
  changes what the CPU-oriented "Next" reasoning in the ROADMAP is about but not
  whether it is right — most hosts still have no GPU.
- **NVML works, and the package to install is `nvidia-ml-py`** — not `pynvml`,
  which is a deprecated shim whose import hook raises `FutureWarning` from
  inside `torch/cuda/__init__.py` and so breaks the whole suite under `-W error`
  at `import torch`. With `nvidia-ml-py 13.610.43` the two optional fields
  report (`utilization: 1.0`, 59–66 °C) and the suite stays clean. The binding
  is still not a dependency and never becomes one. DEVELOPMENT.md, *Optional:
  NVML*, carries the traceback and the recovery.

**Memory**: peak allocation on a 30 s clip is **1,575 MiB** at this model's
`chunk_size: 352800` / `num_overlap: 2`. That figure is *not* bounded by
chunking — the "Whole-track memory" limitation below puts the mixture, the
accumulator and the weight tensor on the device for the whole track, so the peak
grows about 1.35 MiB per second of audio, reaching 2,343 MiB on a 10-minute
track. Feature 036 measured that properly and corrected the catalog's
`recommended_vram_mb` from it; see `docs/features/036-gpu-validation-followups.md`
for the method, the parameters and the whole-device figures.

The four defects this exercise exposed — none of them reachable without a GPU —
are feature **036**.

### `frontend/src/api/generated/api.d.ts`

Regenerated and committed, and the diff is **descriptions only** — the
`create_job` docstring gained the new error codes and the offloading note. No
request or response shape changed anywhere in this feature, and no other frontend
file moved.

## Known limitations

- **Whole-track memory.** The decoded mixture, the accumulator and the weight
  tensor all live in memory at once (`num_stems × channels × samples × 4` bytes
  each), following upstream's `demix_track`. A 10-minute stereo track is roughly
  half a gigabyte of float32 on top of the model. Streaming overlap-add is future
  work.
- **Cancellation granularity is one chunk**, which on CPU is several seconds of
  wall clock at the default 8 s window. It is prompt in chunks, not in
  milliseconds.
- **The model is moved to the device on every run** if the device changed; there
  is no eviction, so a long-lived process holds the network on whichever device
  it last used. With one model and one job at a time this is the intended
  behaviour, not an oversight.
- **`float32` only.** CUDA gets `torch.autocast`, but there is no explicit
  half-precision or `channels_last` tuning, no batching of windows, and no
  `torch.compile`. All of those are performance work with a real GPU to measure
  on.
- **`model_weights_invalid` and `model_parameters_invalid` are `500`s**, and
  because a separator is built inside `POST /jobs`, they are answers to *that*
  request rather than job-failure codes. They are deployment faults — a corrupted
  install, or a catalog entry that does not match its checkpoint — and there is
  nothing a client can do about either. Both are in the create-job error table in
  `docs/contracts/rest-api.md`, and both have an API-level test.
- **Installed weights are never re-verified** on load (feature 025's documented
  limitation); the integration tier is the only thing that re-hashes them.
- **No frontend affordance** for installing a model, so the high-quality tier is
  offered but a user has no in-app way to make it work yet. That is the
  model-management UI, and it is a later numbered feature.

## Noticed, out of scope

- `ModelInstaller.describe()` takes a `CatalogEntry`, but the name and the way
  the routes call it read as though it took a `Model`; passing a `Model` fails
  with an unhelpful `AttributeError` deep inside pydantic. A type annotation
  would have caught it. Not touched — it is feature 025's code and works
  correctly through the routes.
- `Job` does not carry `chunks_completed` / `chunks_total` on the REST resource,
  so a client that reconnects mid-job learns the fraction but not the chunk
  counts until the next `job_progress` event. Possibly deliberate; worth a look
  when the progress UI is revisited.
