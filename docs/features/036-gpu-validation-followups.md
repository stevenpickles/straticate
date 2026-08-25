# [036] GPU validation follow-ups

Branch: `036-gpu-validation-followups`
Status: PR OPEN
Dependencies: 026, 029

## Objective

Four defects found by executing the CUDA path for the first time, on an
NVIDIA GeForce RTX 4060 Laptop GPU (8188 MiB, driver 610.47 / CUDA 13.3), on
2026-08-25. None was reachable before a GPU was available.

## Scope

### 1. The catalog's VRAM requirement is wrong (medium)

> **This section's original premise — "wrong by more than 5×" — did not survive
> re-measurement.** It was right that the number was a guess and right that the
> number is wrong; it was wrong about the size and the direction of the error.
> See *[What re-measuring actually found](#what-re-measuring-actually-found)*.
> The text below is preserved as the original report.

`models/catalog.json` declares `requirements.recommended_vram_mb: 8192` for
`vocals-hq-001`. **Measured peak allocation is 1,634 MiB** — 1,575 MiB on a 30 s
clip, 1,634 MiB on a 20 s clip through the API, at the catalog's own
`chunk_size: 352800` / `num_overlap: 2`.

That figure was inherited from upstream guidance, not measured. It matters
because it is user-facing (`Model.requirements`) and would tell a user with a
4 GB card that a model needing ~1.6 GB will not run. Nothing currently *acts*
on the number, so it misinforms rather than blocks — which is why it went
unnoticed.

Fix: correct it to a measured value with headroom, and record in the feature doc
how it was measured and at which inference parameters (peak scales with
`chunk_size`, so the number is only meaningful alongside them). Consider adding
`minimum_vram_mb` if a floor is worth stating separately from a recommendation.

### 2. DEVELOPMENT.md's CUDA verification command undoes the install (medium)

The *PyTorch and CUDA* section correctly warns that "a later `uv sync` puts the
CPU build back", then immediately prescribes:

```bash
uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

**`uv run` re-syncs the environment before running**, so it reinstalls the CPU
wheel and then reports `2.13.0+cpu / False` — the verification step destroys
what it is verifying, and reports the failure it caused as though it were the
original state. Observed verbatim:

```
Uninstalled 1 package in 6.35s
Installed 1 package in 17.48s
torch: 2.13.0+cpu
cuda available: False
```

Fix: verify with `uv run --no-sync …` or the venv interpreter directly
(`.venv/Scripts/python.exe`, `.venv/bin/python`), and say plainly that **any**
`uv run` or `uv sync` reverts the CUDA build. The same applies to the section's
`curl localhost:8000/api/v1/system/devices` check — the server must be started
without re-syncing, or it will be running the CPU wheel. Worth also noting that
`pytest` must be invoked the same way, or the integration tier silently runs on
CPU.

### 3. The NVML package to install is `nvidia-ml-py`, not `pynvml` (low)

Feature 026 treats NVML as optional (correct — ARCHITECTURE.md §12). But
installing `pynvml`, the obvious-looking name, installs a **deprecated shim**
whose import raises `FutureWarning` from inside `torch/cuda/__init__.py`. Since
the backend suite runs under `-W error` (DEVELOPMENT.md), that turns a green
suite red:

```
E FutureWarning: The pynvml package is deprecated. Please install nvidia-ml-py
  instead. If you did not install pynvml directly, please report this to the
  maintainers of the package that installed pynvml for you.
```

`nvidia-ml-py` provides the same `pynvml` module, works identically, and leaves
the suite clean — verified both ways. Fix: document `nvidia-ml-py` explicitly
wherever NVML is mentioned, and say why the other name is a trap.

### 4. The GPU test's docstring is now false (low)

`backend/tests/test_roformer_integration.py::test_cuda_runtime_stats_report_real_memory`
says: *"Never executed on a CPU-only host — including the one this feature was
developed on, where it was **not** run."* It has now run, and passed. Update it
to record when and on what, so the next reader is not misled into thinking the
path is still unverified.

## Out of scope

Adding NVML as a dependency (it must stay optional). Changing inference
parameters or chunk sizing. GPU support for any other separator.

## Acceptance criteria

- [x] `recommended_vram_mb` reflects a measured figure, with the measurement and
      its inference parameters recorded
- [ ] Following DEVELOPMENT.md's CUDA section end to end leaves a working CUDA
      build and reports it correctly
- [ ] NVML guidance names `nvidia-ml-py` and explains the `pynvml` trap
- [ ] The GPU test's docstring matches reality
- [ ] Suite still clean under `-W error`

## Required tests

A test pinning that every catalogued model's `recommended_vram_mb` is present
and plausible would not have caught this (the old value was plausible). The real
guard is the measurement being recorded; prefer documentation over a test that
cannot fail.

## Notes / decisions

### What re-measuring actually found

Every figure below was measured on this branch, on **2026-08-25**, on an NVIDIA
GeForce RTX 4060 Laptop GPU (8,188 MiB, driver 610.47 / CUDA 13.3, WDDM) with
`torch 2.13.0+cu130` on Python 3.12.11, against the real `vocals-hq-001`
checkpoint. Each row is **its own process**, so the CUDA context, the caching
allocator and the resident network are all fresh: what is reported is what a
server that has just started needs for a track of that length. Whole-device
usage was sampled every 20 ms from `torch.cuda.mem_get_info`, and `nvidia-smi`
confirmed the card was otherwise at **0 MiB** before each run.

The measuring script is not committed (it configures a separator directly, which
is the integration tier's job); it built a stereo 44.1 kHz tone with
`tests/audio_fixtures.write_tone_wav`, ran `RoFormerSeparator.separate` against
`cuda:0` and reported `torch.cuda.max_memory_allocated`,
`torch.cuda.max_memory_reserved`, the separator's own
`runtime_stats().device.memory_peak_bytes` and the sampled whole-device peak.
`memory_peak_bytes` and `max_memory_allocated` agreed to the byte on every run,
which is the first thing worth knowing: **the number the application reports is
correct**. It is simply not the number a user needs.

#### Peak against track length, at the catalog's own parameters

`chunk_size: 352800` (8 s), `num_overlap: 2`. MiB throughout.

| clip | chunks | peak allocated | peak reserved | whole-device peak | RTF |
| --- | --- | --- | --- | --- | --- |
| 10 s | 5 | 1,549 | 1,884 | 3,001 | 2.46 |
| 20 s | 7 | 1,561 | 1,904 | 3,021 | 3.61 |
| 30 s | 10 | 1,575 | 1,906 | 3,023 | 4.20 |
| 60 s | 17 | 1,615 | 1,958 | 3,075 | 5.25 |
| 2 min | 32 | 1,700 | 2,038 | 3,155 | 5.95 |
| 4 min | 62 | 1,860 | 2,198 | 3,315 | 6.20 |
| 6 min | 92 | 2,021 | 2,358 | 3,475 | 6.34 |
| 10 min | 152 | 2,343 | 3,096 | **4,213** | 6.47 |

**The premise that chunking bounds the peak is false, and the feature's own
dependency says why.** 026's known limitations already record it: "the decoded
mixture, the accumulator and the weight tensor all live in memory at once …
following upstream's `demix_track`". Those three tensors are on the *device*, so
peak allocation grows linearly with the track — **≈1.35 MiB per second of
audio** — and 026's "roughly half a gigabyte of float32 [for a 10-minute stereo
track] on top of the model" was a statement about VRAM as much as about RAM. The
30 s and 20 s clips the original report measured are the shortest end of a line
that keeps climbing; they were never going to reveal it.

#### Peak against `chunk_size`, at a fixed 60 s clip

| `chunk_size` | window | chunks | peak allocated | peak reserved | whole-device peak |
| --- | --- | --- | --- | --- | --- |
| 88,200 | 2 s | 62 | 1,136 | 1,280 | 2,397 |
| 176,400 | 4 s | 32 | 1,297 | 1,486 | 2,603 |
| 352,800 | 8 s | 17 | 1,615 | 1,958 | 3,075 |
| 705,600 | 16 s | 10 | 2,259 | 2,784 | 3,901 |

`num_overlap` does **not** move the peak: 4 instead of 2, same clip, same
`chunk_size`, gave 1,620 MiB against 1,615 MiB — while halving throughput (RTF
2.63 against 5.33), because it doubles the number of forward passes over the
same audio. Overlap buys quality with time, not with memory.

The two sweeps fit one line to within 3 MiB across every measurement above:

```text
peak allocated (MiB) ≈ 895 + 1.82 × (chunk_size / 1000) + 1.35 × track_seconds
```

895 MiB is the resident network — 228,202,852 parameters × 4 bytes = 870 MiB —
plus its workspace. So the catalog's chunking chooses the middle term, and the
track chooses the last one.

#### The figure a card actually has to have free is roughly twice the reported one

`max_memory_allocated` counts tensors. A card must also hold:

- **The CUDA context**, which on this host is **1,079 MiB before a single byte
  of tensor is allocated** — measured by calling `torch.cuda.mem_get_info` in a
  fresh process and reading `total - free`. This is a WDDM/`cu130` figure and
  will differ elsewhere, but it is never zero.
- **The caching allocator's reservation**, which exceeds live tensors by
  330–750 MiB here and grows with fragmentation over a long run (3,096 MiB
  reserved against 2,343 MiB allocated on the 10-minute track).

Whole-device peak ≈ context + reserved, and that is the number that decides
whether a job runs. For the 10-minute track it is **4,213 MiB**, not 2,343 and
certainly not 1,634.

#### The values chosen, and the headroom

```json
"requirements": {
  "recommended_vram_mb": 6144,
  "minimum_vram_mb": 4096,
  "minimum_ram_mb": 8192
}
```

- **`minimum_vram_mb: 4096`** — the floor. Interpolating the length sweep, a
  fresh process reaches 4,096 MiB of whole-device use at about a **9-minute**
  track, so a 4 GiB card with nothing else resident runs any normal song; a
  typical 3–4 minute track leaves it ~700 MiB spare. Below 4 GiB, do not try.
  This is a real change in what the catalog says: the old single figure told a
  4 GiB owner "no", and the measurement says "yes, for the music you have".
- **`recommended_vram_mb: 6144`** — the comfortable figure: the measured
  4,213 MiB for a 10-minute track, plus ~1.9 GiB of headroom. That headroom is
  deliberate and is spent on three things, none of them padding: a card that is
  also driving a display (a desktop compositor is commonly 500–800 MiB), a CUDA
  context that differs by driver, OS and CUDA minor version, and allocator
  fragmentation on tracks longer than the 10 minutes measured. Both numbers are
  card sizes that exist, which is the point of a hardware recommendation.

**So the original report's direction was right and its magnitude was wrong.**
8,192 → 6,144 is a 25% correction, not the ">5×" the scope section and the
ROADMAP claimed (the ROADMAP sentence is corrected in this PR). The value of
the change is not the size of the delta: it is that the number is now measured,
that a floor is stated separately from a recommendation, and that the parameters
and the track-length dependence the number is meaningless without are written
down.

### `minimum_vram_mb` was added — a schema change, and why it earns its keep

The manifest schema already carried `requirements.minimum_ram_mb`, so a VRAM
floor is the symmetric field rather than a new concept, and the measurement
produced two genuinely different numbers with two different meanings: "will this
run at all on my card" and "will this run comfortably". One number cannot answer
both, and collapsing them is exactly how the old 8,192 came to mislead — a
recommendation being read as a requirement.

Nothing *acts* on either field, and that is unchanged and deliberate: both are
advisory, both are documented as advisory in `ModelRequirements` and in the
manifest schema, and no job is refused for failing one. A model with no CUDA
capability has no use for either and omits both, which the fake fixtures do.

Changed for it: `models/schemas/model-manifest.schema.json` (both VRAM
properties now carry descriptions saying what they mean and that they are
measured), `backend/src/straticate/schemas/models.py`, `models/catalog.json`,
`backend/tests/test_schemas.py`'s representative payload, and a regenerated
`frontend/src/api/generated/api.d.ts` — **the only frontend file touched**, and
its diff is the one new optional field plus description text.
