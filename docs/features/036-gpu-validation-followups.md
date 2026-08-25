# [036] GPU validation follow-ups

Branch: `036-gpu-validation-followups`
Status: PR OPEN
PR: #42
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

### 5. Added mid-feature: feature 034 made two documents wrong (medium)

034 (*Lazy separator builders*) moves `torch`, `numpy`, `einops`,
`rotary-embedding-torch` and `beartype` into `[project.optional-dependencies]
torch` and imports the RoFormer builder lazily, so a plain `uv sync` no longer
installs PyTorch at all. Two documents nobody else owns this wave went stale:

- **DEVELOPMENT.md**, where the CUDA instructions now need `uv sync --extra
  torch` as the step before the wheel swap — and where `uv run`'s re-sync
  acquired a *second* way to undo the environment, which belongs in one place
  with the first rather than as two unrelated warnings.
- **ARCHITECTURE.md §14**, which called PyTorch "a runtime dependency from
  feature 026 onwards". It is now an optional one.

## Out of scope

Adding NVML as a dependency (it must stay optional). Changing inference
parameters or chunk sizing. GPU support for any other separator.

## Acceptance criteria

- [x] `recommended_vram_mb` reflects a measured figure, with the measurement and
      its inference parameters recorded
- [x] Following DEVELOPMENT.md's CUDA section end to end leaves a working CUDA
      build and reports it correctly — done from `rm -rf .venv`, all seven steps
- [x] NVML guidance names `nvidia-ml-py` and explains the `pynvml` trap
- [x] The GPU test's docstring matches reality
- [x] Suite still clean under `-W error`

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

### The CUDA section was rewritten, then followed from an empty `.venv`

The report was right and the mechanism is worse than it looked: `uv run` re-syncs
*before* it runs, so the reversion is not limited to the verification command —
every `uv run` in DEVELOPMENT.md that re-pins `torch` reverts the wheel: the
quality checks, the server, `export_openapi`. `uv sync` goes further and
*prunes*, so it removes anything installed into `.venv` by hand as well, which
on a GPU host takes the optional NVML binding with it. (`uv run` does not prune
— see *What `uv run` really does* below, where the distinction turns out to
matter.) Verified:

```text
$ uv pip list | grep -i "nvidia\|^torch "
nvidia-ml-py           13.610.43
torch                  2.13.0+cu130
$ uv sync
Resolved 51 packages in 2ms
Uninstalled 2 packages in 2.72s
Installed 1 package in 21.73s
 - nvidia-ml-py==13.610.43
 - torch==2.13.0+cu130
 + torch==2.13.0+cpu
```

The section now says that plainly, lists the two escapes (`uv run --no-sync …`
and `.venv/Scripts/python.exe` / `.venv/bin/python`), notes that `uv pip
install` is safe because it does not re-sync, and carries a pointer from the
quality-checks block and from the test-strategy row that describes the
integration tier.

**Verified by following it end to end**, on 2026-08-25, in this worktree, from
`rm -rf backend/.venv`. (This run predates the 034 scope addition below, so
step 1 is a bare `uv sync`; the whole sequence was then re-run against 034's
`pyproject.toml` — see *What `uv run` really does* — and passed again.)

| step | command as written in DEVELOPMENT.md | result |
| --- | --- | --- |
| 1 | `uv sync` | `.venv` rebuilt |
| 2 | `.venv/Scripts/python.exe -c "import torch; …"` | `2.13.0+cpu False` |
| 3 | `uv pip install --reinstall-package torch --index-url …/cu130 torch` | `+ torch==2.13.0+cu130` |
| 4 | `.venv/Scripts/python.exe -c "import torch; …"` | **`2.13.0+cu130 True`** |
| 5 | `uv run --no-sync uvicorn straticate.main:app --port 8123` | starts, wheel intact |
| 6 | `curl …/api/v1/system/devices` | `cuda:0` **first**, then `cpu` |
| 7 | `uv run --no-sync pytest -m integration` | **4 passed**, `cuda:0`, wheel intact |

Step 6's response is a bare array, not an object with a `devices` key — the old
section did not show a response at all, and the new one shows the real shape.

Both traps were also reproduced deliberately, so the warnings are not
theoretical. The verification command the old section prescribed:

```text
$ uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
Uninstalled 1 package in 2.60s
Installed 1 package in 18.42s
2.13.0+cpu False
```

…and the quieter one, on the same host, immediately after step 7 passed 4/4 on
`cuda:0`:

```text
$ uv run pytest -m integration -q -rs
Uninstalled 1 package in 2.27s
Installed 1 package in 19.95s
[026] cpu: 10.0 s of audio in 63.9 s (RTF 0.156, 5 chunks)
SKIPPED [1] tests\test_roformer_integration.py:193: no CUDA device is available
3 passed, 1 skipped, 725 deselected in 71.34s
```

A green run, on a machine with a working GPU, in which the GPU test skipped
itself and the performance figure is 15× off. That is the one worth putting in
front of the reader. (Both blocks are from the pre-034 `pyproject.toml`, where
`torch` was a plain dependency. Under 034's extra the same two reversions happen
under `uv run --extra torch`, reproduced verbatim below; DEVELOPMENT.md carries
the post-034 spellings.)

### What `uv run` really does once `torch` is an optional extra

The brief for the added scope said that `uv run pytest` without `--extra torch`
"silently *uninstalls* torch and runs the integration tier on nothing".
**Measured, that is not what happens** — and the truth is more awkward, because
it points the opposite way on a GPU host. Every row below was run with uv 0.8.23
against 034's `backend/pyproject.toml`, starting from a `.venv` holding
`torch 2.13.0+cu130`:

| command | effect on `torch` |
| --- | --- |
| `uv sync` | **removed entirely** (with `nvidia-ml-py` alongside it) |
| `uv sync --extra torch` | reinstalled as the **CPU** wheel |
| `uv run pytest -m integration` | **untouched** — 4 passed on `cuda:0` |
| `uv run --extra torch pytest -m integration` | reinstalled as the **CPU** wheel; 3 passed, 1 skipped |
| `uv run --no-sync pytest -m integration` | untouched — 4 passed on `cuda:0` |
| `.venv/Scripts/python.exe -c "import torch"` | untouched |

`uv run` syncs *inexactly*: it installs and corrects the packages the requested
extras require, and leaves extraneous ones alone. Without `--extra torch`,
`torch` is extraneous, so `uv run` has nothing to correct and the CUDA wheel
survives. `uv sync` is exact and prunes, which is why it removes both the wheel
and any hand-installed NVML binding.

Two consequences, and they pull in opposite directions — which is exactly why
DEVELOPMENT.md now states them together in one table rather than as two
warnings:

- **On a CPU host**, `--extra torch` is required, and the brief's instinct is
  right for the reason that matters: the next plain `uv sync` takes torch away,
  and then the real-separator tests have nothing to import. `uv run --extra
  torch <ruff|pyright|pytest>` is the habit that always works there.
- **On a GPU host, `--extra torch` is the flag that breaks it.** It is what
  re-pins `torch` to the locked CPU wheel, whereupon the `gpu` test skips itself
  on a machine with a working GPU. `--no-sync` is the habit that always works
  there.

The post-034 spelling of the original defect, verbatim:

```text
$ uv run --extra torch python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
Uninstalled 2 packages in 2.00s
Installed 2 packages in 18.13s
2.13.0+cpu False

$ uv run --extra torch pytest -m integration -q -rs
Uninstalled 1 package in 1.96s
Installed 1 package in 17.62s
SKIPPED [1] tests\test_roformer_integration.py:193: no CUDA device is available
3 passed, 1 skipped, 725 deselected in 56.21s
```

The document does not tell anyone to rely on `uv run`'s not-pruning. That is a
property of `uv run`, not of this project, and it would be a poor thing to build
a habit on.

**The CUDA instructions were re-verified on top of `uv sync --extra torch`**,
start to finish, with 034's `pyproject.toml` and `uv.lock` checked out over this
branch (they are 034's files; the overlay was reverted immediately afterwards
and nothing from it is committed here):

| step | result |
| --- | --- |
| `uv sync` | `.venv` rebuilt; `import torch` → `ModuleNotFoundError` |
| `uv sync --extra torch` | `torch 2.13.0+cpu`, `cuda False` |
| `uv pip install --reinstall-package torch --index-url …/cu130 torch` | `+ torch==2.13.0+cu130` |
| `.venv/Scripts/python.exe -c "import torch; …"` | **`2.13.0+cu130 True`** |
| `uv run --no-sync uvicorn straticate.main:app --port 8123` | starts; wheel intact |
| `curl …/api/v1/system/devices` | `cuda:0` first, then `cpu` |
| `uv run --no-sync pytest -m integration` | **4 passed** on `cuda:0`; wheel intact |

**Ordering note.** DEVELOPMENT.md now describes `uv sync --extra torch`, which
does not exist until 034 merges. If 036 lands first, that one command is ahead
of the code by however long the gap is; everything else in the section is
correct either way. 034 touches neither DEVELOPMENT.md nor ARCHITECTURE.md, so
there is no merge conflict in either direction.

### ARCHITECTURE.md §14

One clause: "PyTorch is a runtime dependency from feature 026 onwards" → an
optional one, installed through the `torch` extra since 034, with the real
engines imported lazily so the application starts and serves without it. The
CPU-wheel-pin paragraph around it is unchanged and still correct. Nothing else
in that file was touched.

### Which `cuNNN` indexes actually exist

The old text offered `cu121`, `cu124`, `cu126`, "…", which reads as though any
`cuNNN` at or below the driver's maximum works. A PyTorch index is a directory
of files: it has a wheel for a given torch version and platform or it does not,
and one that does not surfaces as `uv` being unable to find `torch` at all.
Enumerated on 2026-08-25 from `https://download.pytorch.org/whl/cuNNN/torch/`
for the pinned `torch 2.13.0`:

| index | `torch 2.13.0` |
| --- | --- |
| `cu130` | Linux and Windows (`cp312-cp312-win_amd64`) |
| `cu129` | Linux only — no Windows wheel at any Python version |
| `cu126` | Linux and Windows (`cp312-cp312-win_amd64`) |
| `cu128` | none; that index stops at `torch 2.11.0` |
| `cu124` | none; stops at `2.6.0` |
| `cu121` | none; stops at `2.5.1` |

So both of the old text's named examples are dead for this torch, and on Windows
the real choice is `cu130` or `cu126`. The table is in DEVELOPMENT.md with the
`curl` that produced it and a note that it is version-specific and must be
re-checked when the torch pin moves.

### The GPU test's docstring

`test_cuda_runtime_stats_report_real_memory` claimed it had never executed. It
now records when it did (2026-08-25), on what (RTX 4060 Laptop, 8,188 MiB,
driver 610.47 / CUDA 13.3, `torch 2.13.0+cu130`), and what it measured:
`cuda:0`, `memory_total_bytes` 8,585,281,536, `memory_peak_bytes` **1,531.7 MiB**
for its own 5 s clip at `chunk_size: 352800` (2 chunks), and — because
`nvidia-ml-py` was installed — utilization 1.0 at 59 °C, so the two
`is None or …` assertions at the end exercised their populated branch rather
than their `None` one. It also warns that the peak is not bounded by chunking.

The 5 s figure was measured for this document rather than copied: the test
asserts bounds, not values, and printing figures from a test that a CPU host
skips would put an unverifiable number in the file. The module docstring's
commands were corrected in the same pass — they said `uv run pytest -m
integration`, which on this host is exactly the invocation that makes the test
skip.

### NVML: `nvidia-ml-py`, and why `pynvml` is a trap rather than a synonym

Reproduced both ways on this branch. `pip install pynvml` does **not** install a
competing binding — it installs `nvidia-ml-py` *plus* a `_pynvml_redirector`
meta-path finder whose `find_spec` warns:

```text
$ uv pip install pynvml
Installed 2 packages in 62ms
 + nvidia-ml-py==13.610.43
 + pynvml==13.0.1
$ .venv/Scripts/python.exe -W error -c "import torch"
  File "…\torch\__init__.py", line 2189, in <module>
    _C._initExtension(_manager_path())
  File "…\torch\cuda\__init__.py", line 64, in <module>
    import pynvml
  File "…\_pynvml_redirector.py", line 29, in find_spec
    warnings.warn(PYNVML_MSG, FutureWarning, stacklevel=2)
FutureWarning: The pynvml package is deprecated. Please install nvidia-ml-py
instead. …
```

The blast radius is what makes it worth documenting. `torch/cuda/__init__.py`
line 64 imports `pynvml` **at torch import time**, unconditionally — so the
warning does not fire when telemetry is sampled, it fires when anything imports
torch. Under `-W error` that is every test in the real-separator tier, failing
at import, with a message about a package installed for an optional feature.

`uv pip uninstall pynvml` removes only the shim and leaves the `nvidia-ml-py` it
dragged in, which then works:

```text
$ .venv/Scripts/python.exe -W error -c "…"
torch 2.13.0+cu130 True
name NVIDIA GeForce RTX 4060 Laptop GPU
util 0 temp 51
driver 610.47
```

…and under load, through `RoFormerSeparator.runtime_stats()` during a real
separation, `utilization: 1.0` at 62–66 °C. The suite is clean under `-W error`
with `nvidia-ml-py 13.610.43` installed.

Documented in DEVELOPMENT.md (a new *Optional: NVML* subsection with the
traceback and the recovery), in feature 026's document where NVML was listed as
never exercised, and in `NvmlProbe`'s own docstring — that last one because the
code reads `importlib.import_module("pynvml")`, which is precisely the sight
that sends someone to `pip install pynvml`.

**It is not added as a dependency**, per ARCHITECTURE.md §12 and this feature's
out-of-scope list. One consequence worth stating where the reader will meet it:
being outside the lock file means `uv sync` prunes it, along with the CUDA
wheel — which is why it appears in the `uv` table above rather than only here.

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
