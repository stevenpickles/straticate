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

import asyncio
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from starlette.types import Message, Receive, Scope, Send

from straticate.api.audio import SettingsDep
from straticate.api.job_outputs import completed_job, stem_file_missing, stem_source
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


class StemFileResponse(FileResponse):
    """A :class:`FileResponse` whose vanished file is the documented 404.

    Two things go wrong with a plain ``FileResponse`` on this route, and both
    are the same time-of-check/time-of-use gap: the handler proves the file
    exists, and the response reads it some moments later. A job directory
    "can be removed underneath a live job" (see
    :mod:`straticate.api.job_outputs`), so those moments are not theoretical.

    - Without ``stat_result``, ``FileResponse`` re-``stat``s the path itself
      and raises ``RuntimeError`` if it is gone. The handler therefore passes
      the ``stat_result`` it already has, which removes the second ``stat``
      entirely.
    - ``FileResponse`` then sends its headers **before** opening the file, so a
      file that vanished after the handler checked it produces a
      ``FileNotFoundError`` with the ``200`` already on the wire — nothing left
      to convert. This class therefore opens the file *first*, while a proper
      response can still be chosen, and only then delegates.

    That leaves one irreducible window — between this open and Starlette's own,
    microseconds later — and one irrecoverable case: a file lost part-way
    through streaming, when the status line is long gone. Both are re-raised
    honestly rather than papered over.

    Raising an :class:`~straticate.errors.ApplicationError` from here works
    because a response is sent *inside* the route's exception-handling wrapper:
    the registered handler turns it into the standard envelope.
    """

    def __init__(self, path: Path, *, job_id: str, stem_name: str, **kwargs: Any) -> None:
        super().__init__(path, **kwargs)
        self._job_id = job_id
        self._stem_name = stem_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the file, mapping a pre-send filesystem failure onto a 404."""
        started = False

        async def watched_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            (await asyncio.to_thread(open, self.path, "rb")).close()
            await super().__call__(scope, receive, watched_send)
        except (OSError, RuntimeError) as exc:
            if started:
                raise
            raise stem_file_missing(self._job_id, self._stem_name) from exc


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
    ``stem_file_missing`` (404) when the listed stem's file is gone from disk —
    including when it disappears *between* the check and the send, which is
    what :class:`StemFileResponse` and the passed-through ``stat_result``
    exist for.
    """
    job, result = completed_job(manager, job_id)
    available = [stem.name for stem in result.stems]
    path, info = stem_source(settings.data_dir, job.id, stem_name, available)
    return StemFileResponse(
        path,
        job_id=job.id,
        stem_name=stem_name,
        stat_result=info,
        media_type=stem_media_type(path),
        filename=path.name,
        content_disposition_type="inline",
    )
