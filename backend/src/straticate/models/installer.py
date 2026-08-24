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
- **The ``.part`` never survives.** A ``finally`` unlinks it on every exit —
  success (where the rename already consumed it), failure, and cancellation.
- **Nothing blocks the event loop.** The download is fully asynchronous, and the
  per-chunk work left on the loop is one buffered ``write`` and one
  ``hashlib.update`` of at most :data:`DEFAULT_CHUNK_BYTES`. That is deliberate
  rather than lazy: :func:`asyncio.to_thread` cannot be cancelled, so moving the
  write off the loop would create a window in which the ``finally`` unlinks a
  ``.part`` that a live worker thread is still writing to — trading a
  sub-millisecond memcpy for an orphaned file. Feature 022's export shields its
  worker threads for exactly this reason; here the cheaper answer is not to
  create the window.

Resumable downloads are **out of scope** (see
``docs/features/025-model-download-manager.md``): a failed install starts over.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

import httpx2

from straticate.errors import ApplicationError
from straticate.models.catalog import CatalogEntry, ModelArtifact, ModelCatalog
from straticate.models.layout import (
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
        # Registration is a plain dict write with no ``await`` in front of it,
        # so on a single-threaded event loop two requests cannot both decide
        # they are the first one.
        running = _RunningInstall(total_bytes=artifact.size_bytes)
        self._running[model_id] = running
        running.task = asyncio.create_task(
            self._run(model_id, artifact, running), name=f"install-{model_id}"
        )
        return self.describe(entry)

    def remove(self, model_id: str) -> Model:
        """Delete ``model_id``'s installed weights, returning it to ``available``.

        Idempotent: removing weights that are not there succeeds and reports
        ``available``, because that is already the state the caller asked for. A
        recorded failure is cleared too — the model is back to "never
        installed", which is the truth once nothing is on disk.

        Raises:
            ApplicationError: ``model_not_found`` (404) for an unknown ID,
                ``model_not_downloadable`` (409) for a built-in model,
                ``model_busy`` (409) while an install is running — the running
                download would otherwise publish weights a moment after they
                were removed.
        """
        entry = self._catalog.get_entry(model_id)
        if entry.artifact is None:
            raise _model_not_downloadable(model_id)
        if model_id in self._running:
            raise _model_busy(model_id)
        self._failures.pop(model_id, None)
        remove_weights(self._models_dir, model_id)
        return self.describe(entry)

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
            self._failures[model_id] = exc.to_error_info()
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
            digest = await self._download(artifact, part, running)
            if digest != artifact.sha256:
                raise ModelInstallError(
                    CHECKSUM_MISMATCH,
                    f"The downloaded artifact for model {model_id!r} does not match the "
                    "SHA-256 pinned in the catalog; it was discarded.",
                    {"model_id": model_id, "expected": artifact.sha256, "actual": digest},
                )
            os.replace(part, target)
        except OSError as exc:
            logger.exception("Could not write the weights artifact for model %r", model_id)
            raise ModelInstallError(
                DOWNLOAD_FAILED,
                f"The weights artifact for model {model_id!r} could not be written to disk.",
                {"model_id": model_id, "reason": FILESYSTEM_ERROR},
            ) from exc
        finally:
            _discard(part)

    async def _download(self, artifact: ModelArtifact, part: Path, running: _RunningInstall) -> str:
        """Stream the artifact into ``part``; return its SHA-256 hex digest.

        The response is never read whole. ``size_bytes`` bounds it twice — once
        against a declared ``Content-Length`` before any body is read, and once
        per chunk against the running total, which is what covers a chunked or
        length-less response.

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
                    raise ModelInstallError(
                        DOWNLOAD_FAILED,
                        f"The weights host answered {response.status_code} for "
                        f"{_safe_url(artifact.download_url)}.",
                        {"reason": HTTP_STATUS, "status_code": response.status_code},
                    )
                _reject_declared_overrun(response, expected)
                with part.open("wb") as sink:
                    async for chunk in response.aiter_bytes(self._chunk_bytes):
                        received += len(chunk)
                        if received > expected:
                            raise ModelInstallError(
                                DOWNLOAD_FAILED,
                                "The weights host is serving more data than the catalog "
                                f"declares ({expected} bytes); the download was stopped.",
                                {"reason": SIZE_EXCEEDED, "expected_bytes": expected},
                            )
                        digest.update(chunk)
                        sink.write(chunk)
                        running.downloaded_bytes = received
        except httpx2.HTTPError as exc:
            raise ModelInstallError(
                DOWNLOAD_FAILED,
                f"The weights artifact could not be fetched from "
                f"{_safe_url(artifact.download_url)}.",
                {"reason": CONNECTION_FAILED},
            ) from exc
        if received != expected:
            raise ModelInstallError(
                DOWNLOAD_FAILED,
                f"The weights host sent {received} bytes but the catalog declares "
                f"{expected}; the download was incomplete.",
                {"reason": SIZE_MISMATCH, "expected_bytes": expected, "received_bytes": received},
            )
        return digest.hexdigest()

    async def __aenter__(self) -> Self:
        """Support ``async with`` in tests and scripts."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Cancel any running install on the way out."""
        await self.aclose()


def _reject_declared_overrun(response: httpx2.Response, expected: int) -> None:
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
            f"The weights host declares {length} bytes but the catalog declares "
            f"{expected}; the download was refused.",
            {"reason": SIZE_EXCEEDED, "expected_bytes": expected, "declared_bytes": length},
        )


def _safe_url(url: str) -> str:
    """Return ``url`` without any credentials it may carry.

    A catalog is a file a user can edit, and an error message is a thing a user
    pastes into an issue. Nothing else here strips anything: the host and path
    are exactly what makes a download failure diagnosable.
    """
    try:
        parsed = httpx2.URL(url)
    except httpx2.InvalidURL:  # pragma: no cover - the catalog validated the scheme
        return "<invalid url>"
    return str(parsed.copy_with(userinfo=b""))


def _discard(part: Path) -> None:
    """Remove a leftover ``.part`` file, tolerating a filesystem that refuses.

    Cleanup must never replace the failure (or the cancellation) that brought us
    here, so an unlink that cannot proceed is logged and swallowed.
    """
    try:
        part.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - a locked or vanished temporary file
        logger.warning("Could not remove the partial weights file %s", part, exc_info=True)


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
    "ModelInstallError",
    "ModelInstaller",
]
