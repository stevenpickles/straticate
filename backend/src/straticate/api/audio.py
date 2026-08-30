"""Audio endpoints: multipart upload, fetch, and delete.

The upload pipeline is: stream the multipart body to disk in chunks while
enforcing ``Settings.max_upload_bytes`` → probe the stored bytes with
ffprobe → register the :class:`~straticate.schemas.AudioFile` record.
Rejected uploads never leave files behind.
"""

from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, UploadFile

from straticate.audio import (
    AudioAnalysisError,
    AudioProbeError,
    AudioStore,
    StereoAnalysisCache,
    analyse_stereo,
    probe_audio,
)
from straticate.audio.ffmpeg import FFmpegTimeout
from straticate.config import Settings
from straticate.errors import ApplicationError
from straticate.schemas import AudioFile, StereoAnalysis

_CHUNK_SIZE = 1024 * 1024
"""Upload read granularity; also bounds how far past the limit we read."""

router = APIRouter(prefix="/audio", tags=["audio"])


def get_audio_store(request: Request) -> AudioStore:
    """Dependency accessor for the application's :class:`AudioStore`."""
    return cast(AudioStore, request.app.state.audio_store)


def get_app_settings(request: Request) -> Settings:
    """Dependency accessor for the settings the app was created with."""
    return cast(Settings, request.app.state.settings)


def get_analysis_cache(request: Request) -> StereoAnalysisCache:
    """Dependency accessor for the application's :class:`StereoAnalysisCache`."""
    return cast(StereoAnalysisCache, request.app.state.stereo_analysis_cache)


StoreDep = Annotated[AudioStore, Depends(get_audio_store)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
AnalysisCacheDep = Annotated[StereoAnalysisCache, Depends(get_analysis_cache)]


def _not_found(audio_id: str) -> ApplicationError:
    """Build the standard 404 for an unknown audio ID."""
    return ApplicationError(
        "audio_not_found",
        f"No uploaded audio with ID {audio_id!r}.",
        status_code=404,
    )


async def _save_upload(file: UploadFile, store: AudioStore, audio_id: str, limit: int) -> int:
    """Stream ``file`` to the store's directory for ``audio_id``.

    Returns the total size in bytes.

    Raises:
        ApplicationError: ``audio_too_large`` (413) as soon as the stream
            exceeds ``limit``; the partial file is left for the caller's
            cleanup handler to remove.
    """
    destination = store.prepare_original_path(audio_id, file.filename or "")
    size = 0
    with destination.open("wb") as out:
        while chunk := await file.read(_CHUNK_SIZE):
            size += len(chunk)
            if size > limit:
                raise ApplicationError(
                    "audio_too_large",
                    "The uploaded file exceeds the maximum allowed size.",
                    status_code=413,
                    detail={"max_upload_bytes": limit},
                )
            out.write(chunk)
    return size


@router.post("", status_code=201)
async def upload_audio(file: UploadFile, store: StoreDep, settings: SettingsDep) -> AudioFile:
    """Accept a multipart audio upload; validate, store, probe, register.

    Returns 201 with the registered :class:`AudioFile`. Errors:
    ``audio_too_large`` (413) when the body exceeds
    ``Settings.max_upload_bytes``; ``audio_not_decodable`` (422) when
    ffprobe cannot decode the bytes as audio (the extension is never
    trusted); ``audio_probe_timed_out`` (504) when ffprobe exceeds
    ``Settings.ffmpeg_timeout_seconds``.

    The last two are deliberately distinct. ``audio_not_decodable`` tells the
    user their file is the problem and re-uploading it will not help; a probe
    that ran out of time says nothing about the file, and retrying is exactly
    the right response.
    """
    audio_id = store.new_id()
    try:
        size = await _save_upload(file, store, audio_id, settings.max_upload_bytes)
        metadata = await probe_audio(
            store.original_path(audio_id, file.filename or ""),
            timeout_seconds=settings.ffmpeg_timeout_seconds,
        )
    except AudioProbeError as exc:
        store.remove_files(audio_id)
        raise ApplicationError(
            "audio_not_decodable",
            "The uploaded file could not be decoded as audio.",
            status_code=422,
        ) from exc
    except FFmpegTimeout as exc:
        store.remove_files(audio_id)
        raise ApplicationError(
            "audio_probe_timed_out",
            "Reading the uploaded file's metadata timed out.",
            status_code=504,
            detail={"timeout_seconds": exc.timeout_seconds},
        ) from exc
    except BaseException:
        store.remove_files(audio_id)
        raise

    record = AudioFile(
        id=audio_id,
        filename=file.filename or "upload",
        size_bytes=size,
        uploaded_at=datetime.now(UTC),
        metadata=metadata,
    )
    try:
        store.register(record)
    except BaseException:
        # A failed sidecar write (disk full, permissions) must not leave a
        # half-registered upload: the client is getting a 500, so the files
        # go too, and the "rejected uploads never leave files behind"
        # guarantee holds for this failure the way it does for a bad probe.
        store.remove_files(audio_id)
        raise
    return record


@router.get("/{audio_id}")
async def get_audio(audio_id: str, store: StoreDep) -> AudioFile:
    """Fetch one uploaded audio record; 404 ``audio_not_found`` if unknown."""
    record = store.get(audio_id)
    if record is None:
        raise _not_found(audio_id)
    return record


@router.get("/{audio_id}/analysis")
async def get_audio_analysis(
    audio_id: str, store: StoreDep, settings: SettingsDep, cache: AnalysisCacheDep
) -> StereoAnalysis:
    """Measure an upload's stereo image (feature 063).

    Returns the full-band L/R correlation of the whole track and the derived
    ``wide_stereo`` flag — see :class:`~straticate.schemas.StereoAnalysis` and
    :mod:`straticate.audio.analysis`, which holds the threshold, its provenance
    and the streaming discipline.

    **It measures and never applies.** No job configuration is derived from the
    answer on either side of the wire, and this route is not reachable from the
    separation path at all: an upload separates identically whether or not
    anyone ever asks for it.

    The measurement runs on the **first** request and is then cached in-process
    for the audio's lifetime; concurrent first requests share one computation
    rather than starting a second decode of the same file, and
    ``DELETE /audio/{audio_id}`` drops the entry. That first request is held
    open for the length of the pass — about a second for a three-minute track —
    which is acceptable precisely because this endpoint **gates nothing**: it is
    an enrichment, and every control works while it is outstanding.

    Errors: ``audio_not_found`` (404) when the ID is unknown *or* its file is
    gone from disk; ``audio_not_decodable`` (422) when FFmpeg cannot decode
    bytes that ffprobe accepted at upload; ``audio_analysis_timed_out`` (504)
    when the decode exceeds ``Settings.ffmpeg_timeout_seconds``. The last two are
    distinct for the reason :func:`upload_audio` gives: a tool that ran out of
    time has said nothing about the file, and retrying is the right response.
    """
    record = store.get(audio_id)
    if record is None:
        raise _not_found(audio_id)
    path = store.original_path(audio_id, record.filename)
    if not path.is_file():
        raise _not_found(audio_id)

    async def compute() -> StereoAnalysis:
        return await analyse_stereo(
            path, record.metadata, timeout_seconds=settings.ffmpeg_timeout_seconds
        )

    try:
        return await cache.get(audio_id, compute)
    except AudioAnalysisError as exc:
        raise ApplicationError(
            "audio_not_decodable",
            "The uploaded file could not be decoded as audio.",
            status_code=422,
        ) from exc
    except FFmpegTimeout as exc:
        raise ApplicationError(
            "audio_analysis_timed_out",
            "Measuring the uploaded file's stereo image timed out.",
            status_code=504,
            detail={"timeout_seconds": exc.timeout_seconds},
        ) from exc


@router.delete("/{audio_id}", status_code=204)
async def delete_audio(audio_id: str, store: StoreDep, cache: AnalysisCacheDep) -> None:
    """Delete an uploaded audio record and its files; 404 if unknown.

    Also drops any cached stereo analysis: the ID is the cache key, and a future
    upload cannot reuse it (they are ULIDs), so a surviving entry would be dead
    weight that outlived the bytes it described.
    """
    if not store.delete(audio_id):
        raise _not_found(audio_id)
    cache.discard(audio_id)
