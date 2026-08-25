# [034] Lazy separator builders (torch optional again)

Branch: `034-lazy-separator-builders`
Status: PR OPEN
Dependencies: 026
PR: #…

## Objective

The application imports, starts, serves and runs fake-separator jobs **with
PyTorch absent**. Installing torch is what enables the real separator, and
nothing else changes.

## The defect

Feature 030 reported it. `backend/src/straticate/inference/registry.py`
imported the RoFormer builder at module scope, so
`straticate.main` → `inference/registry.py` → `inference/roformer/` → `torch`
happened at *import* time. The application therefore could not start without
PyTorch at all, even for a run that would only ever touch the fake engine.

Three costs, one design regression:

- the `e2e` CI job installed torch (~183 MiB on Linux) it never executed — it
  drives the fake separator exclusively;
- a fake-separator-only deployment (CI, a frontend developer, a contributor
  without the disk) had to install it anyway;
- it undid a property **feature 018 deliberately designed for**. Torch was an
  *optional runtime probe* precisely so "normal CI never requires CUDA"
  (ARCHITECTURE.md §14) and so the absence of torch was "a normal, first-class,
  tested path, not an error" (`docs/features/018-device-detection.md`). Feature
  026 made torch a hard dependency of *inference*, which is right; it
  accidentally made it a hard dependency of *importing the app*, which is not.

## What changed

### 1. The architecture name moved out of the torch-importing module

New `backend/src/straticate/inference/roformer/architecture.py` holds
`ROFORMER_ARCHITECTURE` and nothing else. The registry needs the architecture
*name* at import time to key its builder map; it must not import torch to get
it. `roformer/separator.py` now imports the constant from there and re-exports
it, so every existing import of it — from `straticate.inference`, from
`straticate.inference.roformer`, from `…roformer.separator` — is unchanged.

### 2. Both packages re-export the torch-backed names lazily (PEP 562)

`straticate.inference.roformer` and `straticate.inference` gained a module
`__getattr__` that imports `roformer/separator.py` on first access to
`RoFormerSeparator`, `RoFormerParameters`, `NvmlProbe`,
`DEFAULT_CHUNK_SAMPLES` or `DEFAULT_NUM_OVERLAP`, and memoises the result in
module globals so the second access is an ordinary attribute lookup. The names,
the `__all__` lists and the types a caller (or pyright) sees are exactly what
they were — the `if TYPE_CHECKING` imports keep the static surface intact. What
changed is *when* the cost is paid.

### 3. The RoFormer builder resolves its implementation on first use

`roformer_separator_builder()` keeps its signature and its place in
`default_separator_builders()`; the import now happens inside the `build`
closure, which the registry runs from `_build_once` — i.e. inside the worker
thread `SeparatorRegistry.aget` dispatches to, exactly where a real backend's
several hundred megabytes were already being loaded. `SeparatorRegistry`'s
public surface, its per-model instance cache, and its `threading.Lock`
arrangement (029/026 hardened both, and a review verified the lock choice
specifically) are untouched.

### 4. `torch` became an optional extra

`backend/pyproject.toml` moved `torch`, `numpy`, `einops`,
`rotary-embedding-torch` and `beartype` into
`[project.optional-dependencies] torch`. All five are there only because the
vendored Mel-Band RoFormer architecture imports them (`numpy` is reached solely
through `roformer/vendor/mel_filters.py`, and torch warns at import when it is
missing), so all five move together. The CPU-wheel index pinning is
**unchanged** — `[[tool.uv.index]] pytorch-cpu` with `explicit = true` and
`[tool.uv.sources] torch` still govern the requirement, because `tool.uv.sources`
applies wherever a requirement is declared. The CUDA story is untouched.

```console
uv sync                 # a working application, no PyTorch
uv sync --extra torch   # …plus the real separator
```

### 5. CI

`backend` installs the extra and keeps running the real separator's unit tests;
`e2e` does not install it and still passes. `uv run` re-syncs the environment to
the extras it is given, so `--extra torch` is repeated on every `uv run` step of
the `backend` job — omitting it on one step would silently uninstall torch for
the next.

## Error contract: unchanged, deliberately

A job for a model whose backend cannot be imported raises the **existing**
`separator_unavailable` (501), with the **existing** message and the existing
`detail` — byte for byte what a catalogued-but-unimplemented architecture (say
`demucs`) has always produced. `registry._separator_unavailable()` is now the
one place that envelope is built, and `test_torch_optional.py` pins the two
paths as equal so they cannot drift.

No new error code was invented: from the client's side "this build has no
implementation able to run that model" is a single fact, however the server
reached it. The message was **not** changed to mention the missing package
either — that would be a change to a documented response body for the benefit
of a reader who cannot act on it. Instead the registry logs a `WARNING` naming
the model, the architecture, the underlying `ImportError` and the fix
(`uv sync --extra torch`). A deployment fault belongs in the server's log.

`docs/contracts/rest-api.md` therefore needed no edit.

## Expected modules/files

- `backend/src/straticate/inference/roformer/architecture.py` (new)
- `backend/src/straticate/inference/roformer/__init__.py`
- `backend/src/straticate/inference/roformer/separator.py`
- `backend/src/straticate/inference/__init__.py`
- `backend/src/straticate/inference/registry.py`
- `backend/pyproject.toml`, `backend/uv.lock`
- `backend/tests/test_torch_optional.py` (new)
- `.github/workflows/ci.yml`

## Acceptance criteria

- [x] With the builder's module unimportable, the app imports, starts, serves,
      and runs a fake-separator job end to end.
- [x] A job for a model whose backend is unavailable returns the existing
      `separator_unavailable` (501) envelope.
- [x] With torch installed, behaviour is identical to today — every
      pre-existing test passes unchanged.
- [x] `uv sync` without the extra produces a working app; `uv sync --extra
      torch` adds torch; CPU-wheel pinning intact.
- [x] The `e2e` CI job no longer installs torch and still passes; the `backend`
      job still does.
- [x] Backend gates green, suite clean under `-W error`; frontend gates green.

## Required tests

`backend/tests/test_torch_optional.py`, 16 tests:

**How absence is simulated** — the same way feature 018 does it: nothing is
uninstalled. A `torch_absent()` context manager evicts `torch` and
`straticate.inference.roformer.separator` from `sys.modules`, pops the values
the lazy re-exports have memoised, and installs a `sys.meta_path` finder that
raises `ModuleNotFoundError` for `torch` — restoring all three on the way out,
so the rest of the suite (which legitimately imports torch) is untouched. A
first test asserts the window really hides torch, so the others cannot pass
vacuously.

- the simulation hides torch, and restores it (2)
- every lazy re-export genuinely re-imports rather than serving a cache, and an
  unknown name is still an `AttributeError`, not an `ImportError` (2)
- **in a subprocess** — `import straticate.main` (and `create_app()`) leaves
  `torch` out of `sys.modules`; so does importing the roformer package for its
  architecture name; so does building the default registry (3). A subprocess is
  the only honest place for this: torch *is* installed for the `backend` job.
- the registry's 501: right code/status/message/detail; provably
  indistinguishable from an unimplemented architecture; the package named in the
  log and not in the envelope; not cached, so the same registry builds the
  separator once torch is back; the fake engine unaffected (5)
- the application: `create_app()` succeeds; `/health`, `/models` and
  `/separation-modes` serve; a fake job runs upload → job → completion →
  result → real `RIFF` stem bytes; a job for a torch-backed model whose weights
  **are** installed is the documented 501 envelope; and the inverse — with torch
  present that same request is a `201` (4)

Full backend suite: **741 passed, 4 deselected** under `-W error`
(725 before this feature). E2E tier against a torch-free backend: **13 passed**.

## What each CI job costs

| | before | after |
| --- | --- | --- |
| `backend` | `uv sync` — 32 base + dev packages **+ torch and its 12 companions** | `uv sync --extra torch` — identical |
| `e2e` | `uv sync` — same, **torch included** | `uv sync` — 32 packages, **no torch** |

What the `e2e` job stops downloading and unpacking: the `torch` CPU wheel
(~183 MiB on `linux_x86_64`, per feature 030's measurement) plus 27.0 MiB of
wheels it drags in — `numpy` 16.0, `sympy` 6.0, `networkx` 2.0, `beartype` 1.3,
`setuptools` 0.8, `mpmath` 0.5, and `fsspec`/`filelock`/`jinja2`/`markupsafe`/
`einops`/`rotary-embedding-torch` under 0.2 each. Installed, that is **545 MiB
of `site-packages`** (measured on this checkout: torch 458.5, numpy 41.8, the
pure-Python companions 44.4).

It also stops *importing* it: `import straticate.main` went from ~2.8 s to
**~0.75 s** on this machine, so every backend start in the tier — and every
`uv run python -m straticate` on a machine without the extra — saves about two
seconds. The `backend` job's cost is unchanged in every respect.

Wall-clock: `e2e`'s "Sync backend dependencies" step was 6 s with a warm uv
cache (030's measurement); the tier runs in parallel with `backend` and
`frontend` and added nothing to the pipeline's wall-clock before, so it still
adds nothing. The saving is bandwidth, cache size and cold-start time, not
critical path.

## Notes / decisions

- **`SeparatorRegistry.architectures` still reports both architectures**,
  whether or not the extra is installed. It is a statement of what this build
  *implements*; whether an implementation's dependencies are present is settled
  when the first model of that architecture is built, and reported as the same
  501. Probing every backend's imports at startup so the set could be filtered
  would pay torch's ~2 s import on every start for a fact only `POST /jobs`
  ever needs — the exact cost this feature exists to stop paying. The existing
  test asserting `frozenset({"fake", "mel_band_roformer"})` therefore passes
  unchanged.
- **A model is still *offered* on `/models` when its backend is missing.**
  Hiding it would be a second, silent catalog filter (feature 032 owns that
  concept) and would make "why can't I see the HQ model?" an unanswerable
  question. The honest answer arrives at `POST /jobs`.
- **`uv run` re-syncs.** There is no `UV_EXTRA` environment variable and no
  `[tool.uv] default-extras`, so `uv run pytest` after `uv sync --extra torch`
  would *remove* torch. Every torch-needing command must say `--extra torch`
  itself. This is a real ergonomic trap for a developer working on the real
  separator and is the one thing `DEVELOPMENT.md` must spell out.

### Out of scope — needs folding in by whoever owns the file

- **`DEVELOPMENT.md` (feature 036 owns it).** Two edits are needed:
  its "PyTorch and CUDA" section should say that a plain `uv sync` no longer
  installs torch, that `uv sync --extra torch` is what a user who wants real
  separation runs (and that the CUDA-build command follows it), and that
  `uv run` needs `--extra torch` too. The quality-bar commands for anyone
  touching the real separator become `uv run --extra torch <ruff|pyright|pytest>`.
- **`ARCHITECTURE.md` §14** currently says "PyTorch is a runtime dependency from
  feature 026 onwards". It is now an *optional* runtime dependency: mandatory
  for the real separator, absent by default, with its CPU-wheel pinning
  unchanged. The sentence wants one clause. Not edited here — this feature's
  documentation scope was its own feature doc and its ROADMAP row.
