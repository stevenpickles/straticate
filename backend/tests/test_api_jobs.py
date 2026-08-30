"""Tests for the ``/api/v1/jobs`` endpoints.

These run the **real** application: a real job manager (started by the
application lifespan on the test's own event loop, which is where the manager's
single-loop contract requires its callers to be), a real event hub, and a real
``FakeSeparator`` writing real stems — only its simulated delays are zeroed, by
injecting a :class:`SeparatorRegistry` onto ``app.state``.

Everything that needs coordination is gated with :class:`asyncio.Event`; no
sleep is ever used as synchronization.
"""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient, WebSocketTestSession

from straticate.config import Settings
from straticate.inference import (
    FAKE_ARCHITECTURE,
    ROFORMER_ARCHITECTURE,
    FakeSeparator,
    SeparationProgress,
    Separator,
    SeparatorInfo,
    SeparatorRegistry,
    fake_separator_builder,
    job_stems_dir,
    separator_info_from_model,
)
from straticate.jobs import CancellationToken, JobEvent, JobManager
from straticate.main import create_app
from straticate.models import CATALOG_FILENAME
from straticate.models.layout import weights_path
from straticate.schemas import AudioFile, AudioMetadata, ComputeDevice, Model
from straticate.schemas.events import JobCancelledEvent, JobCompletedEvent, JobFailedEvent
from straticate.schemas.jobs import (
    JobState,
    SeparationConfiguration,
    SeparationResult,
    SeparationResultMetrics,
    Stem,
)
from straticate.system import CPU_DEVICE_ID, CUDA_BACKEND, DeviceDetector
from tests.audio_fixtures import write_tone_wav
from tests.conftest import fake_quality_id
from tests.roformer_fixtures import TINY_SAMPLE_RATE, tiny_catalog_block, write_tiny_weights

JOBS_URL = "/api/v1/jobs"
WS_URL = "/api/v1/ws"
WAIT_TIMEOUT = 30.0

VOCALS_MODEL_ID = "fake-vocals-001"
STANDARD_MODEL_ID = "fake-standard-001"

FAKE_GPU = ComputeDevice(
    id="cuda:0",
    backend=CUDA_BACKEND,
    name="NVIDIA GeForce RTX 5090",
    memory_total_bytes=34359738368,
)


class StaticProbe:
    """Probe reporting one fixed device, so device resolution is deterministic."""

    backend: str = CUDA_BACKEND

    def detect(self) -> list[ComputeDevice]:
        return [FAKE_GPU]


class ScriptedSeparator:
    """A separator whose run can be gated, for cancellation timing tests.

    It performs no audio work at all: the point of these tests is the endpoint
    and the manager, and the real ``FakeSeparator`` covers the audio.
    """

    def __init__(
        self,
        info: SeparatorInfo,
        *,
        started: asyncio.Event,
        gate: asyncio.Event,
    ) -> None:
        self._info = info
        self.started = started
        self.gate = gate

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
        if stage_callback is not None:
            stage_callback(JobState.SEPARATING)
        self.started.set()
        await self.gate.wait()
        cancellation_token.raise_if_cancelled()
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

    def types(self, job_id: str) -> list[str]:
        return [event.type for event in self.events if event.job_id == job_id]


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


def build_app(data_dir: Path, *, models_dir: Path | None = None) -> FastAPI:
    """An application isolated to ``data_dir``, with deterministic devices."""
    settings = (
        Settings(data_dir=data_dir)
        if models_dir is None
        else Settings(data_dir=data_dir, models_dir=models_dir)
    )
    app = create_app(settings)
    app.state.device_detector = DeviceDetector(probes=[StaticProbe()])
    app.state.separator_registry = fast_registry()
    return app


def register_audio(app: FastAPI, *, seconds: float = 0.4, filename: str = "song.wav") -> str:
    """Write a real tone WAV into the app's audio store and register it."""
    store = app.state.audio_store
    audio_id = cast(str, store.new_id())
    path = cast(Path, store.prepare_original_path(audio_id, filename))
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
def jobs_app(tmp_path: Path) -> FastAPI:
    return build_app(tmp_path)


@pytest.fixture
async def jobs_client(jobs_app: FastAPI) -> AsyncIterator[httpx2.AsyncClient]:
    """A client for a running application (lifespan started on this loop)."""
    async with jobs_app.router.lifespan_context(jobs_app):
        transport = httpx2.ASGITransport(app=jobs_app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
def audio_id(jobs_app: FastAPI) -> str:
    return register_audio(jobs_app)


@pytest.fixture
async def recorder(
    jobs_client: httpx2.AsyncClient, jobs_app: FastAPI
) -> AsyncIterator[EventRecorder]:
    """A listener on the running app's job manager (needs the lifespan)."""
    listener = EventRecorder()
    manager = manager_of(jobs_app)
    manager.add_listener(listener)
    yield listener
    manager.remove_listener(listener)


def manager_of(app: FastAPI) -> JobManager:
    return cast(JobManager, app.state.job_manager)


def configuration(audio_id: str, **overrides: Any) -> dict[str, Any]:
    """A create-job request body.

    ``quality_id`` follows ``mode_id`` unless the caller pins it: the shipped
    fixtures back ``balanced`` in ``vocals`` and ``fast`` in ``standard_stems``
    (see ``conftest.FAKE_QUALITY_TIERS``).
    """
    mode_id = cast(str, overrides.pop("mode_id", "vocals"))
    body: dict[str, Any] = {
        "audio_id": audio_id,
        "mode_id": mode_id,
        "quality_id": fake_quality_id(mode_id),
    }
    body.update(overrides)
    return body


async def create_job(client: httpx2.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await client.post(JOBS_URL, json=body)
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


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


# -- create -----------------------------------------------------------------


async def test_create_returns_201_queued_with_the_resolved_model_and_device(
    jobs_client: httpx2.AsyncClient, audio_id: str
) -> None:
    job = await create_job(jobs_client, **configuration(audio_id))

    assert job["state"] == "queued"
    assert job["progress"] == 0.0
    assert job["audio_id"] == audio_id
    assert job["model_id"] == VOCALS_MODEL_ID
    assert job["started_at"] is None
    assert job["finished_at"] is None
    assert job["error"] is None
    assert job["result"] is None
    # The request omitted `device_id`; the response carries the resolved one.
    # It omitted `stereo_handling` too, and the response carries its default:
    # the field is optional on the wire (feature 041) but always present on a
    # job, so a client reading a job back never has to know what the server's
    # default was.
    assert job["configuration"] == {
        "audio_id": audio_id,
        "mode_id": "vocals",
        "quality_id": "balanced",
        "device_id": FAKE_GPU.id,
        "stereo_handling": "as_is",
    }


@pytest.mark.parametrize("handling", ["mono", "mono_bass"])
async def test_create_echoes_a_requested_stereo_handling(
    jobs_client: httpx2.AsyncClient, audio_id: str, handling: str
) -> None:
    """Feature 041's field survives the create round trip onto the job record.

    Parametrised over both non-default values since feature 062: the enum is on
    the wire, so a value the schema forgot would be a 422 here rather than a
    quiet fallback to ``as_is``.
    """
    job = await create_job(jobs_client, **configuration(audio_id, stereo_handling=handling))
    assert job["configuration"]["stereo_handling"] == handling

    fetched = await jobs_client.get(f"{JOBS_URL}/{job['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["configuration"]["stereo_handling"] == handling


async def test_create_rejects_an_unknown_stereo_handling(
    jobs_client: httpx2.AsyncClient, audio_id: str
) -> None:
    """A closed enum, so a typo is a 422 rather than a silently ignored field."""
    response = await jobs_client.post(
        JOBS_URL, json=configuration(audio_id, stereo_handling="narrow")
    )
    assert response.status_code == 422, response.text


async def test_create_honours_a_pinned_device(
    jobs_client: httpx2.AsyncClient, audio_id: str
) -> None:
    job = await create_job(jobs_client, **configuration(audio_id, device_id=CPU_DEVICE_ID))
    assert job["configuration"]["device_id"] == CPU_DEVICE_ID


async def test_create_returns_before_the_separation_runs(
    jobs_client: httpx2.AsyncClient, jobs_app: FastAPI, audio_id: str, tmp_path: Path
) -> None:
    """No inference happens inside the request handler (AGENTS.md principle 4).

    The separator is gated shut, so the only way the response can arrive is if
    the handler returned without waiting for the separation.
    """
    started, gate = asyncio.Event(), asyncio.Event()
    registry, _ = gated_registry(started, gate)
    jobs_app.state.separator_registry = registry

    job = await create_job(jobs_client, **configuration(audio_id))
    job_id = cast(str, job["id"])
    assert job["state"] == "queued"
    assert job["result"] is None
    assert not job_stems_dir(tmp_path, job_id).exists()

    # The separation begins only after the request has been answered, and it is
    # still running (gated) while the client is free to do anything else.
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)
    running: dict[str, Any] = (await jobs_client.get(f"{JOBS_URL}/{job_id}")).json()
    assert running["state"] == "separating"
    assert running["result"] is None

    gate.set()


async def test_create_resolves_the_model_of_the_requested_mode(
    jobs_client: httpx2.AsyncClient, audio_id: str
) -> None:
    job = await create_job(jobs_client, **configuration(audio_id, mode_id="standard_stems"))
    assert job["model_id"] == STANDARD_MODEL_ID


# -- lifecycle --------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode_id", "model_id", "stems"),
    [
        ("vocals", VOCALS_MODEL_ID, ["vocals", "instrumental"]),
        ("standard_stems", STANDARD_MODEL_ID, ["vocals", "drums", "bass", "other"]),
    ],
)
async def test_a_created_job_runs_to_completion_with_stems_on_disk(
    jobs_client: httpx2.AsyncClient,
    recorder: EventRecorder,
    audio_id: str,
    tmp_path: Path,
    mode_id: str,
    model_id: str,
    stems: list[str],
) -> None:
    """Nothing in the path is hardcoded to two stems."""
    created = await create_job(jobs_client, **configuration(audio_id, mode_id=mode_id))
    job_id = cast(str, created["id"])

    terminal = await recorder.wait_for_terminal(job_id)
    assert isinstance(terminal, JobCompletedEvent), terminal

    response = await jobs_client.get(f"{JOBS_URL}/{job_id}")
    assert response.status_code == 200
    job: dict[str, Any] = response.json()
    assert job["state"] == "completed"
    assert job["progress"] == 1.0
    assert job["started_at"] is not None
    assert job["finished_at"] is not None

    result = cast(dict[str, Any], job["result"])
    assert result["job_id"] == job_id
    assert result["model_id"] == model_id
    assert [stem["name"] for stem in result["stems"]] == stems
    assert result["metrics"]["processing_seconds"] >= 0.0

    stems_dir = job_stems_dir(tmp_path, job_id)
    assert sorted(path.name for path in stems_dir.iterdir()) == sorted(
        f"{stem}.wav" for stem in stems
    )
    for path in stems_dir.iterdir():
        assert path.stat().st_size > 0


# -- list and get -----------------------------------------------------------


async def test_list_returns_jobs_in_submission_order(
    jobs_client: httpx2.AsyncClient, audio_id: str
) -> None:
    assert (await jobs_client.get(JOBS_URL)).json() == []

    submitted = [
        cast(str, (await create_job(jobs_client, **configuration(audio_id)))["id"])
        for _ in range(3)
    ]

    response = await jobs_client.get(JOBS_URL)
    assert response.status_code == 200
    listed: list[dict[str, Any]] = response.json()
    assert [job["id"] for job in listed] == submitted


async def test_get_unknown_job_returns_job_not_found(jobs_client: httpx2.AsyncClient) -> None:
    response = await jobs_client.get(f"{JOBS_URL}/01NOTAJOB")
    assert_envelope(response, "job_not_found", 404)


# -- cancellation -----------------------------------------------------------


def gated_registry(
    started: asyncio.Event, gate: asyncio.Event
) -> tuple[SeparatorRegistry, list[ScriptedSeparator]]:
    """A registry whose fake-architecture models get a gated scripted separator."""
    built: list[ScriptedSeparator] = []

    def build(model: Model) -> ScriptedSeparator:
        separator = ScriptedSeparator(separator_info_from_model(model), started=started, gate=gate)
        built.append(separator)
        return separator

    return SeparatorRegistry({FAKE_ARCHITECTURE: build}), built


async def test_cancelling_a_queued_job_cancels_it_immediately(
    jobs_client: httpx2.AsyncClient, jobs_app: FastAPI, recorder: EventRecorder, audio_id: str
) -> None:
    started, gate = asyncio.Event(), asyncio.Event()
    registry, _ = gated_registry(started, gate)
    jobs_app.state.separator_registry = registry

    running = await create_job(jobs_client, **configuration(audio_id))
    queued = await create_job(jobs_client, **configuration(audio_id))
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)
    assert manager_of(jobs_app).get(cast(str, queued["id"])).state is JobState.QUEUED

    response = await jobs_client.post(f"{JOBS_URL}/{queued['id']}/cancel")
    assert response.status_code == 200
    cancelled: dict[str, Any] = response.json()
    assert cancelled["id"] == queued["id"]
    assert cancelled["state"] == "cancelled"
    assert cancelled["started_at"] is None

    terminal = await recorder.wait_for_terminal(cast(str, queued["id"]))
    assert isinstance(terminal, JobCancelledEvent)
    # It never ran: no `job_started`, so the separator was never entered.
    assert recorder.types(cast(str, queued["id"])) == ["job_created", "job_cancelled"]

    gate.set()
    await recorder.wait_for_terminal(cast(str, running["id"]))


async def test_cancelling_a_running_job_is_a_request_the_separator_honours(
    jobs_client: httpx2.AsyncClient, jobs_app: FastAPI, recorder: EventRecorder, audio_id: str
) -> None:
    started, gate = asyncio.Event(), asyncio.Event()
    registry, _ = gated_registry(started, gate)
    jobs_app.state.separator_registry = registry

    created = await create_job(jobs_client, **configuration(audio_id))
    job_id = cast(str, created["id"])
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

    response = await jobs_client.post(f"{JOBS_URL}/{job_id}/cancel")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    # Cancellation is a request: the job is still processing when it returns.
    assert body["state"] == "separating"
    assert body["finished_at"] is None

    gate.set()
    terminal = await recorder.wait_for_terminal(job_id)
    assert isinstance(terminal, JobCancelledEvent)

    final: dict[str, Any] = (await jobs_client.get(f"{JOBS_URL}/{job_id}")).json()
    assert final["state"] == "cancelled"
    assert final["finished_at"] is not None


async def test_cancelling_a_terminal_job_is_idempotent(
    jobs_client: httpx2.AsyncClient, recorder: EventRecorder, audio_id: str
) -> None:
    created = await create_job(jobs_client, **configuration(audio_id))
    job_id = cast(str, created["id"])
    await recorder.wait_for_terminal(job_id)

    for _ in range(2):
        response = await jobs_client.post(f"{JOBS_URL}/{job_id}/cancel")
        assert response.status_code == 200
        assert response.json()["state"] == "completed"

    assert recorder.types(job_id).count("job_cancelled") == 0


async def test_cancelling_an_unknown_job_returns_job_not_found(
    jobs_client: httpx2.AsyncClient,
) -> None:
    response = await jobs_client.post(f"{JOBS_URL}/01NOTAJOB/cancel")
    assert_envelope(response, "job_not_found", 404)


# -- error codes ------------------------------------------------------------


async def test_unknown_audio_is_a_404(jobs_client: httpx2.AsyncClient) -> None:
    response = await jobs_client.post(JOBS_URL, json=configuration("01NOSUCHAUDIO"))
    error = assert_envelope(response, "audio_not_found", 404)
    assert error["detail"] == {"audio_id": "01NOSUCHAUDIO"}


async def test_registered_audio_whose_file_vanished_is_a_404(
    jobs_client: httpx2.AsyncClient, jobs_app: FastAPI, audio_id: str
) -> None:
    store = jobs_app.state.audio_store
    cast(Path, store.original_path(audio_id, "song.wav")).unlink()

    response = await jobs_client.post(JOBS_URL, json=configuration(audio_id))
    assert_envelope(response, "audio_not_found", 404)


async def test_unknown_mode_is_a_404(jobs_client: httpx2.AsyncClient, audio_id: str) -> None:
    response = await jobs_client.post(JOBS_URL, json=configuration(audio_id, mode_id="karaoke"))
    error = assert_envelope(response, "separation_mode_not_found", 404)
    assert error["detail"] == {"mode_id": "karaoke"}


async def test_unknown_quality_option_is_a_404(
    jobs_client: httpx2.AsyncClient, audio_id: str
) -> None:
    response = await jobs_client.post(JOBS_URL, json=configuration(audio_id, quality_id="ultra"))
    error = assert_envelope(response, "quality_option_not_found", 404)
    assert error["detail"] == {"mode_id": "vocals", "quality_id": "ultra"}


async def test_unknown_device_is_a_404(jobs_client: httpx2.AsyncClient, audio_id: str) -> None:
    response = await jobs_client.post(JOBS_URL, json=configuration(audio_id, device_id="cuda:9"))
    error = assert_envelope(response, "device_not_found", 404)
    assert error["detail"] == {"device_id": "cuda:9"}


async def test_a_model_without_a_separator_is_a_501(tmp_path: Path) -> None:
    """A catalogued model whose architecture this build cannot run."""
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / CATALOG_FILENAME).write_text(
        json.dumps(
            {
                "catalog_version": 1,
                "models": [
                    {
                        "schema_version": 1,
                        "id": "roformer-hq-001",
                        "display_name": "RoFormer HQ",
                        "architecture": "mel_band_roformer",
                        "version": "1.0",
                        "separation_mode": "vocals",
                        "stems": ["vocals", "instrumental"],
                        "sample_rate": 44100,
                        "capabilities": {"cuda": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    app = build_app(tmp_path / "data", models_dir=models_dir)
    audio = register_audio(app)

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(JOBS_URL, json=configuration(audio))

    error = assert_envelope(response, "separator_unavailable", 501)
    assert error["detail"] == {
        "model_id": "roformer-hq-001",
        "architecture": "mel_band_roformer",
    }


async def test_a_shutting_down_manager_is_a_503(
    jobs_client: httpx2.AsyncClient, jobs_app: FastAPI, audio_id: str
) -> None:
    created = await create_job(jobs_client, **configuration(audio_id))
    await manager_of(jobs_app).aclose()

    assert_envelope(
        await jobs_client.post(JOBS_URL, json=configuration(audio_id)),
        "service_unavailable",
        503,
    )
    assert_envelope(
        await jobs_client.post(f"{JOBS_URL}/{created['id']}/cancel"),
        "service_unavailable",
        503,
    )
    # Reads stay available while the manager winds down.
    assert (await jobs_client.get(f"{JOBS_URL}/{created['id']}")).status_code == 200
    assert (await jobs_client.get(JOBS_URL)).status_code == 200


async def test_a_malformed_request_is_a_validation_error(jobs_client: httpx2.AsyncClient) -> None:
    response = await jobs_client.post(JOBS_URL, json={"audio_id": "01A"})
    assert_envelope(response, "validation_error", 422)


# -- REST → manager → hub → WebSocket ---------------------------------------


def receive_until(session: WebSocketTestSession, event_type: str) -> list[dict[str, Any]]:
    """Read events until (and including) the first one of ``event_type``."""
    events: list[dict[str, Any]] = []
    while True:
        event = cast(dict[str, Any], session.receive_json())
        events.append(event)
        if event["type"] == event_type:
            return events


def create_job_over_portal(
    client: TestClient, app: FastAPI, body: dict[str, Any]
) -> dict[str, Any]:
    """``POST /jobs`` on the application's own event loop, from a sync test.

    The app runs on the ``TestClient``'s portal loop, which is where the job
    manager lives — so the request is marshalled onto it exactly as the
    manager's single-loop contract requires.
    """
    portal = client.portal
    assert portal is not None

    async def post() -> dict[str, Any]:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.post(JOBS_URL, json=body)
        assert response.status_code == 201, response.text
        return cast(dict[str, Any], response.json())

    return portal.call(post)


def test_a_websocket_client_observes_the_full_sequence_for_an_api_created_job(
    tmp_path: Path,
) -> None:
    """The end-to-end path: REST create → job manager → event hub → browser."""
    app = build_app(tmp_path)
    audio = register_audio(app)

    with TestClient(app) as client, client.websocket_connect(WS_URL) as session:
        job = create_job_over_portal(client, app, configuration(audio))
        job_id = cast(str, job["id"])
        events = receive_until(session, "job_completed")

    assert {cast(str, event["job_id"]) for event in events} == {job_id}
    types = [cast(str, event["type"]) for event in events]
    assert types[0] == "job_created"
    assert types[1] == "job_started"
    assert types[-1] == "job_completed"
    assert types.index("job_stage_changed") < types.index("job_progress")

    created = cast(dict[str, Any], events[0]["job"])
    assert created["state"] == "queued"
    assert created["model_id"] == VOCALS_MODEL_ID
    assert created["configuration"]["device_id"] == FAKE_GPU.id

    stages = [cast(str, e["stage"]) for e in events if e["type"] == "job_stage_changed"]
    assert stages == [
        "preparing",
        "decoding",
        "loading_model",
        "separating",
        "post_processing",
        "encoding",
    ]

    progress = [e for e in events if e["type"] == "job_progress"]
    assert progress, "a real chunked separation reports progress"
    assert progress[-1]["progress"] == 1.0

    completed = cast(dict[str, Any], events[-1]["result"])
    assert completed["job_id"] == job_id
    assert [stem["name"] for stem in completed["stems"]] == ["vocals", "instrumental"]


# -- feature 026: capabilities, weights, and a loop that keeps serving -------


def write_catalog_with(models: list[dict[str, Any]], models_dir: Path) -> Path:
    """Write a one-off catalog so a test can describe the model it needs."""
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / CATALOG_FILENAME).write_text(
        json.dumps({"catalog_version": 1, "models": models}), encoding="utf-8"
    )
    return models_dir


def roformer_entry(**overrides: Any) -> dict[str, Any]:
    """A catalog entry for the real architecture, tuned tiny for tests."""
    entry: dict[str, Any] = {
        "schema_version": 1,
        "id": "tiny-vocals-001",
        "display_name": "Tiny Vocals",
        "architecture": ROFORMER_ARCHITECTURE,
        "version": "test",
        "separation_mode": "vocals",
        "stems": ["vocals", "instrumental"],
        "sample_rate": TINY_SAMPLE_RATE,
        "capabilities": {"cuda": True, "cpu": True},
        "default_inference_parameters": tiny_catalog_block(),
    }
    entry.update(overrides)
    return entry


async def post_job(app: FastAPI, audio: str, **overrides: Any) -> httpx2.Response:
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(JOBS_URL, json=configuration(audio, **overrides))


async def test_a_model_the_resolved_device_cannot_run_is_a_409(tmp_path: Path) -> None:
    """A CUDA-only model on a CPU-only host is refused at *create* time.

    The alternative — what the code did before feature 026 — was ``201``
    followed by a generic ``separation_failed`` event minutes later.
    """
    models_dir = write_catalog_with(
        [roformer_entry(capabilities={"cuda": True, "cpu": False})], tmp_path / "models"
    )
    app = create_app(Settings(data_dir=tmp_path / "data", models_dir=models_dir))
    app.state.device_detector = DeviceDetector(probes=[])  # CPU only
    audio = register_audio(app)

    error = assert_envelope(await post_job(app, audio), "model_device_unsupported", 409)
    assert error["detail"] == {
        "model_id": "tiny-vocals-001",
        "device_id": CPU_DEVICE_ID,
        "device_backend": "cpu",
        "supported_backends": ["cuda"],
    }


async def test_a_model_whose_weights_are_not_installed_is_a_409(tmp_path: Path) -> None:
    """``model_weights_missing``, not a crash and not a 500."""
    models_dir = write_catalog_with([roformer_entry()], tmp_path / "models")
    app = create_app(Settings(data_dir=tmp_path / "data", models_dir=models_dir))
    app.state.device_detector = DeviceDetector(probes=[])
    audio = register_audio(app)

    error = assert_envelope(await post_job(app, audio), "model_weights_missing", 409)
    assert error["detail"] == {"model_id": "tiny-vocals-001"}


async def test_installed_weights_make_the_same_request_succeed(tmp_path: Path) -> None:
    """The 409 above is about the weights, not about the wiring around them."""
    models_dir = write_catalog_with([roformer_entry()], tmp_path / "models")
    write_tiny_weights(weights_path(models_dir, "tiny-vocals-001"))
    app = create_app(Settings(data_dir=tmp_path / "data", models_dir=models_dir))
    app.state.device_detector = DeviceDetector(probes=[])
    audio = register_audio(app)

    response = await post_job(app, audio)
    assert response.status_code == 201, response.text
    body = cast(dict[str, Any], response.json())
    assert body["model_id"] == "tiny-vocals-001"
    assert body["configuration"]["device_id"] == CPU_DEVICE_ID


async def test_loading_a_separator_does_not_stall_the_application(tmp_path: Path) -> None:
    """``POST /jobs`` may not freeze the server while weights load.

    Feature 029 recorded this as 026's to fix: ``SeparatorRegistry.get()`` built
    on a cache miss *inside* the ``async def`` handler, so a real backend's
    several-hundred-megabyte load blocked the job worker, the event dispatcher,
    every other request and all WebSocket delivery. The proof is the same shape
    features 022 and 025 used: park the expensive step and serve other routes
    while it is parked.
    """
    entered = threading.Event()
    release = threading.Event()
    build_threads: set[int] = set()

    def gated_builder(model: Model) -> Separator:
        build_threads.add(threading.get_ident())
        entered.set()
        release.wait(timeout=30)
        return FakeSeparator(separator_info_from_model(model), chunk_delay_seconds=0.0)

    app = build_app(tmp_path)
    app.state.separator_registry = SeparatorRegistry({FAKE_ARCHITECTURE: gated_builder})
    audio = register_audio(app)

    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            creating = asyncio.create_task(client.post(JOBS_URL, json=configuration(audio)))
            await asyncio.to_thread(entered.wait, 30)

            # The separator is loading in a worker thread; the loop is free.
            assert (await client.get("/api/v1/health")).status_code == 200
            assert (await client.get("/api/v1/models")).status_code == 200
            assert (await client.get(JOBS_URL)).status_code == 200

            release.set()
            created = await creating

    assert created.status_code == 201, created.text
    assert threading.get_ident() not in build_threads


async def test_a_catalog_entry_this_build_cannot_run_is_a_500_from_create(tmp_path: Path) -> None:
    """Building the separator happens in the handler, so its faults answer here.

    ``model_parameters_invalid`` and ``model_weights_invalid`` are deployment
    faults, not client mistakes, and the REST contract lists them with the other
    job-creation errors because this is the request that surfaces them.
    """
    block = tiny_catalog_block()
    block["inference"]["chunk_size"] = 0
    models_dir = write_catalog_with(
        [roformer_entry(default_inference_parameters=block)], tmp_path / "models"
    )
    write_tiny_weights(weights_path(models_dir, "tiny-vocals-001"))
    app = create_app(Settings(data_dir=tmp_path / "data", models_dir=models_dir))
    app.state.device_detector = DeviceDetector(probes=[])
    audio = register_audio(app)

    error = assert_envelope(await post_job(app, audio), "model_parameters_invalid", 500)
    assert error["detail"]["model_id"] == "tiny-vocals-001"


async def test_weights_that_do_not_match_the_architecture_are_a_500_from_create(
    tmp_path: Path,
) -> None:
    models_dir = write_catalog_with([roformer_entry()], tmp_path / "models")
    # A checkpoint for a differently-shaped network of the same architecture.
    write_tiny_weights(weights_path(models_dir, "tiny-vocals-001"), num_bands=16)
    app = create_app(Settings(data_dir=tmp_path / "data", models_dir=models_dir))
    app.state.device_detector = DeviceDetector(probes=[])
    audio = register_audio(app)

    error = assert_envelope(await post_job(app, audio), "model_weights_invalid", 500)
    assert error["detail"]["model_id"] == "tiny-vocals-001"


# -- development fixtures (feature 032) --------------------------------------


def development_app(tmp_path: Path, *, development: bool) -> FastAPI:
    """The repository catalog, with its development fixtures on or off.

    ``build_app`` inherits the session's environment, which enables fixtures so
    that most of this module can separate audio at all. These tests are about
    what each state actually accepts, so the setting is passed explicitly.
    """
    app = create_app(Settings(data_dir=tmp_path / "data", include_development_models=development))
    app.state.device_detector = DeviceDetector(probes=[StaticProbe()])
    app.state.separator_registry = fast_registry()
    return app


async def test_a_job_cannot_select_a_hidden_fixture(tmp_path: Path) -> None:
    """The tier the fake model backed is simply not offered any more.

    ``vocals``/``balanced`` was ``fake-vocals-001`` — and the first option of
    its mode, which feature 011's UI preselects. With fixtures hidden the mode
    still exists (the RoFormer backs ``high_quality``) but the tier does not, so
    the request is refused with the code that already means that.
    """
    app = development_app(tmp_path, development=False)
    audio = register_audio(app)

    response = await post_job(app, audio, mode_id="vocals", quality_id="balanced")
    error = assert_envelope(response, "quality_option_not_found", 404)
    assert error["detail"] == {"mode_id": "vocals", "quality_id": "balanced"}


async def test_a_job_cannot_select_a_tier_only_a_fixture_backs(tmp_path: Path) -> None:
    """The fixture's ``fast`` tier is not a tier a default server offers.

    Until feature 028 the whole ``standard_stems`` mode vanished on a default
    server and this was a ``separation_mode_not_found``. The mode is back — the
    Demucs entry backs its ``balanced`` tier — so what is refused now is the one
    tier that is still only a fixture, and it is refused as an unknown quality.
    """
    app = development_app(tmp_path, development=False)
    audio = register_audio(app)

    response = await post_job(app, audio, mode_id="standard_stems", quality_id="fast")
    error = assert_envelope(response, "quality_option_not_found", 404)
    assert error["detail"] == {"mode_id": "standard_stems", "quality_id": "fast"}


async def test_the_same_request_succeeds_when_fixtures_are_enabled(tmp_path: Path) -> None:
    """The refusals above are the setting, not a broken job pipeline."""
    app = development_app(tmp_path, development=True)
    audio = register_audio(app)

    response = await post_job(app, audio, mode_id="vocals", quality_id="balanced")
    assert response.status_code == 201, response.text
    assert cast(dict[str, Any], response.json())["model_id"] == VOCALS_MODEL_ID
