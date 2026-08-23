"""Tests for the asynchronous job manager.

Executors are small scripted async functions coordinated with ``asyncio.Event``
so timing is deterministic — no sleeps as synchronization.
"""

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest
from fastapi import Request

from straticate.errors import ApplicationError
from straticate.jobs import JobContext, JobEvent, JobExecutor, JobManager, get_job_manager
from straticate.main import create_app
from straticate.schemas.events import (
    JobCancelledEvent,
    JobCompletedEvent,
    JobCreatedEvent,
    JobFailedEvent,
    JobProgressEvent,
    JobStageChangedEvent,
    JobStartedEvent,
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


class EventRecorder:
    """Sync listener that records events and lets tests await specific ones."""

    def __init__(self) -> None:
        self.events: list[JobEvent] = []
        self._changed = asyncio.Event()

    def __call__(self, event: JobEvent) -> None:
        self.events.append(event)
        self._changed.set()

    async def wait_for(self, predicate: Callable[[JobEvent], bool]) -> JobEvent:
        """Return the first (past or future) event matching ``predicate``."""
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

    def for_job(self, job_id: str) -> list[JobEvent]:
        return [event for event in self.events if event.job_id == job_id]


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


async def instant_executor(job: Job, context: JobContext) -> SeparationResult:
    """Executor that succeeds immediately without stages or progress."""
    return make_result(job.id)


@pytest.fixture
async def manager() -> AsyncIterator[JobManager]:
    m = JobManager()
    m.start()
    yield m
    await m.aclose()


@pytest.fixture
def recorder(manager: JobManager) -> EventRecorder:
    r = EventRecorder()
    manager.add_listener(r)
    return r


# -- submit / get / list ----------------------------------------------------


async def test_submit_returns_queued_job_with_timestamps(manager: JobManager) -> None:
    configuration = make_configuration()
    job = manager.submit(configuration, instant_executor, model_id="model-1")

    assert job.state is JobState.QUEUED
    assert job.progress == 0.0
    assert len(job.id) == 26  # ULID canonical encoding
    assert job.audio_id == configuration.audio_id
    assert job.configuration == configuration
    assert job.model_id == "model-1"
    assert job.created_at.tzinfo is not None
    assert job.started_at is None
    assert job.finished_at is None
    assert job.error is None
    assert job.result is None

    assert manager.get(job.id).state is JobState.QUEUED
    assert [j.id for j in manager.list_jobs()] == [job.id]


async def test_get_unknown_job_raises_job_not_found(manager: JobManager) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        manager.get("no-such-job")
    assert excinfo.value.code == "job_not_found"
    assert excinfo.value.status_code == 404


async def test_cancel_unknown_job_raises_job_not_found(manager: JobManager) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        manager.cancel("no-such-job")
    assert excinfo.value.code == "job_not_found"
    assert excinfo.value.status_code == 404


async def test_list_jobs_preserves_submission_order(
    manager: JobManager, recorder: EventRecorder
) -> None:
    ids = [manager.submit(make_configuration(), instant_executor).id for _ in range(3)]
    assert [j.id for j in manager.list_jobs()] == ids
    for job_id in ids:
        await recorder.wait_for_terminal(job_id)
    assert [j.id for j in manager.list_jobs()] == ids


# -- FIFO scheduling --------------------------------------------------------


async def test_fifo_with_single_active_job(manager: JobManager, recorder: EventRecorder) -> None:
    order: list[str] = []
    started_one = asyncio.Event()
    gate_one = asyncio.Event()
    started_two = asyncio.Event()
    gate_two = asyncio.Event()

    def gated(name: str, started: asyncio.Event, gate: asyncio.Event) -> JobExecutor:
        async def executor(job: Job, context: JobContext) -> SeparationResult:
            order.append(name)
            started.set()
            await gate.wait()
            return make_result(job.id)

        return executor

    job_one = manager.submit(make_configuration(), gated("one", started_one, gate_one))
    job_two = manager.submit(make_configuration(), gated("two", started_two, gate_two))

    await asyncio.wait_for(started_one.wait(), timeout=WAIT_TIMEOUT)
    # While job one runs, job two must still be queued and not started.
    assert manager.get(job_two.id).state is JobState.QUEUED
    assert not started_two.is_set()

    gate_one.set()
    await recorder.wait_for_terminal(job_one.id)
    await asyncio.wait_for(started_two.wait(), timeout=WAIT_TIMEOUT)
    gate_two.set()
    await recorder.wait_for_terminal(job_two.id)

    assert order == ["one", "two"]
    assert manager.get(job_one.id).state is JobState.COMPLETED
    assert manager.get(job_two.id).state is JobState.COMPLETED


# -- happy path -------------------------------------------------------------


async def test_happy_path_emits_events_in_order(
    manager: JobManager, recorder: EventRecorder
) -> None:
    async def executor(job: Job, context: JobContext) -> SeparationResult:
        context.set_stage(JobState.PREPARING)
        context.set_stage(JobState.SEPARATING)
        context.report_progress(0.5, 1, 2, audio_processed_seconds=10.0, audio_total_seconds=20.0)
        return make_result(job.id, model_id="model-hq")

    job = manager.submit(make_configuration(), executor, model_id="model-hq")
    await recorder.wait_for_terminal(job.id)

    events = recorder.for_job(job.id)
    assert [type(e) for e in events] == [
        JobCreatedEvent,
        JobStartedEvent,
        JobStageChangedEvent,
        JobStageChangedEvent,
        JobProgressEvent,
        JobCompletedEvent,
    ]

    created = events[0]
    assert isinstance(created, JobCreatedEvent)
    assert created.job.id == job.id
    assert created.job.state is JobState.QUEUED

    final = manager.get(job.id)
    started = events[1]
    assert isinstance(started, JobStartedEvent)
    assert started.started_at == final.started_at

    stage_one = events[2]
    assert isinstance(stage_one, JobStageChangedEvent)
    assert stage_one.stage is JobState.PREPARING
    assert stage_one.previous_stage is JobState.QUEUED

    stage_two = events[3]
    assert isinstance(stage_two, JobStageChangedEvent)
    assert stage_two.stage is JobState.SEPARATING
    assert stage_two.previous_stage is JobState.PREPARING

    progress = events[4]
    assert isinstance(progress, JobProgressEvent)
    assert progress.stage is JobState.SEPARATING
    assert progress.progress == 0.5
    assert progress.chunks_completed == 1
    assert progress.chunks_total == 2
    assert progress.elapsed_seconds >= 0.0
    assert progress.audio_processed_seconds == 10.0
    assert progress.audio_total_seconds == 20.0

    completed = events[5]
    assert isinstance(completed, JobCompletedEvent)
    assert completed.result.job_id == job.id
    assert completed.result.model_id == "model-hq"

    assert final.state is JobState.COMPLETED
    assert final.progress == 1.0
    assert final.result == completed.result
    assert final.finished_at is not None
    assert final.error is None


# -- cancellation -----------------------------------------------------------


async def test_cancel_queued_job_never_runs(manager: JobManager, recorder: EventRecorder) -> None:
    started_one = asyncio.Event()
    gate_one = asyncio.Event()
    ran_two = False

    async def blocking_executor(job: Job, context: JobContext) -> SeparationResult:
        started_one.set()
        await gate_one.wait()
        return make_result(job.id)

    async def never_executor(job: Job, context: JobContext) -> SeparationResult:
        nonlocal ran_two
        ran_two = True
        return make_result(job.id)

    manager.submit(make_configuration(), blocking_executor)
    job_two = manager.submit(make_configuration(), never_executor)
    await asyncio.wait_for(started_one.wait(), timeout=WAIT_TIMEOUT)

    snapshot = manager.cancel(job_two.id)
    assert snapshot.state is JobState.CANCELLED
    assert snapshot.finished_at is not None
    assert snapshot.started_at is None

    cancelled = await recorder.wait_for_terminal(job_two.id)
    assert isinstance(cancelled, JobCancelledEvent)
    assert cancelled.stage_at_cancellation is JobState.QUEUED

    # Run a third job to completion: FIFO guarantees the worker has passed
    # (and skipped) job two by the time job three finishes.
    gate_one.set()
    job_three = manager.submit(make_configuration(), instant_executor)
    await recorder.wait_for_terminal(job_three.id)

    assert not ran_two
    assert not any(isinstance(e, JobStartedEvent) for e in recorder.for_job(job_two.id))


async def test_cancel_running_job_via_cooperative_token(
    manager: JobManager, recorder: EventRecorder
) -> None:
    started = asyncio.Event()
    gate = asyncio.Event()

    async def executor(job: Job, context: JobContext) -> SeparationResult:
        context.set_stage(JobState.SEPARATING)
        started.set()
        await gate.wait()
        context.cancellation.raise_if_cancelled()
        return make_result(job.id)

    job = manager.submit(make_configuration(), executor)
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

    manager.cancel(job.id)
    gate.set()

    cancelled = await recorder.wait_for_terminal(job.id)
    assert isinstance(cancelled, JobCancelledEvent)
    assert cancelled.stage_at_cancellation is JobState.SEPARATING

    final = manager.get(job.id)
    assert final.state is JobState.CANCELLED
    assert final.finished_at is not None
    assert final.result is None


async def test_cancel_wins_when_executor_finishes_without_observing_token(
    manager: JobManager, recorder: EventRecorder
) -> None:
    started = asyncio.Event()
    gate = asyncio.Event()

    async def executor(job: Job, context: JobContext) -> SeparationResult:
        started.set()
        await gate.wait()
        return make_result(job.id)  # never checks the token

    job = manager.submit(make_configuration(), executor)
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)
    manager.cancel(job.id)
    gate.set()

    cancelled = await recorder.wait_for_terminal(job.id)
    assert isinstance(cancelled, JobCancelledEvent)
    assert manager.get(job.id).state is JobState.CANCELLED


async def test_cancel_of_terminal_job_is_a_no_op(
    manager: JobManager, recorder: EventRecorder
) -> None:
    job = manager.submit(make_configuration(), instant_executor)
    await recorder.wait_for_terminal(job.id)
    snapshot = manager.cancel(job.id)
    assert snapshot.state is JobState.COMPLETED
    assert not any(isinstance(e, JobCancelledEvent) for e in recorder.for_job(job.id))


# -- failure ----------------------------------------------------------------


async def test_executor_exception_fails_job_with_error_info(
    manager: JobManager, recorder: EventRecorder
) -> None:
    async def executor(job: Job, context: JobContext) -> SeparationResult:
        raise RuntimeError("boom")

    job = manager.submit(make_configuration(), executor)
    failed = await recorder.wait_for_terminal(job.id)

    assert isinstance(failed, JobFailedEvent)
    assert failed.error.code == "separation_failed"
    assert failed.error.message == "boom"

    final = manager.get(job.id)
    assert final.state is JobState.FAILED
    assert final.error == failed.error
    assert final.finished_at is not None
    assert final.result is None


async def test_application_error_from_executor_preserves_its_code(
    manager: JobManager, recorder: EventRecorder
) -> None:
    async def executor(job: Job, context: JobContext) -> SeparationResult:
        raise ApplicationError(
            "cuda_out_of_memory",
            "VRAM exhausted.",
            status_code=500,
            detail={"requested_mb": 8192},
        )

    job = manager.submit(make_configuration(), executor)
    failed = await recorder.wait_for_terminal(job.id)

    assert isinstance(failed, JobFailedEvent)
    assert failed.error.code == "cuda_out_of_memory"
    assert failed.error.message == "VRAM exhausted."
    assert failed.error.detail == {"requested_mb": 8192}


async def test_backward_stage_change_fails_the_job(
    manager: JobManager, recorder: EventRecorder
) -> None:
    async def executor(job: Job, context: JobContext) -> SeparationResult:
        context.set_stage(JobState.SEPARATING)
        context.set_stage(JobState.PREPARING)  # backward: programming error
        return make_result(job.id)

    job = manager.submit(make_configuration(), executor)
    failed = await recorder.wait_for_terminal(job.id)
    assert isinstance(failed, JobFailedEvent)
    assert failed.error.code == "separation_failed"
    assert manager.get(job.id).state is JobState.FAILED


# -- listeners --------------------------------------------------------------


async def test_raising_listener_does_not_break_processing(
    manager: JobManager, recorder: EventRecorder
) -> None:
    def bad_listener(event: JobEvent) -> None:
        raise RuntimeError("listener boom")

    # Registered before an additional recorder so later listeners still run.
    manager.add_listener(bad_listener)
    late_recorder = EventRecorder()
    manager.add_listener(late_recorder)

    job = manager.submit(make_configuration(), instant_executor)
    await recorder.wait_for_terminal(job.id)
    await late_recorder.wait_for_terminal(job.id)
    assert manager.get(job.id).state is JobState.COMPLETED


async def test_coroutine_listeners_are_supported(manager: JobManager) -> None:
    received: list[JobEvent] = []
    done = asyncio.Event()

    async def listener(event: JobEvent) -> None:
        received.append(event)
        if isinstance(event, JobCompletedEvent):
            done.set()

    manager.add_listener(listener)
    job = manager.submit(make_configuration(), instant_executor)
    await asyncio.wait_for(done.wait(), timeout=WAIT_TIMEOUT)
    assert [e.type for e in received] == ["job_created", "job_started", "job_completed"]
    assert all(e.job_id == job.id for e in received)


async def test_remove_listener_stops_delivery(manager: JobManager, recorder: EventRecorder) -> None:
    removable = EventRecorder()
    manager.add_listener(removable)
    manager.remove_listener(removable)
    job = manager.submit(make_configuration(), instant_executor)
    await recorder.wait_for_terminal(job.id)
    assert removable.events == []


# -- progress throttling ----------------------------------------------------


async def test_progress_events_are_throttled_and_final_progress_delivered() -> None:
    # An enormous interval makes throttling deterministic: only the first
    # report and the final (progress >= 1.0) report may emit events.
    m = JobManager(progress_min_interval=3600.0)
    m.start()
    recorder = EventRecorder()
    m.add_listener(recorder)
    try:

        async def executor(job: Job, context: JobContext) -> SeparationResult:
            context.set_stage(JobState.SEPARATING)
            for i in range(1, 6):
                context.report_progress(i / 10, i, 10)
            context.report_progress(1.0, 10, 10)
            return make_result(job.id)

        job = m.submit(make_configuration(), executor)
        await recorder.wait_for_terminal(job.id)

        progress_events = [e for e in recorder.for_job(job.id) if isinstance(e, JobProgressEvent)]
        assert [e.progress for e in progress_events] == [0.1, 1.0]
        assert progress_events[-1].chunks_completed == 10
        assert m.get(job.id).progress == 1.0
    finally:
        await m.aclose()


# -- shutdown ---------------------------------------------------------------


async def test_aclose_with_running_and_queued_jobs_shuts_down_cleanly(
    manager: JobManager, recorder: EventRecorder
) -> None:
    started = asyncio.Event()
    gate = asyncio.Event()  # never set: the job blocks until shutdown

    async def blocking_executor(job: Job, context: JobContext) -> SeparationResult:
        started.set()
        await gate.wait()
        return make_result(job.id)

    running = manager.submit(make_configuration(), blocking_executor)
    queued = manager.submit(make_configuration(), instant_executor)
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

    await manager.aclose()

    assert manager.get(running.id).state is JobState.CANCELLED
    assert manager.get(running.id).finished_at is not None
    assert any(isinstance(e, JobCancelledEvent) for e in recorder.for_job(running.id))
    assert manager.get(queued.id).state is JobState.QUEUED

    with pytest.raises(RuntimeError):
        manager.submit(make_configuration(), instant_executor)
    # Second aclose (from the fixture) must be a clean no-op.


# -- app wiring -------------------------------------------------------------


async def test_app_lifespan_starts_and_closes_the_job_manager() -> None:
    app = create_app()
    manager = app.state.job_manager
    assert isinstance(manager, JobManager)

    async with app.router.lifespan_context(app):
        request = Request({"type": "http", "app": app})
        assert get_job_manager(request) is manager

        recorder = EventRecorder()
        manager.add_listener(recorder)
        job = manager.submit(make_configuration(), instant_executor)
        completed = await recorder.wait_for_terminal(job.id)
        assert isinstance(completed, JobCompletedEvent)

    with pytest.raises(RuntimeError):
        manager.submit(make_configuration(), instant_executor)
