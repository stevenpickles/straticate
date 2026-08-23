"""Tests for the WebSocket event hub.

Sockets are replaced with :class:`FakeSocket` doubles whose sends can be gated
with ``asyncio.Event``, so slow-client behaviour is deterministic — no sleeps
as synchronization anywhere in this module.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from straticate.jobs import EventHub, EventSocket, JobContext, JobExecutor, JobManager
from straticate.jobs.hub import (
    CLOSE_GOING_AWAY,
    CLOSE_INTERNAL_ERROR,
    CLOSE_TRY_AGAIN_LATER,
)
from straticate.schemas.common import ErrorInfo
from straticate.schemas.events import (
    GpuMetrics,
    JobCompletedEvent,
    JobCreatedEvent,
    JobFailedEvent,
    JobProgressEvent,
    JobStageChangedEvent,
    JobStartedEvent,
    ModelInfo,
    ProcessingMetrics,
    RuntimeMetricsEvent,
    WebSocketEvent,
)
from straticate.schemas.jobs import (
    Job,
    JobState,
    SeparationConfiguration,
    SeparationResult,
    SeparationResultMetrics,
    Stem,
)

WAIT_TIMEOUT = 5.0


class FakeSocket:
    """A minimal :class:`~straticate.jobs.hub.EventSocket` double.

    Records what was sent and how it was closed, can block inside
    ``send_text`` (``gate``) or fail there (``fail_on_send``), and exposes an
    event-driven :meth:`wait_for` so tests never poll or sleep.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.close_code: int | None = None
        self.send_attempts = 0
        self.fail_on_send = False
        self.gate: asyncio.Event | None = None
        self._changed = asyncio.Event()

    async def send_text(self, data: str) -> None:
        self.send_attempts += 1
        self._changed.set()
        if self.fail_on_send:
            raise RuntimeError("socket is broken")
        if self.gate is not None:
            await self.gate.wait()
        self.sent.append(data)
        self._changed.set()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_code = code
        self._changed.set()

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        """Await until ``predicate()`` holds, driven by socket state changes."""
        while not predicate():
            self._changed.clear()
            if predicate():
                return
            await asyncio.wait_for(self._changed.wait(), timeout=WAIT_TIMEOUT)

    def open_gate(self) -> None:
        """Release a gated sender (fails loudly if the socket was not gated)."""
        gate = self.gate
        assert gate is not None
        gate.set()

    def payloads(self) -> list[dict[str, Any]]:
        return [cast(dict[str, Any], json.loads(text)) for text in self.sent]

    def types(self) -> list[str]:
        return [cast(str, payload["type"]) for payload in self.payloads()]


def as_socket(socket: FakeSocket) -> EventSocket:
    """Narrow a double to the protocol the hub consumes."""
    return socket


@pytest.fixture
async def hub() -> AsyncIterator[EventHub]:
    h = EventHub()
    yield h
    await h.aclose()


# -- builders ---------------------------------------------------------------


def make_configuration() -> SeparationConfiguration:
    return SeparationConfiguration(
        audio_id="01AUDIO0000000000000000000",
        mode_id="vocals",
        quality_id="high_quality",
        device_id=None,
    )


def make_result(job_id: str, model_id: str = "model-1") -> SeparationResult:
    return SeparationResult(
        job_id=job_id,
        model_id=model_id,
        stems=[Stem(name="vocals", duration_seconds=1.0, sample_rate_hz=44100, channels=2)],
        metrics=SeparationResultMetrics(processing_seconds=0.5, realtime_factor=2.0),
    )


def make_progress(job_id: str = "01JOB", progress: float = 0.5) -> JobProgressEvent:
    return JobProgressEvent(
        type="job_progress",
        job_id=job_id,
        stage=JobState.SEPARATING,
        progress=progress,
        chunks_completed=31,
        chunks_total=48,
        elapsed_seconds=18.2,
        audio_processed_seconds=148.0,
        audio_total_seconds=227.4,
    )


def make_completed(job_id: str = "01JOB") -> JobCompletedEvent:
    return JobCompletedEvent(type="job_completed", job_id=job_id, result=make_result(job_id))


def make_failed(job_id: str = "01JOB") -> JobFailedEvent:
    return JobFailedEvent(
        type="job_failed",
        job_id=job_id,
        error=ErrorInfo(code="separation_failed", message="nope"),
    )


def make_created(job_id: str = "01JOB") -> JobCreatedEvent:
    job = Job(
        id=job_id,
        audio_id="01AUDIO0000000000000000000",
        configuration=make_configuration(),
        model_id="model-1",
        state=JobState.QUEUED,
        progress=0.0,
        created_at=datetime.now(UTC),
        started_at=None,
        finished_at=None,
        error=None,
        result=None,
    )
    return JobCreatedEvent(type="job_created", job_id=job_id, job=job)


def make_metrics(job_id: str = "01JOB") -> RuntimeMetricsEvent:
    return RuntimeMetricsEvent(
        type="runtime_metrics",
        job_id=job_id,
        model=ModelInfo(
            id="vocals-hq-001",
            display_name="Vocals — High Quality",
            architecture="mel_band_roformer",
            version="1.0",
            separation_mode="vocals",
            stem_count=2,
        ),
        gpu=GpuMetrics(
            device_id="cuda:0",
            name="NVIDIA GeForce RTX 5090",
            backend="cuda",
            memory_allocated_bytes=1,
            memory_peak_bytes=2,
            memory_total_bytes=3,
            utilization=0.91,
            temperature_celsius=63,
        ),
        processing=ProcessingMetrics(
            stage=JobState.SEPARATING,
            chunks_completed=31,
            chunks_total=48,
            elapsed_seconds=18.2,
            audio_processed_seconds=148.0,
            realtime_factor=7.9,
        ),
    )


def gated_executor(gate: asyncio.Event) -> JobExecutor:
    """An executor that reaches ``separating`` and then waits for ``gate``."""

    async def executor(job: Job, context: JobContext) -> SeparationResult:
        context.set_stage(JobState.SEPARATING)
        await gate.wait()
        context.report_progress(1.0, 4, 4, 10.0, 10.0)
        return make_result(job.id)

    return executor


async def blocked_client(hub: EventHub) -> FakeSocket:
    """Register a client and park its sender task inside ``send_text``."""
    socket = FakeSocket()
    socket.gate = asyncio.Event()
    hub.register(as_socket(socket))
    hub.publish(make_created())
    await socket.wait_for(lambda: socket.send_attempts == 1)
    return socket


# -- construction -----------------------------------------------------------


def test_client_queue_size_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        EventHub(client_queue_size=0)


async def test_publish_without_clients_is_a_noop(hub: EventHub) -> None:
    hub.publish(make_progress())
    assert hub.connection_count == 0


async def test_register_is_idempotent(hub: EventHub) -> None:
    socket = FakeSocket()
    hub.register(as_socket(socket))
    hub.register(as_socket(socket))
    assert hub.connection_count == 1

    hub.publish(make_progress())
    await socket.wait_for(lambda: len(socket.sent) == 1)
    assert socket.types() == ["job_progress"]


# -- fan-out and serialization ----------------------------------------------


async def test_all_clients_receive_the_same_serialized_event(hub: EventHub) -> None:
    first, second = FakeSocket(), FakeSocket()
    hub.register(as_socket(first))
    hub.register(as_socket(second))

    hub.publish(make_progress())

    await first.wait_for(lambda: len(first.sent) == 1)
    await second.wait_for(lambda: len(second.sent) == 1)
    assert first.sent == second.sent
    assert hub.connection_count == 2


async def test_job_progress_serializes_to_the_documented_shape(hub: EventHub) -> None:
    socket = FakeSocket()
    hub.register(as_socket(socket))

    hub.publish(make_progress(job_id="01JOBPROGRESS", progress=0.65))

    await socket.wait_for(lambda: len(socket.sent) == 1)
    assert socket.payloads()[0] == {
        "type": "job_progress",
        "job_id": "01JOBPROGRESS",
        "stage": "separating",
        "progress": 0.65,
        "chunks_completed": 31,
        "chunks_total": 48,
        "elapsed_seconds": 18.2,
        "audio_processed_seconds": 148.0,
        "audio_total_seconds": 227.4,
    }


async def test_job_completed_serializes_to_the_documented_shape(hub: EventHub) -> None:
    socket = FakeSocket()
    hub.register(as_socket(socket))

    hub.publish(make_completed(job_id="01JOBDONE"))

    await socket.wait_for(lambda: len(socket.sent) == 1)
    payload = socket.payloads()[0]
    assert payload["type"] == "job_completed"
    assert payload["job_id"] == "01JOBDONE"
    result = cast(dict[str, Any], payload["result"])
    assert result["job_id"] == "01JOBDONE"
    assert result["model_id"] == "model-1"
    assert result["stems"] == [
        {"name": "vocals", "duration_seconds": 1.0, "sample_rate_hz": 44100, "channels": 2}
    ]
    assert result["metrics"] == {"processing_seconds": 0.5, "realtime_factor": 2.0}


async def test_job_stage_changed_serializes_to_the_documented_shape(hub: EventHub) -> None:
    socket = FakeSocket()
    hub.register(as_socket(socket))

    hub.publish(
        JobStageChangedEvent(
            type="job_stage_changed",
            job_id="01JOB",
            stage=JobState.SEPARATING,
            previous_stage=JobState.LOADING_MODEL,
        )
    )

    await socket.wait_for(lambda: len(socket.sent) == 1)
    assert socket.payloads()[0] == {
        "type": "job_stage_changed",
        "job_id": "01JOB",
        "stage": "separating",
        "previous_stage": "loading_model",
    }


async def test_timestamps_serialize_as_json_strings(hub: EventHub) -> None:
    socket = FakeSocket()
    hub.register(as_socket(socket))

    started_at = datetime(2026, 8, 23, 12, 0, 5, tzinfo=UTC)
    hub.publish(JobStartedEvent(type="job_started", job_id="01JOB", started_at=started_at))

    await socket.wait_for(lambda: len(socket.sent) == 1)
    payload = socket.payloads()[0]
    assert payload["type"] == "job_started"
    started = cast(str, payload["started_at"])
    assert started.startswith("2026-08-23T12:00:05")


async def test_runtime_metrics_events_are_forwarded_unchanged(hub: EventHub) -> None:
    """Feature 019 publishes through the same hook; the hub never special-cases."""
    socket = FakeSocket()
    hub.register(as_socket(socket))

    event = make_metrics()
    hub.publish(event)

    await socket.wait_for(lambda: len(socket.sent) == 1)
    assert socket.payloads()[0] == event.model_dump(mode="json")


async def test_delivery_preserves_publication_order(hub: EventHub) -> None:
    socket = FakeSocket()
    hub.register(as_socket(socket))

    events: list[WebSocketEvent] = [
        make_created(),
        make_progress(progress=0.25),
        make_progress(progress=0.75),
        make_completed(),
    ]
    for event in events:
        hub.publish(event)

    await socket.wait_for(lambda: len(socket.sent) == len(events))
    assert socket.types() == ["job_created", "job_progress", "job_progress", "job_completed"]


# -- failure isolation ------------------------------------------------------


async def test_failing_client_is_unregistered_and_others_keep_receiving(hub: EventHub) -> None:
    broken, healthy = FakeSocket(), FakeSocket()
    broken.fail_on_send = True
    hub.register(as_socket(broken))
    hub.register(as_socket(healthy))

    hub.publish(make_progress(progress=0.1))

    await broken.wait_for(lambda: broken.close_code is not None)
    assert broken.close_code == CLOSE_INTERNAL_ERROR
    assert broken.sent == []
    assert hub.connection_count == 1

    hub.publish(make_completed())
    await healthy.wait_for(lambda: len(healthy.sent) == 2)
    assert healthy.types() == ["job_progress", "job_completed"]
    assert broken.send_attempts == 1  # never written to again


async def test_unregister_stops_delivery_and_is_idempotent(hub: EventHub) -> None:
    socket = FakeSocket()
    hub.register(as_socket(socket))
    hub.publish(make_progress())
    await socket.wait_for(lambda: len(socket.sent) == 1)

    await hub.unregister(as_socket(socket))
    await hub.unregister(as_socket(socket))
    await hub.unregister(as_socket(FakeSocket()))
    assert hub.connection_count == 0

    hub.publish(make_completed())
    assert socket.types() == ["job_progress"]
    assert socket.close_code is None  # the endpoint owns the socket, not the hub


# -- backpressure / overflow policy -----------------------------------------


async def test_overflow_drops_the_oldest_progress_frame() -> None:
    hub = EventHub(client_queue_size=2)
    try:
        socket = await blocked_client(hub)

        hub.publish(make_progress(progress=0.1))  # buffered
        hub.publish(make_progress(progress=0.2))  # buffer now full
        hub.publish(make_progress(progress=0.3))  # evicts progress 0.1

        socket.open_gate()
        await socket.wait_for(lambda: len(socket.sent) == 3)
        assert socket.types() == ["job_created", "job_progress", "job_progress"]
        assert [p["progress"] for p in socket.payloads()[1:]] == [0.2, 0.3]
        assert hub.connection_count == 1
    finally:
        await hub.aclose()


async def test_overflow_never_drops_a_terminal_event() -> None:
    hub = EventHub(client_queue_size=2)
    try:
        socket = await blocked_client(hub)

        hub.publish(make_progress(progress=0.1))
        hub.publish(make_progress(progress=0.2))  # buffer full
        hub.publish(make_completed())  # evicts progress 0.1, keeps the terminal event

        socket.open_gate()
        await socket.wait_for(lambda: len(socket.sent) == 3)
        assert socket.types() == ["job_created", "job_progress", "job_completed"]
        assert socket.payloads()[1]["progress"] == 0.2
        assert hub.connection_count == 1
    finally:
        await hub.aclose()


async def test_overflow_disconnects_a_client_buffered_full_of_undroppable_state() -> None:
    hub = EventHub(client_queue_size=1)
    try:
        socket = await blocked_client(hub)

        hub.publish(make_failed())  # buffer full with a terminal event
        hub.publish(make_progress())  # nothing evictable -> disconnect, never truncate

        await socket.wait_for(lambda: socket.close_code is not None)
        assert socket.close_code == CLOSE_TRY_AGAIN_LATER
        assert hub.connection_count == 0
        socket.open_gate()
    finally:
        await hub.aclose()


async def test_a_blocked_client_never_delays_other_clients() -> None:
    hub = EventHub(client_queue_size=2)
    try:
        blocked = await blocked_client(hub)
        fast = FakeSocket()
        hub.register(as_socket(fast))

        async def fast_received(count: int) -> None:
            await fast.wait_for(lambda: len(fast.sent) == count)

        for index in range(6):
            hub.publish(make_progress(progress=index / 10))
            await fast_received(index + 1)
        hub.publish(make_completed())
        await fast_received(7)

        assert fast.types()[-1] == "job_completed"
        assert blocked.sent == []  # still parked in its very first send
        blocked.open_gate()
    finally:
        await hub.aclose()


# -- shutdown ---------------------------------------------------------------


async def test_aclose_closes_every_connection_and_disables_the_hub() -> None:
    hub = EventHub()
    first, second = FakeSocket(), FakeSocket()
    hub.register(as_socket(first))
    hub.register(as_socket(second))

    await hub.aclose()
    await hub.aclose()  # idempotent

    assert first.close_code == CLOSE_GOING_AWAY
    assert second.close_code == CLOSE_GOING_AWAY
    assert hub.connection_count == 0
    assert hub.is_closed

    hub.publish(make_completed())
    assert first.sent == []
    with pytest.raises(RuntimeError, match="closed"):
        hub.register(as_socket(FakeSocket()))


async def test_aclose_tolerates_a_socket_that_fails_to_close() -> None:
    class HostileSocket(FakeSocket):
        async def close(self, code: int = 1000, reason: str | None = None) -> None:
            raise RuntimeError("already gone")

    hub = EventHub()
    hub.register(as_socket(HostileSocket()))
    healthy = FakeSocket()
    hub.register(as_socket(healthy))

    await hub.aclose()

    assert healthy.close_code == CLOSE_GOING_AWAY


# -- integration with a real JobManager -------------------------------------


async def test_hub_listener_never_stalls_the_manager_dispatcher() -> None:
    """A wedged client must delay neither other clients nor the job pipeline."""
    manager = JobManager()
    hub = EventHub(client_queue_size=8)
    manager.add_listener(hub.publish)
    manager.start()
    try:
        blocked = await blocked_client(hub)
        fast = FakeSocket()
        hub.register(as_socket(fast))

        release = asyncio.Event()
        job = manager.submit(make_configuration(), gated_executor(release), model_id="model-1")

        # The pipeline reaches `separating` even though `blocked` is wedged.
        await fast.wait_for(lambda: len(fast.sent) == 4)
        assert blocked.sent == []

        release.set()
        await fast.wait_for(lambda: "job_completed" in fast.types())
        assert fast.types() == [
            "job_created",
            "job_started",
            "job_stage_changed",
            "job_stage_changed",
            "job_progress",
            "job_completed",
        ]
        assert all(payload["job_id"] == job.id for payload in fast.payloads())
        assert manager.get(job.id).state is JobState.COMPLETED
        blocked.open_gate()
    finally:
        await manager.aclose()
        await hub.aclose()


async def test_hub_client_receives_the_full_manager_event_sequence(hub: EventHub) -> None:
    manager = JobManager()
    manager.add_listener(hub.publish)
    manager.start()
    try:
        socket = FakeSocket()
        hub.register(as_socket(socket))

        release = asyncio.Event()
        release.set()
        job = manager.submit(make_configuration(), gated_executor(release), model_id="model-1")

        await socket.wait_for(lambda: "job_completed" in socket.types())
        payloads = socket.payloads()
        assert [cast(str, p["type"]) for p in payloads] == [
            "job_created",
            "job_started",
            "job_stage_changed",
            "job_stage_changed",
            "job_progress",
            "job_completed",
        ]
        created = cast(dict[str, Any], payloads[0]["job"])
        assert created["id"] == job.id
        stages = [p["stage"] for p in payloads if p["type"] == "job_stage_changed"]
        assert stages == ["preparing", "separating"]
    finally:
        await manager.aclose()


async def test_manager_events_reach_two_hub_clients(hub: EventHub) -> None:
    manager = JobManager()
    manager.add_listener(hub.publish)
    manager.start()
    try:
        first, second = FakeSocket(), FakeSocket()
        hub.register(as_socket(first))
        hub.register(as_socket(second))

        release = asyncio.Event()
        release.set()
        manager.submit(make_configuration(), gated_executor(release), model_id="model-1")

        await first.wait_for(lambda: "job_completed" in first.types())
        await second.wait_for(lambda: "job_completed" in second.types())
        assert first.sent == second.sent
    finally:
        await manager.aclose()
