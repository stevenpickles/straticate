# Vendored Mel-Band RoFormer architecture

This directory holds a **pinned copy** of a third-party neural-network
architecture. It is not maintained here and it is not a dependency — it is
source, copied once, with its licence.

## Why vendored rather than depended on

A checkpoint only loads into code whose module structure and hyperparameters
match it *exactly*. Tracking a dependency's releases would mean that a routine
`uv lock --upgrade` could silently rename a submodule, change a layer's shape,
or reorder a `ModuleList` — and the first symptom would be a state dict that no
longer loads (or, worse, one that loads with `strict=False` somewhere and
produces quiet nonsense). Architecture code that a published checkpoint is
pinned to is pinned code.

The upstream package is also folder-oriented: its public API separates *every
WAV in an input folder*, which cannot report per-chunk progress and cannot be
cancelled cooperatively. Straticate needs the model class, not that pipeline —
the chunked overlap-add loop lives in `../separator.py`, where progress,
cancellation and telemetry belong.

Straticate deliberately does **not** depend on a high-level separation library:
those ship their own model registries and downloaders, which would duplicate
features 010 (model catalog) and 025 (download manager).

## What was taken, and from where

| | |
| --- | --- |
| Upstream project | [`openmirlab/melband-roformer-infer`](https://github.com/openmirlab/melband-roformer-infer) |
| Version taken | tag `v0.1.5`, commit `fda5d8cb65403a04e2d143ecd130f508c2f8370f` |
| Licence | MIT — `LICENSE` in this directory, copied verbatim (Copyright (c) 2025 OpenMIRLab) |
| Files copied | `src/mel_band_roformer/mel_band_roformer.py` → `mel_band_roformer.py`<br>`src/mel_band_roformer/attend.py` → `attend.py` |

Upstream in turn adapts two other MIT-licensed projects, and the attribution
travels with the code:

- [`lucidrains/BS-RoFormer`](https://github.com/lucidrains/BS-RoFormer) — the
  band-split RoFormer this architecture derives from, and the origin of
  `attend.py`.
- [`KimberleyJSN/Mel-Band-Roformer-Vocal-Model`](https://github.com/KimberleyJSN/melbandroformer)
  — the mel-band variant and the training recipe the Kim Vocal 2 checkpoint
  comes from.

Not copied: upstream's inference pipeline, CLI, MLX backend, downloader, model
registry and checkpoint tables. Straticate has its own catalog (010), its own
download manager (025) and its own chunk loop (026).

`../separator.py`'s chunked overlap-add windowing follows upstream's
`src/mel_band_roformer/utils.py::demix_track` (same tag) — same chunk size,
same `num_overlap`, same linear fade window, same reflect-padded borders, same
weight normalization — reimplemented rather than copied so it can report
progress, honour a cancellation token and run inside a worker thread.

## Straticate's modifications

Kept to the minimum that lets the code run inside this application, and listed
here in full.

1. **`mel_band_roformer.py` — `librosa` replaced by `mel_filters.py`.**
   Upstream calls `librosa.filters.mel(...)` exactly once, at construction, to
   decide which FFT bins belong to which mel band. Depending on librosa for one
   pure function would add numba, llvmlite, scipy, soundfile, audioread, pooch
   and friends (25 packages, ~90 MB of wheels measured) to an install
   ARCHITECTURE.md §14 requires to stay lean. `mel_filters.py` is a faithful
   transcription of librosa's own implementation, restricted to the defaults
   this architecture uses; it was verified **bit-identical** to
   `librosa.filters.mel` across 245 `(sample_rate, n_fft, n_mels)` combinations,
   and `tests/test_roformer_mel_filters.py` pins the band widths it produces for
   the shipped checkpoint's configuration so drift fails in normal CI.

2. **`attend.py` — the deprecated SDPA backend selector.**
   `torch.backends.cuda.sdp_kernel(enable_flash=…, …)` is deprecated in favour
   of `torch.nn.attention.sdpa_kernel([SDPBackend, …])` and emits a
   `FutureWarning`; the backend suite treats a warning as a finding. The
   *selection* is unchanged — same backends per device class — only the API
   expressing it. The `packaging`-based "torch ≥ 2.0" assertion went with it
   (`pyproject.toml` declares a floor of 2.4), as did an unused `einops` import.

3. **`attend.py` — `print_once` → `logging`.** Upstream printed its backend
   choice to stdout. A server process logs.

4. **`mel_band_roformer.py` — an explicit window on the STFT bin-count probe.**
   The constructor runs one throwaway `torch.stft` purely to read `.shape[1]`,
   the number of frequency bins. torch warns when `window` is omitted, and the
   backend suite treats a warning as a finding, so the probe passes the
   rectangular window torch's own warning text recommends for exactly this case.
   No window can change a bin count, so the value read is identical.

5. **Docstring headers** in both files gained a `VENDORED CODE` banner pointing
   here.

Items 1–4 each carry a `Straticate modification:` comment at the site, and there
are exactly four such comments in this directory — `grep -rn 'Straticate
modification' .` is meant to return this list and nothing else. (Item 5 is the
banner at the top of each file, which says so itself.) Nothing else was touched:
no reformatting, no renaming, no type annotations, no lint fixes. `backend/pyproject.toml` therefore excludes this directory from Ruff
and from Pyright, so the copy stays diffable against upstream.
