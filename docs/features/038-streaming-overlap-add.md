# [038] Streaming overlap-add (bounded VRAM)

Branch: `038-streaming-overlap-add`
Status: PR OPEN
Dependencies: 026, 028, 039
PR: #55

## Objective

Make peak VRAM a function of the **chunk size**, not of the track length, so a
long enough track cannot exhaust the card. Both torch backends; output
bit-identical to the pre-change implementation.

## The problem, measured

Feature 036 measured `vocals-hq-001` on an RTX 4060 (8,188 MiB), one fresh
process per configuration, whole-device usage sampled every 20 ms:

| clip | peak allocated (MiB) | whole-device peak (MiB) |
| --- | --- | --- |
| 10 s | 1,549 | 3,001 |
| 30 s | 1,575 | 3,023 |
| 2 min | 1,700 | 3,155 |
| 6 min | 2,021 | 3,475 |
| 10 min | 2,343 | **4,213** |

Peak **grows linearly with track length** at roughly **1.35 MiB per second of
audio**, fitting (with a chunk-size sweep) to within 3 MiB:

```text
peak ≈ 895 + 1.82 × (chunk_size / 1000) + 1.35 × seconds
```

Chunking bounds the *working* set but not the total, because features 026 and
028 held the decoded mixture, the output accumulator and the weight tensor on
the **device** for the whole track — 026 records this as a known limitation
("streaming overlap-add is future work"); 036 and 028 gave it numbers.

**Concrete thresholds:** a 4 GiB card exhausted at roughly **9 minutes** of
audio, an 8 GiB card at roughly **20 minutes**. Long DJ sets, live recordings
and album-length files are inside that range, and the failure is a CUDA OOM
part-way through a job the user has already waited minutes for.

Note also that `max_memory_allocated` counts tensors only: the CUDA context is
**1,079 MiB before a single tensor** (1,078.6 MiB again on every run of this
feature's sweep), and the allocator reserves 150–750 MiB above live tensors.
What a card must have free is the whole-device figure, not the allocated one.

## What features 039 and 028 left, and what was taken

- **`_run_chunks` was the hole.** 039 extracted the shared skeleton leaving that
  method as the documented extension point. This feature works inside it, in
  both backends, plus one new shared module.
- **RoFormer allocated its `weights` accumulator at the full `(stems, channels,
  samples)`** when a `(samples,)` vector would do. Taken: `HostOverlapAdd` holds
  a vector, which is what Demucs already did.
- **Both backends held the whole mixture *and* the whole accumulator on the
  device.** Taken: neither does now.
- **`chunk_size` is not a memory dial for Demucs** (028 measured it):
  `use_train_segment` pads every window back up to the training segment. Not
  reached for.
- **028 had already taken the in-place win** — whole-track steps written in
  place cut the slope from 5.85 to 1.85 MiB/s and a ten-minute peak from 4,023
  to 1,662 MiB. That was the baseline to beat, and it is the "before" column
  below.

## Scope

- Stream the overlap-add: keep only the chunks in flight on the device and
  accumulate output on the host, so device residency is bounded by the window
  rather than by duration. Both backends.
- Keep the `Separator` contract exactly: chunk-grained progress, cancellation
  between chunks, `.part`-then-rename stem writes, `runtime_stats()` as a cheap
  non-blocking snapshot, the CUDA peak reset per run, and Demucs' name-based
  stem mapping with its `_check_sources` / `_check_sample_rate` guards.
- Re-derive `recommended_vram_mb` / `minimum_vram_mb` for both catalog entries
  from the new measurements.

## Out of scope

- Changing separation output. A streaming implementation must be
  **bit-identical**, proven by hash on CPU *and* CUDA.
- `main.py`, `config.py`, `README.md`, `DEVELOPMENT.md`, `frontend/**` —
  feature 042 owns them.
- The vendored architecture code under `roformer/vendor/` and `demucs/vendor/`.
- Any catalog change other than the two `requirements` figures.
- `num_overlap` / `overlap` / `chunk_size` tuning; feature 041's mono
  fold-down; adding an architecture.

## Expected modules/files

- `backend/src/straticate/inference/torch_overlap_add.py` — **new**;
  `HostOverlapAdd`, the shared host-resident accumulator.
- `backend/src/straticate/inference/roformer/separator.py` — `_run_chunks`.
- `backend/src/straticate/inference/demucs/separator.py` — `_run_chunks`,
  `_centred_window`.
- `backend/src/straticate/inference/torch_separator.py` — the extension point's
  docstring.
- `backend/tests/test_inference_overlap_add.py` — **new**.
- `backend/tests/test_{roformer,demucs}_separator.py` — structural CI tests.
- `backend/tests/test_{roformer,demucs}_integration.py` — the GPU flatness tier.
- `models/catalog.json` — the two `requirements` blocks.

## Acceptance criteria

- [x] Peak device memory is flat (within noise) across 30 s, 2 min, 6 min and
      10 min inputs, for both models — and, measured well past the brief's
      range, flat **to the byte** out to 60 minutes for `vocals-hq-001`, and to
      about 38 minutes for `standard-stems-001`, after which its residual
      reduction term adds 0.077 MiB per second of audio whole-device
- [x] Output is bit-identical to `dev` for both models, proven by hash, on CPU
      and CUDA
- [x] `recommended_vram_mb` / `minimum_vram_mb` re-derived for both entries — one
      of the four moves, and *why the other three do not* is the finding
- [x] Wall-clock cost measured and reported against the current RTFs
- [x] Progress, cancellation, telemetry and stem mapping behave exactly as
      before
- [x] A track long enough to exhaust the card today completes
- [x] All backend gates green, suite clean under `-W error`; integration tier
      passes for both backends

## Required tests

- `test_inference_overlap_add.py` — the accumulator's buffers are on the host;
  streaming accumulation is `torch.equal` to a whole-track reference written the
  way both backends were written before this feature; the weight is a vector;
  the guards refuse a window that does not fit.
- `test_{roformer,demucs}_separator.py::test_device_residency_does_not_scale_with_the_number_of_chunks`
  — the structural property, in normal CI, against the synthetic checkpoints.
- `test_demucs_separator.py::test_the_decoded_mixture_is_not_normalized_in_place`
  — the CPU-run trap described below, checked against **the tensor the loop
  itself builds** (taken by standing in front of the factory), because
  re-deriving it from the `PcmAudio` afterwards compares two fresh tensors and
  cannot fail.
- `test_demucs_separator.py::test_two_overlapping_windows_agree_where_they_overlap`
  — the same fault seen from outside `_centred_window`: a sample covered by two
  windows must reach the network with the same value both times. Both tests were
  confirmed to **fail** with `sub_` restored.
- `test_{roformer,demucs}_integration.py::test_peak_device_memory_is_flat_across_track_lengths`
  — `@pytest.mark.gpu`, the hardware claim itself.

## Notes / decisions

### The shape of the change

`HostOverlapAdd` (`inference/torch_overlap_add.py`) holds the two whole-track
tensors — the `estimate × envelope` sum and the per-sample weight — as float32
on the **CPU**, and is fed one window at a time. Each backend's loop then does
the same three things: slice a window out of the host-resident mixture, move
*that* to the device, and hand the estimate straight back. Nothing whole-track
touches the card.

It is one shared class and **not** a shared loop, deliberately: 039's whole
point was that the two chunk loops genuinely differ — window shape, stride,
padding, normalization, autocast — and the streaming decision is about where a
buffer lives, which is a single object, not a control-flow abstraction. Adding a
third architecture is still two methods; it now has a buffer to stream into
rather than a `torch.zeros(..., device=device)` to copy.

### Bit-identity is what decided the design, not a check afterwards

Element-wise `float32` arithmetic — multiply, add, clamp, divide — is correctly
rounded under IEEE 754 and gives the same bits on CPU and CUDA. A **reduction**
does not: a CUDA tree reduction and a CPU cascade sum disagree in the last bits.
Measured on this host over a 1,000,003-element float32 tensor, `x.mean()` came
back as `0x3A5B9EC0`-ish on both but two ULP apart (`978509046` against
`978509048` read as int32); `mul`, `add`, `div` by a broadcast vector, `div` by a
0-dim scalar and the whole `div_/mul_/add_` finishing chain were **identical to
the bit** across the two devices.

That single fact draws the line:

- **Everything element-wise moved to the host**: the accumulation, the final
  divide by the weight, RoFormer's `nan_to_num`, Demucs' `mul_(scale).add_(shift)`.
- **The envelope stays on the device and is multiplied into the estimate
  there**, so the bits crossing to the host are the bits the previous
  implementation accumulated. (`torch.linspace` and `torch.arange` are not
  guaranteed to agree across devices either, and building the window on the host
  would have risked it for no gain.)
- **Demucs' normalization statistics stay on the device.** `shift` and `scale`
  are `reference.mean()` and `reference.std()` over the *whole* mono reference —
  reductions, and therefore the one thing that cannot move. The mono mixdown
  that produces the reference (`mean` over two channels: one addition and one
  halving per sample, in the only order there is) *is* element-wise, so it is
  computed on the host and only the resulting `(samples,)` vector is moved to
  the card — four bytes per sample, transiently, freed before the first forward
  pass. This is the one thing in the feature that is still O(duration) on the
  device; see *Known limitations*.

Two further checks that could have gone wrong and did not:

- **Input contiguity.** Before this change RoFormer handed the network a
  *strided view* into a device-resident padded mixture; now it hands it a
  contiguous tensor copied from the host. `MelBandRoformer` was run on both
  forms of the same window, on CPU and on CUDA, at three offsets: identical to
  the bit each time.
- **Broadcasting the weight.** RoFormer's divisor went from `(stems, channels,
  samples)` to `(samples,)`. Every element of the wide tensor accumulated the
  same sequence of adds, so it held the same value the vector holds, and
  broadcasting reads it back unchanged.

### The proof

`RoFormerSeparator` and `DemucsSeparator` were driven directly, one fresh
process per run, against the real installed checkpoints, on `dev`'s source tree
and on this branch, and every written stem was SHA-256'd.

| model | device | clips | stems hashed | result |
| --- | --- | --- | --- | --- |
| `vocals-hq-001` | `cpu` | 12 s, 30 s | 4 | identical |
| `vocals-hq-001` | `cuda:0` | 30 s, 2 min, 6 min, 10 min, **45 min**, **60 min** | 12 | identical |
| `standard-stems-001` | `cpu` | 12 s, 30 s | 8 | identical |
| `standard-stems-001` | `cuda:0` | 30 s, 2 min, 6 min, 10 min, **60 min** | 20 | identical |

**Forty-four stems, twelve on CPU and thirty-two on CUDA, every one
byte-for-byte what `dev` produces** for the same input and the same parameters —
including a 60-minute track through 902 chunks, where any drift in the
accumulation would have had every opportunity to show.

Re-verified after code review, because three of the review's findings
(`nan_to_num_` in place, `.contiguous()` on the returned estimates, and `div_`
in `_centred_window`) touch the numerics path even though all three should be
neutral: the CPU rows and the `cuda:0` 10-minute and 45-minute rows were re-run
against the post-review code and hash the same as before, to the byte.

### What re-measuring found

Method is 036's, unchanged: **one fresh process per row**, so the CUDA context,
the caching allocator and the resident network are all new; whole-device usage
sampled every 20 ms from `torch.cuda.mem_get_info`; `nvidia-smi` confirmed the
card at **0 MiB** before the sweep. NVIDIA GeForce RTX 4060 Laptop GPU
(8,188 MiB, driver 610.47 / CUDA 13.3, WDDM), `torch 2.13.0+cu130`, Python
3.12.11, 2026-08-25. The `dev` column reproduces 036's and 028's published
tables to within a megabyte, which is the first thing worth knowing: the
measurement is the same measurement.

The CUDA context alone was **1,078.6 MiB** on every one of the sixteen runs.

#### `vocals-hq-001` (Mel-Band RoFormer), `chunk_size: 352800`, `num_overlap: 2`

MiB throughout.

| clip | chunks | alloc before | alloc **after** | reserved before | reserved **after** | whole-device before | whole-device **after** |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 30 s | 10 | 1,575.4 | **1,526.1** | 1,906 | **1,864** | 3,022.6 | **2,980.6** |
| 2 min | 32 | 1,699.5 | **1,526.1** | 2,038 | **1,864** | 3,154.6 | **2,980.6** |
| 6 min | 92 | 2,021.1 | **1,526.1** | 2,358 | **1,864** | 3,474.6 | **2,980.6** |
| 10 min | 152 | 2,342.6 | **1,526.1** | 3,096 | **1,864** | 4,212.6 | **2,980.6** |

**Flat to the byte.** Not "within noise" — the same number, three times over,
for twenty times the chunks. There is nothing left on the card whose size
depends on the input, so there is nothing left to vary.

Slope: **1.35 → 0.00 MiB per second of audio.**

#### `standard-stems-001` (Hybrid Transformer Demucs), `chunk_size: 343980`, `overlap: 0.25`

| clip | chunks | alloc before | alloc **after** | reserved before | reserved **after** | whole-device before | whole-device **after** |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 30 s | 6 | 604.1 | **550.8** | 766 | **700** | 1,880.6 | **1,814.6** |
| 2 min | 21 | 773.1 | **551.1** | 998 | **698** | 2,112.6 | **1,812.6** |
| 6 min | 62 | 1,217.6 | **550.3** | 1,442 | **772** | 2,556.6 | **1,886.6** |
| 10 min | 103 | 1,661.5 | **550.2** | 1,986 | **764** | 3,100.6 | **1,878.6** |

Peak **allocated** spans 0.9 MiB across a twentyfold change in track length —
flat. The whole-device figure spans 74 MiB and is **not monotonic** (10 min is
*below* 6 min), which is the shape of allocator segment granularity rather than
of a slope: the transient mono reference described above is 60 MiB at 6 minutes
and 100 MiB at 10, and the allocator sometimes keeps a segment that size in
reserve afterwards and sometimes reuses it. It never reaches the peak, because
the forward pass's own working set is five times larger.

Slope: **1.85 → 0.00 MiB per second of audio allocated**, and no measurable
trend whole-device.

Pushed further, on this branch, to find where the transient mono reference
eventually does start to count — because the eight rows above are all *below*
the length at which it can, and a floor derived only from them would be a floor
derived from the easy half of the range:

| clip | chunks | alloc | reserved | whole-device |
| --- | --- | --- | --- | --- |
| 20 min | 206 | 550.4 | 772 | 1,886.6 |
| 45 min | 462 | 616.0 | 706 | 1,820.6 |
| 60 min | 616 | 767.8 | 792 | 1,906.6 |
| 90 min | 924 | 1,070.3 | 1,096 | **2,210.6** |

Peak **allocated** is flat to about **38 minutes** and then rises at **0.168 MiB
per second of audio** — four bytes per sample, exactly the mono reference, which
overtakes the forward pass's ~382 MiB working set at 2,271 s of audio. The fit
predicts 622 MiB at 45 min and 773 at 60; measured, 616.0 and 767.8.
**Whole-device follows it**, more slowly (the allocator absorbs part of the
block into the pool it would have reserved anyway): flat within 74 MiB out to an
hour, then 2,210.6 MiB at 90 minutes, a slope of **0.077 MiB per second** past
the crossover. That is 11× and 24× smaller than the 1.85 MiB/s it replaced, and
it is **not zero** — which is why the Demucs entry's `requirements` do not move
below 028's figures, and why the first known limitation says so plainly.

### Wall clock: no measurable cost

Three measurements, because the obvious one was misleading.

**1. The accumulation itself, timed in isolation.** Both chunk loops were run in
**one process against one resident `vocals-hq-001`**, alternating, on a
30-second clip (10 chunks) — same model instance, same decoded source, the loop
the only variable. Forward passes came to 98.54 s streaming against 97.87 s
whole-track. Everything each loop does *outside* the forward pass, for the whole
clip: **11 ms streaming, 16 ms whole-track**. The streaming loop is the cheaper
of the two, because RoFormer's weight accumulator went from `(stems, channels,
samples)` to `(samples,)` and writes half as much. The two outputs were
`torch.equal`.

**2. Cross-process, alternating, three rounds** (12 s clip, `cpu`, one fresh
process per cell, read across):

| round | streaming | whole-track |
| --- | --- | --- |
| 1 | 43.94 s | 45.36 s |
| 2 | 51.94 s | 52.39 s |
| 3 | 55.27 s | 54.19 s |

Indistinguishable — streaming is faster in two rounds of three. Note instead the
**25% rise from round 1 to round 3**: that is this laptop throttling under
sustained load, and it is far larger than anything the change does. It is also
exactly what an A/B that runs one variant before the other measures: two such
pairs, run earlier, suggested a 10–13% CPU regression that alternating rounds do
not reproduce and that the in-process measurement above says has nowhere to come
from. Recorded here because the wrong conclusion was reachable from a
perfectly ordinary experiment.

**3. The longest run there is**, on CUDA: a 45-minute track, 677 chunks —
**424.9 s whole-track against 426.0 s streaming, +0.27%**. A per-chunk cost
would show here more clearly than anywhere else, and does not.

For completeness, the sweep's own RTF columns. Each row is two fresh processes,
`dev` first and this branch second, so they carry the drift described above:

| model | clip | RTF before | RTF after |
| --- | --- | --- | --- |
| `vocals-hq-001` | 30 s | 4.46 | 4.44 |
| `vocals-hq-001` | 2 min | 6.09 | 6.02 |
| `vocals-hq-001` | 6 min | 6.25 | 6.12 |
| `vocals-hq-001` | 10 min | 6.30 | 6.03 |
| `standard-stems-001` | 30 s | 10.55 | 9.70 |
| `standard-stems-001` | 2 min | 20.93 | 21.94 |
| `standard-stems-001` | 6 min | 26.12 | 24.91 |
| `standard-stems-001` | 10 min | 24.06 | 24.79 |

The arithmetic agrees with the conclusion. Per chunk, RoFormer moves 2.7 MiB of
mixture onto the card and 2.7 MiB of estimate off it; Demucs 2.6 on and 10.5
off. Over a ten-minute track that is 1.0 GiB and 1.5 GiB — tens of MiB per
second across a link that does gigabytes, next to seconds of forward pass. On
**CPU there is no transfer at all**: the only change is which allocation a
buffer comes from.

Reference points this is measured against, from 028: RoFormer 0.299 CPU / 4.496
`cuda:0`; Demucs 1.63 CPU / 10.7–13.5 `cuda:0`. This host reproduced RoFormer's
CPU figure exactly on `dev` (RTF 0.298 on a 30 s clip) and measured Demucs' CPU
at 2.05–2.11, faster than 028's row — different hardware, not a different
result.

### The `requirements` re-derived

```json
"vocals-hq-001": {
  "recommended_vram_mb": 4096,   // was 6144
  "minimum_vram_mb":     4096,   // unchanged in value; changed in meaning
  "minimum_ram_mb":      8192    // unchanged; see below for what it covers
}
"standard-stems-001": {
  "recommended_vram_mb": 4096,   // unchanged
  "minimum_vram_mb":     3072,   // unchanged
  "minimum_ram_mb":      8192    // unchanged
}
```

**Only one of the four VRAM figures moves, and the reason the other three do not
is the point.** A floor is set by the worst case a user can reach, and this
feature changed which case that is.

- **`vocals-hq-001`, recommended 6,144 → 4,096.** 036 set 6,144 as "the measured
  4,213 MiB for a ten-minute track plus ~1.9 GiB of headroom", where the
  ten-minute figure was simply the largest of a rising line. The line is gone:
  2,980.6 MiB at 30 seconds, at ten minutes, at forty-five and at sixty, to the
  byte. 4,096 is that plus 1,115 MiB — enough for a desktop compositor or a
  CUDA context larger than this host's 1,079 MiB, and no longer holding room
  for a track longer than the one measured, because that is not a thing to hold
  room for any more.
- **`vocals-hq-001`, minimum stays 4,096, and its meaning changes completely.**
  036's floor meant "a 4 GiB card runs the music you actually have — about nine
  minutes' worth". It now means **a 4 GiB card runs anything**. 3,072 was
  considered and rejected: 3,072 − 2,981 is 91 MiB, which is a coincidence
  rather than a floor, and a driver whose context is 100 MiB larger than this
  one's would turn it into an OOM.
- **`standard-stems-001` keeps 028's 4,096 / 3,072, and the first draft of this
  feature was wrong to lower them to 3,072 / 2,048.** Those numbers were derived
  from 30 s, 2 min, 6 min and 10 min clips — every one of them below the
  ~38-minute crossover documented above, i.e. from exactly the part of the range
  where this architecture *is* flat. Measured past it, a 90-minute track needs
  **2,210.6 MiB**, so a card meeting an advertised 2,048 floor would OOM on it,
  and the advertised floor would have re-introduced the failure this feature
  exists to remove — quietly, and only for the users with the longest files.
  3,072 holds to roughly **four and a half hours** on this fit. What changes for
  Demucs is therefore not the number but its coverage: 028's 3 GiB floor meant
  *about a ten-minute track*, and it now means *about a four-hour one*.

Both remain advisory; nothing is refused for failing them (036 established
that, and it is unchanged). The manifest schema's and `Requirements`'
descriptions previously said peak "scales with track length"; the first draft of
this feature replaced that with an unconditional "does not", which is true for
RoFormer and false for Demucs. Both now say that it *may*, name the architecture
that still does, and tell whoever derives the next entry's figures to measure at
the longest track it is meant to support.

### A track long enough to exhaust the card today

Two long tracks were run end to end, `dev` and this branch, on `cuda:0`:

| model | track | chunks | | alloc | reserved | whole-device |
| --- | --- | --- | --- | --- | --- | --- |
| `vocals-hq-001` | 45 min | 677 | before | 6,368.4 | 7,334 | **8,187.6** |
| `vocals-hq-001` | 45 min | 677 | **after** | **1,526.1** | **1,864** | **2,980.6** |
| `vocals-hq-001` | 60 min | 902 | before | 8,182.1 | **9,146** | **8,187.6** |
| `vocals-hq-001` | 60 min | 902 | **after** | **1,526.1** | **1,864** | **2,980.6** |
| `standard-stems-001` | 60 min | 616 | before | 7,454.0 | 8,042 | **8,187.6** |
| `standard-stems-001` | 60 min | 616 | **after** | **767.8** | **792** | **1,906.6** |

Stems identical in every pair.

At 45 minutes `dev` uses **8,187.6 MiB of an 8,188 MiB card** — the card is
full, and anything else resident on it (a desktop compositor, a second process,
a browser) is the difference between finishing and not. At 60 minutes it
reserves **9,146 MiB, which is more memory than the card has.** That is not an
error in the measurement: on Windows/WDDM the driver lets a process oversubscribe
into shared system memory rather than failing, so what would be an out-of-memory
error elsewhere is instead a silent spill across PCIe. This branch needs 1,864
MiB reserved for both, and for a 30-second clip, and would need the same for a
six-hour one. Demucs' 60-minute run tells the same story from 7,454 MiB down to
768. What these runs *do* need is host memory — 10.7 GiB for the 60-minute
RoFormer, 11.7 for the Demucs — which is the subject of *What moved to host RAM*
below and of the second known limitation.

On wall clock those two pairs disagree with each other: **+0.27% at 45 minutes
and +6.3% at 60** (578.8 s against 615.4), each a single `dev`-then-branch pair
on a laptop that the alternating rounds above showed drifting 25% under
sustained load. Both are within that drift and neither is repeated, so the
honest statement is the one the isolated measurement supports — the accumulation
costs milliseconds — with the caveat that the largest gap observed anywhere in
this feature's measurements is that 6.3%, on the longest run, in the ordering
most likely to produce it.

**Where the failure is a failure**, this branch is the difference between a job
completing and a job dying. A 4 GiB card is the release-blocking case — 036 put
its limit at about nine minutes of audio — so the same 20-minute track was run
twice with the process capped by
`torch.cuda.set_per_process_memory_fraction(0.368)`, which is what a 4 GiB card
has left once its CUDA context is resident (2.94 GiB):

```text
--- dev ---
  File "…/roformer/vendor/mel_band_roformer.py", line 136, in forward
    q = self.rotary_embed.rotate_queries_or_keys(q)
  …
torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 94.00 MiB.
  GPU 0 has a total capacity of 8.00 GiB of which 4.03 GiB is free.
  2.94 GiB allowed; Of the allocated memory 2.81 GiB is allocated by PyTorch…

--- new ---
  302 chunks, 182.0 s, RTF 6.60,
  peak allocated 1,526.1 MiB · reserved 1,864 MiB · whole-device 2,980.6 MiB
```

Note *where* `dev` died: **inside a forward pass**, not at the allocation of the
accumulator. The mixture, the accumulator and the weight tensor had already
taken 2.81 GiB of the 2.94 available before the network asked for a 94 MiB
working tensor and was refused. That is precisely the failure this feature was
opened for — "a CUDA OOM part-way through a job the user has already waited
minutes for" — reproduced, and then made not to happen.

### What moved to host RAM — measured, not estimated

The accumulator and the weight did not vanish; they moved. The obvious worry is
that this feature trades a CUDA OOM for a host `MemoryError` on a machine that
meets the published `minimum_ram_mb: 8192`. It was measured rather than reasoned
about — peak working set and peak commit for the whole process, from
`GetProcessMemoryInfo`, one fresh process per row, MiB:

| model | clip | | peak working set | peak commit |
| --- | --- | --- | --- | --- |
| `standard-stems-001` | 10 min | before | 3,448.6 | 6,915.5 |
| `standard-stems-001` | 10 min | **after** | **3,287.1** | **5,517.9** |
| `standard-stems-001` | 60 min | before | 11,947.3 | 21,313.7 |
| `standard-stems-001` | 60 min | **after** | **11,714.4** | **14,034.3** |
| `standard-stems-001` | 90 min | after | 16,885.6 | 19,536.7 |
| `vocals-hq-001` | 10 min | before | 3,888.8 | 8,506.1 |
| `vocals-hq-001` | 10 min | **after** | **3,659.5** | **7,049.0** |
| `vocals-hq-001` | 60 min | after | 10,653.1 | 14,047.6 |

**At every length that could be compared, this branch uses *less* host memory
than `dev`, not more** — 161 MiB less for a ten-minute Demucs run, 229 less for
RoFormer, and 1.4 GiB less commit. The accumulator arriving on the host is
offset by what no longer happens: `dev` copied the whole finished accumulator
device→host at the end of the loop, so the host paid for it either way, and it
paid for the wide `(stems, channels, samples)` weight tensor's copy too.

So `minimum_ram_mb: 8192` is **not falsified by this change**. What is new is
that longer tracks are now reachable at all, and those need host memory in
proportion to their length. Fitting the rows above:

```text
peak working set (MiB) ≈ 1,600 + 2.81 × seconds   (standard-stems-001)
peak working set (MiB) ≈ 2,260 + 2.33 × seconds   (vocals-hq-001)
```

Demucs' 2.81 MiB per second of audio is the accumulator (1.35), the four
finished int16 stems (0.67), the host mixture (0.34), the decoded PCM (0.17),
the weight (0.17) and the float temporaries stem assembly builds one at a time.
So **8,192 MiB of RAM covers about 39 minutes of audio for Demucs and about 42
for RoFormer**; 16 GiB covers roughly 85 and 100; 32 GiB, three hours and more.
A four-minute song needs about 1.7 GiB either way.

That is the honest form of this feature's headline. **038 does not make track
length free — it moves what limits it from the graphics card to the host**,
where there is typically four to eight times more of it, where it can be added
without buying a GPU, and where running out is a `MemoryError` at a predictable
size rather than a CUDA OOM whose threshold depended on a card the user cannot
change. `minimum_ram_mb` is left at 8,192 (it is out of this feature's scope,
and it is right for any normal song), but the figure it covers is now written
down, which it was not before.

## Known limitations

- **Demucs' normalization statistics are still O(duration) on the device, and
  that is why its `requirements` do not move.** The mono reference is four bytes
  per sample — 100 MiB for a ten-minute track, 605 MiB for an hour — held only
  until `shift` and `scale` are computed, before the first forward pass. Below
  about **38 minutes** of audio it is smaller than the forward pass's own
  ~382 MiB working set and invisible; above it, peak allocated rises at
  0.168 MiB per second (measured 616 MiB at 45 minutes, 768 at 60, 1,070 at 90,
  against 550 flat below) and whole-device at 0.077 (2,210.6 MiB at 90 minutes).
  Eleven to twenty-four times smaller than the 1.85 MiB/s it replaced, and not
  zero. It is there because `mean()` and `std()` over the whole track are
  reductions, and a reduction is the one operation whose result differs between
  host and device; moving it would have changed the audio, which this feature is
  not allowed to do. Removing it needs a different bargain — a blocked reduction
  whose result is *defined* rather than inherited — which is a change to the
  output and therefore a separate, numbered decision. **RoFormer has no
  equivalent**: no whole-track reduction, which is why its figure is flat to the
  byte and its `recommended_vram_mb` could drop.
- **Host RAM, not VRAM, now limits track length.** Measured above: about 39
  minutes of audio per 8 GiB for Demucs, 42 for RoFormer. This branch uses less
  host memory than `dev` at every length `dev` could complete, so nothing
  regressed — but a user with 8 GiB of RAM and a 4 GiB card will now meet the
  RAM wall where they used to meet the VRAM one. `minimum_ram_mb` stays 8,192
  (out of scope here, and correct for any normal song); a feature that wants to
  advertise album-length or DJ-set separation should re-derive it, or spill the
  accumulator to disk, which is the next thing to do if this ever binds.
- **RoFormer's reflect-padded borders are still materialised whole-track on the
  host**, so the decoded mixture is held twice in RAM during a run. Cheap next
  to the accumulator, and it is part of the 2.33 MiB/s above.
- **Neither backend reads the mixture from disk in windows.** The brief invited
  considering it; the decoded PCM is host-resident already, and the measurements
  above say host RAM is comfortable up to lengths well past any music file. It
  is the obvious next move if the previous limitation ever binds.
- **On Windows/WDDM there is no clean OOM to observe.** The driver lets a
  process oversubscribe into shared system memory rather than failing, which is
  why `dev`'s 60-minute runs "reserved" 9,146 MiB (RoFormer) and 8,042 (Demucs)
  on an 8,188 MiB card and still finished, slowly. Capping the process is how
  this feature reproduced the real failure; on Linux, or on any card without
  that fallback, `dev` simply dies.
- **The measurements are one host.** RTX 4060 Laptop, WDDM, `cu130`, 64 GiB of
  RAM. The CUDA context (1,079 MiB) and the allocator's behaviour differ
  elsewhere; the flatness does not, because it is a property of what the code
  allocates.
