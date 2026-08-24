"""Tests for the ``/api/v1/jobs/{id}/result`` and ``/stems/{stem}`` endpoints.

Like ``test_api_jobs.py`` these run the **real** application: a real job
manager started by the application lifespan on the test's own event loop, a
real event hub, and a real ``FakeSeparator`` writing real 16-bit WAV stems —
only its simulated delays are zeroed, by injecting a
:class:`SeparatorRegistry` onto ``app.state``. Every wait is gated with an
:class:`asyncio.Event`; no sleep is ever used as synchronization.

The stem assertions compare **bytes**: what the endpoint serves is read back
from the file the separator wrote, whole and sliced.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from starlette.types import Message

from straticate.api.results import DEFAULT_STEM_MEDIA_TYPE, stem_media_type
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
WAIT_TIMEOUT = 30.0

VOCALS_MODEL_ID = "fake-vocals-001"
STANDARD_MODEL_ID = "fake-standard-001"
VOCALS_STEMS = ["vocals", "instrumental"]
STANDARD_STEMS = ["vocals", "drums", "bass", "other"]

FAKE_GPU = ComputeDevice(
    id="cuda:0",
    backend=CUDA_BACKEND,
    name="NVIDIA GeForce RTX 5090",
    memory_total_bytes=34359738368,
)

WAV_MEDIA_TYPE = "audio/wav"


class StaticProbe:
    """Probe reporting one fixed device, so device resolution is deterministic."""

    backend: str = CUDA_BACKEND

    def detect(self) -> list[ComputeDevice]:
        return [FAKE_GPU]


class StageGatedSeparator:
    """A separator that parks the job in one chosen state until released.

    It performs no audio work: it announces ``stage`` (or nothing at all, which
    leaves the job in the ``preparing`` state the manager set before running
    the executor), signals ``started`` and waits on ``gate``. That is how the
    409 tests reach every non-completed state through the real machinery
    instead of poking at the manager's internals. Setting ``fail`` makes it
    raise instead, for the ``failed`` case.
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


def build_app(data_dir: Path) -> FastAPI:
    """An application isolated to ``data_dir``, with deterministic devices."""
    app = create_app(Settings(data_dir=data_dir))
    app.state.device_detector = DeviceDetector(probes=[StaticProbe()])
    app.state.separator_registry = fast_registry()
    return app


def register_audio(app: FastAPI, *, seconds: float = 0.4, filename: str = "song.wav") -> str:
    """Write a real tone WAV into the app's audio store and register it."""
    store = app.state.audio_store
    audio_id = cast(str, store.new_id())
    path = cast(Path, store.original_path(audio_id, filename))
    write_tone_wav(path, seconds=seconds)
    store.register(
        AudioFile(
            id=audio_id,
            filename=filename,
            size_bytes=path.stat().st_size,
            uploaded_at=datetime.now(UTC),
            metadata=AudioMetadata(
                duration_seconds=seconds,
                container="wav",
                codec="pcm_s16le",
                channels=2,
                sample_rate_hz=44100,
                bit_depth=16,
                bit_rate_bps=1411000,
            ),
        )
    )
    return audio_id


@pytest.fixture
def results_app(tmp_path: Path) -> FastAPI:
    return build_app(tmp_path)


@pytest.fixture
async def results_client(results_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client for a running application (lifespan started on this loop)."""
    async with results_app.router.lifespan_context(results_app):
        transport = httpx.ASGITransport(app=results_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
def audio_id(results_app: FastAPI) -> str:
    return register_audio(results_app)


@pytest.fixture
async def recorder(
    results_client: httpx.AsyncClient, results_app: FastAPI
) -> AsyncIterator[EventRecorder]:
    """A listener on the running app's job manager (needs the lifespan)."""
    listener = EventRecorder()
    manager = cast(JobManager, results_app.state.job_manager)
    manager.add_listener(listener)
    yield listener
    manager.remove_listener(listener)


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


async def create_job(client: httpx.AsyncClient, **body: Any) -> str:
    response = await client.post(JOBS_URL, json=body)
    assert response.status_code == 201, response.text
    return cast(str, response.json()["id"])


async def run_to_completion(
    client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str, mode_id: str = "vocals"
) -> str:
    """Create a job with the real fake separator and await its completion."""
    job_id = await create_job(client, **configuration(audio_id, mode_id=mode_id))
    terminal = await recorder.wait_for_terminal(job_id)
    assert isinstance(terminal, JobCompletedEvent), terminal
    return job_id


def result_url(job_id: str) -> str:
    return f"{JOBS_URL}/{job_id}/result"


def stem_url(job_id: str, stem: str) -> str:
    return f"{JOBS_URL}/{job_id}/stems/{stem}"


def assert_envelope(response: httpx.Response, code: str, status: int) -> dict[str, Any]:
    """Assert the standard error envelope and return the error object."""
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}
    error = cast(dict[str, Any], body["error"])
    assert set(error) == {"code", "message", "detail"}
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    return error


# -- GET /jobs/{id}/result --------------------------------------------------


@pytest.mark.parametrize(
    ("mode_id", "model_id", "stems"),
    [
        ("vocals", VOCALS_MODEL_ID, VOCALS_STEMS),
        ("standard_stems", STANDARD_MODEL_ID, STANDARD_STEMS),
    ],
)
async def test_result_of_a_completed_job(
    results_client: httpx.AsyncClient,
    recorder: EventRecorder,
    audio_id: str,
    mode_id: str,
    model_id: str,
    stems: list[str],
) -> None:
    """Nothing on this path is hardcoded to two stems."""
    job_id = await run_to_completion(results_client, recorder, audio_id, mode_id)

    response = await results_client.get(result_url(job_id))
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()

    assert set(result) == {"job_id", "model_id", "stems", "metrics"}
    assert result["job_id"] == job_id
    assert result["model_id"] == model_id
    assert [stem["name"] for stem in result["stems"]] == stems
    for stem in cast(list[dict[str, Any]], result["stems"]):
        assert set(stem) == {"name", "duration_seconds", "sample_rate_hz", "channels"}
        assert stem["duration_seconds"] > 0.0
        assert stem["sample_rate_hz"] == 44100
        assert stem["channels"] == 2
    assert set(result["metrics"]) == {"processing_seconds", "realtime_factor"}


async def test_result_matches_the_result_on_the_job_record(
    results_client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    """``/result`` serves exactly the record ``GET /jobs/{id}`` carries."""
    job_id = await run_to_completion(results_client, recorder, audio_id)

    job = (await results_client.get(f"{JOBS_URL}/{job_id}")).json()
    result = (await results_client.get(result_url(job_id))).json()
    assert result == job["result"]


async def test_result_of_an_unknown_job_returns_job_not_found(
    results_client: httpx.AsyncClient,
) -> None:
    error = assert_envelope(await results_client.get(result_url("01NOTAJOB")), "job_not_found", 404)
    assert error["detail"] == {"job_id": "01NOTAJOB"}


@pytest.mark.parametrize(
    ("stage", "expected_state"),
    [
        (None, JobState.PREPARING),
        (JobState.DECODING, JobState.DECODING),
        (JobState.LOADING_MODEL, JobState.LOADING_MODEL),
        (JobState.SEPARATING, JobState.SEPARATING),
        (JobState.POST_PROCESSING, JobState.POST_PROCESSING),
        (JobState.ENCODING, JobState.ENCODING),
    ],
)
async def test_result_of_a_running_job_returns_409(
    results_client: httpx.AsyncClient,
    results_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
    stage: JobState | None,
    expected_state: JobState,
) -> None:
    started, gate = asyncio.Event(), asyncio.Event()
    results_app.state.separator_registry = gated_registry(started, gate, stage=stage)

    job_id = await create_job(results_client, **configuration(audio_id))
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

    error = assert_envelope(
        await results_client.get(result_url(job_id)), "result_not_available", 409
    )
    assert error["detail"] == {"job_id": job_id, "state": expected_state.value}
    # The stem route reports the same condition.
    stem_error = assert_envelope(
        await results_client.get(stem_url(job_id, "vocals")), "result_not_available", 409
    )
    assert stem_error["detail"]["state"] == expected_state.value

    gate.set()
    await recorder.wait_for_terminal(job_id)


async def test_result_of_a_queued_job_returns_409(
    results_client: httpx.AsyncClient,
    results_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
) -> None:
    """The second job stays ``queued`` behind the gated first one."""
    started, gate = asyncio.Event(), asyncio.Event()
    results_app.state.separator_registry = gated_registry(started, gate)

    running = await create_job(results_client, **configuration(audio_id))
    queued = await create_job(results_client, **configuration(audio_id))
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

    error = assert_envelope(
        await results_client.get(result_url(queued)), "result_not_available", 409
    )
    assert error["detail"] == {"job_id": queued, "state": "queued"}

    gate.set()
    await recorder.wait_for_terminal(running)
    await recorder.wait_for_terminal(queued)


async def test_result_of_a_cancelled_job_returns_409(
    results_client: httpx.AsyncClient,
    results_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
) -> None:
    started, gate = asyncio.Event(), asyncio.Event()
    results_app.state.separator_registry = gated_registry(started, gate)

    running = await create_job(results_client, **configuration(audio_id))
    queued = await create_job(results_client, **configuration(audio_id))
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

    cancel = await results_client.post(f"{JOBS_URL}/{queued}/cancel")
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["state"] == "cancelled"

    error = assert_envelope(
        await results_client.get(result_url(queued)), "result_not_available", 409
    )
    assert error["detail"] == {"job_id": queued, "state": "cancelled"}
    assert_envelope(
        await results_client.get(stem_url(queued, "vocals")), "result_not_available", 409
    )

    gate.set()
    await recorder.wait_for_terminal(running)


async def test_result_of_a_failed_job_returns_409(
    results_client: httpx.AsyncClient,
    results_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
) -> None:
    started, gate = asyncio.Event(), asyncio.Event()
    results_app.state.separator_registry = gated_registry(started, gate, fail=True)

    job_id = await create_job(results_client, **configuration(audio_id))
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)
    gate.set()
    terminal = await recorder.wait_for_terminal(job_id)
    assert isinstance(terminal, JobFailedEvent), terminal

    error = assert_envelope(
        await results_client.get(result_url(job_id)), "result_not_available", 409
    )
    assert error["detail"] == {"job_id": job_id, "state": "failed"}


# -- GET /jobs/{id}/stems/{stem} --------------------------------------------


@pytest.mark.parametrize(
    ("mode_id", "stems"),
    [("vocals", VOCALS_STEMS), ("standard_stems", STANDARD_STEMS)],
)
async def test_every_stem_is_served_byte_identical_to_the_file_on_disk(
    results_client: httpx.AsyncClient,
    recorder: EventRecorder,
    audio_id: str,
    tmp_path: Path,
    mode_id: str,
    stems: list[str],
) -> None:
    job_id = await run_to_completion(results_client, recorder, audio_id, mode_id)

    served: list[bytes] = []
    for stem in stems:
        response = await results_client.get(stem_url(job_id, stem))
        assert response.status_code == 200, response.text
        on_disk = stem_path(tmp_path, job_id, stem).read_bytes()
        assert response.content == on_disk
        assert response.content.startswith(b"RIFF")
        assert response.content[8:12] == b"WAVE"
        assert int(response.headers["content-length"]) == len(on_disk)
        served.append(response.content)

    # A real four-stem job serves four *different* files, not one repeated.
    assert len({bytes(payload) for payload in served}) == len(stems)


async def test_stem_response_headers(
    results_client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    job_id = await run_to_completion(results_client, recorder, audio_id)
    response = await results_client.get(stem_url(job_id, "vocals"))

    assert response.status_code == 200
    assert response.headers["content-type"] == WAV_MEDIA_TYPE
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-disposition"] == 'inline; filename="vocals.wav"'
    assert "etag" in response.headers
    assert "last-modified" in response.headers


async def test_stem_headers_are_readable_cross_origin(
    results_client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    """Every header the contract documents must be exposed to browser JS.

    Without ``expose_headers`` a cross-origin fetch receives these headers and
    the browser hides all of them: the player could read the bytes but not the
    ``Content-Range`` describing which bytes, nor the validators ``If-Range``
    needs.
    """
    job_id = await run_to_completion(results_client, recorder, audio_id)
    response = await results_client.get(
        stem_url(job_id, "vocals"),
        headers={"Origin": "http://localhost:5173", "Range": "bytes=0-99"},
    )

    assert response.status_code == 206
    exposed = {
        name.strip().lower()
        for name in response.headers["access-control-expose-headers"].split(",")
    }
    assert {
        "accept-ranges",
        "content-disposition",
        "content-range",
        "etag",
        "last-modified",
    } <= exposed


def test_media_type_is_derived_from_the_file_suffix() -> None:
    """Not a constant in the handler: 022's formats only add a mapping."""
    assert stem_media_type(Path("vocals.wav")) == WAV_MEDIA_TYPE
    assert stem_media_type(Path("vocals.WAV")) == WAV_MEDIA_TYPE
    assert stem_media_type(Path("vocals.flac")) == "audio/flac"
    assert stem_media_type(Path("vocals.unknown")) == DEFAULT_STEM_MEDIA_TYPE


# -- Range ------------------------------------------------------------------


async def test_range_returns_206_with_the_exact_slice(
    results_client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str, tmp_path: Path
) -> None:
    job_id = await run_to_completion(results_client, recorder, audio_id)
    on_disk = stem_path(tmp_path, job_id, "vocals").read_bytes()
    assert len(on_disk) > 100

    response = await results_client.get(stem_url(job_id, "vocals"), headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert response.content == on_disk[:100]
    assert len(response.content) == 100
    assert response.headers["content-range"] == f"bytes 0-99/{len(on_disk)}"
    assert response.headers["content-length"] == "100"
    assert response.headers["content-type"] == WAV_MEDIA_TYPE


async def test_a_mid_file_range_returns_that_slice(
    results_client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str, tmp_path: Path
) -> None:
    job_id = await run_to_completion(results_client, recorder, audio_id)
    on_disk = stem_path(tmp_path, job_id, "vocals").read_bytes()
    start, end = 1000, 1999

    response = await results_client.get(
        stem_url(job_id, "vocals"), headers={"Range": f"bytes={start}-{end}"}
    )
    assert response.status_code == 206
    assert response.content == on_disk[start : end + 1]
    assert response.headers["content-range"] == f"bytes {start}-{end}/{len(on_disk)}"


async def test_an_open_ended_range_serves_the_rest_of_the_file(
    results_client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str, tmp_path: Path
) -> None:
    job_id = await run_to_completion(results_client, recorder, audio_id)
    on_disk = stem_path(tmp_path, job_id, "vocals").read_bytes()
    size = len(on_disk)
    start = size // 2

    response = await results_client.get(
        stem_url(job_id, "vocals"), headers={"Range": f"bytes={start}-"}
    )
    assert response.status_code == 206
    assert response.content == on_disk[start:]
    assert response.headers["content-range"] == f"bytes {start}-{size - 1}/{size}"


async def test_a_suffix_range_serves_the_final_bytes(
    results_client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str, tmp_path: Path
) -> None:
    job_id = await run_to_completion(results_client, recorder, audio_id)
    on_disk = stem_path(tmp_path, job_id, "vocals").read_bytes()
    size = len(on_disk)

    response = await results_client.get(stem_url(job_id, "vocals"), headers={"Range": "bytes=-64"})
    assert response.status_code == 206
    assert response.content == on_disk[-64:]
    assert response.headers["content-range"] == f"bytes {size - 64}-{size - 1}/{size}"


async def test_a_range_past_the_end_of_the_file_is_rejected(
    results_client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str, tmp_path: Path
) -> None:
    job_id = await run_to_completion(results_client, recorder, audio_id)
    size = stem_path(tmp_path, job_id, "vocals").stat().st_size

    response = await results_client.get(
        stem_url(job_id, "vocals"), headers={"Range": f"bytes={size}-{size + 100}"}
    )
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{size}"


async def test_a_malformed_range_is_rejected(
    results_client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    job_id = await run_to_completion(results_client, recorder, audio_id)
    response = await results_client.get(
        stem_url(job_id, "vocals"), headers={"Range": "frames=0-10"}
    )
    assert response.status_code == 400


async def test_a_ranged_request_for_a_missing_stem_still_404s(
    results_client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    """Range never bypasses the lookup rules."""
    job_id = await run_to_completion(results_client, recorder, audio_id)
    response = await results_client.get(
        stem_url(job_id, "nosuchstem"), headers={"Range": "bytes=0-99"}
    )
    assert_envelope(response, "stem_not_found", 404)


# -- stem lookup failures ---------------------------------------------------


async def test_stem_of_an_unknown_job_returns_job_not_found(
    results_client: httpx.AsyncClient,
) -> None:
    assert_envelope(await results_client.get(stem_url("01NOTAJOB", "vocals")), "job_not_found", 404)


async def test_unknown_stem_returns_stem_not_found(
    results_client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    job_id = await run_to_completion(results_client, recorder, audio_id)

    error = assert_envelope(
        await results_client.get(stem_url(job_id, "drums")), "stem_not_found", 404
    )
    assert error["detail"] == {
        "job_id": job_id,
        "stem": "drums",
        "available_stems": VOCALS_STEMS,
    }


async def test_a_stem_of_another_mode_is_not_served(
    results_client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str, tmp_path: Path
) -> None:
    """The *result* is the authority, not the filesystem.

    A file planted in the job's stem directory that the result does not list
    is not servable — the endpoint never lists the directory.
    """
    job_id = await run_to_completion(results_client, recorder, audio_id)
    planted = stem_path(tmp_path, job_id, "drums")
    planted.write_bytes(b"RIFFplanted")

    assert_envelope(await results_client.get(stem_url(job_id, "drums")), "stem_not_found", 404)


async def test_a_deleted_stem_file_returns_stem_file_missing(
    results_client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str, tmp_path: Path
) -> None:
    """An orphaned job directory is a 404, never a 500 (014's limitation)."""
    job_id = await run_to_completion(results_client, recorder, audio_id)
    stem_path(tmp_path, job_id, "vocals").unlink()

    error = assert_envelope(
        await results_client.get(stem_url(job_id, "vocals")), "stem_file_missing", 404
    )
    assert error["detail"] == {"job_id": job_id, "stem": "vocals"}
    # The result itself still resolves: only the bytes are gone.
    assert (await results_client.get(result_url(job_id))).status_code == 200
    # Its sibling is unaffected.
    assert (await results_client.get(stem_url(job_id, "instrumental"))).status_code == 200


async def test_a_removed_job_directory_returns_stem_file_missing(
    results_client: httpx.AsyncClient, recorder: EventRecorder, audio_id: str, tmp_path: Path
) -> None:
    job_id = await run_to_completion(results_client, recorder, audio_id)
    for stem in VOCALS_STEMS:
        stem_path(tmp_path, job_id, stem).unlink()
    stem_path(tmp_path, job_id, "vocals").parent.rmdir()
    job_output_dir(tmp_path, job_id).rmdir()

    assert_envelope(await results_client.get(stem_url(job_id, "vocals")), "stem_file_missing", 404)


# -- path safety ------------------------------------------------------------

SECRET = b"not-a-stem-you-may-have"

TRAVERSAL_STEM_NAMES = [
    pytest.param("%2E%2E", id="encoded-dot-dot"),
    pytest.param("%2E%2E%2Fsecret", id="encoded-parent-slash"),
    pytest.param("%2E%2E%2F%2E%2E%2Fsecret", id="encoded-double-parent"),
    pytest.param("....%2F%2F..%2Fsecret", id="encoded-dot-truncation"),
    pytest.param("%2Fetc%2Fpasswd", id="encoded-absolute-posix"),
    pytest.param("C%3A%5CWindows%5Cwin.ini", id="encoded-absolute-windows"),
    pytest.param("..%5C..%5Csecret", id="encoded-windows-parent"),
    pytest.param("vocals%5C..%5C..%5Csecret", id="windows-separators"),
    pytest.param("%2E%2E%252Fsecret", id="double-encoded"),
    pytest.param("vocals.wav", id="suffix-appended"),
    pytest.param("Vocals", id="uppercase"),
    pytest.param("..%00vocals", id="null-byte"),
    pytest.param("~", id="tilde"),
    pytest.param(".ssh", id="leading-dot"),
]


@pytest.mark.parametrize("stem_name", TRAVERSAL_STEM_NAMES)
async def test_traversal_attempts_produce_a_clean_404(
    results_client: httpx.AsyncClient,
    recorder: EventRecorder,
    audio_id: str,
    tmp_path: Path,
    stem_name: str,
) -> None:
    """Never a 500, and never a byte from outside the job's stem directory."""
    job_id = await run_to_completion(results_client, recorder, audio_id)
    # Plant readable files everywhere a traversal could plausibly land.
    for target in (
        tmp_path / "secret",
        tmp_path / "secret.wav",
        job_output_dir(tmp_path, job_id) / "secret",
        job_output_dir(tmp_path, job_id).parent / "secret",
        stem_path(tmp_path, job_id, "vocals").parent / "secret",
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(SECRET)

    response = await results_client.get(stem_url(job_id, stem_name))

    assert response.status_code == 404, f"{stem_name!r} -> {response.status_code}"
    assert SECRET not in response.content
    body: dict[str, Any] = response.json()
    assert set(body) == {"error"}
    assert set(cast(dict[str, Any], body["error"])) == {"code", "message", "detail"}


RAW_TRAVERSAL_PATHS = [
    pytest.param("stems/..", id="dot-dot"),
    pytest.param("stems/../../../secret", id="parent-segments"),
    pytest.param("stems/../../{job}/stems/vocals", id="parent-segments-back-into-place"),
    pytest.param("stems/./vocals", id="current-directory-segment"),
    pytest.param("stems//vocals", id="double-slash"),
    pytest.param("stems/vocals/../../result", id="dot-segments-inside-the-path"),
    pytest.param("stems", id="stems-directory-itself"),
    pytest.param("stems/", id="empty-stem-name"),
    pytest.param("stems/subdir/vocals", id="extra-segment"),
]


async def raw_asgi_get(app: FastAPI, path: str) -> tuple[int, dict[str, str], bytes]:
    """Send a GET whose **raw** path reaches the app unnormalized.

    ``httpx`` resolves dot segments client-side (RFC 3986), so a request built
    through it can never carry ``..`` to the server. A real ASGI server does
    not normalize, so these paths are driven straight at the application.
    """
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
        "state": {},
    }
    await app(scope, receive, send)

    start = next(message for message in messages if message["type"] == "http.response.start")
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1") for key, value in start["headers"]
    }
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return cast(int, start["status"]), headers, body


@pytest.mark.parametrize("raw_path", RAW_TRAVERSAL_PATHS)
async def test_an_unnormalized_url_path_never_serves_a_file(
    results_client: httpx.AsyncClient,
    results_app: FastAPI,
    recorder: EventRecorder,
    audio_id: str,
    tmp_path: Path,
    raw_path: str,
) -> None:
    """Raw ``..`` segments reach the router and are simply not routes."""
    job_id = await run_to_completion(results_client, recorder, audio_id)
    for target in (
        tmp_path / "secret",
        tmp_path / "secret.wav",
        job_output_dir(tmp_path, job_id) / "secret",
        stem_path(tmp_path, job_id, "vocals").parent / "secret",
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(SECRET)

    status, headers, body = await raw_asgi_get(
        results_app, f"{JOBS_URL}/{job_id}/{raw_path.format(job=job_id)}"
    )

    assert status == 404, f"{raw_path!r} -> {status}"
    assert SECRET not in body
    assert not headers.get("content-type", "").startswith("audio/")
