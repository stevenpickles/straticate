"""Export endpoint: a completed job's stems transcoded and packaged for download.

``GET /jobs/{job_id}/export?format=…&stems=…`` turns the 16-bit WAV stems the
separator wrote into the format the user asked for — 24-bit WAV, 32-bit float
WAV, or FLAC — and hands back either the bare audio file (exactly one stem
requested) or a zip containing one file per stem plus a ``separation.json``
manifest (more than one, including the default "all stems").

Four things this module is careful about:

- **The event loop is never blocked.** FFmpeg transcoding and zip writing are
  seconds of CPU for a four-stem, ten-minute job — exactly the "expensive work
  in a request handler" AGENTS.md principle 4 and ARCHITECTURE.md §14 forbid.
  Blocking here would stall the job worker, the event dispatcher and every
  connected WebSocket client, so every blocking step runs in a worker thread
  via :func:`asyncio.to_thread` (the same discipline
  :mod:`straticate.audio.probe` and :mod:`straticate.inference.pcm` already
  use for their subprocesses).
- **FFmpeg is the single compatibility layer** (ARCHITECTURE.md §5). Nothing
  here writes a container or an encoder by hand; a format is a
  ``(container, codec)`` pair handed to ``ffmpeg``, and sample rate and
  channel count are deliberately left untouched.
- **Artifacts are built once and reused.** A completed job's stems are
  immutable, so the built file is cached under
  ``{data_dir}/jobs/{job_id}/exports/`` (:func:`~straticate.jobs.layout.job_exports_dir`
  is the sole authority on that path since feature 058, so this module never
  builds it by hand) under a name derived from the format and the sorted stem
  list, and a repeated identical download is served straight from disk.
  Concurrent identical requests share **one** build: the
  cache check is a fast path, and the real guard is a per-artifact
  :class:`BuildLocks` entry that the second request waits on before
  re-checking. Without it, N simultaneous downloads would each transcode every
  stem, N times the CPU, competing with a running separation.
- **A reader never sees a partial artifact.** Every build writes to a unique
  ``.part`` file and is renamed into place with :func:`os.replace`, the same
  discipline :class:`~straticate.inference.FakeSeparator` uses for stems, so a
  concurrent or failed export can never serve a truncated zip.
- **A cancelled request cleans up after itself.** ``asyncio.to_thread`` is not
  cancellable: if the handler unwound while a worker thread was still running
  FFmpeg, the thread would keep writing into a staging directory that was
  being torn down, and the ``.part`` file would be orphaned forever (nothing
  has a retention policy). Builds are therefore **shielded** — a client that
  disconnects mid-download leaves the build to finish and publish its
  artifact, and the ``.part`` is unlinked in a ``finally`` that runs only
  after the worker thread has returned.

**Errors say what failed, not where.** FFmpeg's stderr and filesystem error
strings carry absolute server paths, so they are *logged* and the client is
told only a short classification (:data:`TRANSCODE_FAILED`,
:data:`FILESYSTEM_ERROR`) in ``detail.reason`` — the same discipline
:func:`straticate.errors._handle_unexpected_error` applies to unhandled
exceptions.

The lookups (``job_not_found`` / ``result_not_available`` / ``stem_not_found``
/ ``stem_file_missing``) are :mod:`straticate.api.job_outputs`'s, shared with
the result router so the two cannot disagree about a status code.

**Bit depth is honest, not magical.** The separator writes 16-bit PCM WAV
today, so ``wav_pcm24`` and ``wav_float32`` change the container encoding and
add no information — they exist so the export path is complete before a real
separator lands, and so a user who needs 24-bit or float files downstream gets
them. Nothing here recovers detail the stems never had.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import zipfile
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import AfterValidator, AwareDatetime, BaseModel, Field

from straticate.api.audio import SettingsDep
from straticate.api.job_outputs import completed_job, stem_not_found, stem_source_path
from straticate.api.results import ManagerDep, stem_media_type
from straticate.audio.ffmpeg import FFmpegTimeout, run_ffmpeg
from straticate.errors import ApplicationError
from straticate.jobs.layout import job_exports_dir
from straticate.schemas import SeparationResult

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["export"])

TRANSCODE_FAILED = "transcode_failed"
"""``detail.reason``: FFmpeg exited non-zero encoding one stem."""

FILESYSTEM_ERROR = "filesystem_error"
"""``detail.reason``: the archive or the artifact could not be written."""

MANIFEST_NAME = "separation.json"
"""Name of the manifest entry inside a multi-stem export archive."""

ZIP_MEDIA_TYPE = "application/zip"

_MAX_READABLE_NAME_LENGTH = 64
"""Longest joined stem list kept literal in an artifact name (else hashed)."""

_DIGEST_LENGTH = 16
"""Hex characters of the SHA-256 fallback used for very long stem lists."""


class ExportFormat(StrEnum):
    """Audio format an export is encoded to.

    Deliberately defined here rather than in :mod:`straticate.schemas`: it is
    one endpoint's query-parameter vocabulary, not a shared entity, and
    nothing else in the application encodes audio. Being a query parameter it
    still reaches the generated frontend types (FastAPI registers a query
    enum as a component), which is what feature 024's format picker needs, and
    an unrecognised value is the standard FastAPI ``validation_error`` (422)
    rather than a code invented here.
    """

    WAV_PCM24 = "wav_pcm24"
    WAV_FLOAT32 = "wav_float32"
    FLAC = "flac"


@dataclass(frozen=True, slots=True)
class _FormatSpec:
    """How one :class:`ExportFormat` is produced and named.

    Attributes:
        container: FFmpeg muxer name (``-f``). Passed explicitly because the
            build writes to a ``.part`` file, whose suffix tells FFmpeg
            nothing.
        codec: FFmpeg encoder name (``-acodec``).
        suffix: File suffix of the produced audio file.
    """

    container: str
    codec: str
    suffix: str


_FORMAT_SPECS: dict[ExportFormat, _FormatSpec] = {
    ExportFormat.WAV_PCM24: _FormatSpec(container="wav", codec="pcm_s24le", suffix=".wav"),
    ExportFormat.WAV_FLOAT32: _FormatSpec(container="wav", codec="pcm_f32le", suffix=".wav"),
    ExportFormat.FLAC: _FormatSpec(container="flac", codec="flac", suffix=".flac"),
}
"""Encoder and container per export format. Sample rate and channels are never
passed, so the source's are preserved exactly."""


class ExportManifest(BaseModel):
    """The ``separation.json`` written into a multi-stem export archive.

    The job's :class:`~straticate.schemas.SeparationResult` is embedded
    **verbatim** under ``result`` rather than being flattened into a parallel
    shape, so the archive documents itself with the same contract the API
    already serves at ``GET /jobs/{job_id}/result``. The remaining fields
    describe the export itself.

    This model lives in the router rather than in :mod:`straticate.schemas`:
    no route returns it — it is a file inside a zip, not a response body — so
    it stays out of the OpenAPI component list, and its shape is documented in
    ``docs/contracts/rest-api.md`` instead.
    """

    format: ExportFormat = Field(description="Format the stems in this archive are encoded in.")
    model_id: str = Field(description="ID of the model that performed the separation.")
    stems: list[str] = Field(description="Stem names actually included in this archive.")
    exported_at: AwareDatetime = Field(description="When the archive was built (UTC).")
    result: SeparationResult = Field(description="The job's separation result, verbatim.")


class ExportError(Exception):
    """A build step failed (FFmpeg, the zip writer, or the filesystem).

    ``reason`` is the short, **client-safe** classification that reaches
    ``detail.reason`` — one of :data:`TRANSCODE_FAILED` or
    :data:`FILESYSTEM_ERROR`. The diagnostic text (FFmpeg's stderr, an OS error
    string) is logged at the raise site and deliberately never travels with the
    exception: it carries absolute server paths, and no other 500 in this
    application leaks those.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BuildLocks:
    """Per-artifact build locks, so one artifact is built once at a time.

    The handler's ``artifact.is_file()`` check is only a fast path: between it
    and the build there is no atomicity, so N simultaneous identical downloads
    would each transcode every stem. Holding a lock keyed by the artifact path
    across "check again, then build" collapses them into one build that the
    others wait for and then serve from cache.

    Entries are reference-counted and dropped when the last waiter leaves, so
    the registry does not grow with every artifact a long-running server has
    ever produced. One registry lives per application (see
    :func:`get_export_locks`), which also keeps the locks bound to a single
    event loop.
    """

    def __init__(self) -> None:
        self._entries: dict[Path, tuple[asyncio.Lock, int]] = {}

    def building(self, key: Path) -> int:
        """Number of requests currently holding or waiting for ``key``."""
        entry = self._entries.get(key)
        return 0 if entry is None else entry[1]

    @asynccontextmanager
    async def acquire(self, key: Path) -> AsyncGenerator[None]:
        """Hold the lock for ``key`` for the duration of the block.

        Registration is a plain dict update with no ``await`` in front of it,
        so on a single-threaded event loop two requests cannot both decide they
        are the first.
        """
        entry = self._entries.get(key)
        lock = asyncio.Lock() if entry is None else entry[0]
        self._entries[key] = (lock, (0 if entry is None else entry[1]) + 1)
        try:
            async with lock:
                yield
        finally:
            held, waiters = self._entries[key]
            if waiters <= 1:
                del self._entries[key]
            else:
                self._entries[key] = (held, waiters - 1)


def get_export_locks(request: Request) -> BuildLocks:
    """Return this application's :class:`BuildLocks`, creating it on demand.

    Created lazily on ``app.state`` rather than in
    :func:`straticate.main.create_app` so the whole mechanism stays inside this
    router. There is no race: the function contains no ``await``, and the event
    loop is single-threaded.
    """
    locks = getattr(request.app.state, "export_locks", None)
    if locks is None:
        locks = BuildLocks()
        request.app.state.export_locks = locks
    return cast(BuildLocks, locks)


LocksDep = Annotated[BuildLocks, Depends(get_export_locks)]


FormatQuery = Annotated[
    ExportFormat,
    Query(
        alias="format",
        description=(
            "Audio format to encode the stems in. The stems are 16-bit PCM WAV on disk, "
            "so `wav_pcm24` and `wav_float32` change the encoding without adding "
            "information."
        ),
    ),
]


def _reject_blank_selection(value: str | None) -> str | None:
    """Reject a present-but-empty ``stems`` value.

    ``?stems=`` (or a value of nothing but whitespace) is a client bug, not a
    request for every stem: omitting the parameter is how you ask for all of
    them. Rejecting it here, in the parameter's own validation, makes it the
    standard FastAPI ``validation_error`` (422) rather than a code invented
    for one endpoint.
    """
    if value is not None and not value.strip():
        raise ValueError("stems must name at least one stem; omit it to export all of them")
    return value


StemsQuery = Annotated[
    str | None,
    AfterValidator(_reject_blank_selection),
    Query(
        description=(
            "Comma-separated stem names to export. Omit to export every stem of the "
            "job's result. Each name is validated against that result."
        ),
    ),
]

_EXPORT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": (
            "The transcoded stem (exactly one requested) or a zip of the stems plus "
            "`separation.json` (more than one). Always `Content-Disposition: attachment`."
        ),
        "content": {
            media_type: {"schema": {"type": "string", "format": "binary"}}
            for media_type in ("audio/wav", "audio/flac", ZIP_MEDIA_TYPE)
        },
    },
}


def _export_failed(job_id: str, export_format: ExportFormat, reason: str) -> ApplicationError:
    """Build the 500 for a build step that failed.

    An FFmpeg non-zero exit, an unwritable data directory or a zip failure are
    all server-side faults the client cannot fix by changing its request — but
    they are *expected* faults with a documented code, never an unhandled
    traceback.

    ``reason`` is a short classification (:data:`TRANSCODE_FAILED`,
    :data:`FILESYSTEM_ERROR`), never the underlying tool's message: FFmpeg's
    stderr and OS error strings contain absolute server paths. The diagnostic
    text is logged instead.
    """
    return ApplicationError(
        "export_failed",
        f"Could not build the {export_format.value} export for job {job_id!r}.",
        status_code=500,
        detail={"job_id": job_id, "format": export_format.value, "reason": reason},
    )


def _export_timed_out(job_id: str, export_format: ExportFormat) -> ApplicationError:
    """Build the 504 for an FFmpeg transcode that ran out of time.

    Its own code, not an :data:`~ExportError` reason: ``export_failed`` says the
    encode was attempted and failed, which is a fact about the audio or the
    disk. A timeout says the server gave up on a subprocess, which is a fact
    about the server — different cause, different remedy (retry, or raise
    ``STRATICATE_FFMPEG_TIMEOUT_SECONDS``), and therefore a different code.
    """
    return ApplicationError(
        "export_timed_out",
        f"Encoding the {export_format.value} export for job {job_id!r} timed out.",
        status_code=504,
        detail={"job_id": job_id, "format": export_format.value},
    )


def parse_stem_selection(stems: str | None, available: list[str], job_id: str) -> list[str]:
    """Resolve the ``stems`` query parameter against the job's result.

    Omitting the parameter selects **every** stem the result lists. A supplied
    list is split on commas and each entry is stripped of surrounding
    whitespace; the selection is then returned in the result's own order and
    deduplicated, so ``drums,vocals`` and ``vocals,drums,vocals`` describe the
    same export (and therefore hit the same cached artifact).

    The result is the only authority on stem names, so a traversal attempt, an
    absolute path or an empty entry is simply not one of them and exits here
    as a clean ``stem_not_found`` — never a 500, and never a path.

    Raises:
        ApplicationError: ``stem_not_found`` (404) for any requested name the
            result does not list.
    """
    if stems is None:
        return list(available)
    requested = [name.strip() for name in stems.split(",")]
    for name in requested:
        if name not in available:
            raise stem_not_found(job_id, name, available)
    selected = set(requested)
    return [name for name in available if name in selected]


def artifact_name(export_format: ExportFormat, stems: list[str], *, archive: bool) -> str:
    """Return the deterministic cache file name for one export.

    The name is derived from the format and the **sorted** stem list, so the
    order the client listed them in does not produce a second copy. Stem names
    match ``^[a-z][a-z0-9_]*$``, so they are filesystem-safe as they stand; a
    model with enough stems to make the joined name unwieldy falls back to a
    SHA-256 digest, keeping the path length bounded.
    """
    joined = "-".join(sorted(stems))
    if len(joined) > _MAX_READABLE_NAME_LENGTH:
        joined = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
    suffix = ".zip" if archive else _FORMAT_SPECS[export_format].suffix
    return f"{export_format.value}-{joined}{suffix}"


def download_name(
    job_id: str, export_format: ExportFormat, stems: list[str], *, archive: bool
) -> str:
    """Return the ``filename`` offered in ``Content-Disposition``.

    ``{job_id}-{format}.zip`` for an archive, ``{job_id}-{format}-{stem}.{ext}``
    for a single stem — the same prefix either way, so a user who exports the
    same job twice in two formats ends up with two distinguishable files.
    """
    if archive:
        return f"{job_id}-{export_format.value}.zip"
    return f"{job_id}-{export_format.value}-{stems[0]}{_FORMAT_SPECS[export_format].suffix}"


def transcode_sync(
    source: Path, destination: Path, spec: _FormatSpec, timeout_seconds: float
) -> None:
    """Blocking FFmpeg transcode of one stem (runs in a worker thread).

    Sample rate and channel count are not passed, so FFmpeg preserves the
    source's. ``-map 0:a:0`` selects the single audio stream, so nothing but
    audio is ever carried into the output.

    Args:
        source: The stem file to read.
        destination: The ``.part`` file to write.
        spec: Container/encoder pair for the requested format.
        timeout_seconds: Bound for the FFmpeg invocation, taken from the
            request's ``Settings.ffmpeg_timeout_seconds``.

    Raises:
        ExportError: FFmpeg exited non-zero. Its stderr names absolute server
            paths, so it is logged here and the exception carries only
            :data:`TRANSCODE_FAILED`.
        FFmpegTimeout: FFmpeg exceeded ``timeout_seconds``.
            Propagated as itself, not folded into :data:`TRANSCODE_FAILED`: a
            wedged encoder is an operational fault with its own status code
            (``export_timed_out``), not a failed encode.
    """
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-f",
        spec.container,
        "-acodec",
        spec.codec,
        str(destination),
    ]
    result = run_ffmpeg(command, timeout_seconds=timeout_seconds)
    if result.returncode != 0:
        logger.error(
            "ffmpeg exited %d encoding %s as %s: %s",
            result.returncode,
            source,
            spec.codec,
            result.stderr.decode("utf-8", "replace").strip(),
        )
        raise ExportError(TRANSCODE_FAILED)


def _write_archive_sync(destination: Path, members: list[tuple[str, Path]], manifest: str) -> None:
    """Blocking zip build (runs in a worker thread).

    Args:
        destination: The ``.part`` file to write.
        members: ``(entry name, file on disk)`` pairs, one per stem.
        manifest: Serialized ``separation.json`` content.
    """
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry_name, path in members:
            archive.write(path, arcname=entry_name)
        archive.writestr(MANIFEST_NAME, manifest)


def _build_manifest(result: SeparationResult, export_format: ExportFormat, stems: list[str]) -> str:
    """Serialize the ``separation.json`` for this export."""
    manifest = ExportManifest(
        format=export_format,
        model_id=result.model_id,
        stems=list(stems),
        exported_at=datetime.now(UTC),
        result=result,
    )
    return manifest.model_dump_json(indent=2)


async def build_artifact(
    artifact: Path,
    sources: list[tuple[str, Path]],
    result: SeparationResult,
    export_format: ExportFormat,
    *,
    archive: bool,
    timeout_seconds: float,
) -> None:
    """Transcode (and optionally zip) into ``artifact``, atomically.

    Everything blocking happens in a worker thread; this coroutine only awaits.
    The build writes to a ``.part`` name unique to this call — two concurrent
    identical exports therefore never share a partial file — and
    :func:`os.replace` publishes it.

    Two details that only bite in production:

    - The ``finally`` unlinks the ``.part`` on **every** exit, including
      cancellation. On success the rename already consumed it, so the unlink is
      a no-op; on failure or cancellation it is what stops a permanent leak
      (nothing ever cleans up an export directory). This coroutine is only ever
      run shielded, so the ``finally`` cannot run while a worker thread is
      still writing — see :func:`_build_cached`.
    - If ``artifact`` appeared while this build was running, the rename is
      **skipped**. The published file may already be open in another response
      (:class:`~fastapi.responses.FileResponse` holds a handle), and on Windows
      replacing an open file raises ``PermissionError`` — which would fail a
      request whose transcode actually succeeded. The two builds produce
      equivalent audio, so keeping the published one is free.

    Raises:
        ExportError: Any build step failed.
    """
    spec = _FORMAT_SPECS[export_format]
    part = artifact.with_name(f"{artifact.name}.{uuid4().hex}.part")
    try:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        if archive:
            # ``ignore_cleanup_errors`` because a failed build's staging
            # directory is not worth turning into a second, misleading error.
            with TemporaryDirectory(
                dir=artifact.parent, prefix=".build-", ignore_cleanup_errors=True
            ) as staging:
                members: list[tuple[str, Path]] = []
                for name, source in sources:
                    encoded = Path(staging) / f"{name}{spec.suffix}"
                    await asyncio.to_thread(transcode_sync, source, encoded, spec, timeout_seconds)
                    members.append((encoded.name, encoded))
                manifest = _build_manifest(result, export_format, [name for name, _ in sources])
                await asyncio.to_thread(_write_archive_sync, part, members, manifest)
        else:
            await asyncio.to_thread(transcode_sync, sources[0][1], part, spec, timeout_seconds)
        if not artifact.is_file():
            await asyncio.to_thread(os.replace, part, artifact)
    except OSError as exc:
        logger.exception("Could not write the export artifact %s", artifact)
        raise ExportError(FILESYSTEM_ERROR) from exc
    finally:
        _discard(part)


def _discard(part: Path) -> None:
    """Remove a leftover ``.part`` file, tolerating a filesystem that refuses.

    Cleanup must never replace the failure (or the cancellation) that brought
    us here, so an unlink that cannot proceed is logged and swallowed.
    """
    try:
        part.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - a locked or vanished temporary file
        logger.warning("Could not remove the partial export file %s", part, exc_info=True)


async def _build_cached(
    locks: BuildLocks,
    artifact: Path,
    sources: list[tuple[str, Path]],
    result: SeparationResult,
    export_format: ExportFormat,
    *,
    archive: bool,
    timeout_seconds: float,
) -> None:
    """Build ``artifact`` unless another request is already building it.

    The lock is held across "check again, then build", which is what makes the
    handler's optimistic ``is_file()`` check safe: a second request for the
    same artifact waits here and then finds the file, rather than running a
    duplicate transcode of every stem.

    Raises:
        ExportError: Any build step failed.
    """
    async with locks.acquire(artifact):
        if artifact.is_file():
            return
        await build_artifact(
            artifact,
            sources,
            result,
            export_format,
            archive=archive,
            timeout_seconds=timeout_seconds,
        )


async def _shielded(work: Coroutine[Any, Any, None]) -> None:
    """Await ``work``, letting it finish even if *this* coroutine is cancelled.

    :func:`asyncio.to_thread` cannot be cancelled: the worker thread runs
    FFmpeg to completion whatever the caller does. Unwinding the build
    immediately would therefore tear down its staging directory under a live
    subprocess — a ``PermissionError`` out of ``TemporaryDirectory.__exit__``
    on Windows, a thread writing into an unlinked directory on POSIX — and
    orphan the ``.part`` file for good.

    Shielding instead lets the build finish and publish its artifact, so a
    client that disconnects mid-download merely warms the cache. The caller
    still unwinds immediately.
    """
    task = asyncio.ensure_future(work)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # The build outlives us; consume its outcome so an ExportError raised
        # after we are gone is not reported as a never-retrieved exception.
        task.add_done_callback(_log_orphaned_build)
        raise


def _log_orphaned_build(task: asyncio.Task[None]) -> None:
    """Retrieve the outcome of a build whose requester has disconnected."""
    if task.cancelled():  # pragma: no cover - the build itself is never cancelled
        return
    error = task.exception()
    if error is not None:
        logger.warning("Export build failed after the client disconnected", exc_info=error)


@router.get("/{job_id}/export", response_class=FileResponse, responses=_EXPORT_RESPONSES)
async def export_job_stems(
    job_id: str,
    manager: ManagerDep,
    settings: SettingsDep,
    locks: LocksDep,
    export_format: FormatQuery = ExportFormat.WAV_PCM24,
    stems: StemsQuery = None,
) -> FileResponse:
    """Download a completed job's stems in the requested format.

    Exactly one stem requested → that stem's transcoded audio file. More than
    one (including the default, which is every stem of the job's result) → a
    zip holding one file per stem plus a ``separation.json`` manifest. Either
    way the response is ``Content-Disposition: attachment``; a single-stem
    export deliberately carries no manifest (see
    ``docs/contracts/rest-api.md``).

    The built file is cached under the job's ``exports/`` directory and reused:
    a completed job's stems never change, so a repeated identical download is
    served straight from disk without running FFmpeg again. Simultaneous
    identical requests share one build rather than racing (see
    :class:`BuildLocks`), and a request cancelled mid-build leaves the build to
    finish rather than abandoning a partial file.

    Errors (see ``docs/contracts/rest-api.md``): ``job_not_found`` (404),
    ``result_not_available`` (409, with the job's current ``state`` in
    ``detail``), ``stem_not_found`` (404, with ``available_stems`` in
    ``detail``), ``stem_file_missing`` (404), ``export_failed`` (500),
    ``export_timed_out`` (504) when FFmpeg exceeds its bounded run time, and an
    unknown ``format`` as the standard ``validation_error`` (422).
    """
    job, result = completed_job(manager, job_id)
    available = [stem.name for stem in result.stems]
    selected = parse_stem_selection(stems, available, job.id)
    sources = [
        (name, stem_source_path(settings.data_dir, job.id, name, available)) for name in selected
    ]
    archive = len(sources) != 1

    exports_dir = job_exports_dir(settings.data_dir, job.id)
    artifact = exports_dir / artifact_name(export_format, selected, archive=archive)
    if not artifact.is_file():
        try:
            await _shielded(
                _build_cached(
                    locks,
                    artifact,
                    sources,
                    result,
                    export_format,
                    archive=archive,
                    timeout_seconds=settings.ffmpeg_timeout_seconds,
                )
            )
        except FFmpegTimeout as exc:
            raise _export_timed_out(job.id, export_format) from exc
        except ExportError as exc:
            raise _export_failed(job.id, export_format, exc.reason) from exc

    return FileResponse(
        artifact,
        media_type=ZIP_MEDIA_TYPE if archive else stem_media_type(artifact),
        filename=download_name(job.id, export_format, selected, archive=archive),
        content_disposition_type="attachment",
    )
