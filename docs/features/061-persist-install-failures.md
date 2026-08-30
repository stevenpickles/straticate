# [061] Persist model install failures

Branch: `061-persist-install-failures`
Status: PR OPEN
Dependencies: —
PR: #89

## Objective

A failed model install's error survives a backend restart. Before this
feature `ModelInstaller` kept a failed install's `ErrorInfo` only in
`self._failures`, a plain in-memory dict (feature 037 §3 recorded this as a
known gap): restarting the backend after a failed install made `GET
/models/{model_id}` report the model bare `available` again, the error gone,
indistinguishable from a model that was never attempted. After this feature,
`/models` reports `failed` with the same error after a restart, cleared the
moment the next install attempt starts or the next install succeeds — exactly
as it already was cleared before a restart.

## Scope

- `backend/src/straticate/models/layout.py` — `INSTALL_FAILURE_FILENAME` and
  `install_failure_path(models_dir, model_id)`, a sibling of `weights_path`
  inside the same per-model directory (`{models_dir}/weights/{model_id}/`), so
  `remove_weights`'s directory delete already clears it with no second code
  path.
- `backend/src/straticate/models/installer.py`:
  - `_write_failure_sidecar` / `_read_failure_sidecar` /
    `_delete_failure_sidecar` — the sidecar's atomic write (`.tmp` →
    `os.replace`, same directory, **no `fsync`** — documented trade, see
    Notes), a tolerant read that treats a missing or corrupt file as absent,
    and a tolerant delete.
  - `ModelInstaller.__init__` now calls `_restore_failures()`, which walks
    every downloadable catalog entry once at construction: weights already on
    disk beats a stale sidecar (deleted, nothing restored — a later attempt
    must have succeeded after a crash lost the sidecar's own delete);
    otherwise a sidecar that parses becomes this process's in-memory failure.
  - `_run` now writes the sidecar in the same two places it already writes
    `self._failures[model_id]` (the classified-`ModelInstallError` branch and
    the unclassified-`Exception` branch).
  - `start_install` now deletes the sidecar synchronously, in the same
    statement that already does `self._failures.pop(model_id, None)`, before
    the download task is created.
  - `remove` needed no new sidecar-delete call: `remove_weights` already
    deletes the model's whole directory (sidecar included) whenever it
    exists, covering both "weights existed" and "only a failure sidecar
    existed" — the existing `self._failures.pop(...)` call was already
    correct.
- `backend/src/straticate/models/__init__.py` — re-exports
  `INSTALL_FAILURE_FILENAME` and `install_failure_path`.
- `backend/tests/test_model_installer.py` — new tests (below) plus a
  `Builder.restart(harness)` helper that boots a fresh `FastAPI` app over an
  existing harness's `models_dir`, simulating a backend restart with nothing
  shared in process memory.

## Out of scope

- Everything else in the models lifecycle: resumable downloads, re-verifying
  installed weights, update-in-place — all v0.4.0 work with a different
  lifecycle (`docs/features/025-model-download-manager.md`). This feature
  never touches a `.part` file or `weights.bin` itself.
- `audio/storage.py` (056) and `jobs/*` (057) durability — parallel features,
  untouched.
- Frontend — the `/models` response shape is unchanged; no schema or
  contract change, so nothing to regenerate.

## Expected modules/files

- `backend/src/straticate/models/layout.py`
- `backend/src/straticate/models/installer.py`
- `backend/src/straticate/models/__init__.py`
- `backend/tests/test_model_installer.py`
- `docs/features/061-persist-install-failures.md`, `ROADMAP.md`

## Acceptance criteria

- [x] A failed install's `installation.state` and `installation.error` are
      identical before and after a simulated restart (fresh `ModelInstaller`
      over the same `models_dir`).
- [x] A successful install (including a retry that succeeds after a prior
      failure) leaves no sidecar file on disk.
- [x] A new install attempt deletes the sidecar synchronously, before the
      download itself begins.
- [x] Weights installed + a leftover sidecar (simulating a crash between
      publishing weights and clearing a prior failure) → the sidecar is
      removed at boot and the model reports `installed`, no error.
- [x] A sidecar that fails to parse (truncated/hand-edited) → treated as
      absent, `available`, and a warning is logged; startup does not raise.
- [x] No schema change; `/models`'s `installation.error` shape is unchanged.
- [x] Full backend suite green, quartet green.

## Required tests

All in `backend/tests/test_model_installer.py`, new section "surviving a
restart (feature 061)":

- `test_a_failed_install_survives_a_restart` — checksum-mismatch failure in
  one app, then `build.restart(harness)`; asserts the second app's
  `installation` is `failed` with the identical `error` object.
- `test_a_successful_install_clears_the_persisted_failure` — a 503-then-200
  retry (mirroring the existing `test_a_retry_after_a_failure_clears_the_error`
  in-memory test); asserts the sidecar file exists after the failure and is
  gone after the successful retry, and that a restart afterwards still reports
  `installed`.
- `test_a_new_attempt_clears_the_sidecar_before_downloading` — after a
  failure, starts a second attempt and asserts the sidecar is already gone
  the instant `POST .../install` returns `202`, before the (still-failing)
  retry's download runs.
- `test_weights_present_with_a_stale_sidecar_is_cleaned_up_on_restart` — a
  successful install, then a sidecar file injected directly (the state a real
  crash-between-publish-and-clear would leave); restart asserts `installed`,
  no error, and the sidecar removed.
- `test_a_corrupt_sidecar_boots_clean_with_a_warning` — an unparseable sidecar
  written directly; restart asserts `available`, no error, and a `caplog`
  warning containing "corrupt".

### Proved to fail first

Before restoring the fix, `src/straticate/models/installer.py`,
`layout.py`, and `__init__.py` were stashed back to their pre-061 state
(`git stash push -- <those three files>`, keeping the new tests). The new
tests then failed to even collect (`ImportError: cannot import name
'install_failure_path'`), which itself proves the sidecar mechanism did not
exist — but to see the actual pre-fix *behavior* rather than a collection
error, a standalone script drove the pre-061 `ModelInstaller` through the
identical two-app "restart" shape without importing anything new:

```text
install response: 202
BEFORE restart: failed {'code': 'download_failed', 'message': "Installing
model 'vocals-hq-001' failed unexpectedly; see the server log.", 'detail':
{'model_id': 'vocals-hq-001', 'reason': 'unexpected_error'}}
AFTER restart: available None
```

This confirms the exact bug 061 fixes: `failed` → `available`, error gone,
across a restart. `git stash pop` restored the fix; the full suite (below)
then passed, including all five new tests.

## Notes / decisions

1. **No `fsync` on the sidecar write, unlike the weights artifact itself.**
   `installer.py`'s existing pipeline `fsync`s the `.part` before renaming
   `weights.bin` into place because a torn weights file is loaded silently
   and *permanently* — nothing ever re-hashes installed weights. A lost
   sidecar write is a different order of harm: it only reverts one model, for
   one boot, to the pre-061 behavior of reporting `available` instead of
   `failed` after a crash in the narrow window between the write and the next
   `fsync`-backed disk flush. Paying a synchronous `fsync` on the event loop
   for every failed install to close that narrow a window was judged not
   worth it; the module docstring's new "A failure survives a restart"
   section documents this trade explicitly, per the assignment.
2. **Corrupt sidecar is not deleted at load time, only lazily.** Deleting a
   sidecar while `_restore_failures()` is walking the catalog at construction
   would turn a read into a mutation during startup, for no operational
   benefit — the same lazy-delete-on-next-write path every other clear
   (`start_install`, success, `remove`) already goes through cleans it up the
   next time the model is touched. Documented on `_read_failure_sidecar`.
3. **`remove()` needed no additional sidecar-delete call.** `remove_weights`
   (`layout.py`) already `shutil.rmtree`s the model's whole directory
   whenever it exists — sidecar included — regardless of whether
   `weights.bin` itself was present. The existing `self._failures.pop(...)`
   line in `remove()` was therefore already sufficient once the sidecar lives
   inside that same directory; no redundant delete call was added.
4. **Sidecar path chosen over the assignment's literal
   `{models_dir}/{model_id}/install-failure.json`.** The real on-disk layout
   (`layout.py`, predating this feature) keeps a model's weights at
   `{models_dir}/weights/{model_id}/weights.bin`, not
   `{models_dir}/{model_id}/...` — there is no per-model directory directly
   under `models_dir`. `install_failure_path` follows the existing
   `model_weights_dir` helper instead, landing the sidecar at
   `{models_dir}/weights/{model_id}/install-failure.json`, so it shares a
   directory (and therefore a filesystem, and `remove_weights`'s single
   delete) with `weights.bin` and its `.part`. This is a literal-text
   deviation from the assignment in service of its own stated design
   principle — "same directory" atomic rename and one delete path — applied
   to the actual layout module rather than an approximate path in the
   assignment prose.

## Known limitations

- **An install *interrupted* by a restart still reports `available`.** A
  process killed mid-download writes no failure sidecar (nothing failed — it
  was interrupted), leaves its `.part` behind, and the model is offered again
  on the next boot. Same symptom as the bug this feature fixes, different
  cause, pre-existing, and untouched here: interrupted-download recovery
  belongs to the resumable-downloads lifecycle deferred to v0.4.0.
- **`test_a_new_attempt_clears_the_sidecar_before_downloading` has a narrow
  theoretical flake** (review finding): the cleared-before-download assertion
  holds because the retry's loopback download takes more loop turns than the
  ASGI round-trip. It cannot false-pass, only rarely false-fail; pinning the
  retry open with a blocked artifact would make it unconditional if it ever
  flakes in CI.
- The sidecar is the only thing this feature writes under `models_dir`;
  resumable downloads, re-verification, and update-in-place remain
  deliberately out of scope for v0.4.0, as before.
