# [036] GPU validation follow-ups

Branch: `036-gpu-validation-followups`
Status: PLANNED
Dependencies: 026, 029

## Objective

Four defects found by executing the CUDA path for the first time, on an
NVIDIA GeForce RTX 4060 Laptop GPU (8188 MiB, driver 610.47 / CUDA 13.3), on
2026-08-25. None was reachable before a GPU was available.

## Scope

### 1. The catalog's VRAM requirement is wrong by more than 5× (medium)

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

- [ ] `recommended_vram_mb` reflects a measured figure, with the measurement and
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
