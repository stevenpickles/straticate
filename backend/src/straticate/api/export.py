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
  ``{data_dir}/jobs/{job_id}/exports/`` under a name derived from the format
  and the sorted stem list, and a repeated identical download is served
  straight from disk.
- **A reader never sees a partial artifact.** Every build writes to a unique
  ``.part`` file and is renamed into place with :func:`os.replace`, the same
  discipline :class:`~straticate.inference.FakeSeparator` uses for stems, so a
  concurrent or failed export can never serve a truncated zip.

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
import os
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse
from pydantic import AfterValidator, AwareDatetime, BaseModel, Field

from straticate.api.audio import SettingsDep
from straticate.api.job_outputs import completed_job, stem_not_found, stem_source_path
from straticate.api.results import ManagerDep, stem_media_type
from straticate.errors import ApplicationError
from straticate.inference import job_output_dir
from straticate.schemas import SeparationResult

router = APIRouter(prefix="/jobs", tags=["export"])

EXPORTS_DIRECTORY = "exports"
"""Name of the export-artifact directory under a job's output directory."""

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
    """A build step failed (FFmpeg, the zip writer, or the filesystem)."""


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
    """
    return ApplicationError(
        "export_failed",
        f"Could not build the {export_format.value} export for job {job_id!r}.",
        status_code=500,
        detail={"job_id": job_id, "format": export_format.value, "reason": reason},
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


def transcode_sync(source: Path, destination: Path, spec: _FormatSpec) -> None:
    """Blocking FFmpeg transcode of one stem (runs in a worker thread).

    Sample rate and channel count are not passed, so FFmpeg preserves the
    source's. ``-map 0:a:0`` selects the single audio stream, so nothing but
    audio is ever carried into the output.

    Raises:
        ExportError: FFmpeg exited non-zero.
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
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise ExportError(f"ffmpeg could not encode {source.name!r}: {message}")


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


async def _build_artifact(
    artifact: Path,
    sources: list[tuple[str, Path]],
    result: SeparationResult,
    export_format: ExportFormat,
    *,
    archive: bool,
) -> None:
    """Transcode (and optionally zip) into ``artifact``, atomically.

    Everything blocking happens in a worker thread; this coroutine only awaits.
    The build writes to a ``.part`` name unique to this call — two concurrent
    identical exports therefore never share a partial file — and
    :func:`os.replace` publishes it. On any failure the ``.part`` file is
    removed, so a later request rebuilds rather than serving rubbish.

    Raises:
        ExportError: Any build step failed.
    """
    spec = _FORMAT_SPECS[export_format]
    artifact.parent.mkdir(parents=True, exist_ok=True)
    part = artifact.with_name(f"{artifact.name}.{uuid4().hex}.part")
    try:
        if archive:
            with TemporaryDirectory(dir=artifact.parent, prefix=".build-") as staging:
                members: list[tuple[str, Path]] = []
                for name, source in sources:
                    encoded = Path(staging) / f"{name}{spec.suffix}"
                    await asyncio.to_thread(transcode_sync, source, encoded, spec)
                    members.append((encoded.name, encoded))
                manifest = _build_manifest(result, export_format, [name for name, _ in sources])
                await asyncio.to_thread(_write_archive_sync, part, members, manifest)
        else:
            await asyncio.to_thread(transcode_sync, sources[0][1], part, spec)
        await asyncio.to_thread(os.replace, part, artifact)
    except OSError as exc:
        part.unlink(missing_ok=True)
        raise ExportError(str(exc)) from exc
    except ExportError:
        part.unlink(missing_ok=True)
        raise


@router.get("/{job_id}/export", response_class=FileResponse, responses=_EXPORT_RESPONSES)
async def export_job_stems(
    job_id: str,
    manager: ManagerDep,
    settings: SettingsDep,
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
    served straight from disk without running FFmpeg again.

    Errors (see ``docs/contracts/rest-api.md``): ``job_not_found`` (404),
    ``result_not_available`` (409, with the job's current ``state`` in
    ``detail``), ``stem_not_found`` (404, with ``available_stems`` in
    ``detail``), ``stem_file_missing`` (404), ``export_failed`` (500), and an
    unknown ``format`` as the standard ``validation_error`` (422).
    """
    job, result = completed_job(manager, job_id)
    available = [stem.name for stem in result.stems]
    selected = parse_stem_selection(stems, available, job.id)
    sources = [
        (name, stem_source_path(settings.data_dir, job.id, name, available)) for name in selected
    ]
    archive = len(sources) != 1

    exports_dir = job_output_dir(settings.data_dir, job.id) / EXPORTS_DIRECTORY
    artifact = exports_dir / artifact_name(export_format, selected, archive=archive)
    if not artifact.is_file():
        try:
            await _build_artifact(artifact, sources, result, export_format, archive=archive)
        except ExportError as exc:
            raise _export_failed(job.id, export_format, str(exc)) from exc

    return FileResponse(
        artifact,
        media_type=ZIP_MEDIA_TYPE if archive else stem_media_type(artifact),
        filename=download_name(job.id, export_format, selected, archive=archive),
        content_disposition_type="attachment",
    )
