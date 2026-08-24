"""Tests for the runtime telemetry sampler (feature 019).

Three tiers, in this order:

- **Unit** — the sampler driven by hand against a recording :class:`EventHub`
  double and a stub separator, so event construction, the lifecycle and every
  degenerate case are exact.
- **Wiring** — the application lifespan: a fresh sampler per cycle, wired to
  the manager, and the ``sampler → manager → hub`` shutdown order.
- **End to end** — a real job through a real ``JobManager``, real ``EventHub``
  and the real ``FakeSeparator``, observed through a fake socket.

Coordination is :class:`asyncio.Event`-driven throughout; no ``sleep`` is ever
used as synchronization. Where a test must assert that *nothing* happens, it
gives the loop a bounded number of ticks (:func:`drain_loop`) while the
sampler's interval is long enough that a further sample could not be due.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI

from straticate.config import Settings
from straticate.inference import (
    FAKE_ARCHITECTURE,
    DeviceStats,
    ProcessingStats,
    SeparationProgress,
    Separator,
    SeparatorInfo,
    SeparatorRegistry,
    SeparatorRuntimeStats,
    fake_separator_builder,
)
from straticate.jobs import CancellationToken, EventHub, EventSocket, JobEvent, JobManager
from straticate.main import create_app, lifespan
from straticate.schemas import AudioFile, AudioMetadata, Model
from straticate.schemas.common import ErrorInfo
from straticate.schemas.events import (
    JobCancelledEvent,
    JobCompletedEvent,
    JobFailedEvent,
    JobStartedEvent,
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
from straticate.telemetry import DEFAULT_SAMPLE_INTERVAL_SECONDS, TelemetrySampler
from tests.audio_fixtures import write_tone_wav

WAIT_TIMEOUT = 30.0

IDLE_INTERVAL = 60.0
"""A sampling interval long enough that only the loop's opening tick fires.

The sampling loop samples first and *then* sleeps, so with this interval a
started job produces exactly one sample — which makes "nothing more was
published" a deterministic assertion rather than a race.
"""

BUSY_INTERVAL = 0.001
"""A sampling interval for tests that need repeated ticks."""

JOBS_URL = "/api/v1/jobs"
VOCALS_MODEL_ID = "fake-vocals-001"
JOB_ID = "01JOB0000000000000000000A"
OTHER_JOB_ID = "01JOB0000000000000000000B"

STUB_INFO = SeparatorInfo(
    model_id="stub-001",
    display_name="Stub — High Quality",
    architecture="stub",
    version="1.0",
    separation_mode="vocals",
    stems=("vocals", "instrumental"),
    sample_rate=44100,
)

MODEL_KEYS = {
    "id",
    "display_name",
    "architecture",
    "version",
    "separation_mode",
    "stem_count",
}
GPU_KEYS = {
    "device_id",
    "name",
    "backend",
    "memory_allocated_bytes",
    "memory_peak_bytes",
    "memory_total_bytes",
    "utilization",
    "temperature_celsius",
}
PROCESSING_KEYS = {
    "stage",
    "chunks_completed",
    "chunks_total",
    "elapsed_seconds",
    "audio_processed_seconds",
    "realtime_factor",
}


# -- doubles ----------------------------------------------------------------


class RecordingHub(EventHub):
    """An :class:`EventHub` that records publications instead of sending them.

    It also counts how often :attr:`connection_count` was consulted, which is
    what lets the "nobody is listening" test observe ticks that deliberately
    build nothing at all.
    """

    def __init__(self, *, connections: int = 1) -> None:
        super().__init__()
        self.published: list[RuntimeMetricsEvent] = []
        self.connections = connections
        self.polls = 0
        self.changed = asyncio.Event()

    @property
    def connection_count(self) -> int:
        self.polls += 1
        self.changed.set()
        return self.connections

    def publish(self, event: WebSocketEvent) -> None:
        self.published.append(cast(RuntimeMetricsEvent, event))
        self.changed.set()

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        """Await until ``predicate()`` holds, driven by hub activity."""
        while not predicate():
            self.changed.clear()
            if predicate():
                return
            await asyncio.wait_for(self.changed.wait(), timeout=WAIT_TIMEOUT)


class StubSeparator:
    """A :class:`Separator` that only answers ``runtime_stats()``.

    ``fail_times`` makes the first *n* calls raise, which is how the "a bad
    tick is swallowed and sampling continues" test is made deterministic.
    """

    def __init__(
        self,
        stats: SeparatorRuntimeStats | None = None,
        *,
        fail_times: int = 0,
    ) -> None:
        self.stats = stats
        self.fail_times = fail_times
        self.calls = 0

    @property
    def info(self) -> SeparatorInfo:
        return STUB_INFO

    def runtime_stats(self) -> SeparatorRuntimeStats | None:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("the statistics accessor exploded")
        return self.stats

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
        raise AssertionError("this stub never separates")  # pragma: no cover


class ImmediateSeparator:
    """A separator with statistics from the instant its first stage begins.

    Used for the register-vs-start race: ``separate`` publishes-worthy stats
    exist before the separator yields, and it does not finish until the test
    has seen a ``runtime_metrics`` — so a sampler that missed the start would
    deadlock the test rather than pass it by luck.
    """

    def __init__(self, info: SeparatorInfo, *, gate: asyncio.Event) -> None:
        self._info = info
        self._gate = gate
        self._stats: SeparatorRuntimeStats | None = None

    @property
    def info(self) -> SeparatorInfo:
        return self._info

    def runtime_stats(self) -> SeparatorRuntimeStats | None:
        return self._stats

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
        self._stats = make_stats(job_id, model=self._info)
        await self._gate.wait()
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


class FakeSocket:
    """A minimal :class:`~straticate.jobs.EventSocket` double for the hub."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.close_code: int | None = None
        self._changed = asyncio.Event()

    async def send_text(self, data: str) -> None:
        self.sent.append(data)
        self._changed.set()

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_code = code
        self._changed.set()

    async def wait_for(self, predicate: Callable[[], bool]) -> None:
        while not predicate():
            self._changed.clear()
            if predicate():
                return
            await asyncio.wait_for(self._changed.wait(), timeout=WAIT_TIMEOUT)

    def payloads(self) -> list[dict[str, Any]]:
        return [cast(dict[str, Any], json.loads(text)) for text in self.sent]

    def types(self) -> list[str]:
        return [cast(str, payload["type"]) for payload in self.payloads()]


def as_separator(separator: StubSeparator | ImmediateSeparator) -> Separator:
    """Narrow a double to the protocol the sampler consumes."""
    return separator


def as_socket(socket: FakeSocket) -> EventSocket:
    """Narrow a double to the protocol the hub consumes."""
    return socket


# -- builders ---------------------------------------------------------------


def make_stats(
    job_id: str = JOB_ID,
    *,
    device: bool = True,
    model: SeparatorInfo = STUB_INFO,
) -> SeparatorRuntimeStats:
    """A snapshot shaped like the example in the WebSocket contract."""
    return SeparatorRuntimeStats(
        job_id=job_id,
        model=model,
        device=(
            DeviceStats(
                device_id="cuda:0",
                name="NVIDIA GeForce RTX 5090",
                backend="cuda",
                memory_allocated_bytes=9234179686,
                memory_peak_bytes=10133099161,
                memory_total_bytes=34359738368,
                utilization=0.91,
                temperature_celsius=63.0,
            )
            if device
            else None
        ),
        processing=ProcessingStats(
            stage=JobState.SEPARATING,
            chunks_completed=31,
            chunks_total=48,
            elapsed_seconds=18.2,
            audio_processed_seconds=148.0,
            audio_total_seconds=227.4,
            realtime_factor=7.9,
            last_chunk_seconds=0.4,
            mean_chunk_seconds=0.5,
        ),
    )


def started(job_id: str = JOB_ID) -> JobStartedEvent:
    return JobStartedEvent(type="job_started", job_id=job_id, started_at=datetime.now(UTC))


def completed(job_id: str = JOB_ID) -> JobCompletedEvent:
    return JobCompletedEvent(
        type="job_completed",
        job_id=job_id,
        result=SeparationResult(
            job_id=job_id,
            model_id=STUB_INFO.model_id,
            stems=[Stem(name="vocals", duration_seconds=1.0, sample_rate_hz=44100, channels=2)],
            metrics=SeparationResultMetrics(processing_seconds=0.5, realtime_factor=2.0),
        ),
    )


def cancelled(job_id: str = JOB_ID) -> JobCancelledEvent:
    return JobCancelledEvent(
        type="job_cancelled", job_id=job_id, stage_at_cancellation=JobState.SEPARATING
    )


def failed(job_id: str = JOB_ID) -> JobFailedEvent:
    return JobFailedEvent(
        type="job_failed",
        job_id=job_id,
        error=ErrorInfo(code="separation_failed", message="nope"),
    )


TERMINAL_BUILDERS: list[Callable[[str], JobEvent]] = [completed, cancelled, failed]
TERMINAL_IDS = ["job_completed", "job_cancelled", "job_failed"]


async def drain_loop(times: int = 5) -> None:
    """Give the event loop a bounded number of ticks.

    Used only to assert that nothing further happens; never to wait *for*
    something (that is always :class:`asyncio.Event`-gated).
    """
    for _ in range(times):
        await asyncio.sleep(0)


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def hub() -> RecordingHub:
    return RecordingHub()


@pytest.fixture
async def sampler(hub: RecordingHub) -> AsyncIterator[TelemetrySampler]:
    instance = TelemetrySampler(hub, interval_seconds=IDLE_INTERVAL)
    yield instance
    await instance.aclose()


# -- event construction -----------------------------------------------------


def test_sample_once_builds_the_event_from_the_three_projections(
    sampler: TelemetrySampler,
) -> None:
    """``model``/``gpu``/``processing`` are the documented projections, verbatim."""
    stats = make_stats()
    sampler.register(JOB_ID, as_separator(StubSeparator(stats)))

    event = sampler.sample_once(JOB_ID)

    assert event is not None
    assert event.type == "runtime_metrics"
    assert event.job_id == JOB_ID
    assert event.model == stats.model.to_model_info()
    assert stats.device is not None
    assert event.gpu == stats.device.to_gpu_metrics()
    assert event.processing == stats.processing.to_processing_metrics()


def test_sample_once_matches_the_websocket_contract_shape(sampler: TelemetrySampler) -> None:
    """The serialized event has exactly the fields docs/contracts describes."""
    sampler.register(JOB_ID, as_separator(StubSeparator(make_stats())))

    event = sampler.sample_once(JOB_ID)

    assert event is not None
    payload = cast(dict[str, Any], json.loads(json.dumps(event.model_dump(mode="json"))))
    assert set(payload) == {"type", "job_id", "model", "gpu", "processing"}
    assert set(cast(dict[str, Any], payload["model"])) == MODEL_KEYS
    assert set(cast(dict[str, Any], payload["gpu"])) == GPU_KEYS
    assert set(cast(dict[str, Any], payload["processing"])) == PROCESSING_KEYS
    assert payload["processing"]["stage"] == "separating"
    assert payload["gpu"]["utilization"] == 0.91


def test_a_separator_without_a_device_publishes_gpu_null(sampler: TelemetrySampler) -> None:
    """``device=None`` is the contract's "running on CPU" shape."""
    sampler.register(JOB_ID, as_separator(StubSeparator(make_stats(device=False))))

    event = sampler.sample_once(JOB_ID)

    assert event is not None
    assert event.gpu is None
    assert event.model_dump(mode="json")["gpu"] is None


def test_no_event_without_statistics(sampler: TelemetrySampler) -> None:
    """A separator that has not run yet publishes nothing."""
    sampler.register(JOB_ID, as_separator(StubSeparator(None)))

    assert sampler.sample_once(JOB_ID) is None


def test_no_event_for_a_snapshot_belonging_to_another_job(sampler: TelemetrySampler) -> None:
    """A separator is reused across jobs; a stale snapshot must never be published."""
    sampler.register(JOB_ID, as_separator(StubSeparator(make_stats(OTHER_JOB_ID))))

    assert sampler.sample_once(JOB_ID) is None


def test_no_event_for_an_unregistered_job(sampler: TelemetrySampler) -> None:
    assert sampler.sample_once(JOB_ID) is None


def test_a_non_positive_interval_is_rejected(hub: RecordingHub) -> None:
    with pytest.raises(ValueError, match="interval_seconds"):
        TelemetrySampler(hub, interval_seconds=0.0)


# -- sampling lifecycle -----------------------------------------------------


async def test_nothing_is_published_before_the_job_starts(
    sampler: TelemetrySampler, hub: RecordingHub
) -> None:
    sampler.register(JOB_ID, as_separator(StubSeparator(make_stats())))

    await drain_loop()

    assert hub.published == []
    assert sampler.active_job_id is None


async def test_sampling_starts_on_job_started(sampler: TelemetrySampler, hub: RecordingHub) -> None:
    sampler.register(JOB_ID, as_separator(StubSeparator(make_stats())))

    sampler.on_job_event(started())
    await hub.wait_for(lambda: len(hub.published) >= 1)

    assert sampler.active_job_id == JOB_ID
    assert hub.published[0].job_id == JOB_ID
    assert hub.published[0].type == "runtime_metrics"


@pytest.mark.parametrize("terminal", TERMINAL_BUILDERS, ids=TERMINAL_IDS)
async def test_sampling_stops_on_a_terminal_event(
    sampler: TelemetrySampler,
    hub: RecordingHub,
    terminal: Callable[[str], JobEvent],
) -> None:
    """Every terminal event ends sampling and drops the registration."""
    sampler.register(JOB_ID, as_separator(StubSeparator(make_stats())))
    sampler.on_job_event(started())
    await hub.wait_for(lambda: len(hub.published) >= 1)
    published = len(hub.published)

    sampler.on_job_event(terminal(JOB_ID))
    await drain_loop()

    assert len(hub.published) == published
    assert sampler.active_job_id is None
    assert sampler.registered_job_ids == frozenset()


@pytest.mark.parametrize("terminal", TERMINAL_BUILDERS, ids=TERMINAL_IDS)
async def test_a_terminal_event_drops_the_registration_of_a_job_that_never_started(
    sampler: TelemetrySampler,
    terminal: Callable[[str], JobEvent],
) -> None:
    """A job cancelled while queued must not leak its registration either."""
    sampler.register(JOB_ID, as_separator(StubSeparator(make_stats())))

    sampler.on_job_event(terminal(JOB_ID))

    assert sampler.registered_job_ids == frozenset()


async def test_a_terminal_event_for_another_job_leaves_sampling_alone(
    sampler: TelemetrySampler, hub: RecordingHub
) -> None:
    sampler.register(JOB_ID, as_separator(StubSeparator(make_stats())))
    sampler.register(OTHER_JOB_ID, as_separator(StubSeparator(None)))
    sampler.on_job_event(started())
    await hub.wait_for(lambda: len(hub.published) >= 1)

    sampler.on_job_event(completed(OTHER_JOB_ID))

    assert sampler.active_job_id == JOB_ID
    assert sampler.registered_job_ids == frozenset({JOB_ID})


async def test_nothing_is_published_while_no_client_is_connected(hub: RecordingHub) -> None:
    """A tick with zero connections builds nothing — it does not even sample."""
    hub.connections = 0
    sampler = TelemetrySampler(hub, interval_seconds=BUSY_INTERVAL)
    separator = StubSeparator(make_stats())
    sampler.register(JOB_ID, as_separator(separator))
    try:
        sampler.on_job_event(started())
        await hub.wait_for(lambda: hub.polls >= 3)

        assert hub.published == []
        assert separator.calls == 0
    finally:
        await sampler.aclose()


async def test_a_failing_tick_is_swallowed_and_sampling_continues(hub: RecordingHub) -> None:
    """One bad tick neither kills the sampler nor escapes it."""
    sampler = TelemetrySampler(hub, interval_seconds=BUSY_INTERVAL)
    separator = StubSeparator(make_stats(), fail_times=1)
    sampler.register(JOB_ID, as_separator(separator))
    try:
        sampler.on_job_event(started())
        await hub.wait_for(lambda: len(hub.published) >= 2)

        assert separator.calls >= 3  # the failing call plus the ones that worked
        assert sampler.active_job_id == JOB_ID
    finally:
        await sampler.aclose()


async def test_the_listener_never_raises(
    sampler: TelemetrySampler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An internal failure is logged, never dispatched back into the manager."""

    def boom(self: TelemetrySampler, job_id: str) -> None:
        raise RuntimeError("sampler is broken")

    monkeypatch.setattr(TelemetrySampler, "_start", boom)

    sampler.on_job_event(started())  # must not raise

    assert sampler.active_job_id is None


# -- the register-vs-start race ---------------------------------------------


async def test_registering_after_the_job_started_still_samples(
    sampler: TelemetrySampler, hub: RecordingHub
) -> None:
    """Belt and braces: a late registration starts sampling immediately.

    The endpoint registers with no ``await`` between ``submit`` and
    ``register``, so this ordering cannot occur through the API — but any other
    producer that gets it wrong loses one sample, not the job's whole telemetry.
    """
    sampler.on_job_event(started())
    await drain_loop()
    assert hub.published == []
    assert sampler.active_job_id is None

    sampler.register(JOB_ID, as_separator(StubSeparator(make_stats())))
    await hub.wait_for(lambda: len(hub.published) >= 1)

    assert sampler.active_job_id == JOB_ID


async def test_a_late_registration_for_a_finished_job_does_not_start_sampling(
    sampler: TelemetrySampler, hub: RecordingHub
) -> None:
    sampler.on_job_event(started())
    sampler.on_job_event(completed())

    sampler.register(JOB_ID, as_separator(StubSeparator(make_stats())))
    await drain_loop()

    assert hub.published == []
    assert sampler.active_job_id is None


# -- shutdown ---------------------------------------------------------------


async def test_aclose_cancels_sampling_and_drops_every_registration(
    hub: RecordingHub,
) -> None:
    sampler = TelemetrySampler(hub, interval_seconds=IDLE_INTERVAL)
    sampler.register(JOB_ID, as_separator(StubSeparator(make_stats())))
    sampler.register(OTHER_JOB_ID, as_separator(StubSeparator(None)))
    sampler.on_job_event(started())
    await hub.wait_for(lambda: len(hub.published) >= 1)
    published = len(hub.published)

    await sampler.aclose()
    await drain_loop()

    assert sampler.is_closed
    assert sampler.registered_job_ids == frozenset()
    assert sampler.active_job_id is None
    assert len(hub.published) == published


async def test_aclose_is_safe_twice(hub: RecordingHub) -> None:
    sampler = TelemetrySampler(hub, interval_seconds=IDLE_INTERVAL)
    sampler.register(JOB_ID, as_separator(StubSeparator(make_stats())))
    sampler.on_job_event(started())

    await sampler.aclose()
    await sampler.aclose()

    assert sampler.is_closed


async def test_a_closed_sampler_starts_nothing(hub: RecordingHub) -> None:
    sampler = TelemetrySampler(hub, interval_seconds=BUSY_INTERVAL)
    await sampler.aclose()

    sampler.register(JOB_ID, as_separator(StubSeparator(make_stats())))
    sampler.on_job_event(started())
    await drain_loop()

    assert sampler.registered_job_ids == frozenset()
    assert sampler.active_job_id is None
    assert hub.published == []


# -- application wiring -----------------------------------------------------


CLOSE_ORDER: list[str] = []
"""Records the order in which the lifespan tears its components down."""


class ScriptedJobManager(JobManager):
    async def aclose(self) -> None:
        await super().aclose()
        CLOSE_ORDER.append("manager")


class ScriptedEventHub(EventHub):
    async def aclose(self, *, drain_timeout: float = 0.0) -> None:
        await super().aclose(drain_timeout=drain_timeout)
        CLOSE_ORDER.append("hub")


class ScriptedSampler(TelemetrySampler):
    async def aclose(self) -> None:
        await super().aclose()
        CLOSE_ORDER.append("sampler")


class FastSampler(TelemetrySampler):
    """The real sampler with a test-sized interval, injected into the lifespan."""

    def __init__(self, hub: EventHub, *, interval_seconds: float = BUSY_INTERVAL) -> None:
        super().__init__(hub, interval_seconds=interval_seconds)


async def test_the_lifespan_creates_a_fresh_sampler_per_cycle_and_closes_it() -> None:
    app = FastAPI()

    async with lifespan(app):
        first = cast(TelemetrySampler, app.state.telemetry_sampler)
        assert not first.is_closed
        assert first.interval_seconds == DEFAULT_SAMPLE_INTERVAL_SECONDS
    assert first.is_closed

    async with lifespan(app):
        second = cast(TelemetrySampler, app.state.telemetry_sampler)
        assert second is not first
        assert not second.is_closed
    assert second.is_closed


async def test_the_lifespan_wires_the_sampler_to_the_job_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sampler's listener is registered, so a starting job begins sampling."""
    monkeypatch.setattr("straticate.main.TelemetrySampler", FastSampler)
    app = FastAPI()
    running = asyncio.Event()

    async def wedged(job: Job, context: object) -> SeparationResult:
        running.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    async with lifespan(app):
        manager = cast(JobManager, app.state.job_manager)
        sampler = cast(TelemetrySampler, app.state.telemetry_sampler)
        hub = cast(EventHub, app.state.event_hub)
        socket = FakeSocket()
        hub.register(as_socket(socket))
        job = manager.submit(
            SeparationConfiguration(
                audio_id="01AUDIO0000000000000000000",
                mode_id="vocals",
                quality_id="balanced",
                device_id=None,
            ),
            cast(Any, wedged),
            model_id=STUB_INFO.model_id,
        )
        sampler.register(job.id, as_separator(StubSeparator(make_stats(job.id))))
        await asyncio.wait_for(running.wait(), timeout=WAIT_TIMEOUT)
        await socket.wait_for(lambda: "runtime_metrics" in socket.types())

        assert sampler.active_job_id == job.id

    assert sampler.is_closed
    assert sampler.registered_job_ids == frozenset()


async def test_the_lifespan_closes_the_sampler_then_the_manager_then_the_hub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing may be sampled into a closing hub, and the hub closes last."""
    CLOSE_ORDER.clear()
    monkeypatch.setattr("straticate.main.JobManager", ScriptedJobManager)
    monkeypatch.setattr("straticate.main.EventHub", ScriptedEventHub)
    monkeypatch.setattr("straticate.main.TelemetrySampler", ScriptedSampler)
    app = FastAPI()

    async with lifespan(app):
        pass

    assert CLOSE_ORDER == ["sampler", "manager", "hub"]


async def test_the_hub_is_closed_even_when_the_sampler_fails_to_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sampler teardown failure must not leave the manager or hub running."""

    class ExplodingSampler(TelemetrySampler):
        async def aclose(self) -> None:
            await super().aclose()
            raise RuntimeError("sampler teardown exploded")

    monkeypatch.setattr("straticate.main.TelemetrySampler", ExplodingSampler)
    app = FastAPI()

    with pytest.raises(RuntimeError, match="exploded"):
        async with lifespan(app):
            pass

    assert cast(EventHub, app.state.event_hub).is_closed


# -- end to end -------------------------------------------------------------


def register_audio(app: FastAPI, *, seconds: float, filename: str = "song.wav") -> str:
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


def build_app(tmp_path: Path, registry: SeparatorRegistry) -> FastAPI:
    app = create_app(Settings(data_dir=tmp_path))
    app.state.separator_registry = registry
    return app


def fast_registry() -> SeparatorRegistry:
    """The real fake separator with every simulated delay removed."""
    return SeparatorRegistry(
        {
            FAKE_ARCHITECTURE: fake_separator_builder(
                chunk_seconds=0.1,
                chunk_delay_seconds=0.0,
                model_load_seconds=0.0,
            )
        }
    )


async def test_a_real_job_publishes_contract_shaped_runtime_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real manager + real hub + real ``FakeSeparator``, seen by a real client.

    The ``gpu`` block is the separator's own honestly-labelled device profile
    (``backend: "fake"``), which is exactly why the telemetry path is
    demonstrable on a GPU-free machine.
    """
    monkeypatch.setattr("straticate.main.TelemetrySampler", FastSampler)
    app = build_app(tmp_path, fast_registry())
    audio_id = register_audio(app, seconds=1.5)
    socket = FakeSocket()

    async with app.router.lifespan_context(app):
        cast(EventHub, app.state.event_hub).register(as_socket(socket))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                JOBS_URL,
                json={"audio_id": audio_id, "mode_id": "vocals", "quality_id": "balanced"},
            )
            assert response.status_code == 201, response.text
            job_id = cast(str, response.json()["id"])
            await socket.wait_for(lambda: "job_completed" in socket.types())

    types = socket.types()
    payloads = socket.payloads()
    samples = [payload for payload in payloads if payload["type"] == "runtime_metrics"]
    assert samples, f"no runtime_metrics among {types}"

    sample = samples[0]
    assert sample["job_id"] == job_id
    assert set(sample) == {"type", "job_id", "model", "gpu", "processing"}
    assert set(cast(dict[str, Any], sample["model"])) == MODEL_KEYS
    assert sample["model"]["id"] == VOCALS_MODEL_ID
    assert sample["model"]["stem_count"] == 2
    assert set(cast(dict[str, Any], sample["gpu"])) == GPU_KEYS
    assert sample["gpu"]["backend"] == FAKE_ARCHITECTURE
    assert set(cast(dict[str, Any], sample["processing"])) == PROCESSING_KEYS

    # Interleaved: after the job started, before it ended, and never outside.
    first_sample = types.index("runtime_metrics")
    assert types.index("job_started") < first_sample
    assert first_sample < types.index("job_completed")
    last_sample = max(index for index, kind in enumerate(types) if kind == "runtime_metrics")
    assert last_sample < types.index("job_completed")


async def test_telemetry_is_published_for_a_job_whose_first_stage_begins_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The register-vs-start race, proven through the real endpoint.

    ``ImmediateSeparator`` has publishable statistics from the instant its
    first stage begins and refuses to finish until the test has seen a
    ``runtime_metrics``. If ``create_job`` registered the separator any later
    than it does — anywhere behind an ``await`` — this test would time out
    instead of passing.
    """
    monkeypatch.setattr("straticate.main.TelemetrySampler", FastSampler)
    gate = asyncio.Event()
    separators: list[ImmediateSeparator] = []

    def build(model: Model) -> Separator:
        separator = ImmediateSeparator(
            SeparatorInfo(
                model_id=model.id,
                display_name=model.display_name,
                architecture=model.architecture,
                version=model.version,
                separation_mode=model.separation_mode,
                stems=tuple(model.stems),
                sample_rate=model.sample_rate,
            ),
            gate=gate,
        )
        separators.append(separator)
        return separator

    app = build_app(tmp_path, SeparatorRegistry({FAKE_ARCHITECTURE: build}))
    audio_id = register_audio(app, seconds=0.4)
    socket = FakeSocket()

    async with app.router.lifespan_context(app):
        cast(EventHub, app.state.event_hub).register(as_socket(socket))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                JOBS_URL,
                json={"audio_id": audio_id, "mode_id": "vocals", "quality_id": "balanced"},
            )
            assert response.status_code == 201, response.text
            job_id = cast(str, response.json()["id"])
            await socket.wait_for(lambda: "runtime_metrics" in socket.types())
            gate.set()
            await socket.wait_for(lambda: "job_completed" in socket.types())

    samples = [payload for payload in socket.payloads() if payload["type"] == "runtime_metrics"]
    assert samples
    assert all(sample["job_id"] == job_id for sample in samples)
    assert len(separators) == 1
