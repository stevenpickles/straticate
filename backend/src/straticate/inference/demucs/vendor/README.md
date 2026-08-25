# Vendored Hybrid Transformer Demucs architecture

This directory holds a **pinned copy** of a third-party neural-network
architecture. It is not maintained here and it is not a dependency — it is
source, copied once, with its licence.

## Why vendored rather than depended on

Feature 028 evaluated `demucs` (the PyPI package) honestly against the criteria
feature 026 set, and reached the same answer for stronger reasons.

1. **A checkpoint is pinned to its architecture code.** A `.th` package loads
   only into a network whose module structure and hyperparameters match it
   exactly. Tracking a dependency's releases would mean a routine
   `uv lock --upgrade` could rename a submodule or reshape a layer, and the
   first symptom would be a state dict that no longer loads — or, worse, one
   that loads partially and produces quiet nonsense. Architecture code a
   published checkpoint is pinned to is pinned source. The pinning here is
   literal: the checkpoint pickles a *reference to the class object*
   `demucs.htdemucs.HTDemucs`, so the class path is part of the artifact.
2. **The package brings its own model registry and remote-file downloader.**
   `demucs` depends on `huggingface-hub`, ships `demucs/remote/*.yaml` naming
   every published checkpoint, and fetches them with
   `torch.hub.load_state_dict_from_url`. That is features 010 and 025,
   duplicated, with a second copy of "where do weights live" and "has this one
   been verified". `docs/features/027-mdx-fast-separator.md` is about exactly
   this hazard.
3. **It brings its own audio I/O too.** `lameenc` (an MP3 encoder) and `sphn`
   (audio decoding) are hard dependencies, against ARCHITECTURE.md §5's single
   FFmpeg compatibility layer; `tqdm` writes progress bars to stdout, which a
   server process does not do.
4. **Its separation entry point is pipeline-shaped.**
   `demucs.api.Separator.separate_audio_file(path)` reads the file itself.
   `apply_model` does offer a per-chunk `callback` — so per-chunk progress
   *would* have been reachable, unlike feature 026's upstream — but it has no
   cancellation token, spawns its own `ThreadPoolExecutor`, and handles only
   `KeyboardInterrupt`. Cooperative cancellation between chunks would have
   meant raising out of a callback through somebody else's futures.

Vendoring `HTDemucs` costs eight files and **zero new dependencies**: everything
they import (`torch`, `einops`, `numpy`) is already in the `torch` extra that
feature 026 established. Depending would have added eight packages to get one
`nn.Module`. That is the trade, and it is why this directory is larger than
`../../roformer/vendor/` and still the smaller choice.

`../separator.py`'s chunked overlap-add follows upstream's
`demucs/apply.py::apply_model` (same commit) — same window, same
`(1 - overlap)` stride, same triangular transition weight, same centred
`TensorChunk.padded` windowing, same `center_trim`, same
mixture normalization from `demucs/api.py::separate_tensor` — reimplemented
rather than copied so it can report progress, honour a cancellation token and
run inside a worker thread.

`../separator.py` also **does not use upstream's checkpoint loader**. Upstream's
`demucs/states.py::load_model` is a plain `torch.load` over a fully trusted
pickle that then calls the pickled class. Straticate reads the same file with a
restricted unpickler that resolves the architecture reference to an inert
placeholder and refuses every module a real checkpoint does not name, and builds
the network from the hyperparameters in `models/catalog.json` instead
(ARCHITECTURE.md §9).

## What was taken, and from where

| | |
| --- | --- |
| Upstream project | [`adefossez/demucs`](https://github.com/adefossez/demucs) — the maintained fork; `facebookresearch/demucs` is archived |
| Commit taken | `eeac1d15891af95b1288d2884b95baa3e5baa96c` (2026-07-11, branch `main`) |
| Licence | MIT — `LICENSE` in this directory, copied verbatim from that commit (Copyright (c) Meta Platforms, Inc. and affiliates). Byte-identical to the archived repository's `LICENSE`. |
| Files copied | `demucs/htdemucs.py` · `demucs/hdemucs.py` · `demucs/demucs.py` · `demucs/transformer.py` · `demucs/spec.py` · `demucs/utils.py` · `demucs/wiener.py`, each to the same name here; plus an excerpt of `demucs/states.py` |

Only `HTDemucs` is used. The rest are here because it imports them:
`hdemucs` for `pad1d`/`ScaledEmbedding`/`HEncLayer`/`MultiWrap`/`HDecLayer`,
`demucs` for `DConv`/`rescale_module`, `transformer` for
`CrossTransformerEncoder`, `spec` for the STFT pair, `utils` for `center_trim`
and `unfold`, `wiener` for the (unused at `wiener_iters: 0`) Wiener filter, and
`states` for the `capture_init` decorator on the two model constructors.

`wiener.py` is itself vendored code, and its attribution travels with it: it is
[Open-Unmix](https://github.com/sigsep/open-unmix-pytorch)'s
`openunmix/filtering.py`, MIT, Copyright (c) 2019 Inria (Fabian-Robert Stöter,
Antoine Liutkus). Its header records that. **This is one of the reasons the copy
was taken from the maintained fork rather than the archived repository**: the
archived tree imports `from openunmix.filtering import wiener` — a whole second
separation package, as a dependency, for one function on a code path
`htdemucs` never reaches — while the fork vendors the file. Taking the fork
removed a modification this directory would otherwise have had to carry.

Not copied: upstream's `api.py`, `apply.py`, `separate.py`, CLI, training code,
`repo.py`/`pretrained.py`/`remote/` (its model registry and downloader),
`audio.py` (its audio I/O), and the rest of `states.py`. Straticate has its own
catalog (010), its own download manager (025), its own decoder
(`inference/pcm.py`) and its own chunk loop (028).

### Divergence between the fork and the archived repository

Recorded because it is the reason the fork was chosen, and because the
difference has to be *known* not to matter for the checkpoint this feature pins.
At the two commits compared (`facebookresearch/demucs@e976d93` — the final state
of the archived repository — and `adefossez/demucs@eeac1d15`):

| file | difference |
| --- | --- |
| `htdemucs.py`, `hdemucs.py` | `from openunmix.filtering import wiener` → `from .wiener import wiener`; Intel XPU added alongside the existing MPS complex-number workaround, and the "move back" now targets the tensor's original device rather than a hard-coded `"mps"` |
| `spec.py` | the same MPS/XPU widening |
| `demucs.py` | one type annotation (`mods: tp.List[nn.Module]`) |
| `transformer.py` | **identical** |
| `utils.py` | a `fatal()` helper added, a stray `nonlocal` removed |
| `LICENSE` | **identical** |

None of it touches a parameter name, a layer shape or a `ModuleList` order, so
the published `htdemucs` state dict loads into either — and it is checked, not
assumed: `tests/test_demucs_integration.py` asserts the real checkpoint loads
with no missing and no unexpected keys.

## Straticate's modifications

Kept to the minimum that lets the code run inside this application, listed here
in full and marked at their sites. Each file also carries a `VENDORED CODE`
banner pointing here.

1. **`demucs.py` — `julius` is imported lazily.** Upstream imports it at module
   scope. It is used only by `Demucs`, the v3 time-domain model, which this
   application never builds — the file is vendored because `HDemucs`/`HTDemucs`
   import `DConv` and `rescale_module` from it. A module-scope import would make
   `julius` a hard dependency of importing the architecture at all, for a class
   that cannot be reached. The two `julius.resample_frac` call sites import it
   locally instead, so `Demucs` still works wherever `julius` is installed and
   raises a plain `ImportError` where it is not.
2. **`states.py` is an excerpt.** Only `capture_init` is copied, verbatim.
   Upstream's module also holds `load_model` — a `torch.load` over a fully
   trusted pickle that then *calls* the pickled class — which is precisely the
   thing `../separator.py` deliberately does not do. Leaving it in the tree
   would be an unused footgun.
3. **A `VENDORED CODE` banner** at the top of every file.

Nothing else was changed: no parameter renamed, no default altered, no layer
touched.

## Linting

Every file here is excluded from Ruff and Pyright (`backend/pyproject.toml`) so
this copy stays byte-diffable against the upstream commit it came from. That is
the same rule `../../roformer/vendor/` follows. Straticate's own code — the
separator, the checkpoint reader, the chunk loop — lives in `../separator.py`
and is **not** excluded.

## The weights are a separate question

The MIT licence in this directory covers **this code**. The pretrained weights
are not in the upstream repository at all — they are fetched from
`dl.fbaipublicfiles.com` — and they are **not** MIT. See
`docs/features/028-demucs-four-stem.md`, *Licensing*, and the
`licensing` block of the `standard-4stem-001` entry in `models/catalog.json`.
Straticate never redistributes them.
