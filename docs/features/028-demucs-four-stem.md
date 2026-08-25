# [028] 4-stem separation (Hybrid Transformer Demucs)

Branch: `028-demucs-four-stem`
Status: PR OPEN
Dependencies: 014, 018, 025, 026, 034
PR: #45

## Objective

`standard_stems` has a real model. Feature 032 correctly stopped offering the
fake four-stem fixture to users, which left that separation mode with nothing
behind it — so a default server could only do vocals. This feature restores
four-stem separation with a real model behind the existing `Separator` seam:
Hybrid Transformer Demucs (`htdemucs`, v4), producing `vocals`, `drums`, `bass`
and `other` with real chunk-grained progress, real cooperative cancellation and
real device telemetry, on CUDA when available and on CPU otherwise.

## Scope

- **`backend/src/straticate/inference/demucs/`**
  - `architecture.py` — the architecture *name* (`htdemucs`), torch-free, so the
    registry can key its builder map by it (feature 034's pattern).
  - `vendor/` — a pinned copy of the Hybrid Transformer Demucs architecture with
    its `LICENSE` and a `README.md` recording provenance and every modification.
  - `separator.py` — `DemucsSeparator`, `DemucsParameters`, the restricted
    checkpoint reader, and the chunked overlap-add loop with stages, progress,
    cancellation and `runtime_stats()`.
- **`inference/registry.py`** — `demucs_separator_builder`, registered under
  `htdemucs`, imported lazily inside `build`.
- **`inference/__init__.py`** — `DEMUCS_ARCHITECTURE` eagerly,
  `DemucsParameters` / `DemucsSeparator` lazily.
- **`models/catalog.json`** — the `standard-stems-001` entry: artifact (real URL,
  size and SHA-256), licensing, `quality_tier: balanced`, measured
  `requirements`, and the checkpoint's hyperparameters as
  `default_inference_parameters`.
- **`backend/pyproject.toml`** — Ruff/Pyright exclusions for the new vendored
  files. **No new dependency.**
- `ARCHITECTURE.md` §9, `docs/contracts/rest-api.md`, `models/catalog.py`'s
  `_derive_modes` docstring — one sentence each, all of them statements that
  `standard_stems` has no real model, which this feature makes false.
- `.gitignore` — `models/**/*.th`, the suffix Demucs checkpoints use.
- The ROADMAP ledger row.

## Out of scope

- `frontend/**` — feature 037 owns it. **No frontend file was touched**, and
  none needed to be: no schema changed, so `api/generated/api.d.ts` is
  unmodified.
- Features 027 (blocked) and 038 (streaming overlap-add).
- Changing what the RoFormer separator does; changing 032's filtering; a
  model-management endpoint.

## Expected modules/files

- `backend/src/straticate/inference/demucs/{__init__,architecture,separator}.py`
- `backend/src/straticate/inference/demucs/vendor/{__init__,htdemucs,hdemucs,demucs,transformer,spec,utils,wiener,states}.py`,
  `vendor/{LICENSE,README.md}`
- `backend/src/straticate/inference/{__init__,registry}.py`,
  `backend/src/straticate/models/catalog.py`
- `backend/tests/{demucs_fixtures,test_demucs_separator,test_demucs_integration}.py`
  plus additions to `test_inference_registry.py`, `test_torch_optional.py`, and
  updates to `test_model_catalog.py`, `test_models_api.py`, `test_api_jobs.py`
- `models/catalog.json`, `backend/pyproject.toml`, `.gitignore`

## Acceptance criteria

- [x] A real four-stem separation runs end to end through the job pipeline, with
      stems that are genuinely separated (measured against ground truth, with a
      mixture baseline — see *Separation quality*).
- [x] `GET /separation-modes` offers `standard_stems` again on a default server,
      with four stems.
- [x] The checkpoint loads with **no missing and no unexpected keys**
      (41,984,456 parameters).
- [x] Stem-to-file mapping is explicit and cannot silently mis-assign; a
      reordered catalog stem list maps correctly, and a transposed `sources`
      list fails loudly against the checkpoint's own record.
- [x] Progress is real work; cancellation is prompt and leaves no partial stem.
- [x] `runtime_stats()` is a cheap non-blocking snapshot; `gpu: null` on CPU.
- [x] Weights absent → `model_weights_missing` (409); backend unavailable →
      `separator_unavailable` (501), unchanged.
- [x] The catalog `sha256` is the real hash of the real file, from an immutable
      URL.
- [x] Licensing recorded exactly, with primary-source evidence, and honest about
      redistribution.
- [x] VRAM figures measured, with the length-scaling question answered.
- [x] Normal CI needs no GPU and no download; suite clean under `-W error`; all
      gates green.

## Required tests

- `test_demucs_separator.py` — the full contract against a synthetic checkpoint
  built at test time (`demucs_fixtures.py`): stem list and playable WAVs, four
  distinct stems, mono handling, a source shorter than one window, exact stage
  sequence, chunk-grained monotonic progress, chunk count following both
  `chunk_size` and `overlap`, progress arriving from a worker thread, the loop
  staying responsive, cancellation mid-run and before the first chunk, cleanup
  after a mid-encode failure, `runtime_stats()` before/during/after, every error
  code, one-separation-at-a-time, the catalog-parameter validation, the stem
  mapping under three different advertised orders, and the restricted checkpoint
  reader (both that it resolves the architecture reference without importing it,
  and that it refuses an arbitrary callable).
- `test_inference_registry.py` — the Demucs builder configured purely from a
  catalog entry, and `model_weights_missing`.
- `test_torch_optional.py` — the demucs package names its architecture without
  importing torch; its lazy exports all fail without torch; its misspelling is
  still a pyright error.
- `test_demucs_integration.py` — **deselected by default**: the real state dict
  loading with no missing/unexpected keys, the checkpoint naming only
  allowlisted pickle globals, the installed file re-hashed against the pinned
  digest, a real separation end to end, and the CUDA telemetry path
  (`@pytest.mark.gpu`).

## Notes / decisions

### Licensing — what was verified, and how

**The code and the weights are two different questions, and the answer is
different for each.** They are recorded separately in the catalog entry because
collapsing them is exactly how a permissive-sounding summary gets made.

#### Code: MIT, verified from the LICENSE file that was copied

| evidence | value |
| --- | --- |
| `LICENSE` at `adefossez/demucs@eeac1d15891af95b1288d2884b95baa3e5baa96c` | MIT License, "Copyright (c) Meta Platforms, Inc. and affiliates" — copied verbatim into `vendor/LICENSE` |
| the archived `facebookresearch/demucs` `LICENSE` | **byte-identical** (`diff` reports no difference) |
| GitHub repo metadata, both repositories | `license.spdx_id: "MIT"` |
| both READMEs | "Demucs is released under the MIT license as found in the [LICENSE](LICENSE) file." |
| `pyproject.toml` of the fork | `license = { text = "MIT License" }`, author Alexandre Défossez |

The vendored `wiener.py` carries its own attribution in its header, which
travels with the file: Open-Unmix's `openunmix/filtering.py`, MIT, Copyright (c)
2019 Inria (Fabian-Robert Stöter, Antoine Liutkus).

#### Weights: not MIT, and no formal licence was ever designated

The MIT `LICENSE` covers the code *in* the repository. **The pretrained weights
are not in either repository** — `demucs/remote/files.txt` lists file names and
`demucs/pretrained.py` prefixes them with
`ROOT_URL = "https://dl.fbaipublicfiles.com/demucs/"`, so what the repository
ships is a pointer. Nothing in either repository states a licence for what is at
the other end of that pointer, and neither README mentions the weights in its
licence section at all.

The only statement with standing is the author's, in the issue that asked:

| | |
| --- | --- |
| repository | `facebookresearch/demucs` |
| issue | [#327, "License of pre-trained models"](https://github.com/facebookresearch/demucs/issues/327), opened 2022-05-05 by `Magix-Jakob` |
| comment | https://github.com/facebookresearch/demucs/issues/327#issuecomment-1134828611 |
| author | `adefossez` — **Alexandre Défossez**, `author_association: CONTRIBUTOR`, and the author of Demucs |
| date | **2022-05-23T15:36:04Z** |

> "The model weights are not covered by the MIT license, and are provided only
> for scientific purposes."

A later comment on the same issue, from contributor `CarlGao4` (2024-06-09),
adds the reason:

> "The models are trained using MusDB dataset, which requires the result model
> can only be used for research purpose."

The issue is still **open**. Everything above was read from the GitHub API on
2026-08-25, not from a summary — that mistake is what
`docs/features/027-mdx-fast-separator.md` is about. **Nothing was found that
contradicts it**: there is no model card, no `remote/` file header, no
`LICENSE-weights`, and no statement anywhere in either repository that assigns
the weights a licence.

#### What the manifest says, and why it is honest rather than optimistic

```json
"licensing": {
  "code_license": "MIT",
  "weights_license": "No formal licence designated. …",
  "redistribution_permitted": false,
  "commercial_use_permitted": false,
  "attribution": "Weights: Hybrid Transformer Demucs (htdemucs, v4) by …"
}
```

`redistribution_permitted: false` is the load-bearing one, and it is not a
formality: **Straticate never redistributes weights.** Feature 025 pins a URL
and a SHA-256 and the *user* installs them from the author's own server. That is
what makes shipping this catalog entry defensible where shipping the file would
not be, and it is why the `.th` suffix was added to `.gitignore` in this PR.

Feature 037 is building the UI that renders `licensing` before a user installs
anything. These fields are what it will show, which is the other reason they say
"research use only" plainly rather than leaving a reader to infer it.

### The checkpoint's SHA-256, and how it was obtained

```text
8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4
84 141 911 bytes
https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th
```

Three independent confirmations:

1. The file was downloaded with `curl` from that URL and hashed locally with
   `sha256sum`.
2. **Upstream's own integrity check agrees, and it is in the file name.**
   `demucs/states.py::save_with_checksum` names a published checkpoint
   `{signature}-{sha256[:8]}.th`, and `demucs/repo.py::check_checksum` verifies
   a download by re-hashing and comparing that prefix (`torch.hub`'s
   `check_hash=True` does the same). The measured digest begins `8726e21a`,
   which is the second half of the file name. An integration test asserts that
   correspondence so it cannot drift.
3. The entry was then installed **through feature 025's own installer**, which
   verifies the pinned digest before publishing; it reported `installed` in ~3 s
   with no error, so the pinned value is what a real install actually sees.

**The URL is immutable in the way that matters.** It is not a mutable branch
reference like `.../resolve/main/...`: the file name is *derived from the
content*, upstream's own loader refuses a file whose hash does not match its
name, and the object has been unchanged since `Last-Modified: Wed, 26 Oct 2022`
(uploaded by `uname:defossez`, per the S3 metadata header). Replacing the
content at that path would break upstream's own client.

### Vendor, do not depend — and the reasons are stronger than 026's

Feature 026 vendored, and this feature evaluated the `demucs` PyPI package
honestly against the same criteria before doing the same. The full argument is
in `backend/src/straticate/inference/demucs/vendor/README.md`; in summary:

1. **A checkpoint is pinned to its architecture code**, and here the pinning is
   literal: the `.th` package pickles a *reference to the class object*
   `demucs.htdemucs.HTDemucs`, so the module path is part of the artifact. A
   routine `uv lock --upgrade` that renamed a submodule would break loading.
2. **The package brings its own model registry and remote-file downloader** —
   `huggingface-hub` as a hard dependency, `demucs/remote/*.yaml` naming every
   published checkpoint, and `torch.hub.load_state_dict_from_url` to fetch them.
   That is features 010 and 025 duplicated, which the brief called close to
   disqualifying on its own, and which `docs/features/027` warns about.
3. **It brings its own audio I/O too**: `lameenc` (an MP3 encoder) and `sphn`,
   against ARCHITECTURE.md §5's single FFmpeg compatibility layer; plus `tqdm`,
   which writes progress bars to stdout.
4. **Cancellation.** Unlike 026's upstream, `demucs.apply.apply_model` *does*
   offer a per-chunk `callback` (`{"state": "start"|"end"}`), so real per-chunk
   progress was reachable through the package — that half of the "stop and
   report" condition was not triggered. Cooperative cancellation was not: there
   is no token, the function spawns its own `ThreadPoolExecutor`, and the only
   interrupt it handles is `KeyboardInterrupt`. Cancelling would have meant
   raising out of a progress callback through somebody else's futures.

**Was vendoring disproportionate?** The brief asked, because Hybrid Transformer
Demucs is a substantially larger architecture than Mel-Band RoFormer. It is
larger — eight files and ~2,900 lines against 026's two files and ~700 — but the
cost that matters is *dependencies*, and there it is the cheaper option by a
wide margin: the vendored files import `torch`, `einops` and `numpy` and nothing
else, **all three of which the `torch` extra already installs for feature 026**.
Depending would have added eight packages to obtain one `nn.Module`. So: vendor,
and `pyproject.toml` gained no dependency at all.

**The copy is taken from the maintained fork, `adefossez/demucs`, at commit
`eeac1d15891af95b1288d2884b95baa3e5baa96c` (2026-07-11).**
`facebookresearch/demucs` is **archived** (last commit 2023-11-16). The
divergence between the two was diffed file by file and is recorded in
`vendor/README.md`; the summary is that the fork adds Intel XPU support beside
the existing MPS complex-number workaround, one type annotation, a `fatal()`
helper — and, materially for this feature, **vendors Open-Unmix's Wiener filter
into the repository** as `demucs/wiener.py` where the archived tree imports
`from openunmix.filtering import wiener`. Taking the fork therefore *removed* a
modification this directory would otherwise have had to carry. `transformer.py`
and `LICENSE` are byte-identical between the two. None of the differences
touches a parameter name, a layer shape or a `ModuleList` order, and the
integration test proves it: the published checkpoint loads into the fork's code
with no missing and no unexpected keys.

Two modifications remain, both marked at their sites and listed in
`vendor/README.md`: `julius` is imported lazily in `demucs.py` (it is used only
by the v3 time-domain model this application never builds), and `states.py` is
an *excerpt* carrying only the `capture_init` decorator — deliberately leaving
behind upstream's `load_model`, which is a `torch.load` over a fully trusted
pickle that then calls the pickled class.

### A checkpoint is data, and this backend treats it as data

Upstream loads a checkpoint by unpickling it and calling the class it names.
Straticate does not, for two reasons that happen to point the same way.

The practical one: `demucs.htdemucs.HTDemucs` is not importable here — the
architecture is vendored under a different module path — so the reference has to
be resolved to *something* regardless. The other one is that ARCHITECTURE.md §9
already says where per-model tuning lives: the catalog. So the hyperparameters
come from `default_inference_parameters`, the network is built from those, and
`load_state_dict(strict=True)` is what proves the two agree.

`load_checkpoint_package` reads the file with a **restricted unpickler**: the
architecture reference resolves to an inert placeholder and is never called,
`torch`'s own rebuild machinery is trusted wholesale (the same boundary torch's
`weights_only` loader draws), and everything else must be one of seven named
globals — the ones a real `htdemucs` package actually uses. Anything else is
`model_weights_invalid`.

This is **defence in depth, not a security boundary**: the SHA-256 is the
boundary. What it removes is the class of accident where a file that passed the
digest — because the digest itself was wrong, or the artifact was hand-placed —
still gets to execute code at load time. There is a unit test that a package
naming an ordinary callable is refused, and an integration test that the real
checkpoint needs nothing outside the allowlist.

The published weights are stored in `float16`; `load_state_dict` casts them into
the `float32` network, which is what upstream does too. Both facts are asserted.

### Stem assignment: named, cross-checked, and wrong-by-default if it were positional

**The network emits `drums, bass, other, vocals`. The catalog advertises
`vocals, drums, bass, other`.** Those are different orders, and they have to be:
`ModelCatalog._mode_stems` requires every model in a separation mode to agree on
the stem list, and `fake-standard-001` established `vocals, drums, bass, other`
long before this feature existed.

So a positional `zip` would have written drums into `vocals.wav` on the shipped
entry — not on a hypothetical reordered one, the way feature 026's residual
defect would have. Two independent guards:

1. **The mapping is by name.** `default_inference_parameters.model.sources`
   states the order the *network* emits, and each advertised stem is looked up
   in it (`stem_source_indices`). An advertised stem the network does not
   produce, or a source the catalog does not advertise, is
   `model_parameters_invalid` at construction — never a file nobody can account
   for. A test separates the same audio under three different advertised orders
   and requires each named file to come out byte-identical every time.
2. **The checkpoint is asked.** A Demucs package records the `sources` it was
   trained with, and `_check_sources` compares the catalog's list against it,
   *in order*. The edit this catches is the nasty one: transposing two names in
   `sources` while keeping the same four names would swap two stems' **audio**
   while every assertion about names, counts and shapes still passed. It is now
   a startup error.

`sources` lives in the `model` block because it is genuinely a constructor
argument of the architecture, not a separate Straticate concept. There is no
residual stem here — the network emits all four — so 026's
`output.residual_stem` mechanism does not apply and is not present.

### The training segment is a rational, and the catalog says so

`htdemucs` was trained with `segment = Fraction(39, 5)` (7.8 s), and
`HTDemucs.forward` turns that into a sample count with
`int(self.segment * self.samplerate)`. JSON has no rationals, so the catalog
states `"segment": [39, 5]` and the separator reconstructs the `Fraction`.

That is not decoration. The multiplication is exact for a rational and
approximate for a float, and `int` truncates, so the float spelling of a
fifths-of-a-second segment can land one sample below the training length — and a
window one sample short is one the model silently zero-pads on *every* forward
pass. It happens not to bite for 39/5 (`int(7.8 * 44100)` rounds up to 343,980),
but it does for its neighbours at the same rate: 7/5 gives 61,739 instead of
61,740; 41/5 gives 361,619 instead of 361,620. There is a parametrised test over
those cases, because "this is prudent" is worth less than "here is the case that
breaks".

### Where the chunking came from

The loop follows upstream's `demucs/apply.py::apply_model` numerically — same
window (`int(segment * samplerate)` = 343,980), same `(1 - overlap)` stride at
`overlap: 0.25`, same triangular transition weight raised to
`transition_power: 1`, same centred `TensorChunk.padded` windowing (which pulls
in *real surrounding audio* rather than silence for a short final window), same
`center_trim`, and the same mixture normalization
`demucs/api.py::separate_tensor` applies — but is reimplemented rather than
copied so that it can report progress after every window, check the cancellation
token between windows, and run inside `asyncio.to_thread`. Each window is one
forward pass through a 42-million-parameter hybrid transformer, so
`chunks_completed / chunks_total` is a statement about work done.

**No `torch.autocast` on CUDA**, deliberately and unlike feature 026: this
architecture's masking path runs on complex spectrograms
(`torch.view_as_complex`, `torch.istft`), and complex half precision is not a
working dtype there. Upstream runs it in float32 too.

### Quality tier: `balanced`, and why not `high_quality`

`fake-standard-001` already claims `fast`, leaving `balanced` and
`high_quality`. `htdemucs` is upstream's own default model
(`DEFAULT_MODEL = 'htdemucs'`) and a single 80 MiB file. Upstream also publishes
`htdemucs_ft`, a **bag of four fine-tuned models** — four downloads, ~320 MiB,
and four times the compute per track for a modest SDR gain. That is the entry
that should be able to claim `high_quality` later, and it would be a pure data
edit plus a small amount of bag-averaging work. Claiming `high_quality` now
would either strand that upgrade or force this entry to be renamed and
re-tiered, which is a migration for the sake of a label. `balanced` it is.

## Validation — what was and was not measured

Every figure below was measured on **2026-08-25**, in this worktree, on an
NVIDIA GeForce RTX 4060 Laptop GPU (8,188 MiB, driver 610.47 / CUDA 13.3) with
`torch 2.13.0+cu130` on Python 3.12.11, against the real checkpoint installed by
feature 025's installer. CPU figures are the same host's Intel64 laptop CPU.

**Validated by running it:**

- **The vendored architecture loads the real checkpoint with
  `missing_keys == []` and `unexpected_keys == []`**, 41,984,456 parameters.
  This is the check that proves the vendoring matches the checkpoint.
- The installer downloaded the artifact from the pinned URL and verified the
  pinned SHA-256 (~3 s, 84,141,911 bytes); the integration test then re-hashed
  the installed file and matched.
- **`GET /separation-modes` on a default server** (no
  `STRATICATE_INCLUDE_DEVELOPMENT_MODELS`) returns both modes:

  ```json
  [{"id":"vocals","stems":["vocals","instrumental"],
    "quality_options":[{"id":"high_quality","model_id":"vocals-hq-001"}]},
   {"id":"standard_stems","stems":["vocals","drums","bass","other"],
    "quality_options":[{"id":"balanced","model_id":"standard-stems-001"}]}]
  ```

- **A full job through the real HTTP API** on a generated 20 s stereo mix:
  upload → `POST /jobs` (`201`, device resolved to `cuda:0`, model
  `standard-stems-001`) → `completed` in 1.56 s (RTF 12.80) → `GET /result` →
  all four stems streamed from `GET /jobs/{id}/stems/{name}`, 3,528,044 bytes
  each.
- The whole integration tier, both backends, **9 passed on `cuda:0`** — so
  feature 026 still works alongside this one.

### Separation quality, measured against ground truth

A 20 s stereo mixture was built from four **known** sources: a locally
synthesised speech track (Windows SAPI — no third-party audio, nothing
committed) as the vocal, plus a generated walking bass line, a generated drum
pattern and a generated chord part. It was then separated and every stem
correlated with every true source. **The mixture row is the baseline**, and it is
the row that makes the rest mean anything.

| | true vocals | true drums | true bass | true other |
| --- | --- | --- | --- | --- |
| **mixture** (baseline) | +0.324 | +0.221 | +0.568 | +0.723 |
| `vocals` stem | **+0.952** | +0.015 | +0.004 | −0.007 |
| `drums` stem | +0.019 | **+0.907** | +0.140 | +0.014 |
| `bass` stem | +0.009 | +0.035 | **+0.985** | +0.003 |
| `other` stem | +0.033 | +0.001 | +0.003 | **+0.993** |

Every stem is far more correlated with its own source than the mixture is, and
essentially uncorrelated with the other three. The `other` stem's baseline is
already high (+0.723) because the chord part dominates the mix, which is exactly
why the baseline row has to be there: without it, +0.993 would be a much less
impressive number than it looks.

The same measurement on `cpu` reproduces every figure to three decimal places
(one cell differs: bass +0.986 instead of +0.985), so there is no
CUDA-specific divergence in the output.

### Performance, next to feature 026's

| | 026 RoFormer (`vocals-hq-001`) | 028 Demucs (`standard-stems-001`) |
| --- | --- | --- |
| CPU, 30 s clip | 100.5 s — **RTF 0.299** | 18.3–18.4 s — **RTF 1.63–1.64** |
| `cuda:0`, 30 s clip | 6.7 s — **RTF 4.496** | 2.2–2.8 s — **RTF 10.7–13.5** |

Demucs is ~5.5× faster than RoFormer on CPU and ~2.7× on CUDA, which is what a
42-million-parameter network against a 228-million-parameter one buys. **It is
faster than real time on CPU** — the first model in this project that is, and it
changes the shape of the CPU story the ROADMAP's *Next* section describes for
027. A 3-minute track separates in 117 s on CPU (RTF 1.53, measured) and in
about 15 s on this GPU.

The CUDA range is honest rather than an average: five consecutive warm runs gave
2.2–2.8 s, and one cold-clock run gave 7.3 s (RTF 4.22). Laptop GPU clocks move.

### VRAM, measured — and the length-scaling question, answered

Following feature 036's methodology: **one fresh process per configuration**, so
the CUDA context, the caching allocator and the resident network are all new;
whole-device usage sampled every 20 ms from `torch.cuda.mem_get_info`;
`nvidia-smi` confirmed 232 MiB of unrelated use on the card before each run. The
measuring script is not committed (it configures a separator directly, which is
the integration tier's job).

The **CUDA context alone is 1,078.6 MiB on this host before a single tensor** —
036 measured 1,079 MiB, which is the same figure, and it is why
`max_memory_allocated` is not the number a user needs.

#### Peak against track length, at the catalog's own parameters

`chunk_size: 343980`, `overlap: 0.25`. MiB throughout.

| clip | chunks | peak allocated | peak reserved | whole-device peak | RTF |
| --- | --- | --- | --- | --- | --- |
| 10 s | 2 | 570.2 | 714 | 1,828.6 | 8.76 |
| 30 s | 6 | 604.1 | 766 | 1,880.6 | 16.41 |
| 60 s | 11 | 661.7 | 856 | 1,970.6 | 22.20 |
| 2 min | 21 | 773.1 | 998 | 2,112.6 | 25.95 |
| 4 min | 42 | 994.7 | 1,220 | 2,334.6 | 26.08 |
| 6 min | 62 | 1,217.6 | 1,442 | 2,556.6 | 22.63 |
| 10 min | 103 | 1,661.5 | 1,986 | **3,100.6** | 28.70 |

**Yes — it has feature 038's problem too, and the numbers say so.** Peak grows
linearly with the track at **1.85 MiB per second of audio**, for the same reason
026 does: the decoded mixture, the output accumulator and the per-sample weight
tensor are on the *device* for the whole track. Chunking bounds the working set,
not the total.

That said, the slope is **lower than 026's 1.35 MiB/s would suggest for a
four-source model**, and that is not luck — see below.

#### The gratuitous half of the slope was removed here

The first implementation wrote the whole-track steps with ordinary operators:
`normalized = (mixture - shift) / scale` before the loop, then
`estimates = accumulator / weights`, `* scale`, `+ shift` after it. Each of
those allocates another full-length tensor, and at four sources × two channels ×
four bytes the accumulator alone is 1.41 MiB per second of audio. Measured:

| | slope | 10-minute peak allocated | 10-minute whole-device peak |
| --- | --- | --- | --- |
| ordinary operators | 5.85 MiB/s | 4,022.9 MiB | 5,716.6 MiB |
| in place (shipped) | **1.85 MiB/s** | **1,661.5 MiB** | **3,100.6 MiB** |

Normalizing the mixture in place and finishing the accumulator in place cuts the
slope by more than three. It does **not** make the peak independent of the track
— it cannot, while the accumulator is whole-track, and that is precisely feature
038's job — but it is the difference between a 4 GiB card managing ~17 minutes
of audio and managing ~7.

#### `chunk_size` is not a memory dial for this architecture

036 found peak scaling with `chunk_size` for RoFormer. It does **not** here, and
the reason is worth writing down: `use_train_segment` makes `HTDemucs.forward`
pad every window back up to the training length, so the forward pass allocates
the same working set whatever the window is. A 60 s clip:

| `chunk_size` | window | chunks | peak allocated | whole-device peak | RTF |
| --- | --- | --- | --- | --- | --- |
| 88,200 | 2 s | 40 | 660.6 | 1,928.6 | 8.05 |
| 171,990 | 3.9 s | 21 | 661.7 | 1,928.6 | 13.11 |
| 343,980 | 7.8 s | 11 | 661.7 | 1,970.6 | 20.75 |

A 1 MiB difference for 3.6× the wall clock. Shrinking `chunk_size` here buys
nothing and costs a great deal, which is why the catalog pins it to the training
length and why `DemucsParameters` refuses a value **larger** than it (the
network would be running off its training distribution).

`overlap` does not move the peak either, and costs throughput linearly — the
same conclusion 036 reached for `num_overlap`: 0.0/0.25/0.5/0.75 on a 60 s clip
all peaked at 661.7 MiB, at RTF 25.94/19.40/16.06/9.95.

#### The values chosen, and the headroom

```json
"requirements": {
  "recommended_vram_mb": 4096,
  "minimum_vram_mb": 3072,
  "minimum_ram_mb": 8192
}
```

Fitting whole-device peak ≈ 1,807 + 2.16 × seconds across the sweep:

- **`minimum_vram_mb: 3072`** — the floor. A 3 GiB card with nothing else
  resident reaches its limit at about a **10-minute** track, so it runs any
  normal song with room to spare. 2 GiB reaches it at under **two minutes**,
  which is not a usable model on that card, so the floor sits between them.
- **`recommended_vram_mb: 4096`** — the comfortable figure: the measured
  3,101 MiB for a 10-minute track plus ~1 GiB of headroom, spent on a card that
  is also driving a display (a desktop compositor is commonly 500–800 MiB), on a
  CUDA context that differs by driver and CUDA minor version, and on allocator
  fragmentation past the 10 minutes measured. A 4 GiB card handles about 17
  minutes of audio.

Both are card sizes that exist, which is the point of a hardware
recommendation. Both are advisory and nothing enforces them, exactly as 036
established.

### What was **not** validated — stated plainly

- **No real music was separated.** Nothing copyrighted was downloaded. The
  quality figures above come from a synthesised voice over a generated backing:
  a real measurement of a real model, but not the same as a mastered recording.
  In particular, the four generated sources are far more spectrally separable
  than a real mix, so the correlations should be read as "the mapping and the
  pipeline are right and the model is doing real work", not as an SDR score.
- **Only this one checkpoint** was tried, at its own hyperparameters.
  `htdemucs_ft` (a bag of four) and `htdemucs_6s` (six sources) were not
  attempted; the six-source one would need a new separation mode, and the bag
  would need averaging across models.
- **NVML's `utilization` / `temperature_celsius` were not observed populated on
  this branch.** The binding is not installed in this worktree (`uv sync` prunes
  it), so both fields were `None` in every run here. The lifecycle — initialised
  once, handles cached, absent-binding and driver-failure paths — is tested
  against a double in normal CI, and 036 verified the populated path on this
  same hardware with `nvidia-ml-py`. NVML is not a dependency and never becomes
  one.
- **Only one GPU**, one driver, one OS. All CUDA figures are RTX 4060 Laptop /
  WDDM / `cu130` figures and are labelled as such.
- **The E2E (Playwright) tier was not run locally** — it is `frontend/`, which
  feature 037 owns and which this branch does not touch. It **was** run by CI on
  this branch and passed (1m37s), along with the `frontend` job, so the
  catalog's new entry does not disturb it. That matches the reading:
  `install.spec.ts` derives its mode and tier from the live catalog and asserts
  only `modes.length > 0`, and `separation.spec.ts` picks `quality_options[0]`
  of the four-stem mode, which with `STRATICATE_INCLUDE_DEVELOPMENT_MODELS=1` is
  still the `fast` fixture.
- **`float32` only.** No half precision (the complex path forbids it), no
  `channels_last`, no batching of windows, no `torch.compile`.

## Known limitations

- **Whole-track memory**, as above: peak grows at 1.85 MiB per second of audio
  because the mixture, the accumulator and the weight tensor are device-resident
  for the whole run. Feature 038 is the fix; this feature reduced the slope by
  3.2× but did not remove it.
- **Cancellation granularity is one chunk**, which on CPU is ~3 s of wall clock
  at the 7.8 s window. Prompt in chunks, not in milliseconds.
- **The model is moved to the device on every run** if the device changed; there
  is no eviction, so a long-lived process holds the network on whichever device
  it last used. With one model and one job at a time this is intended.
- **The CUDA device/telemetry helpers are duplicated** between
  `inference/roformer/separator.py` and `inference/demucs/separator.py` —
  `_resolve_torch_device`, `cuda_namespace`, `reset_peak_memory`,
  `device_stats`, `NvmlProbe` and the PCM↔tensor pair, about 200 lines. They
  were deliberately *not* extracted into a shared module: feature 026's tests
  exercise its CUDA path by monkeypatching **that module's** globals
  (`separator_module.cuda_namespace`, `separator_module._NVML`), so folding the
  two together silently breaks a seam this feature has no business changing, and
  "every existing test must pass unchanged" is a harder constraint than "no
  duplication". A shared `inference/torch_device.py` used by both backends is
  the right follow-up and should be its own numbered feature, with 026's tests
  moved onto the new seam in the same PR. One consequence today: two
  `NvmlProbe` instances exist, so a process that ran jobs on both backends would
  initialise NVML twice. NVML refcounts `nvmlInit`, and only one separator runs
  at a time, so this costs nothing measurable — but it is a symptom, not a
  design.
- **`model_weights_invalid` and `model_parameters_invalid` are `500`s**, and
  because a separator is built inside `POST /jobs`, they answer *that* request
  rather than being job-failure codes. Same as 026; they are deployment faults.
- **Installed weights are never re-verified** on load (feature 025's documented
  limitation); the integration tier is the only thing that re-hashes them.
- **No frontend affordance** specific to this model, and none was added — 037
  owns that.

## Noticed, out of scope

- **`ROADMAP.md`'s *Next* section is now partly historical.** It says
  "`standard_stems` is absent until 028 lands a real four-stem model" and offers
  028 tier advice that this PR has acted on. Only the 028 ledger row was
  changed here, per the assignment; the prose belongs to the next ledger-sync
  PR. `docs/features/032-hide-development-models.md` has the same tense problem
  and is deliberately left as the historical record it is.
- **`ROADMAP.md`'s CPU argument for 027 has weakened.** It reasons that "a fast
  tier is a product requirement" because RoFormer is 3.5–5× slower than real
  time on CPU. This model runs at **1.6× real time on CPU**, so the four-stem
  mode already has a usable CPU story and the argument now applies only to the
  `vocals` mode. 027 is blocked on licensing regardless.
- **`ModelInstaller.describe()` takes a `CatalogEntry`** but reads as though it
  took a `Model` — feature 026 noticed this and it is still true. Not touched.
- **`quality_options` still offers uninstalled models.** 025 and 026 both
  deferred the decision and this feature does not reopen it: with two real
  models now, a default server offers two tiers and neither has weights on a
  fresh checkout, which strengthens rather than changes 026's reasoning that the
  answer belongs with the model-management UI (037).
- **A six-stem mode** (`htdemucs_6s`, adding `guitar` and `piano`) is a mode
  the catalog has no entry for and this feature deliberately did not invent.
