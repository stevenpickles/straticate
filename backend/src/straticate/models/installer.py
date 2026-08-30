"""Downloading, verifying and installing model weights.

The pipeline ARCHITECTURE.md §9 specifies, and nothing more::

    temporary artifact  →  SHA-256 verification  →  atomic rename

Every sentence of that is load-bearing:

- **Temporary artifact.** The body is streamed to
  :func:`~straticate.models.layout.partial_weights_path`, a ``.part`` sibling of
  the target, in bounded chunks. A checkpoint is hundreds of megabytes; holding
  one in memory would be a resident-set spike the size of the model, on a
  machine that is about to need that memory for inference.
- **Verification before publication.** The SHA-256 pinned in the manifest is
  computed as the bytes arrive and checked *before* the rename. **An incomplete
  or hash-mismatched artifact is never installed and never loadable** — that
  sentence is ARCHITECTURE.md's and it is the whole point of this module. A
  July 2026 audit found third-party checkpoint repositories being renamed and
  taken down without notice, so a 404 page or a substituted file served in place
  of weights has to fail loudly rather than install something plausible-looking.
- **Atomic rename.** :func:`os.replace` publishes the verified file in one
  step, on the same filesystem, so no reader can ever observe a half-written
  weights file. The same discipline :mod:`straticate.api.export` and
  :class:`~straticate.inference.FakeSeparator` already use.

Three further rules this module keeps:

- **The manifest's ``size_bytes`` is a ceiling, not a hint.** A declared
  ``Content-Length`` larger than it fails before a single body byte is read, and
  the running total is checked per chunk so a chunked or length-less response
  cannot stream an unbounded body into the data directory because a server said
  so. A short response fails too: the byte count must match exactly.
- **Durability before publication.** ``os.replace`` is atomic for the *directory
  entry*, not for the file's data: without an ``fsync`` a power loss shortly
  after a "successful" install can leave a **published** ``weights.bin`` with a
  garbage tail. Nothing ever re-hashes installed weights —
  :func:`~straticate.models.layout.weights_installed` is a bare ``is_file()`` —
  so feature 026 would load that torn file silently, forever. The ``.part`` is
  therefore flushed and ``fsync``-ed before the rename, and the containing
  directory is ``fsync``-ed after it where the platform has a directory handle
  to sync. Unlike :mod:`straticate.api.export`, whose artifacts are cheap to
  rebuild, these are not.
- **The ``.part`` never survives.** A ``finally`` unlinks it on every exit —
  success (where the rename already consumed it), failure, and cancellation.
- **Nothing meaningful blocks the event loop.** The download is fully
  asynchronous, and the per-chunk work left on the loop is one buffered
  ``write`` and one ``hashlib.update`` of at most :data:`DEFAULT_CHUNK_BYTES`.
  That is deliberate rather than lazy: :func:`asyncio.to_thread` cannot be
  cancelled, so moving the write off the loop would create a window in which the
  ``finally`` unlinks a ``.part`` that a live worker thread is still writing to
  — trading a sub-millisecond memcpy for an orphaned file. Feature 022's export
  shields its worker threads for exactly this reason; here the cheaper answer is
  not to create the window.

  The **one** measurable stall is the final ``fsync``, which runs on the loop
  too, and that is a considered trade rather than an oversight. It happens once,
  after the last chunk, and its cost is whatever the OS has left to flush.
  Moving it into ``asyncio.to_thread`` would put the ``fsync`` *and* the rename
  in an uncancellable thread racing the ``finally``'s unlink — turning a bounded
  once-per-install stall into a window where a cancelled install can publish
  weights, or where the rename fails on a ``.part`` that was just deleted. For a
  local single-user application running one job at a time, the stall is the
  cheaper cost. If it ever stops being cheaper, the fix is to shield the whole
  fsync-and-publish step as one unit — never to drop the ``fsync``.

- **A failure message names the model, never the host.** ``download_url`` is
  private to this module (``models/catalog.py`` keeps it off
  :class:`~straticate.schemas.Model` for exactly this reason), and a failure
  message is a thing users paste into issues. Large weights are routinely hosted
  behind presigned URLs whose query string *is* the credential, so no part of
  the URL reaches a client: it is logged instead. For the same reason the
  ``checksum_mismatch`` detail carries the digest that was **received** — a fact
  about what happened — and not the one the catalog pins.

Resumable downloads are **out of scope** (see
``docs/features/025-model-download-manager.md``): a failed install starts over.

**A failure survives a restart (feature 061).** ``self._failures`` alone does
not: it is process memory, and a backend restart used to hand back a bare
``available`` for a model that had, in fact, just failed — the error gone,
with nothing to tell a user why the install they watched fail is now silently
being offered again. Every write to ``self._failures`` is now mirrored to a
sidecar, :func:`~straticate.models.layout.install_failure_path`, right beside
``weights.bin``:

- **Recorded** (:meth:`ModelInstaller._run`) → the sidecar is written
  atomically — a ``.tmp`` sibling then :func:`os.replace`, the same
  publish-by-rename shape the weights use (their temporary is the ``.part``;
  this one is deliberately not, so nothing ever mistakes it for a partial
  download) — minus the ``fsync``. That omission is deliberate: unlike
  weights, a lost sidecar write to a very recent power cut is not silently
  wrong forever, it is merely *forgotten* — the next boot reports ``available``
  instead of ``failed``, which is the pre-061 behaviour, not data corruption.
  Paying a synchronous ``fsync`` on the event loop for every failed install to
  protect against that narrow a window was judged not worth it.
- **Cleared** → two unlink paths: :meth:`ModelInstaller.start_install`
  removes it before a new attempt, and :meth:`ModelInstaller.remove` takes it
  with the model directory's ``rmtree``. Success needs no clear of its own —
  ``start_install`` already ran first, so the sidecar is gone before a
  download that could succeed ever begins.
- **Loaded** (:meth:`ModelInstaller.__init__`) → for every catalogued model,
  the sidecar is read once at construction. Weights already installed beats a
  stale sidecar (a later attempt must have succeeded after a crash lost the
  delete); otherwise a valid sidecar restores the in-memory failure so
  :meth:`describe` reports ``failed`` exactly as it did before the restart. A
  sidecar that fails to parse is logged and treated as absent — it is not
  deleted here, only lazily, the next time an install for that model is
  attempted or succeeds, which is the same path every other clear already
  goes through.

This sidecar is the **only** thing this feature touches under ``models_dir``:
never a ``.part`` file, never ``weights.bin`` itself. Resumable downloads and
re-verifying installed weights remain out of scope, as above.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Self

import httpx2
from pydantic import ValidationError

from straticate.errors import ApplicationError
from straticate.models.catalog import CatalogEntry, ModelArtifact, ModelCatalog
from straticate.models.layout import (
    install_failure_path,
    partial_weights_path,
    remove_weights,
    weights_installed,
    weights_path,
)
from straticate.schemas import ErrorInfo, Model, ModelInstallation, ModelInstallState

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_BYTES = 1024 * 1024
"""Bytes read from the response per iteration (1 MiB)."""

DEFAULT_TIMEOUT_SECONDS = 60.0
"""Connect/read/write bound for the download.

A *per-operation* bound, not a total one: a legitimate multi-hundred-megabyte
download takes minutes, but a minute of silence from the server means the
transfer is dead, and a wedged socket must not park an install forever.
"""

DOWNLOAD_FAILED = "download_failed"
"""Install failure: the artifact could not be fetched in full."""

CHECKSUM_MISMATCH = "checksum_mismatch"
"""Install failure: the artifact's SHA-256 is not the one the manifest pins."""

HTTP_STATUS = "http_status"
"""``detail.reason``: the server answered, but not with ``200``."""

CONNECTION_FAILED = "connection_failed"
"""``detail.reason``: the transfer never completed (refused, reset, timed out)."""

SIZE_EXCEEDED = "size_exceeded"
"""``detail.reason``: the body is larger than the manifest's ``size_bytes``."""

SIZE_MISMATCH = "size_mismatch"
"""``detail.reason``: the body ended short of the manifest's ``size_bytes``."""

FILESYSTEM_ERROR = "filesystem_error"
"""``detail.reason``: the artifact could not be written or published."""

UNEXPECTED_ERROR = "unexpected_error"
"""``detail.reason``: the install raised something this module did not classify.

An install runs detached from the request that started it, so an unhandled
exception has nowhere to surface: the task would die with its exception never
retrieved and the model would revert to ``available`` — indistinguishable from
"never tried". A user who clicked Install would watch the state flick to
``downloading`` and back and be told nothing. Whatever it was, it is reported as
a failure and the traceback goes to the log.
"""


class ModelInstallError(Exception):
    """An install attempt failed, with the code the client is told.

    ``code`` is one of :data:`DOWNLOAD_FAILED` or :data:`CHECKSUM_MISMATCH`;
    ``detail`` carries a short **classification** (``reason``) and the sizes or
    digests involved. It never carries an OS error string or a server path —
    the same discipline :mod:`straticate.api.export` applies, for the same
    reason: those name absolute paths on the machine running the server.
    """

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail: dict[str, Any] = detail or {}

    def to_error_info(self) -> ErrorInfo:
        """This failure as the contract :class:`~straticate.schemas.ErrorInfo`."""
        return ErrorInfo(code=self.code, message=self.message, detail=self.detail)


@dataclass(slots=True)
class _RunningInstall:
    """Bookkeeping for one in-flight install.

    ``downloaded_bytes`` is mutated by the download loop and read by
    :meth:`ModelInstaller.describe`; both run on the same event loop, so no lock
    is needed and no reader can see a torn value.
    """

    total_bytes: int
    downloaded_bytes: int = 0
    task: asyncio.Task[None] | None = field(default=None)


def _model_busy(model_id: str) -> ApplicationError:
    """Build the 409 for a model whose install is already running."""
    return ApplicationError(
        "model_busy",
        f"An install is already running for model {model_id!r}.",
        status_code=409,
        detail={"model_id": model_id},
    )


def _model_not_downloadable(model_id: str) -> ApplicationError:
    """Build the 409 for a model that has no weights to manage.

    Built-in separators — every ``fake`` model today — declare no ``artifact``.
    They are installed by definition, so installing or removing their weights is
    not a request that can be satisfied by anything, and answering ``204`` would
    tell the client a lie about what just happened.
    """
    return ApplicationError(
        "model_not_downloadable",
        f"Model {model_id!r} has no downloadable weights; it is built in and always installed.",
        status_code=409,
        detail={"model_id": model_id},
    )


class ModelInstaller:
    """Owns installed weights on disk and the installs currently running.

    One instance per application (``app.state.model_installer``), built in
    :func:`straticate.main.create_app` beside the catalog it reads.

    Args:
        catalog: The application's catalog; the source of every artifact URL,
            size and digest.
        models_dir: ``Settings.models_dir`` — weights are installed beneath its
            ``weights/`` subdirectory (:mod:`straticate.models.layout`).
        chunk_bytes: Response chunk size; see :data:`DEFAULT_CHUNK_BYTES`.
        timeout_seconds: Per-operation network bound; see
            :data:`DEFAULT_TIMEOUT_SECONDS`.
        client_factory: Builds the HTTP client one install uses. Injectable so
            tests can point every download at a loopback server, and so a future
            feature can add proxy or certificate configuration in one place.
    """

    def __init__(
        self,
        catalog: ModelCatalog,
        models_dir: Path,
        *,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client_factory: Callable[[], httpx2.AsyncClient] | None = None,
    ) -> None:
        self._catalog = catalog
        self._models_dir = models_dir
        self._chunk_bytes = chunk_bytes
        self._timeout_seconds = timeout_seconds
        self._client_factory = client_factory or self._default_client
        self._running: dict[str, _RunningInstall] = {}
        self._failures: dict[str, ErrorInfo] = {}
        self._restore_failures()

    def _restore_failures(self) -> None:
        """Load persisted install failures at construction (feature 061).

        For every downloadable catalog entry: weights already on disk beat a
        sidecar (a later attempt must have succeeded after a crash lost the
        clear), so a stale one is removed and nothing is restored. Otherwise a
        sidecar that parses becomes this process's in-memory failure, exactly
        as if the process had never restarted. A sidecar that does not parse
        is logged and left as absent *and on disk* — see
        :func:`_read_failure_sidecar`.
        """
        for entry in self._catalog.list_entries():
            if entry.artifact is None:
                continue
            model_id = entry.model.id
            if weights_installed(self._models_dir, model_id):
                _delete_failure_sidecar(self._models_dir, model_id)
                continue
            failure = _read_failure_sidecar(self._models_dir, model_id)
            if failure is not None:
                self._failures[model_id] = failure

    def _default_client(self) -> httpx2.AsyncClient:
        """Build the client a download runs on.

        Redirects are followed: release assets on the hosts a catalog would
        realistically name are almost always a redirect to object storage, and
        refusing them would make the manager useless while protecting nothing —
        the pinned SHA-256, not the URL, is what decides whether the bytes are
        the right bytes.
        """
        return httpx2.AsyncClient(timeout=self._timeout_seconds, follow_redirects=True)

    # -- reporting ---------------------------------------------------------

    def describe(self, entry: CatalogEntry) -> Model:
        """Return ``entry``'s model with its **live** installation state.

        This is what the model routes serve. The catalog's own baseline is
        loaded once at startup and cannot know that an install finished ten
        minutes later, so every response goes through here.
        """
        model = entry.model
        artifact = entry.artifact
        if artifact is None:
            return model
        return model.model_copy(update={"installation": self._installation(model.id, artifact)})

    def describe_all(self) -> list[Model]:
        """Every catalogued model, live installation state included."""
        return [self.describe(entry) for entry in self._catalog.list_entries()]

    def _installation(self, model_id: str, artifact: ModelArtifact) -> ModelInstallation:
        """Build the installation block for a model that has weights to manage."""
        running = self._running.get(model_id)
        if running is not None:
            received = min(running.downloaded_bytes, running.total_bytes)
            return ModelInstallation(
                state=ModelInstallState.DOWNLOADING,
                requires_download=True,
                total_bytes=running.total_bytes,
                downloaded_bytes=received,
                progress=received / running.total_bytes if running.total_bytes else 0.0,
            )
        if weights_installed(self._models_dir, model_id):
            return ModelInstallation(
                state=ModelInstallState.INSTALLED,
                requires_download=True,
                total_bytes=artifact.size_bytes,
            )
        failure = self._failures.get(model_id)
        if failure is not None:
            return ModelInstallation(
                state=ModelInstallState.FAILED,
                requires_download=True,
                total_bytes=artifact.size_bytes,
                error=failure,
            )
        return ModelInstallation(
            state=ModelInstallState.AVAILABLE,
            requires_download=True,
            total_bytes=artifact.size_bytes,
        )

    # -- commands ----------------------------------------------------------

    def start_install(self, model_id: str) -> Model:
        """Start downloading ``model_id``'s weights and **return immediately**.

        The download runs as a background task; the caller gets the model back
        with state ``downloading`` and watches
        ``installation.downloaded_bytes`` / ``installation.progress`` on
        ``GET /models/{model_id}``. Holding the HTTP request open for a
        multi-hundred-megabyte transfer is the thing AGENTS.md principle 4 and
        the REST contract's ``202``-style semantics forbid.

        Installing a model whose weights are already present is an idempotent
        no-op: it returns ``installed`` without re-downloading. Updating a model
        in place is out of scope — to force a re-download, remove the weights
        first.

        Raises:
            ApplicationError: ``model_not_found`` (404) for an unknown ID,
                ``model_not_downloadable`` (409) for a built-in model,
                ``model_busy`` (409) when an install is already running.
        """
        entry = self._catalog.get_entry(model_id)
        artifact = entry.artifact
        if artifact is None:
            raise _model_not_downloadable(model_id)
        if model_id in self._running:
            raise _model_busy(model_id)
        if weights_installed(self._models_dir, model_id):
            return self.describe(entry)

        self._failures.pop(model_id, None)
        _delete_failure_sidecar(self._models_dir, model_id)
        # Registration is a plain dict write with no ``await`` in front of it,
        # so on a single-threaded event loop two requests cannot both decide
        # they are the first one.
        running = _RunningInstall(total_bytes=artifact.size_bytes)
        self._running[model_id] = running
        running.task = asyncio.create_task(
            self._run(model_id, artifact, running), name=f"install-{model_id}"
        )
        return self.describe(entry)

    async def remove(self, model_id: str) -> Model:
        """Delete ``model_id``'s weights, returning it to ``available``.

        **A running install is cancelled first.** "I do not want this model's
        weights" includes the ones being fetched, and this is also the only
        escape hatch from a download that will not finish: the network bound is
        per-operation, not a total budget, so a host trickling one byte per
        timeout window could otherwise hold a model in ``downloading`` until the
        process was restarted. The cancelled download unlinks its own ``.part``
        before this returns, so nothing is left behind and no rename can land
        after the removal.

        Idempotent: removing weights that are not there succeeds and reports
        ``available``, because that is already the state the caller asked for. A
        recorded failure is cleared too — the model is back to "never
        installed", which is the truth once nothing is on disk.

        Raises:
            ApplicationError: ``model_not_found`` (404) for an unknown ID,
                ``model_not_downloadable`` (409) for a built-in model.
        """
        entry = self._catalog.get_entry(model_id)
        if entry.artifact is None:
            raise _model_not_downloadable(model_id)
        await self._cancel(model_id)
        self._failures.pop(model_id, None)
        # ``remove_weights`` deletes the whole model directory when it exists,
        # sidecar included, so there is no separate sidecar delete here.
        remove_weights(self._models_dir, model_id)
        return self.describe(entry)

    async def _cancel(self, model_id: str) -> None:
        """Cancel ``model_id``'s running install and wait for it to unwind.

        Waiting is the point: the task's ``finally`` is what unlinks the
        ``.part``, so returning before it has run would leave the caller free to
        delete a directory a dying task is still using.
        """
        running = self._running.get(model_id)
        if running is None or running.task is None:
            return
        running.task.cancel()
        await asyncio.gather(running.task, return_exceptions=True)
        self._running.pop(model_id, None)

    async def wait(self, model_id: str) -> None:
        """Wait for ``model_id``'s running install to settle (no-op if none).

        The install itself never raises here: a failure is recorded on the model
        and read back through :meth:`describe`, exactly as a client would.
        """
        running = self._running.get(model_id)
        if running is None or running.task is None:
            return
        await asyncio.shield(asyncio.gather(running.task, return_exceptions=True))

    async def aclose(self) -> None:
        """Cancel every running install and wait for the tasks to unwind.

        Called from the application lifespan. Each cancelled download unlinks
        its own ``.part`` on the way out, so a shutdown mid-install leaves
        nothing behind.
        """
        tasks = [running.task for running in self._running.values() if running.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()

    # -- the pipeline ------------------------------------------------------

    async def _run(self, model_id: str, artifact: ModelArtifact, running: _RunningInstall) -> None:
        """Run one install, recording its outcome on the installer.

        A failure is *data*, not an exception: the request that started the
        install returned long ago, so the only place to report it is the model
        resource. Cancellation is re-raised so ``aclose`` sees a genuinely
        cancelled task.

        **Every** exception is recorded, not only :class:`ModelInstallError`.
        Letting an unclassified one escape would leave the task dying with an
        unretrieved exception and the model reporting ``available`` — the same
        answer as "never tried", which is the one thing a user who just clicked
        Install must not be told. ``Exception`` and not ``BaseException``:
        :class:`asyncio.CancelledError` is a control-flow signal and must keep
        propagating.
        """
        try:
            await self._install(model_id, artifact, running)
        except ModelInstallError as exc:
            logger.warning(
                "Installing model %r failed (%s/%s): %s",
                model_id,
                exc.code,
                exc.detail.get("reason", "-"),
                exc.message,
            )
            failure = exc.to_error_info()
            self._failures[model_id] = failure
            _write_failure_sidecar(self._models_dir, model_id, failure)
        except Exception:
            logger.exception("Installing model %r raised an unclassified error", model_id)
            failure = ModelInstallError(
                DOWNLOAD_FAILED,
                f"Installing model {model_id!r} failed unexpectedly; see the server log.",
                {"model_id": model_id, "reason": UNEXPECTED_ERROR},
            ).to_error_info()
            self._failures[model_id] = failure
            _write_failure_sidecar(self._models_dir, model_id, failure)
        finally:
            # Dropped last, and never before the failure is recorded: a client
            # polling between the two would otherwise see ``available`` for an
            # install that had in fact just failed.
            self._running.pop(model_id, None)

    async def _install(
        self, model_id: str, artifact: ModelArtifact, running: _RunningInstall
    ) -> None:
        """Download, verify and publish one model's weights.

        Raises:
            ModelInstallError: Any step failed. Nothing is left on disk.
        """
        target = weights_path(self._models_dir, model_id)
        part = partial_weights_path(self._models_dir, model_id)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = await self._download(model_id, artifact, part, running)
            if digest != artifact.sha256:
                # The pinned digest is logged, not returned: it belongs to the
                # private ``artifact`` block. ``actual`` is a fact about the
                # bytes that arrived, so it travels with the failure.
                logger.warning(
                    "Model %r artifact hashed to %s but the catalog pins %s (from %s)",
                    model_id,
                    digest,
                    artifact.sha256,
                    artifact.download_url,
                )
                raise ModelInstallError(
                    CHECKSUM_MISMATCH,
                    f"The downloaded artifact for model {model_id!r} does not match the "
                    "SHA-256 pinned in the catalog; it was discarded.",
                    {"model_id": model_id, "actual": digest},
                )
            os.replace(part, target)
            _sync_directory(target.parent)
        except OSError as exc:
            logger.exception("Could not write the weights artifact for model %r", model_id)
            raise ModelInstallError(
                DOWNLOAD_FAILED,
                f"The weights artifact for model {model_id!r} could not be written to disk.",
                {"model_id": model_id, "reason": FILESYSTEM_ERROR},
            ) from exc
        finally:
            _discard(part)

    async def _download(
        self, model_id: str, artifact: ModelArtifact, part: Path, running: _RunningInstall
    ) -> str:
        """Stream the artifact into ``part``; return its SHA-256 hex digest.

        The response is never read whole. ``size_bytes`` bounds it twice — once
        against a declared ``Content-Length`` before any body is read, and once
        per chunk against the running total, which is what covers a chunked or
        length-less response.

        The file is flushed and ``fsync``-ed before it is closed, so the caller's
        rename publishes bytes that are actually on stable storage.

        **No message here names the download URL.** Weights are routinely served
        from presigned URLs whose query string is the credential, and these
        messages reach every API client through ``installation.error``. The URL
        goes to the log, where it belongs.

        Raises:
            ModelInstallError: ``download_failed`` for a non-200 status, a
                transport failure, or a body of the wrong length.
        """
        expected = artifact.size_bytes
        digest = hashlib.sha256()
        received = 0
        try:
            async with (
                self._client_factory() as client,
                client.stream("GET", artifact.download_url) as response,
            ):
                if response.status_code != 200:
                    logger.warning(
                        "Weights host answered %d for model %r at %s",
                        response.status_code,
                        model_id,
                        artifact.download_url,
                    )
                    raise ModelInstallError(
                        DOWNLOAD_FAILED,
                        f"The weights host answered {response.status_code} for model {model_id!r}.",
                        {
                            "model_id": model_id,
                            "reason": HTTP_STATUS,
                            "status_code": response.status_code,
                        },
                    )
                _reject_declared_overrun(model_id, response, expected)
                with part.open("wb") as sink:
                    async for chunk in response.aiter_bytes(self._chunk_bytes):
                        received += len(chunk)
                        if received > expected:
                            raise ModelInstallError(
                                DOWNLOAD_FAILED,
                                "The weights host is serving more data than the catalog "
                                f"declares for model {model_id!r} ({expected} bytes); the "
                                "download was stopped.",
                                {
                                    "model_id": model_id,
                                    "reason": SIZE_EXCEEDED,
                                    "expected_bytes": expected,
                                },
                            )
                        digest.update(chunk)
                        sink.write(chunk)
                        running.downloaded_bytes = received
                    _sync_to_disk(sink)
        except httpx2.HTTPError as exc:
            logger.warning(
                "Could not fetch the weights artifact for model %r from %s: %r",
                model_id,
                artifact.download_url,
                exc,
            )
            raise ModelInstallError(
                DOWNLOAD_FAILED,
                f"The weights artifact for model {model_id!r} could not be fetched from its host.",
                {"model_id": model_id, "reason": CONNECTION_FAILED},
            ) from exc
        if received != expected:
            raise ModelInstallError(
                DOWNLOAD_FAILED,
                f"The weights host sent {received} bytes for model {model_id!r} but the "
                f"catalog declares {expected}; the download was incomplete.",
                {
                    "model_id": model_id,
                    "reason": SIZE_MISMATCH,
                    "expected_bytes": expected,
                    "received_bytes": received,
                },
            )
        return digest.hexdigest()

    async def __aenter__(self) -> Self:
        """Support ``async with`` in tests and scripts."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Cancel any running install on the way out."""
        await self.aclose()


def _reject_declared_overrun(model_id: str, response: httpx2.Response, expected: int) -> None:
    """Fail before reading a body the server has already said is too big.

    Cheap and worth doing: a mirror serving a 20 GB file where the catalog
    declares 400 MB should cost one request, not 400 MB of disk.

    Raises:
        ModelInstallError: ``download_failed`` / ``size_exceeded``.
    """
    declared = response.headers.get("content-length")
    if declared is None:
        return
    try:
        length = int(declared)
    except ValueError:
        return
    if length > expected:
        raise ModelInstallError(
            DOWNLOAD_FAILED,
            f"The weights host declares {length} bytes for model {model_id!r} but the catalog "
            f"declares {expected}; the download was refused.",
            {
                "model_id": model_id,
                "reason": SIZE_EXCEEDED,
                "expected_bytes": expected,
                "declared_bytes": length,
            },
        )


def _sync_to_disk(sink: IO[bytes]) -> None:
    """Flush ``sink`` all the way to stable storage.

    ``close()`` alone only hands the bytes to the OS page cache, so a rename
    performed straight afterwards can publish a file whose tail is still in
    volatile memory. Nothing ever re-hashes installed weights, so a torn tail
    would be loaded silently and permanently — see the module docstring for why
    this ``fsync`` runs on the event loop rather than in a worker thread.
    """
    sink.flush()
    os.fsync(sink.fileno())


def _sync_directory(directory: Path) -> None:
    """Make the rename itself durable, where the platform allows it.

    ``fsync`` on the containing directory is what persists a new directory
    entry on POSIX. Windows exposes no directory handle to sync — and its
    ``ReplaceFile``/``MoveFileEx`` semantics do not need one — so a refusal to
    open the directory is expected, not an error, and the install is complete
    either way.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover - platform-dependent
        logger.debug("Could not fsync the weights directory %s", directory, exc_info=True)
    finally:
        os.close(descriptor)


def _discard(part: Path) -> None:
    """Remove a leftover ``.part`` file, tolerating a filesystem that refuses.

    Cleanup must never replace the failure (or the cancellation) that brought us
    here, so an unlink that cannot proceed is logged and swallowed.
    """
    try:
        part.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - a locked or vanished temporary file
        logger.warning("Could not remove the partial weights file %s", part, exc_info=True)


# -- the install-failure sidecar (feature 061) --------------------------------
#
# See the module docstring's "A failure survives a restart" section for the
# write/clear/load contract these three functions implement. All three treat
# every failure mode as non-fatal to the install: a sidecar is a convenience
# for surviving a restart, never something an install attempt's own outcome
# depends on.


def _write_failure_sidecar(models_dir: Path, model_id: str, failure: ErrorInfo) -> None:
    """Persist ``failure`` for ``model_id``, atomically and without an fsync.

    Same ``.tmp``-then-:func:`os.replace` shape as the weights artifact
    itself, same directory so the rename is same-filesystem — but with no
    ``fsync`` of either the file or the directory. Unlike weights, losing this
    write to a very-recently-preceding crash does not corrupt anything
    silently and permanently; it only reverts one model, for one boot, to the
    pre-061 behaviour of reporting ``available`` instead of ``failed``. See the
    module docstring.
    """
    path = install_failure_path(models_dir, model_id)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(failure.model_dump_json(), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        # Broader than OSError on purpose: this runs inside ``_run``'s own
        # except blocks, where an escaping serialization error would kill the
        # install task with an unretrieved exception — the one thing ``_run``
        # promises never happens. The in-memory failure is already recorded,
        # so ``describe`` still reports ``failed`` either way.
        logger.warning(
            "Could not persist the install failure for model %r", model_id, exc_info=True
        )
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - a locked or vanished temporary file
            logger.warning("Could not remove the temporary sidecar file %s", tmp, exc_info=True)


def _read_failure_sidecar(models_dir: Path, model_id: str) -> ErrorInfo | None:
    """Read ``model_id``'s persisted failure, or ``None`` if absent or unusable.

    A missing file is the ordinary case (most models never failed) and is not
    logged. A file that exists but will not parse — truncated or left with a
    garbage tail by the same crash window :func:`_write_failure_sidecar`
    accepts, or hand-edited — is logged as a warning and treated exactly like
    a missing one; it is **not** deleted here. Startup does perform one
    mutation elsewhere (``_restore_failures`` removes a sidecar the weights'
    presence proves stale), but a *corrupt* file is left for the next install
    attempt to clear through the ordinary path: an unparseable record is the
    one artifact worth leaving on disk for a human to inspect.
    """
    path = install_failure_path(models_dir, model_id)
    try:
        # Bytes, not text: the crash window this reader accepts can leave a
        # non-UTF-8 garbage tail, and ``read_text`` would raise
        # ``UnicodeDecodeError`` (a ``ValueError``, not an ``OSError``) out of
        # startup for every model at once. ``model_validate_json`` accepts
        # bytes and reports invalid UTF-8 as the same ``ValidationError`` the
        # corrupt branch below already absorbs.
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning(
            "Could not read the install-failure sidecar for model %r", model_id, exc_info=True
        )
        return None
    try:
        return ErrorInfo.model_validate_json(raw)
    except ValidationError:
        logger.warning(
            "Install-failure sidecar for model %r is corrupt; treating as absent", model_id
        )
        return None


def _delete_failure_sidecar(models_dir: Path, model_id: str) -> None:
    """Remove ``model_id``'s persisted failure, tolerating a filesystem that refuses.

    Called wherever ``self._failures.pop(model_id, None)`` already is: the
    start of an install attempt, a successful install (implicitly, since it is
    already gone by then), and a weights removal.
    """
    path = install_failure_path(models_dir, model_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - a locked or vanished sidecar file
        logger.warning(
            "Could not remove the install-failure sidecar for model %r", model_id, exc_info=True
        )


__all__ = [
    "CHECKSUM_MISMATCH",
    "CONNECTION_FAILED",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "DOWNLOAD_FAILED",
    "FILESYSTEM_ERROR",
    "HTTP_STATUS",
    "SIZE_EXCEEDED",
    "SIZE_MISMATCH",
    "UNEXPECTED_ERROR",
    "ModelInstallError",
    "ModelInstaller",
]
