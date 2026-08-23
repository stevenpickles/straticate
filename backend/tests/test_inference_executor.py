"""Tests for the ``Separator`` → ``JobExecutor`` adapter and full job runs.

Everything that needs coordination is gated with :class:`asyncio.Event` (or a
single ``sleep(0)`` loop tick) — no sleeps are used as synchronization.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from straticate.inference import (
    FAKE_STANDARD_INFO,
    FAKE_VOCALS_INFO,
    FakeSeparator,
    ProgressCallback,
    SeparationProgress,
    Separator,
    SeparatorInfo,
    SeparatorJobExecutor,
    SeparatorRuntimeStats,
    StageCallback,
    job_stems_dir,
)
from straticate.jobs import CancellationToken, JobEvent, JobManager
from straticate.schemas.events import (
    JobCancelledEvent,
    JobCompletedEvent,
    JobFailedEvent,
    JobProgressEvent,
    JobStageChangedEvent,
)
from straticate.schemas.jobs import (
    JobState,
    SeparationConfiguration,
    SeparationResult,
    SeparationResultMetrics,
)
from tests.audio_fixtures import peak_amplitude, read_wav, write_tone_wav

WAIT_TIMEOUT = 10.0

EXPECTED_STAGES = [
    JobState.PREPARING,
    JobState.DECODING,
    JobState.LOADING_MODEL,
    JobState.SEPARATING,
    JobState.POST_PROCESSING,
    JobState.ENCODING,
]
"""The stage sequence a ``FakeSeparator``-backed job really goes through.

``preparing`` is set by the job manager before the executor runs; every later
stage is announced by the separator as it enters it, and forwarded verbatim.
"""


class EventRecorder:
    """Sync listener that records events and lets tests await specific ones."""

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
            lambda e: (
                e.job_id == job_id
                and isinstance(e, JobCompletedEvent | JobCancelledEvent | JobFailedEvent)
            )
        )

    def stages(self, job_id: str) -> list[JobState]:
        return [
            event.stage
            for event in self.events
            if isinstance(event, JobStageChangedEvent) and event.job_id == job_id
        ]

    def progress(self, job_id: str) -> list[JobProgressEvent]:
        return [
            event
            for event in self.events
            if isinstance(event, JobProgressEvent) and event.job_id == job_id
        ]


@pytest.fixture
async def manager() -> AsyncIterator[JobManager]:
    """A started job manager with progress throttling disabled."""
    instance = JobManager(progress_min_interval=0.0)
    instance.start()
    try:
        yield instance
    finally:
        await instance.aclose()


@pytest.fixture
def recorder(manager: JobManager) -> EventRecorder:
    """An event recorder registered as a listener on the manager."""
    listener = EventRecorder()
    manager.add_listener(listener)
    return listener


def make_configuration(mode_id: str = "vocals") -> SeparationConfiguration:
    return SeparationConfiguration(
        audio_id="01AUDIO0000000000000000000",
        mode_id=mode_id,
        quality_id="high_quality",
        device_id=None,
    )


def make_separator(info: SeparatorInfo = FAKE_VOCALS_INFO) -> FakeSeparator:
    """A fake separator with simulated delays switched off."""
    return FakeSeparator(info, chunk_seconds=0.1, chunk_delay_seconds=0.0, model_load_seconds=0.0)


class GatedSeparator:
    """Wraps a separator and holds it at the door until a gate opens.

    Lets a test observe a job that is provably *running* (the manager has
    already moved it past ``queued``) without any timing assumption.
    """

    def __init__(self, inner: Separator) -> None:
        self._inner = inner
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    @property
    def info(self) -> SeparatorInfo:
        return self._inner.info

    def runtime_stats(self) -> SeparatorRuntimeStats | None:
        return self._inner.runtime_stats()

    async def separate(
        self,
        input_path: Path,
        configuration: SeparationConfiguration,
        progress_callback: ProgressCallback,
        cancellation_token: CancellationToken,
        *,
        job_id: str,
        output_dir: Path,
        stage_callback: StageCallback | None = None,
    ) -> SeparationResult:
        self.entered.set()
        await self.gate.wait()
        return await self._inner.separate(
            input_path,
            configuration,
            progress_callback,
            cancellation_token,
            job_id=job_id,
            output_dir=output_dir,
            stage_callback=stage_callback,
        )


class ThreadedSeparator:
    """Reports stages and progress from a worker thread, like a real separator.

    Exercises the adapter's off-loop marshalling: a separator that offloads its
    chunk loop must not have to know about the job manager's event loop.
    """

    def __init__(self, info: SeparatorInfo, chunks: int = 3) -> None:
        self._info = info
        self._chunks = chunks

    @property
    def info(self) -> SeparatorInfo:
        return self._info

    def runtime_stats(self) -> SeparatorRuntimeStats | None:
        return None

    async def separate(
        self,
        input_path: Path,
        configuration: SeparationConfiguration,
        progress_callback: ProgressCallback,
        cancellation_token: CancellationToken,
        *,
        job_id: str,
        output_dir: Path,
        stage_callback: StageCallback | None = None,
    ) -> SeparationResult:
        def work() -> None:
            if stage_callback is not None:
                stage_callback(JobState.SEPARATING)
            for index in range(self._chunks):
                cancellation_token.raise_if_cancelled()
                progress_callback(
                    SeparationProgress(
                        chunks_completed=index + 1,
                        chunks_total=self._chunks,
                        audio_processed_seconds=float(index + 1),
                        audio_total_seconds=float(self._chunks),
                    )
                )

        await asyncio.to_thread(work)
        return SeparationResult(
            job_id=job_id,
            model_id=self._info.model_id,
            stems=[],
            metrics=SeparationResultMetrics(processing_seconds=1.0, realtime_factor=3.0),
        )


# -- full runs through the job manager --------------------------------------


async def test_full_job_run_completes_with_a_separation_result(
    tmp_path: Path, manager: JobManager, recorder: EventRecorder
) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.5)
    separator = make_separator()
    executor = SeparatorJobExecutor(separator, input_path=source, data_dir=tmp_path / "data")

    job = manager.submit(make_configuration(), executor, model_id=separator.info.model_id)
    terminal = await recorder.wait_for_terminal(job.id)

    assert isinstance(terminal, JobCompletedEvent)
    finished = manager.get(job.id)
    assert finished.state is JobState.COMPLETED
    assert finished.progress == 1.0
    result = finished.result
    assert result is not None
    assert result.job_id == job.id
    assert result.model_id == "fake-vocals-001"
    assert [stem.name for stem in result.stems] == ["vocals", "instrumental"]
    assert result.metrics.processing_seconds > 0.0
    assert result.metrics.realtime_factor > 0.0

    stems_dir = job_stems_dir(tmp_path / "data", job.id)
    assert stems_dir == tmp_path / "data" / "jobs" / job.id / "stems"
    assert executor.output_dir(job.id) == stems_dir
    for stem in result.stems:
        channels, sample_rate, _, samples = read_wav(stems_dir / f"{stem.name}.wav")
        assert channels == 2
        assert sample_rate == 44100
        assert peak_amplitude(samples) > 1000


async def test_executor_drives_the_documented_stage_sequence(
    tmp_path: Path, manager: JobManager, recorder: EventRecorder
) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.3)
    executor = SeparatorJobExecutor(make_separator(), input_path=source, data_dir=tmp_path / "data")

    job = manager.submit(make_configuration(), executor)
    await recorder.wait_for_terminal(job.id)

    assert recorder.stages(job.id) == EXPECTED_STAGES


async def test_standard_stems_mode_produces_four_stems_through_the_manager(
    tmp_path: Path, manager: JobManager, recorder: EventRecorder
) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.3)
    separator = make_separator(FAKE_STANDARD_INFO)
    executor = SeparatorJobExecutor(separator, input_path=source, data_dir=tmp_path / "data")

    job = manager.submit(
        make_configuration("standard_stems"), executor, model_id=separator.info.model_id
    )
    await recorder.wait_for_terminal(job.id)

    result = manager.get(job.id).result
    assert result is not None
    assert [stem.name for stem in result.stems] == ["vocals", "drums", "bass", "other"]
    stems_dir = job_stems_dir(tmp_path / "data", job.id)
    assert sorted(path.name for path in stems_dir.iterdir()) == [
        "bass.wav",
        "drums.wav",
        "other.wav",
        "vocals.wav",
    ]


async def test_progress_events_are_chunk_grained_and_end_complete(
    tmp_path: Path, manager: JobManager, recorder: EventRecorder
) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.5)
    executor = SeparatorJobExecutor(make_separator(), input_path=source, data_dir=tmp_path / "data")

    job = manager.submit(make_configuration(), executor)
    await recorder.wait_for_terminal(job.id)

    events = recorder.progress(job.id)
    assert [event.chunks_completed for event in events] == list(range(6))
    assert {event.chunks_total for event in events} == {5}
    assert events[-1].progress == 1.0
    assert events[-1].audio_processed_seconds == pytest.approx(0.5, abs=0.01)
    assert events[-1].audio_total_seconds == pytest.approx(0.5, abs=0.01)
    assert [event.stage for event in events] == [JobState.SEPARATING] * 6


async def test_progress_reported_from_a_worker_thread_reaches_the_job(
    tmp_path: Path, manager: JobManager, recorder: EventRecorder
) -> None:
    separator: Separator = ThreadedSeparator(FAKE_VOCALS_INFO)
    executor = SeparatorJobExecutor(
        separator, input_path=tmp_path / "unused.wav", data_dir=tmp_path / "data"
    )

    job = manager.submit(make_configuration(), executor)
    terminal = await recorder.wait_for_terminal(job.id)

    assert isinstance(terminal, JobCompletedEvent)
    assert recorder.stages(job.id) == [JobState.PREPARING, JobState.SEPARATING]
    events = recorder.progress(job.id)
    assert [event.chunks_completed for event in events] == [1, 2, 3]
    assert events[-1].progress == 1.0


async def test_cancellation_through_the_manager_cancels_the_job(
    tmp_path: Path, manager: JobManager, recorder: EventRecorder
) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.5)
    gated = GatedSeparator(make_separator())
    executor = SeparatorJobExecutor(gated, input_path=source, data_dir=tmp_path / "data")

    job = manager.submit(make_configuration(), executor)
    await asyncio.wait_for(gated.entered.wait(), timeout=WAIT_TIMEOUT)

    manager.cancel(job.id)
    gated.gate.set()
    terminal = await recorder.wait_for_terminal(job.id)

    assert isinstance(terminal, JobCancelledEvent)
    cancelled = manager.get(job.id)
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.result is None
    stems_dir = job_stems_dir(tmp_path / "data", job.id)
    assert not stems_dir.exists() or list(stems_dir.iterdir()) == []


async def test_separator_application_error_fails_the_job_with_its_code(
    tmp_path: Path, manager: JobManager, recorder: EventRecorder
) -> None:
    source = write_tone_wav(tmp_path / "source.wav", seconds=0.2)
    # A vocals separator asked for standard_stems: a wiring bug, surfaced as a
    # failed job with the separator's own error code.
    executor = SeparatorJobExecutor(make_separator(), input_path=source, data_dir=tmp_path / "data")

    job = manager.submit(make_configuration("standard_stems"), executor)
    terminal = await recorder.wait_for_terminal(job.id)

    assert isinstance(terminal, JobFailedEvent)
    assert terminal.error.code == "separation_mode_mismatch"


async def test_undecodable_input_fails_the_job(
    tmp_path: Path, manager: JobManager, recorder: EventRecorder
) -> None:
    source = tmp_path / "notes.txt"
    source.write_bytes(b"not audio")
    executor = SeparatorJobExecutor(make_separator(), input_path=source, data_dir=tmp_path / "data")

    job = manager.submit(make_configuration(), executor)
    terminal = await recorder.wait_for_terminal(job.id)

    assert isinstance(terminal, JobFailedEvent)
    assert terminal.error.code == "audio_decode_failed"
    assert recorder.stages(job.id) == [JobState.PREPARING, JobState.DECODING]


async def test_executor_exposes_the_separator_for_telemetry(tmp_path: Path) -> None:
    separator = make_separator()
    executor = SeparatorJobExecutor(
        separator, input_path=tmp_path / "x.wav", data_dir=tmp_path / "data"
    )
    assert executor.separator is separator
