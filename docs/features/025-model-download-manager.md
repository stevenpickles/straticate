# [025] Model download manager (SHA-256, atomic install)

Branch: `025-model-download-manager`
Status: PR OPEN
Dependencies: 010
PR: #30

## Objective

A catalogued model whose weights are not on disk can be installed: streamed to a
temporary file, **verified against the SHA-256 pinned in the catalog**, and
atomically renamed into place — and the API now reports which models are
actually installed, so a client can tell "offered" from "ready". This is the
last piece of infrastructure before real inference: feature 026's separator
becomes installable.

## Scope

- **Installation state on the model resource.** `ModelInstallState`
  (`available` · `downloading` · `installed` · `failed`) and `ModelInstallation`
  (state, `requires_download`, `total_bytes`, `downloaded_bytes`, `progress`,
  `error`) in `backend/src/straticate/schemas/models.py`, carried on `Model`.
  A model with no `artifact` block is `installed` by definition.
- **`ModelLicensing` on `Model`**, from the manifest's existing `licensing`
  block.
- **`backend/src/straticate/models/layout.py`** — one module owning where
  weights live: `weights_path(models_dir, model_id)` and friends, with the model
  ID validated against the manifest's own pattern so it can never escape
  `models_dir`.
- **`backend/src/straticate/models/installer.py`** — `ModelInstaller`: the
  temporary-artifact → SHA-256 → `fsync` → atomic-rename pipeline, the in-flight
  install registry, and the live installation state the routes serve. Every
  failure is recorded on the model (including unclassified ones), and no failure
  message or `detail` carries the download URL or the pinned digest.
- **`ModelArtifact` / `CatalogEntry`** in `models/catalog.py`: the catalog now
  *retains* the manifest's `artifact` block, off the public `Model`.
- **Endpoints** in `api/models.py`: `POST /models/{id}/install` (202, returns
  immediately), `DELETE /models/{id}/weights` (which cancels a running install),
  and installation state on the two existing `GET`s.
- Installer wired into `create_app()`, closed in the lifespan.
- `models/weights/` gitignored; `httpx2` promoted to a runtime dependency.
- `docs/contracts/rest-api.md` and the regenerated
  `frontend/src/api/generated/api.d.ts`.

## Out of scope

- PyTorch, any separator implementation, loading weights into a model (026).
- Adding a real model entry to `models/catalog.json` (026 owns choosing it).
- **Resumable / partial downloads**, mirrors, a remote catalog, and `update` as
  distinct from remove-then-install. A failed install starts over.
- Any model-management **UI**. There is no frontend for this feature beyond the
  regenerated types; a later numbered feature owns the screen.
- Hiding uninstalled tiers from `quality_options` — see *Notes / decisions*.

## Expected modules/files

- `backend/src/straticate/models/layout.py`, `installer.py`, `catalog.py`,
  `__init__.py`
- `backend/src/straticate/schemas/models.py`, `schemas/__init__.py`
- `backend/src/straticate/api/models.py`, `main.py`
- `backend/pyproject.toml`, `.gitignore`
- `backend/tests/test_model_installer.py`, `test_model_layout.py`,
  `weights_server.py`; updates to `test_model_catalog.py`, `test_models_api.py`
- `docs/contracts/rest-api.md`, `frontend/src/api/generated/api.d.ts`

## Acceptance criteria

- [x] A model with no `artifact` reports `installed`, `requires_download: false`,
      and cannot be installed or removed as though it had weights
      (`model_not_downloadable`, 409).
- [x] A successful install streams to a `.part` file, verifies SHA-256, and
      atomically renames into `Settings.models_dir`; the model then reports
      `installed`.
- [x] A hash mismatch installs nothing, deletes the artifact, and reports
      `checksum_mismatch` with the digest that actually arrived (never the
      pinned one, which belongs to the private `artifact` block).
- [x] A truncated download, a short body, an over-long body (declared *and*
      length-less), an HTTP error page and a connection failure each fail with
      the documented code and leave no artifact behind.
- [x] A cancelled install leaves no `.part` and no installed file — cancelled
      directly, via `DELETE .../weights`, or by application shutdown.
- [x] `POST .../install` returns immediately and never blocks the event loop
      (proved with a gated download: the 202 lands while the body is parked, and
      `/health`, `/models` and `/separation-modes` are all served meanwhile).
- [x] A second **install** for the same model while one is running is rejected
      with `model_busy`; the server records exactly one request. A **remove**
      cancels the running install instead of refusing.
- [x] Removing weights returns the model to `available`; install → remove →
      install again works and re-downloads.
- [x] Model IDs cannot escape `models_dir`: every path accessor rejects an
      unusable ID, an unusable ID in a request is a clean `model_not_found`, and
      a catalog carrying one fails at startup.
- [x] Progress is observable on `GET /models/{model_id}` while downloading.
- [x] `frontend/src/api/generated/api.d.ts` regenerated and committed.
- [x] `ruff format --check` · `ruff check` · `pyright` (strict) · `pytest`
      green with no new warnings (the suite passes under `-W error`); frontend
      `format:check` · `lint` · `typecheck` · `test` · `build` green.

## Required tests

- `test_model_layout.py`: valid and unusable model IDs; every path accessor
  refuses an unusable ID; weights stay inside `models_dir`; the `.part` is a
  sibling of its target; remove reports whether anything was there and clears an
  orphaned `.part`.
- Regression tests for the three review findings, each verified to fail against
  the pre-fix code: an unclassified exception reports `failed` rather than
  reverting to `available`; no failure message or `detail` leaks a presigned
  URL's query string, its `user:pass@`, or the pinned SHA-256; and the `.part`
  is `fsync`ed before `os.replace` (spied call order).
- `test_model_installer.py`: the full API surface against a **real loopback HTTP
  server** (`tests/weights_server.py`, bound to `127.0.0.1` on an ephemeral
  port) — happy path, progress, immediate return, event-loop freedom,
  concurrency (`model_busy`), remove/reinstall, retry after failure,
  cancellation, shutdown, licensing visibility, the artifact never appearing in
  a response, and one test per documented failure: `checksum_mismatch`,
  `http_status`, `connection_failed`, `size_exceeded` (declared and mid-stream),
  `size_mismatch`, plus a truncated transfer. Every failure test also asserts
  that neither the weights nor the `.part` survive.
- `test_model_catalog.py`: `licensing` surfaced, `artifact` retained but off the
  public model, `default_inference_parameters` still dropped.

**No test touches the network.** Catalogs are synthetic (`tmp_path` +
`Settings(models_dir=…)`), and every wait is gated by a `threading.Event` in the
serving thread or by the installer's own `wait()`; nothing sleeps for a
duration.

## Notes / decisions

### Progress: a REST field, not a WebSocket event

**Decision: progress lives on the model resource** (`installation.progress` /
`downloaded_bytes`), read from `GET /models/{model_id}`.

The alternative — a `model_install_progress` event on the existing hub — was
weighed honestly. The hub gives live updates for free and already fans out to
every connected client, which is genuinely the nicer shape for a long transfer.
Four things decided against it *for this feature*:

1. **It is a shared-contract change with no consumer.** An event type means
   `schemas/events.py` plus the frontend's typed `WebSocketEvent` union — and
   every frontend file other than the generated types is out of scope here, so
   the event would ship with nothing able to decode it. Guessing a payload for a
   UI nobody has designed is the worst moment to fix a contract.
2. **The state field is needed anyway.** Acceptance criterion one requires
   installation state on the model surface. Once `installation` exists, carrying
   two more numbers in it costs nothing; a parallel event would be a second
   source of truth for the same fact.
3. **ARCHITECTURE.md §11 already says REST is the source of truth for
   reconnect/refresh.** A client that reloads mid-install has to read the state
   from REST regardless.
4. **"No polling loops" (§3, AGENTS.md principle 3) is a rule about *job
   progress***: chunk-grained, ~4 Hz, generated by real inference work while a
   job runs, where an event stream already exists and a timer would be a lie. An
   install is rare, user-initiated, coarse (bytes of one file) and has no job
   record to attach to. A dialog refreshing a model resource while its own
   download runs is not the failure mode that rule exists to prevent.

Adding the event later is **purely additive**: the REST field stays the
reconnect source of truth either way, and the decision is much better made
alongside the UI that consumes it.

### Installation state on `Model`, not a sibling resource

Every place a model is presented is a place "offered vs. ready" matters, and
`GET /models` / `GET /models/{id}` are already the two routes a client reads
models from. A sibling resource would mean a second fetch per model to answer a
question about the model just fetched. The mutable part is confined to one
nested object (`installation`) so the rest of `Model` stays a pure projection of
the manifest.

The catalog itself is loaded **once**, at startup, so it is the wrong place to
answer a question whose answer changes while the process runs.
`ModelCatalog` therefore serves a *baseline* (`installed` for a model with no
artifact, `available` for one with) and `ModelInstaller.describe()` overlays the
live state; the routes serve `describe`, never the catalog's models directly.

### `licensing` is surfaced; `artifact` is not

Feature 010 hid both. 025 splits them, because they are different kinds of
thing. `licensing` is exactly what a user needs *before* deciding to install
weights — that is the only moment the terms can still change the decision — and
it names no implementation detail. `artifact` carries a download URL and a
checksum: operational inputs to the installer, not choices a user makes, and
ARCHITECTURE.md §1's "users choose modes and tiers" applies squarely. The
artifact's *size* does reach the client, as `installation.total_bytes`, because
"this will download 1.2 GB" is a decision input too.

### Weights layout

`{models_dir}/weights/{model_id}/weights.bin`, with the in-flight download at
`weights.bin.part` beside it.

- **A directory per model** so the `.part` is on the same filesystem as its
  target (which is what makes `os.replace` atomic rather than a copy) and so
  removing a model is one directory to delete.
- **An architecture-neutral file name.** A RoFormer checkpoint is a `.ckpt` and
  an ONNX export is a `.onnx`; nothing that loads weights cares about the
  suffix, and application code must not branch on an architecture
  (ARCHITECTURE.md §1). Deriving the suffix from the download URL would also
  make `weights_path(models_dir, model_id)` need the manifest, which is exactly
  the coupling `inference/layout.py` avoids for stems.
- `models/weights/` is gitignored; **weights are never committed**.

### Why the per-chunk write — and the final `fsync` — stay on the event loop

`asyncio.to_thread` cannot be cancelled. Moving the 1 MiB `write` + `hash.update`
into a worker thread would create a window in which the `finally` unlinks a
`.part` a live thread is still writing to — trading a sub-millisecond memcpy for
an orphaned file. Feature 022's export shields its worker threads for exactly
this reason; here the cheaper answer is not to open the window. The expensive
part (the network) is fully asynchronous, and there is a test proving other
requests are served while a download is in flight.

The **one** measurable stall is the `fsync` before the rename, and it is a
considered trade. It happens once, after the last chunk. Moving it into a thread
would put the `fsync` *and* the `os.replace` in an uncancellable worker racing
the `finally`'s unlink — turning a bounded once-per-install stall into a window
where a cancelled install can publish weights, or where the rename fails on a
`.part` that was just deleted. For a local single-user application running one
job at a time, the stall is the cheaper cost. If it stops being cheaper, the fix
is to shield the whole fsync-and-publish step as one unit — never to drop the
`fsync`.

### Durability: `fsync` before publishing

`os.replace` is atomic for the *directory entry*, not for the file's data.
Without a sync, a power loss shortly after a "successful" install can leave a
**published** `weights.bin` with a garbage tail — and since
`weights_installed()` is a bare `is_file()` and nothing ever re-hashes installed
weights, feature 026 would load that torn file silently, forever. Unlike
`api/export.py`'s artifacts, which are cheap to rebuild, these are not. So the
`.part` is flushed and `fsync`ed before the rename, and the containing directory
is `fsync`ed after it where the platform has a directory handle to sync (POSIX;
Windows exposes none and does not need one).

### Every failure is recorded, not only the classified ones

An install runs detached from the request that started it, so an exception the
manager does not classify has nowhere to surface: the task would die with its
exception never retrieved and the model would report `available` — the same
answer as "never tried". A user who clicked Install would watch the state flick
to `downloading` and back and be told nothing. `_run` therefore catches
`Exception` (not `BaseException`: `CancelledError` is control flow and must keep
propagating), records `download_failed` with `reason: unexpected_error`, and logs
the traceback.

### Nothing about the download URL reaches a client

`installation.error.message` is served by `GET /models` to every caller, and
large weights are routinely hosted behind presigned URLs whose **query string is
the credential**. Stripping only `user:pass@` would leak the signature. So no
part of the URL is returned at all: messages name the model, the URL goes to the
log.

The same rule decides what a failure `detail` may carry: **facts about what
happened**, never a field copied out of the private `artifact` block. So
`checksum_mismatch` returns `actual` (the digest that arrived) and not the
pinned `expected` — which is `artifact.sha256`, the thing `catalog.py` exists to
keep off the wire, and which a client cannot act on anyway. Both digests are
logged together. `expected_bytes` is the deliberate exception: `size_bytes` is
already published as `installation.total_bytes`, because the download size is a
decision input before installing.

### Removing weights cancels a running install

`DELETE /models/{id}/weights` used to answer `model_busy` while an install ran.
It now cancels it instead, which is both the honest reading of the request and
the only escape from a download that will not finish: `DEFAULT_TIMEOUT_SECONDS`
is a **per-operation** bound, not a total budget, so a host trickling one byte
per timeout window keeps the transfer nominally alive. With `start_install` also
answering `model_busy`, a restart was previously the only way out.

A dedicated cancel route was considered and rejected as a second way to say the
same thing: the outcome of cancelling an install is exactly "this model has no
weights", which is what `DELETE .../weights` already means. `model_busy` remains
for a second concurrent **install**.

### `download_failed` / `checksum_mismatch` are not HTTP statuses

`POST /install` returns before the download can fail, so there is no request
left to answer with them. They arrive in `installation.error`, following the
precedent this contract already sets for `audio_decode_timed_out` — a code that
"never appears as an HTTP status" and reaches the client in a job's `error`.
`model_not_found` (404), `model_not_downloadable` (409) and `model_busy` (409)
*are* HTTP statuses, because they are answers to the request itself.

### `quality_options` still offers uninstalled models

Feature 010 left open whether a mode should hide tiers whose weights are absent.
**Left alone deliberately.** Today every catalogued model is offered, which is
still correct while nothing uninstalled can be selected for a job. The question
becomes real in **026**, when a job can name a model whose weights are missing,
and it is better decided with that case in hand — along with whether the right
answer is hiding the tier, disabling it with an "Install" affordance, or letting
job creation fail with a clear code.

### `httpx2` is now a runtime dependency

The manager needs an async HTTP client that streams and cancels cleanly. The
repository already standardised on `httpx2` for tests (DEVELOPMENT.md), so it
was promoted from the dev group rather than adding a second client library or
falling back to `urllib` in an uncancellable worker thread.

## Interfaces for downstream features

### For 026 (real separator)

A separator asks the layout module where its weights are, and the installer
whether they are there:

```python
from straticate.models import weights_installed, weights_path

path = weights_path(settings.models_dir, model.id)   # …/weights/{id}/weights.bin
if not weights_installed(settings.models_dir, model.id):
    raise ApplicationError(
        "model_weights_missing",
        f"Model {model.id!r} is catalogued but its weights are not installed.",
        status_code=409,
        detail={"model_id": model.id},
    )
```

- `weights_path(models_dir, model_id) -> Path` — **pure**: computes a path,
  creates nothing, and raises `ValueError` for an ID that is not a valid model
  ID. The file exists only if an install verified and published it, so its
  presence is the guarantee that the bytes match the catalog's SHA-256.
- `weights_installed(models_dir, model_id) -> bool` — the cheap check to make
  before loading.
- A separator builder receives a `Model`, which carries
  `model.installation.state`; but **the disk is the authority**, and
  `weights_installed` reads it directly.

**What to raise when they are missing** is 026's call, and it is a *new* code —
do not reuse `model_not_found` (the model exists) or `separator_unavailable`
(the implementation exists). `model_weights_missing` (409) is the suggestion
above; whatever it is, document it in `docs/contracts/rest-api.md` under the job
error codes, since it will surface at job-creation or job-run time.

026 also owns:

- adding the real catalog entry, with `artifact` (`download_url`, `size_bytes`,
  `sha256`) and `licensing` filled in — the schema and the loader already accept
  both, and nothing else needs to change to make a model installable;
- deciding whether `quality_options` hides uninstalled tiers (see above).

### For a later model-management UI

`GET /models` is a complete view: id, display name, tier, licensing, and the
`installation` block with state, sizes and progress. `POST
/models/{id}/install` then `GET /models/{id}` on a refresh interval is the whole
interaction. If live updates become worth it, add a `model_install_progress`
event; the REST field remains the reconnect source of truth.

## Known limitations

- **No resumable downloads.** An install interrupted at 95% starts over. A
  `Range`-based resume needs the `.part` to survive across attempts, which is a
  different lifecycle from "the `.part` never survives a failure" and would want
  its own numbered feature.
- **No `update`.** Remove and install again. A real update needs versioned
  weights paths so the running model is not pulled out from under a job.
- **No retention or disk-space check.** Nothing warns before a download fills
  the disk, and nothing garbage-collects weights for models a later catalog no
  longer lists — the same gap `data/` already has (feature 021's note).
- **The network bound is per-operation, not a total budget.**
  `DEFAULT_TIMEOUT_SECONDS` (60 s) bounds connect, read and write individually,
  so a host trickling one byte per window keeps a transfer nominally alive for
  as long as it likes; there is no overall deadline and no minimum-throughput
  check. `DELETE /models/{id}/weights` cancels a running install, so this is a
  waste of a socket rather than a stuck model — but a stall-detection bound
  (bytes per interval, or a total deadline derived from `size_bytes`) is real
  future work.
- **Installed weights are never re-verified.** The SHA-256 is checked once, on
  the way in, and `weights_installed()` is thereafter a bare `is_file()`. A file
  corrupted *after* installation — bit rot, an external edit — is not detected.
  Re-hashing hundreds of megabytes on every job would be the wrong trade; a
  `verify` command, or a stored digest checked on load, is the shape a later
  feature would want. The install-time `fsync` closes the one window this
  feature can close on its own (a torn tail from a crash right after the
  rename).
- **Failure state is in memory.** Like job records, a recorded `failed` state
  does not survive a restart; the model simply reports `available` again.
- **No model-management UI.** Deliberate — see *Out of scope*.
