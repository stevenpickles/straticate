"""Tests for ``GET /api/v1/jobs/{id}/export``.

Like ``test_api_results.py`` and ``test_api_jobs.py`` these run the **real**
application: a real job manager started by the application lifespan on the
test's own event loop, a real event hub, and a real ``FakeSeparator`` writing
real 16-bit WAV stems — only its simulated delays are zeroed, by injecting a
:class:`SeparatorRegistry` onto ``app.state``. Every wait is gated with an
:class:`asyncio.Event` (or, where a worker thread must be observed, a
:class:`threading.Event` awaited through :func:`asyncio.to_thread`); no sleep
is ever used as synchronization.

The transcodes are real: CI installs FFmpeg, the fixtures are a fraction of a
second long, and what comes back is verified with **ffprobe** rather than
trusted.
"""

import asyncio
import io
import json
import subprocess
import threading
import zipfile
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, cast

import httpx2
import pytest
from fastapi import FastAPI

from straticate.api import export as export_module
from straticate.api.export import (
    EXPORTS_DIRECTORY,
    MANIFEST_NAME,
    ZIP_MEDIA_TYPE,
    ExportFormat,
    artifact_name,
)
from straticate.audio import ffmpeg as ffmpeg_module
from straticate.audio import probe_audio
from straticate.audio.ffmpeg import FFmpegTimeout
from straticate.config import Settings
from straticate.errors import ApplicationError
from straticate.inference import (
    FAKE_ARCHITECTURE,
    SeparationProgress,
    SeparatorInfo,
    SeparatorRegistry,
    fake_separator_builder,
    job_output_dir,
    separator_info_from_model,
    stem_path,
)
from straticate.jobs import CancellationToken, JobEvent, JobManager
from straticate.main import create_app
from straticate.schemas import AudioFile, AudioMetadata, ComputeDevice, Model
from straticate.schemas.events import JobCancelledEvent, JobCompletedEvent, JobFailedEvent
from straticate.schemas.jobs import (
    JobState,
    SeparationConfiguration,
    SeparationResult,
    SeparationResultMetrics,
    Stem,
)
from straticate.system import CUDA_BACKEND, DeviceDetector
from tests.audio_fixtures import write_tone_wav

JOBS_URL = "/api/v1/jobs"
HEALTH_URL = "/api/v1/health"
WAIT_TIMEOUT = 30.0

VOCALS_MODEL_ID = "fake-vocals-001"
STANDARD_MODEL_ID = "fake-standard-001"
VOCALS_STEMS = ["vocals", "instrumental"]
STANDARD_STEMS = ["vocals", "drums", "bass", "other"]

SOURCE_SECONDS = 0.5
SOURCE_SAMPLE_RATE = 44100
SOURCE_CHANNELS = 2

FAKE_GPU = ComputeDevice(
    id="cuda:0",
    backend=CUDA_BACKEND,
    name="NVIDIA GeForce RTX 5090",
    memory_total_bytes=34359738368,
)

#: ``(format, ffprobe codec, expected bit depth, file suffix, media type)``.
FORMAT_MATRIX = [
    (ExportFormat.WAV_PCM24, "pcm_s24le", 24, ".wav", "audio/wav"),
    (ExportFormat.WAV_FLOAT32, "pcm_f32le", 32, ".wav", "audio/wav"),
    (ExportFormat.FLAC, "flac", 16, ".flac", "audio/flac"),
]


class StaticProbe:
    """Probe reporting one fixed device, so device resolution is deterministic."""

    backend: str = CUDA_BACKEND

    def detect(self) -> list[ComputeDevice]:
        return [FAKE_GPU]


class StageGatedSeparator:
    """A separator that parks the job in one chosen state until released.

    Lifted from ``test_api_results.py``: it performs no audio work, announces
    ``stage`` (or nothing, leaving the job in ``preparing``), signals
    ``started`` and waits on ``gate``. That is how the 409 tests reach a
    non-completed state through the real machinery instead of poking at the
    manager's internals. ``fail`` makes it raise instead.
    """

    def __init__(
        self,
        info: SeparatorInfo,
        *,
        started: asyncio.Event,
        gate: asyncio.Event,
        stage: JobState | None = None,
        fail: bool = False,
    ) -> None:
        self._info = info
        self.started = started
        self.gate = gate
        self.stage = stage
        self.fail = fail

    @property
    def info(self) -> SeparatorInfo:
        return self._info

    def runtime_stats(self) -> None:
        return None

    async def separate(
        self,
        input_path: Path,
        configuration: SeparationConfiguration,
        progress_callback: Callable[[SeparationProgress], None],
        cancellation_token: CancellationToken,
        *,
        job_id: str,
        output_dir: Path,
        stage_callback: Callable[[JobState], None] | None = None,
    ) -> SeparationResult:
        if self.stage is not None and stage_callback is not None:
            stage_callback(self.stage)
        self.started.set()
        await self.gate.wait()
        cancellation_token.raise_if_cancelled()
        if self.fail:
            raise ApplicationError("separation_failed", "boom.", status_code=500)
        progress_callback(SeparationProgress(1, 1, 1.0, 1.0))
        return SeparationResult(
            job_id=job_id,
            model_id=self._info.model_id,
            stems=[
                Stem(
                    name=stem,
                    duration_seconds=1.0,
                    sample_rate_hz=self._info.sample_rate,
                    channels=2,
                )
                for stem in self._info.stems
            ],
            metrics=SeparationResultMetrics(processing_seconds=1.0, realtime_factor=1.0),
        )


class EventRecorder:
    """Sync manager listener that records events and lets tests await them."""

    def __init__(self) -> None:
        self.events: list[JobEvent] = []
        self._changed = asyncio.Event()

    def __call__(self, event: JobEvent) -> None:
        self.events.append(event)
        self._changed.set()

    async def wait_for(self, predicate: Callable[[JobEvent], bool]) -> JobEvent:
        index = 0
        while True:
            while index < len(self.events):
                event = self.events[index]
                index += 1
                if predicate(event):
                    return event
            self._changed.clear()
            await asyncio.wait_for(self._changed.wait(), timeout=WAIT_TIMEOUT)

    async def wait_for_terminal(self, job_id: str) -> JobEvent:
        return await self.wait_for(
            lambda event: (
                event.job_id == job_id
                and isinstance(event, JobCompletedEvent | JobCancelledEvent | JobFailedEvent)
            )
        )


class TranscodeSpy:
    """Counts (and optionally blocks) the blocking FFmpeg call.

    ``calls`` proves the cache actually avoids re-transcoding. ``block``
    parks the *worker thread* inside the transcode on a
    :class:`threading.Event`, which is what makes the event-loop-liveness and
    concurrency tests meaningful: if the transcode ran on the loop, nothing
    else could make progress while it is parked.

    ``block_after`` parks **after** the real FFmpeg call instead of before it,
    so the output file genuinely exists on disk while the thread is held. That
    is what the cancellation test needs: the leak it guards against is a real
    ``.part`` file, and one that was never written cannot be leaked.
    """

    def __init__(
        self, inner: Callable[..., None], *, block: bool = False, block_after: bool = False
    ) -> None:
        self._inner = inner
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self._block = block or block_after
        self._block_after = block_after

    def _park(self) -> None:
        self.entered.set()
        assert self.release.wait(timeout=WAIT_TIMEOUT), "transcode never released"

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        if self._block and not self._block_after:
            self._park()
        self._inner(*args, **kwargs)
        if self._block_after:
            self._park()


# -- fixtures ---------------------------------------------------------------


def fast_registry() -> SeparatorRegistry:
    """The real fake separator with every simulated delay removed."""
    return SeparatorRegistry(
        {
            FAKE_ARCHITECTURE: fake_separator_builder(
                chunk_seconds=0.2,
                chunk_delay_seconds=0.0,
                model_load_seconds=0.0,
            )
        }
    )


def gated_registry(
    started: asyncio.Event,
    gate: asyncio.Event,
    *,
    stage: JobState | None = None,
    fail: bool = False,
) -> SeparatorRegistry:
    """A registry whose fake-architecture models get a gated scripted separator."""

    def build(model: Model) -> StageGatedSeparator:
        return StageGatedSeparator(
            separator_info_from_model(model),
            started=started,
            gate=gate,
            stage=stage,
            fail=fail,
        )

    return SeparatorRegistry({FAKE_ARCHITECTURE: build})


def build_app(data_dir: Path, **overrides: Any) -> FastAPI:
    """An application isolated to ``data_dir``, with deterministic devices."""
    app = create_app(Settings(data_dir=data_dir, **overrides))
    app.state.device_detector = DeviceDetector(probes=[StaticProbe()])
    app.state.separator_registry = fast_registry()
    return app


def register_audio(app: FastAPI, *, seconds: float = SOURCE_SECONDS) -> str:
    """Write a real tone WAV into the app's audio store and register it."""
    store = app.state.audio_store
    audio_id = cast(str, store.new_id())
    path = cast(Path, store.prepare_original_path(audio_id, "song.wav"))
    write_tone_wav(path, seconds=seconds, channels=SOURCE_CHANNELS, sample_rate=SOURCE_SAMPLE_RATE)
    store.register(
        AudioFile(
            id=audio_id,
            filename="song.wav",
            size_bytes=path.stat().st_size,
            uploaded_at=datetime.now(UTC),
            metadata=AudioMetadata(
                duration_seconds=seconds,
                container="wav",
                codec="pcm_s16le",
                channels=SOURCE_CHANNELS,
                sample_rate_hz=SOURCE_SAMPLE_RATE,
                bit_depth=16,
                bit_rate_bps=1411000,
            ),
        )
    )
    return audio_id


@pytest.fixture
def export_app(tmp_path: Path) -> FastAPI:
    return build_app(tmp_path)


@pytest.fixture
async def export_client(export_app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    """A client for a running application (lifespan started on this loop)."""
    async with export_app.router.lifespan_context(export_app):
        transport = httpx2.ASGITransport(app=export_app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
def audio_id(export_app: FastAPI) -> str:
    return register_audio(export_app)


@pytest.fixture
async def recorder(
    export_client: httpx2.AsyncClient, export_app: FastAPI
) -> AsyncIterator[EventRecorder]:
    """A listener on the running app's job manager (needs the lifespan)."""
    listener = EventRecorder()
    manager = cast(JobManager, export_app.state.job_manager)
    manager.add_listener(listener)
    yield listener
    manager.remove_listener(listener)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[TranscodeSpy]:
    """Wrap the blocking transcode so tests can count its invocations."""
    counter = TranscodeSpy(export_module.transcode_sync)
    monkeypatch.setattr(export_module, "transcode_sync", counter)
    yield counter


# -- helpers ----------------------------------------------------------------


def configuration(audio_id: str, **overrides: Any) -> dict[str, Any]:
    """A create-job request body."""
    body: dict[str, Any] = {
        "audio_id": audio_id,
        "mode_id": "vocals",
        "quality_id": "balanced",
    }
    body.update(overrides)
    return body


async def create_job(client: httpx2.AsyncClient, **body: Any) -> str:
    response = await client.post(JOBS_URL, json=body)
    assert response.status_code == 201, response.text
    return cast(str, response.json()["id"])


async def run_to_completion(
    client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str, mode_id: str = "vocals"
) -> str:
    """Create a job with the real fake separator and await its completion."""
    job_id = await create_job(client, **configuration(audio_id, mode_id=mode_id))
    terminal = await recorder.wait_for_terminal(job_id)
    assert isinstance(terminal, JobCompletedEvent), terminal
    return job_id


def export_url(job_id: str) -> str:
    return f"{JOBS_URL}/{job_id}/export"


async def export(
    client: httpx2.AsyncClient,
    job_id: str,
    *,
    export_format: ExportFormat | str | None = None,
    stems: str | None = None,
) -> httpx2.Response:
    params: dict[str, str] = {}
    if export_format is not None:
        params["format"] = str(export_format)
    if stems is not None:
        params["stems"] = stems
    return await client.get(export_url(job_id), params=params)


def assert_envelope(response: httpx2.Response, code: str, status: int) -> dict[str, Any]:
    """Assert the standard error envelope and return the error object."""
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}
    error = cast(dict[str, Any], body["error"])
    assert set(error) == {"code", "message", "detail"}
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    return error


def zip_entries(response: httpx2.Response) -> dict[str, bytes]:
    """Read the response body as a zip and return ``{entry name: bytes}``."""
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None, "the archive is corrupt"
        return {name: archive.read(name) for name in archive.namelist()}


def disposition_filename(response: httpx2.Response) -> str:
    """Extract the ``filename`` from the response's ``Content-Disposition``."""
    header = response.headers["content-disposition"]
    assert header.startswith("attachment;"), header
    _, _, filename = header.partition('filename="')
    return filename.rstrip('"')


async def assert_audio(
    payload: bytes, tmp_path: Path, *, codec: str, bit_depth: int, name: str = "probe"
) -> None:
    """Write ``payload`` to disk and verify it with ffprobe.

    Sample rate, channel count and duration must match the source stems the
    separator wrote; only the encoding changes.
    """
    path = tmp_path / name
    path.write_bytes(payload)
    metadata = await probe_audio(path, timeout_seconds=30.0)
    assert metadata.codec == codec
    assert metadata.bit_depth == bit_depth
    assert metadata.sample_rate_hz == SOURCE_SAMPLE_RATE
    assert metadata.channels == SOURCE_CHANNELS
    assert metadata.duration_seconds == pytest.approx(SOURCE_SECONDS, abs=0.05)


# -- formats ----------------------------------------------------------------


@pytest.mark.parametrize(("fmt", "codec", "bit_depth", "suffix", "media_type"), FORMAT_MATRIX)
async def test_every_format_produces_audio_a_decoder_accepts(
    export_client: httpx2.AsyncClient,
    recorder: EventRecorder,
    audio_id: str,
    tmp_path: Path,
    fmt: ExportFormat,
    codec: str,
    bit_depth: int,
    suffix: str,
    media_type: str,
) -> None:
    """ffprobe, not trust: the bytes really are 24-bit / float32 / FLAC."""
    job_id = await run_to_completion(export_client, recorder, audio_id)

    response = await export(export_client, job_id, export_format=fmt)
    assert response.status_code == 200, response.text
    entries = zip_entries(response)

    for stem in VOCALS_STEMS:
        await assert_audio(
            entries[f"{stem}{suffix}"],
            tmp_path,
            codec=codec,
            bit_depth=bit_depth,
            name=f"{fmt.value}-{stem}{suffix}",
        )


@pytest.mark.parametrize(("fmt", "codec", "bit_depth", "suffix", "media_type"), FORMAT_MATRIX)
async def test_single_stem_export_is_the_bare_audio_file(
    export_client: httpx2.AsyncClient,
    recorder: EventRecorder,
    audio_id: str,
    tmp_path: Path,
    fmt: ExportFormat,
    codec: str,
    bit_depth: int,
    suffix: str,
    media_type: str,
) -> None:
    """One stem → the file itself: right media type, attachment, no manifest."""
    job_id = await run_to_completion(export_client, recorder, audio_id)

    response = await export(export_client, job_id, export_format=fmt, stems="vocals")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == media_type
    assert disposition_filename(response) == f"{job_id}-{fmt.value}-vocals{suffix}"
    assert not response.content.startswith(b"PK"), "a single stem must not be a zip"

    await assert_audio(
        response.content, tmp_path, codec=codec, bit_depth=bit_depth, name=f"single{suffix}"
    )


async def test_the_default_format_is_wav_pcm24(
    export_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str, tmp_path: Path
) -> None:
    job_id = await run_to_completion(export_client, recorder, audio_id)

    response = await export(export_client, job_id, stems="vocals")
    assert response.status_code == 200, response.text
    assert disposition_filename(response) == f"{job_id}-wav_pcm24-vocals.wav"
    await assert_audio(response.content, tmp_path, codec="pcm_s24le", bit_depth=24, name="d.wav")


async def test_an_unknown_format_is_a_validation_error(
    export_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    """No invented code: an unknown enum value is FastAPI's standard 422."""
    job_id = await run_to_completion(export_client, recorder, audio_id)

    error = assert_envelope(
        await export(export_client, job_id, export_format="wav_pcm32"), "validation_error", 422
    )
    assert error["detail"]["errors"], error


# -- single vs. multiple ----------------------------------------------------


@pytest.mark.parametrize(
    ("mode_id", "stems"),
    [("vocals", VOCALS_STEMS), ("standard_stems", STANDARD_STEMS)],
)
async def test_omitting_stems_exports_every_stem_of_the_result(
    export_client: httpx2.AsyncClient,
    recorder: EventRecorder,
    audio_id: str,
    mode_id: str,
    stems: list[str],
) -> None:
    """Two-stem and four-stem jobs behave identically; nothing is hardcoded."""
    job_id = await run_to_completion(export_client, recorder, audio_id, mode_id)

    response = await export(export_client, job_id)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == ZIP_MEDIA_TYPE
    assert disposition_filename(response) == f"{job_id}-wav_pcm24.zip"

    entries = zip_entries(response)
    assert set(entries) == {f"{stem}.wav" for stem in stems} | {MANIFEST_NAME}
    assert len(entries) == len(stems) + 1


async def test_a_subset_of_more_than_one_stem_is_a_zip_of_exactly_those(
    export_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    job_id = await run_to_completion(export_client, recorder, audio_id, "standard_stems")

    response = await export(export_client, job_id, stems="drums,bass")
    assert response.status_code == 200, response.text
    assert set(zip_entries(response)) == {"drums.wav", "bass.wav", MANIFEST_NAME}


async def test_the_stems_in_the_archive_are_mutually_distinct(
    export_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    """Four real stems, four different files — the export is per-stem work."""
    job_id = await run_to_completion(export_client, recorder, audio_id, "standard_stems")

    entries = zip_entries(await export(export_client, job_id))
    audio = [entries[f"{stem}.wav"] for stem in STANDARD_STEMS]
    assert len({bytes(item) for item in audio}) == len(STANDARD_STEMS)


async def test_stem_order_and_duplicates_do_not_change_the_export(
    export_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    """The selection is a set, resolved into the result's own order."""
    job_id = await run_to_completion(export_client, recorder, audio_id, "standard_stems")

    forward = await export(export_client, job_id, stems="drums,bass")
    reversed_ = await export(export_client, job_id, stems="bass,drums,bass")
    assert set(zip_entries(forward)) == set(zip_entries(reversed_))
    assert forward.content == reversed_.content


async def test_a_repeated_single_stem_selection_is_still_a_single_file(
    export_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    """``vocals,vocals`` is one stem, so it is the bare file, not an archive."""
    job_id = await run_to_completion(export_client, recorder, audio_id)

    response = await export(export_client, job_id, stems="vocals,vocals")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "audio/wav"
    assert not response.content.startswith(b"PK")


async def test_whitespace_around_stem_names_is_tolerated(
    export_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    job_id = await run_to_completion(export_client, recorder, audio_id)

    response = await export(export_client, job_id, stems=" vocals , instrumental ")
    assert response.status_code == 200, response.text
    assert set(zip_entries(response)) == {"vocals.wav", "instrumental.wav", MANIFEST_NAME}


async def test_a_single_stem_export_carries_no_manifest(
    export_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    """A documented choice, asserted so it cannot regress silently."""
    job_id = await run_to_completion(export_client, recorder, audio_id)

    response = await export(export_client, job_id, stems="vocals")
    assert MANIFEST_NAME.encode() not in response.content


# -- separation.json --------------------------------------------------------


async def test_the_manifest_embeds_the_separation_result_verbatim(
    export_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    job_id = await run_to_completion(export_client, recorder, audio_id, "standard_stems")

    entries = zip_entries(await export(export_client, job_id, export_format=ExportFormat.FLAC))
    manifest: dict[str, Any] = json.loads(entries[MANIFEST_NAME])

    assert set(manifest) == {"format", "model_id", "stems", "exported_at", "result"}
    assert manifest["format"] == ExportFormat.FLAC.value
    assert manifest["model_id"] == STANDARD_MODEL_ID
    assert manifest["stems"] == STANDARD_STEMS

    served = (await export_client.get(f"{JOBS_URL}/{job_id}/result")).json()
    assert manifest["result"] == served

    parsed = SeparationResult.model_validate(manifest["result"])
    assert parsed.job_id == job_id
    assert [stem.name for stem in parsed.stems] == STANDARD_STEMS


async def test_the_manifest_exported_at_is_timezone_aware(
    export_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    job_id = await run_to_completion(export_client, recorder, audio_id)

    entries = zip_entries(await export(export_client, job_id))
    manifest: dict[str, Any] = json.loads(entries[MANIFEST_NAME])
    exported_at = datetime.fromisoformat(cast(str, manifest["exported_at"]))
    assert exported_at.tzinfo is not None


async def test_the_manifest_lists_only_the_exported_stems(
    export_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    """``stems`` is what is in the archive; ``result.stems`` is the whole job."""
    job_id = await run_to_completion(export_client, recorder, audio_id, "standard_stems")

    entries = zip_entries(await export(export_client, job_id, stems="bass,other"))
    manifest: dict[str, Any] = json.loads(entries[MANIFEST_NAME])
    assert manifest["stems"] == ["bass", "other"]
    assert [stem["name"] for stem in manifest["result"]["stems"]] == STANDARD_STEMS


# -- errors -----------------------------------------------------------------


async def test_export_of_an_unknown_job_returns_job_not_found(
    export_client: httpx2.AsyncClient,
) -> None:
    error = assert_envelope(await export(export_client, "01NOTAJOB"), "job_not_found", 404)
    assert error["detail"] == {"job_id": "01NOTAJOB"}


@pytest.mark.parametrize(
    ("stage", "expected_state"),
    [
        (None, JobState.PREPARING),
        (JobState.DECODING, JobState.DECODING),
        (JobState.SEPARATING, JobState.SEPARATING),
        (JobState.ENCODING, JobState.ENCODING),
    ],
)
async def test_export_of_a_running_job_returns_result_not_available(
    export_client: httpx2.AsyncClient,
    export_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
    stage: JobState | None,
    expected_state: JobState,
) -> None:
    started, gate = asyncio.Event(), asyncio.Event()
    export_app.state.separator_registry = gated_registry(started, gate, stage=stage)

    job_id = await create_job(export_client, **configuration(audio_id))
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

    error = assert_envelope(await export(export_client, job_id), "result_not_available", 409)
    assert error["detail"] == {"job_id": job_id, "state": expected_state.value}

    gate.set()
    await recorder.wait_for_terminal(job_id)


async def test_export_of_a_cancelled_job_returns_result_not_available(
    export_client: httpx2.AsyncClient,
    export_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
) -> None:
    started, gate = asyncio.Event(), asyncio.Event()
    export_app.state.separator_registry = gated_registry(started, gate)

    running = await create_job(export_client, **configuration(audio_id))
    queued = await create_job(export_client, **configuration(audio_id))
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

    cancel = await export_client.post(f"{JOBS_URL}/{queued}/cancel")
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["state"] == "cancelled"

    error = assert_envelope(await export(export_client, queued), "result_not_available", 409)
    assert error["detail"] == {"job_id": queued, "state": "cancelled"}

    gate.set()
    await recorder.wait_for_terminal(running)


async def test_export_of_a_failed_job_returns_result_not_available(
    export_client: httpx2.AsyncClient,
    export_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
) -> None:
    started, gate = asyncio.Event(), asyncio.Event()
    export_app.state.separator_registry = gated_registry(started, gate, fail=True)

    job_id = await create_job(export_client, **configuration(audio_id))
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)
    gate.set()
    terminal = await recorder.wait_for_terminal(job_id)
    assert isinstance(terminal, JobFailedEvent), terminal

    error = assert_envelope(await export(export_client, job_id), "result_not_available", 409)
    assert error["detail"] == {"job_id": job_id, "state": "failed"}


async def test_an_unknown_stem_returns_stem_not_found(
    export_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    job_id = await run_to_completion(export_client, recorder, audio_id)

    error = assert_envelope(
        await export(export_client, job_id, stems="vocals,piano"), "stem_not_found", 404
    )
    assert error["detail"] == {
        "job_id": job_id,
        "stem": "piano",
        "available_stems": VOCALS_STEMS,
    }


@pytest.mark.parametrize(
    "stems",
    [
        "../secret",
        "..%2Fsecret",
        "../../etc/passwd",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "vocals/../../../secret",
        "vocals,../secret",
        ".",
        "..",
        "vocals\x00",
        "%2e%2e%2fsecret",
        "vocals,",
        ",",
    ],
)
async def test_traversal_attempts_are_a_clean_stem_not_found(
    export_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str, stems: str
) -> None:
    """The result is the only authority, so these are simply not stem names."""
    job_id = await run_to_completion(export_client, recorder, audio_id)

    error = assert_envelope(await export(export_client, job_id, stems=stems), "stem_not_found", 404)
    assert error["detail"]["available_stems"] == VOCALS_STEMS


@pytest.mark.parametrize("stems", ["", " ", "   ", "\t"])
async def test_a_blank_stems_value_is_a_validation_error(
    export_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str, stems: str
) -> None:
    """Omitting ``stems`` means "all"; supplying nothing is a client bug."""
    job_id = await run_to_completion(export_client, recorder, audio_id)

    error = assert_envelope(
        await export(export_client, job_id, stems=stems), "validation_error", 422
    )
    assert error["detail"]["errors"], error


async def test_a_deleted_stem_file_returns_stem_file_missing(
    export_client: httpx2.AsyncClient,
    export_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
) -> None:
    """The record outlived its files: 021's 404, not a 500 from FFmpeg."""
    job_id = await run_to_completion(export_client, recorder, audio_id)
    data_dir = cast(Settings, export_app.state.settings).data_dir
    stem_path(data_dir, job_id, "vocals").unlink()

    error = assert_envelope(await export(export_client, job_id), "stem_file_missing", 404)
    assert error["detail"] == {"job_id": job_id, "stem": "vocals"}

    # Its sibling still exports on its own.
    assert (await export(export_client, job_id, stems="instrumental")).status_code == 200


async def test_an_ffmpeg_failure_is_export_failed(
    export_client: httpx2.AsyncClient,
    recorder: EventRecorder,
    audio_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken encoder is a documented 500, never an unhandled traceback."""
    job_id = await run_to_completion(export_client, recorder, audio_id)

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise export_module.ExportError(export_module.TRANSCODE_FAILED)

    monkeypatch.setattr(export_module, "transcode_sync", explode)

    error = assert_envelope(await export(export_client, job_id), "export_failed", 500)
    assert error["detail"] == {
        "job_id": job_id,
        "format": ExportFormat.WAV_PCM24.value,
        "reason": export_module.TRANSCODE_FAILED,
    }


async def test_an_ffmpeg_timeout_is_export_timed_out(
    export_client: httpx2.AsyncClient,
    export_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged encoder is a 504 of its own, and the request still returns.

    The runner is stubbed, so nothing waits for a real timeout. The code is
    distinct from ``export_failed`` on purpose: that one means the encode was
    attempted and failed, which is a claim about the audio; a timeout is a
    claim about the server, and the client's remedy differs.
    """
    job_id = await run_to_completion(export_client, recorder, audio_id)

    def wedged(command: Sequence[str], **_kwargs: Any) -> NoReturn:
        raise FFmpegTimeout(command[0], 600.0)

    monkeypatch.setattr(export_module, "run_ffmpeg", wedged)

    error = assert_envelope(await export(export_client, job_id), "export_timed_out", 504)
    assert error["detail"] == {"job_id": job_id, "format": ExportFormat.WAV_PCM24.value}

    # A timed-out build publishes nothing: no artifact, no .part file.
    exports_dir = (
        job_output_dir(cast(Settings, export_app.state.settings).data_dir, job_id)
        / EXPORTS_DIRECTORY
    )
    assert not exports_dir.exists() or not any(exports_dir.iterdir())


async def test_the_apps_settings_govern_the_export_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``create_app(Settings(...))`` must reach FFmpeg, not just the environment.

    Every other setting arrives through ``app.state.settings``, and
    ``create_app(settings)`` is a documented path every test fixture uses — so
    reading the process-global settings inside the runner would silently ignore
    the bound this application was built with. Here the real
    ``subprocess.run`` is stubbed (no waiting) and records the bound it was
    handed.
    """
    recorded: list[float] = []

    def record_and_wedge(command: Sequence[str], **kwargs: Any) -> NoReturn:
        recorded.append(cast(float, kwargs["timeout"]))
        raise subprocess.TimeoutExpired(list(command), cast(float, kwargs["timeout"]))

    app = build_app(tmp_path, ffmpeg_timeout_seconds=1.5)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            listener = EventRecorder()
            manager = cast(JobManager, app.state.job_manager)
            manager.add_listener(listener)
            try:
                # The job itself runs real FFmpeg; only the export is stubbed.
                job_id = await run_to_completion(client, listener, register_audio(app))
                monkeypatch.setattr(ffmpeg_module.subprocess, "run", record_and_wedge)
                assert_envelope(await export(client, job_id), "export_timed_out", 504)
            finally:
                manager.remove_listener(listener)

    assert recorded == [1.5], "the export must use the application's own bound"


async def test_a_real_ffmpeg_failure_is_export_failed(
    export_client: httpx2.AsyncClient,
    export_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
) -> None:
    """The real subprocess path: FFmpeg exits non-zero on a truncated stem."""
    job_id = await run_to_completion(export_client, recorder, audio_id)
    data_dir = cast(Settings, export_app.state.settings).data_dir
    stem_path(data_dir, job_id, "vocals").write_bytes(b"not a wav file at all")

    response = await export(export_client, job_id, stems="vocals")
    error = assert_envelope(response, "export_failed", 500)
    assert error["detail"]["reason"] == export_module.TRANSCODE_FAILED


async def test_an_export_failure_never_leaks_server_paths_or_ffmpeg_stderr(
    export_client: httpx2.AsyncClient,
    export_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The diagnostic goes to the log; the client gets a classification.

    FFmpeg's stderr names the absolute path of the file it could not read, and
    no other 500 in this application puts server-side detail on the wire.
    """
    job_id = await run_to_completion(export_client, recorder, audio_id)
    data_dir = cast(Settings, export_app.state.settings).data_dir
    stem_path(data_dir, job_id, "vocals").write_bytes(b"not a wav file at all")

    with caplog.at_level("ERROR", logger="straticate.api.export"):
        response = await export(export_client, job_id, stems="vocals")

    assert response.status_code == 500
    body = response.text
    assert str(data_dir) not in body
    assert "vocals.wav" not in body
    assert "ffmpeg" not in body.lower()
    # The operator, however, gets the whole story.
    assert any("ffmpeg exited" in record.getMessage() for record in caplog.records), caplog.text


async def test_a_failed_export_leaves_no_artifact_behind(
    export_client: httpx2.AsyncClient,
    export_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``.part`` file is removed, so a later request rebuilds cleanly."""
    job_id = await run_to_completion(export_client, recorder, audio_id)
    data_dir = cast(Settings, export_app.state.settings).data_dir

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise export_module.ExportError(export_module.TRANSCODE_FAILED)

    monkeypatch.setattr(export_module, "transcode_sync", explode)
    assert_envelope(await export(export_client, job_id), "export_failed", 500)

    exports_dir = job_output_dir(data_dir, job_id) / EXPORTS_DIRECTORY
    assert list(exports_dir.glob("*.part")) == []

    monkeypatch.undo()
    assert (await export(export_client, job_id)).status_code == 200


# -- caching ----------------------------------------------------------------


async def test_a_repeated_identical_export_serves_the_cached_artifact(
    export_client: httpx2.AsyncClient,
    recorder: EventRecorder,
    audio_id: str,
    spy: TranscodeSpy,
) -> None:
    """Completed stems are immutable, so the second download runs no FFmpeg."""
    job_id = await run_to_completion(export_client, recorder, audio_id, "standard_stems")

    first = await export(export_client, job_id)
    assert first.status_code == 200, first.text
    assert spy.calls == len(STANDARD_STEMS)

    second = await export(export_client, job_id)
    assert second.status_code == 200, second.text
    assert spy.calls == len(STANDARD_STEMS), "the cached artifact was re-transcoded"
    assert second.content == first.content


async def test_the_cache_key_covers_the_format_and_the_selection(
    export_client: httpx2.AsyncClient,
    recorder: EventRecorder,
    audio_id: str,
    spy: TranscodeSpy,
) -> None:
    """A different format or a different stem set is a different artifact."""
    job_id = await run_to_completion(export_client, recorder, audio_id)

    await export(export_client, job_id)
    assert spy.calls == 2
    await export(export_client, job_id, export_format=ExportFormat.FLAC)
    assert spy.calls == 4
    await export(export_client, job_id, stems="vocals")
    assert spy.calls == 5
    # Reordered duplicates of an already-built selection hit the same file.
    await export(export_client, job_id, stems="instrumental,vocals")
    assert spy.calls == 5


async def test_artifacts_land_in_the_jobs_export_directory(
    export_client: httpx2.AsyncClient,
    export_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
) -> None:
    job_id = await run_to_completion(export_client, recorder, audio_id)
    data_dir = cast(Settings, export_app.state.settings).data_dir

    assert (await export(export_client, job_id)).status_code == 200
    assert (await export(export_client, job_id, stems="vocals")).status_code == 200

    exports_dir = job_output_dir(data_dir, job_id) / EXPORTS_DIRECTORY
    names = sorted(path.name for path in exports_dir.iterdir())
    assert names == ["wav_pcm24-instrumental-vocals.zip", "wav_pcm24-vocals.wav"]


async def test_a_stale_part_file_is_never_served(
    export_client: httpx2.AsyncClient,
    export_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
) -> None:
    """A leftover ``.part`` is not the artifact path, so it cannot be served."""
    job_id = await run_to_completion(export_client, recorder, audio_id)
    data_dir = cast(Settings, export_app.state.settings).data_dir
    exports_dir = job_output_dir(data_dir, job_id) / EXPORTS_DIRECTORY
    exports_dir.mkdir(parents=True, exist_ok=True)
    name = artifact_name(ExportFormat.WAV_PCM24, VOCALS_STEMS, archive=True)
    (exports_dir / f"{name}.deadbeef.part").write_bytes(b"truncated rubbish")

    response = await export(export_client, job_id)
    assert response.status_code == 200, response.text
    assert set(zip_entries(response)) == {"vocals.wav", "instrumental.wav", MANIFEST_NAME}


def test_artifact_names_stay_bounded_for_a_model_with_many_stems() -> None:
    """A long stem list falls back to a digest, so paths never run away."""
    stems = [f"stem_{index:02d}" for index in range(24)]
    name = artifact_name(ExportFormat.FLAC, stems, archive=True)
    assert len(name) < 40
    assert name == artifact_name(ExportFormat.FLAC, list(reversed(stems)), archive=True)
    assert name != artifact_name(ExportFormat.FLAC, stems[:-1], archive=True)


# -- the event loop stays free ----------------------------------------------


@pytest.fixture
def blocking_spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[TranscodeSpy]:
    """A transcode that parks its worker thread until the test releases it."""
    counter = TranscodeSpy(export_module.transcode_sync, block=True)
    monkeypatch.setattr(export_module, "transcode_sync", counter)
    yield counter
    counter.release.set()


async def test_other_requests_are_served_while_an_export_is_in_flight(
    export_client: httpx2.AsyncClient,
    recorder: EventRecorder,
    audio_id: str,
    blocking_spy: TranscodeSpy,
) -> None:
    """If the transcode ran on the loop, nothing below could complete."""
    job_id = await run_to_completion(export_client, recorder, audio_id, "standard_stems")

    task = asyncio.create_task(export(export_client, job_id))
    await asyncio.wait_for(
        asyncio.to_thread(blocking_spy.entered.wait, WAIT_TIMEOUT), timeout=WAIT_TIMEOUT
    )
    assert blocking_spy.entered.is_set()

    health = await asyncio.wait_for(export_client.get(HEALTH_URL), timeout=WAIT_TIMEOUT)
    assert health.status_code == 200, health.text
    result = await asyncio.wait_for(
        export_client.get(f"{JOBS_URL}/{job_id}/result"), timeout=WAIT_TIMEOUT
    )
    assert result.status_code == 200, result.text

    blocking_spy.release.set()
    response = await asyncio.wait_for(task, timeout=WAIT_TIMEOUT)
    assert response.status_code == 200, response.text
    assert set(zip_entries(response)) == {f"{stem}.wav" for stem in STANDARD_STEMS} | {
        MANIFEST_NAME
    }


async def test_the_job_worker_keeps_running_during_an_export(
    export_client: httpx2.AsyncClient,
    recorder: EventRecorder,
    audio_id: str,
    blocking_spy: TranscodeSpy,
) -> None:
    """A whole separation completes, and its events are dispatched, mid-export."""
    first = await run_to_completion(export_client, recorder, audio_id)

    task = asyncio.create_task(export(export_client, first))
    await asyncio.wait_for(
        asyncio.to_thread(blocking_spy.entered.wait, WAIT_TIMEOUT), timeout=WAIT_TIMEOUT
    )

    second = await create_job(export_client, **configuration(audio_id))
    terminal = await recorder.wait_for_terminal(second)
    assert isinstance(terminal, JobCompletedEvent), terminal

    blocking_spy.release.set()
    assert (await asyncio.wait_for(task, timeout=WAIT_TIMEOUT)).status_code == 200


# -- concurrency, cancellation and cache integrity ---------------------------


class ContentionProbe(export_module.BuildLocks):
    """A build-lock registry that signals when a second request arrives.

    Installed on ``app.state`` before the requests are made, it gives the
    concurrency tests an exact "the other request is now at the lock" moment,
    so nothing has to be timed or slept on.
    """

    def __init__(self) -> None:
        super().__init__()
        self.arrivals = 0
        self.second_arrived = asyncio.Event()

    @asynccontextmanager
    async def acquire(self, key: Path) -> AsyncGenerator[None]:
        self.arrivals += 1
        if self.arrivals >= 2:
            self.second_arrived.set()
        async with super().acquire(key):
            yield


@pytest.fixture
def after_spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[TranscodeSpy]:
    """A transcode that parks its worker thread *after* FFmpeg has written."""
    counter = TranscodeSpy(export_module.transcode_sync, block_after=True)
    monkeypatch.setattr(export_module, "transcode_sync", counter)
    yield counter
    counter.release.set()


def exports_dir_of(app: FastAPI, job_id: str) -> Path:
    data_dir = cast(Settings, app.state.settings).data_dir
    return job_output_dir(data_dir, job_id) / EXPORTS_DIRECTORY


def assert_no_build_residue(exports: Path) -> None:
    """No ``.part`` file and no staging directory survives a finished build."""
    assert list(exports.glob("*.part")) == [], "a partial artifact was left behind"
    assert list(exports.glob(".build-*")) == [], "a staging directory was left behind"


@pytest.mark.parametrize(("stems", "archive"), [("vocals", False), (None, True)])
async def test_a_cancelled_export_leaves_no_part_file_behind(
    export_client: httpx2.AsyncClient,
    export_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
    after_spy: TranscodeSpy,
    stems: str | None,
    archive: bool,
) -> None:
    """A client that disconnects mid-export must not orphan a file forever.

    ``asyncio.to_thread`` cannot be cancelled, so the build is shielded: it
    finishes, publishes its artifact and unlinks its ``.part``. Nothing ever
    cleans an export directory, so a leak here would be permanent.

    The follow-up request is the synchronisation: it queues on the same build
    lock, so it can only return once the shielded build has finished.
    """
    job_id = await run_to_completion(export_client, recorder, audio_id, "standard_stems")

    task = asyncio.create_task(export(export_client, job_id, stems=stems))
    await asyncio.wait_for(
        asyncio.to_thread(after_spy.entered.wait, WAIT_TIMEOUT), timeout=WAIT_TIMEOUT
    )
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    after_spy.release.set()
    follow_up = await asyncio.wait_for(
        export(export_client, job_id, stems=stems), timeout=WAIT_TIMEOUT
    )
    assert follow_up.status_code == 200, follow_up.text

    exports = exports_dir_of(export_app, job_id)
    assert_no_build_residue(exports)
    # The cancelled build published its artifact, so nothing was wasted.
    if archive:
        assert MANIFEST_NAME in zip_entries(follow_up)
    else:
        assert not follow_up.content.startswith(b"PK")


async def test_concurrent_identical_exports_share_one_build(
    export_client: httpx2.AsyncClient,
    export_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
    blocking_spy: TranscodeSpy,
) -> None:
    """The cache check is a fast path; the build lock is the real guard.

    Without it both requests miss the cache and transcode all four stems, and
    the loser's ``os.replace`` lands on a file the winner's response already
    has open — a ``PermissionError`` on Windows, i.e. a 500 for a request whose
    transcode succeeded.
    """
    probe = ContentionProbe()
    export_app.state.export_locks = probe
    job_id = await run_to_completion(export_client, recorder, audio_id, "standard_stems")

    first = asyncio.create_task(export(export_client, job_id))
    await asyncio.wait_for(
        asyncio.to_thread(blocking_spy.entered.wait, WAIT_TIMEOUT), timeout=WAIT_TIMEOUT
    )
    second = asyncio.create_task(export(export_client, job_id))
    await asyncio.wait_for(probe.second_arrived.wait(), timeout=WAIT_TIMEOUT)

    blocking_spy.release.set()
    one, two = await asyncio.wait_for(asyncio.gather(first, second), timeout=WAIT_TIMEOUT)

    assert one.status_code == 200, one.text
    assert two.status_code == 200, two.text
    assert one.content == two.content
    assert blocking_spy.calls == len(STANDARD_STEMS), "the second request rebuilt the artifact"
    assert probe.arrivals == 2
    assert probe.building(Path(str(exports_dir_of(export_app, job_id)))) == 0
    assert_no_build_residue(exports_dir_of(export_app, job_id))


async def test_the_build_lock_registry_does_not_grow(
    export_client: httpx2.AsyncClient, export_app: FastAPI, recorder: EventRecorder, audio_id: str
) -> None:
    """Entries are reference-counted, so a long-lived server does not leak."""
    probe = ContentionProbe()
    export_app.state.export_locks = probe
    job_id = await run_to_completion(export_client, recorder, audio_id)

    for fmt in (ExportFormat.WAV_PCM24, ExportFormat.FLAC, ExportFormat.WAV_FLOAT32):
        assert (await export(export_client, job_id, export_format=fmt)).status_code == 200

    exports = exports_dir_of(export_app, job_id)
    for path in exports.iterdir():
        assert probe.building(path) == 0


async def test_a_build_does_not_clobber_an_artifact_that_is_being_served(
    export_client: httpx2.AsyncClient,
    export_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
) -> None:
    """The publish step is skipped when the artifact already exists.

    A finished export may still be open in a `FileResponse`; on Windows
    `os.replace` onto an open destination raises `PermissionError`, which the
    handler would turn into a 500 for a request whose transcode succeeded. The
    two builds are equivalent, so keeping the published file is free.
    """
    job_id = await run_to_completion(export_client, recorder, audio_id)
    data_dir = cast(Settings, export_app.state.settings).data_dir

    published = await export(export_client, job_id, stems="vocals")
    assert published.status_code == 200, published.text

    exports = exports_dir_of(export_app, job_id)
    artifact = exports / artifact_name(ExportFormat.WAV_PCM24, ["vocals"], archive=False)
    original = artifact.read_bytes()

    result = SeparationResult.model_validate(
        (await export_client.get(f"{JOBS_URL}/{job_id}/result")).json()
    )
    sources = [("vocals", stem_path(data_dir, job_id, "vocals"))]

    with artifact.open("rb") as streaming:  # what FileResponse holds
        await export_module.build_artifact(
            artifact,
            sources,
            result,
            ExportFormat.WAV_PCM24,
            archive=False,
            timeout_seconds=30.0,
        )
        assert streaming.read() == original

    assert artifact.read_bytes() == original
    assert_no_build_residue(exports)
