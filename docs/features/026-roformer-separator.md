# [026] Real separator — HQ vocals (Mel-Band RoFormer)

Branch: `026-roformer-separator`
Status: PR OPEN
Dependencies: 014, 018, 025
PR: #…

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
      CPU; NVML remains optional. *(The CUDA branch was **not executed** — see
      "What was and was not validated".)*
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
summary, four changes, each marked at its site:

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

**The second stem is a residual.** The checkpoint has `num_stems: 1` and emits
vocals; `instrumental` is `mixture - vocals`, computed in the float domain before
either stem is quantized, so the two reconstruct the mixture to within one LSB
(there is a test). Which stem is a residual is *derived*, not configured: the
catalog already states `stems` and (through the hyperparameters) `num_stems`, and
a third place to say the same thing is a third place for them to disagree.

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

### The two items feature 029 deferred to here

**Separator construction no longer runs on the event loop.**
`SeparatorRegistry.aget()` runs the builder in `asyncio.to_thread` and takes a
**per-model** lock, re-checking the cache inside it: two submissions racing for
one model load it once and share the instance, which for a 228-million-parameter
network is a gigabyte rather than a millisecond. Different models still load in
parallel. `get()` stays for synchronous callers and is documented as blocking.

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

This host is **CPU-only**: `GET /api/v1/system/devices` returns one device,
`cpu` / `Intel64 Family 6 Model 170 Stepping 4`.

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

**Not validated — stated plainly:**

- **The CUDA path was never executed.** No GPU was present. `torch.autocast`,
  the CUDA memory figures in `runtime_stats()`, `reset_peak_memory_stats`, the
  `cuda:N` device mapping and the flash-attention backend selection are written
  and type-checked but **unrun**. `test_cuda_runtime_stats_report_real_memory`
  exists, carries `@pytest.mark.gpu`, and skipped. No GPU telemetry number in
  this document or anywhere else is measured; none has been fabricated.
- **NVML** (`utilization`, `temperature_celsius`) was likewise never exercised;
  the binding is not installed and never becomes a dependency.
- **A real music file** was never separated — nothing copyrighted was
  downloaded. The quality figures above come from a synthesised voice over a
  generated backing, which is a real measurement of a real model but is not the
  same as a mastered recording.
- **Only this one checkpoint** was tried, at its own hyperparameters.

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
- **`model_weights_invalid` and `model_parameters_invalid` are `500`s.** They are
  deployment faults — a corrupted install, or a catalog entry that does not match
  its checkpoint — and there is nothing a client can do about either.
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
