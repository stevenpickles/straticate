"""Result endpoints: a completed job's ``SeparationResult`` and its stem audio.

``GET /jobs/{job_id}/result`` hands back the record the separator produced;
``GET /jobs/{job_id}/stems/{stem_name}`` streams one stem's bytes with byte
``Range`` support, so the browser's stem player (feature 023) can seek without
downloading whole files and the export path (feature 022) has a stable place to
build on.

Three rules the handlers here obey:

- **The result is the authority on which stems exist.** A requested stem name
  is validated against ``job.result.stems`` — never against a directory
  listing and never against a hardcoded stem list (AGENTS.md principles 1
  and 6). Two-stem and four-stem jobs go through exactly the same code.
- **Every path is built by** :func:`straticate.inference.layout.stem_path`,
  which rejects any name that is not a valid stem name. A stem name arrives
  from the URL, so nothing here concatenates it into a path by hand and
  nothing sanitizes it — traversal attempts are simply not stem names, and
  come back as a clean 404.
- **Every handler is ``async def``**, consistent with the rest of the API
  (the job manager's read API is called on its own event loop).

Both rules live in :mod:`straticate.api.job_outputs`
(:func:`~straticate.api.job_outputs.completed_job` and
:func:`~straticate.api.job_outputs.stem_source_path`), which the export router
(feature 022) shares, so the two cannot disagree about a status code.

Range handling is Starlette's: :class:`~starlette.responses.FileResponse`
(Starlette 1.6) advertises ``Accept-Ranges: bytes``, answers a single
``Range`` with ``206`` plus ``Content-Range``, merges multiple ranges into a
``multipart/byteranges`` body, and rejects an unsatisfiable range with ``416``
and ``Content-Range: bytes */{size}``. Those two rejections are plain-text
HTTP-level responses rather than the JSON error envelope: they are produced by
the byte-range layer below the application, and a media client reading them
wants the RFC 9110 status and ``Content-Range``, not a JSON body. Every
*application* error on these routes uses the envelope.
"""

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from straticate.api.audio import SettingsDep
from straticate.api.job_outputs import completed_job, stem_source_path
from straticate.jobs import JobManager, get_job_manager
from straticate.schemas import SeparationResult

router = APIRouter(prefix="/jobs", tags=["results"])

ManagerDep = Annotated[JobManager, Depends(get_job_manager)]

STEM_MEDIA_TYPES: dict[str, str] = {
    ".wav": "audio/wav",
    ".flac": "audio/flac",
}
"""Media type served for a stem file, by lower-cased suffix.

The separator writes 16-bit WAV today (feature 014) and feature 022 adds other
formats; the served type follows the file rather than a constant in the
handler, so a new output format only needs an entry here.
"""

DEFAULT_STEM_MEDIA_TYPE = "application/octet-stream"
"""Fallback for a stem file whose suffix is not in :data:`STEM_MEDIA_TYPES`."""

_STEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Stem audio (whole file, or the requested byte range).",
        "content": {
            media_type: {"schema": {"type": "string", "format": "binary"}}
            for media_type in STEM_MEDIA_TYPES.values()
        },
    },
    206: {"description": "Partial content: the requested `Range` of the stem."},
    416: {"description": "The requested `Range` is not satisfiable."},
}


def stem_media_type(path: Path) -> str:
    """Return the media type to serve ``path`` with, derived from its suffix."""
    return STEM_MEDIA_TYPES.get(path.suffix.lower(), DEFAULT_STEM_MEDIA_TYPE)


@router.get("/{job_id}/result")
async def get_job_result(job_id: str, manager: ManagerDep) -> SeparationResult:
    """Fetch the :class:`SeparationResult` of a completed job.

    Errors (see ``docs/contracts/rest-api.md``): ``job_not_found`` (404),
    ``result_not_available`` (409, with the job's current ``state`` in
    ``detail``).
    """
    _job, result = completed_job(manager, job_id)
    return result


@router.get("/{job_id}/stems/{stem_name}", response_class=FileResponse, responses=_STEM_RESPONSES)
async def get_job_stem(
    job_id: str,
    stem_name: str,
    manager: ManagerDep,
    settings: SettingsDep,
) -> FileResponse:
    """Stream one stem of a completed job, with byte ``Range`` support.

    The response advertises ``Accept-Ranges: bytes`` and carries an ``ETag``
    and ``Last-Modified``, so an ``<audio>`` element or a Web Audio fetch can
    seek by requesting ``Range: bytes=start-end`` and receive ``206`` with the
    exact slice. ``Content-Disposition`` is ``inline`` so browsers play the
    stem rather than downloading it; downloading is feature 022's export.

    Errors: ``job_not_found`` (404), ``result_not_available`` (409),
    ``stem_not_found`` (404) when the job's result lists no such stem, and
    ``stem_file_missing`` (404) when the listed stem's file is gone from disk.
    """
    job, result = completed_job(manager, job_id)
    available = [stem.name for stem in result.stems]
    path = stem_source_path(settings.data_dir, job.id, stem_name, available)
    return FileResponse(
        path,
        media_type=stem_media_type(path),
        filename=path.name,
        content_disposition_type="inline",
    )
