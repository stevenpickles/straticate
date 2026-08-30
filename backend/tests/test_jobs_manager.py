"""Tests for the asynchronous job manager.

Executors are small scripted async functions coordinated with ``asyncio.Event``
so timing is deterministic — no sleeps as synchronization.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import cast

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

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


# -- removal (feature 058) ---------------------------------------------------


async def test_remove_of_a_completed_job_pops_the_entry(
    manager: JobManager, recorder: EventRecorder
) -> None:
    job = manager.submit(make_configuration(), instant_executor)
    await recorder.wait_for_terminal(job.id)

    removed = manager.remove(job.id)
    assert removed.id == job.id
    assert removed.state is JobState.COMPLETED

    with pytest.raises(ApplicationError) as excinfo:
        manager.get(job.id)
    assert excinfo.value.code == "job_not_found"
    assert job.id not in [j.id for j in manager.list_jobs()]


async def test_remove_emits_no_event(manager: JobManager, recorder: EventRecorder) -> None:
    """Symmetric with :meth:`JobManager.restore`: a deletion is not a lifecycle event."""
    job = manager.submit(make_configuration(), instant_executor)
    await recorder.wait_for_terminal(job.id)
    before = len(recorder.events)

    manager.remove(job.id)

    # Emit one event of our own and wait for it: the dispatcher is strictly
    # ordered, so anything `remove` had emitted would already be delivered.
    follow_up = manager.submit(make_configuration(), instant_executor)
    await recorder.wait_for_terminal(follow_up.id)
    assert recorder.events[before:] == [
        event for event in recorder.events[before:] if event.job_id == follow_up.id
    ]


async def test_remove_of_a_queued_job_is_refused(
    manager: JobManager, recorder: EventRecorder
) -> None:
    started, gate = asyncio.Event(), asyncio.Event()

    async def blocking_executor(job: Job, context: JobContext) -> SeparationResult:
        started.set()
        await gate.wait()
        return make_result(job.id)

    running = manager.submit(make_configuration(), blocking_executor)
    queued = manager.submit(make_configuration(), instant_executor)
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

    with pytest.raises(ApplicationError) as excinfo:
        manager.remove(queued.id)
    assert excinfo.value.code == "job_active"
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == {"job_id": queued.id, "state": "queued"}
    # Refused, not removed: the entry is still there.
    assert manager.get(queued.id).state is JobState.QUEUED

    gate.set()
    await recorder.wait_for_terminal(running.id)
    await recorder.wait_for_terminal(queued.id)


async def test_remove_of_a_running_job_is_refused(
    manager: JobManager, recorder: EventRecorder
) -> None:
    started, gate = asyncio.Event(), asyncio.Event()

    async def executor(job: Job, context: JobContext) -> SeparationResult:
        context.set_stage(JobState.SEPARATING)
        started.set()
        await gate.wait()
        return make_result(job.id)

    job = manager.submit(make_configuration(), executor)
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

    with pytest.raises(ApplicationError) as excinfo:
        manager.remove(job.id)
    assert excinfo.value.code == "job_active"
    assert excinfo.value.detail == {"job_id": job.id, "state": "separating"}

    gate.set()
    await recorder.wait_for_terminal(job.id)


async def test_remove_of_an_unknown_job_is_job_not_found(manager: JobManager) -> None:
    with pytest.raises(ApplicationError) as excinfo:
        manager.remove("01NOTAJOB")
    assert excinfo.value.code == "job_not_found"
    assert excinfo.value.status_code == 404


async def test_remove_is_available_after_aclose() -> None:
    """Unlike ``submit``/``cancel``, removal touches no queue or worker.

    This pins ``remove()``'s own contract, not a reachable API path: FastAPI
    stops routing requests before the lifespan's ``aclose()`` runs, so
    ``DELETE /jobs/{job_id}`` never actually calls this post-shutdown in the
    running application (see the docstring on ``JobManager.remove``).
    """
    m = JobManager()
    recorder = EventRecorder()
    m.add_listener(recorder)
    m.start()
    job = m.submit(make_configuration(), instant_executor)
    await recorder.wait_for_terminal(job.id)
    await m.aclose()

    removed = m.remove(job.id)
    assert removed.id == job.id


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


# -- worker resilience ------------------------------------------------------


async def test_non_separation_result_return_fails_job_and_worker_survives(
    manager: JobManager, recorder: EventRecorder
) -> None:
    """A protocol-violating return value fails the job, never the worker."""

    async def bad_executor(job: Job, context: JobContext) -> SeparationResult:
        return cast(SeparationResult, {"not": "a result"})

    job = manager.submit(make_configuration(), bad_executor)
    failed = await recorder.wait_for_terminal(job.id)

    assert isinstance(failed, JobFailedEvent)
    assert failed.error.code == "separation_failed"
    assert "SeparationResult" in failed.error.message
    assert manager.get(job.id).state is JobState.FAILED

    # The worker survived: a subsequent job still runs to completion.
    follow_up = manager.submit(make_configuration(), instant_executor)
    assert isinstance(await recorder.wait_for_terminal(follow_up.id), JobCompletedEvent)


async def test_executor_internal_cancelled_error_fails_job_and_worker_survives(
    manager: JobManager, recorder: EventRecorder
) -> None:
    """CancelledError raised by the executor (no shutdown) is a job failure."""

    async def executor(job: Job, context: JobContext) -> SeparationResult:
        raise asyncio.CancelledError

    job = manager.submit(make_configuration(), executor)
    failed = await recorder.wait_for_terminal(job.id)

    assert isinstance(failed, JobFailedEvent)
    assert failed.error.code == "separation_failed"
    assert manager.get(job.id).state is JobState.FAILED

    follow_up = manager.submit(make_configuration(), instant_executor)
    assert isinstance(await recorder.wait_for_terminal(follow_up.id), JobCompletedEvent)


async def test_worker_survives_internal_terminal_marking_error(
    manager: JobManager, recorder: EventRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a bug in the manager's own terminal-marking cannot stall the queue."""

    def broken_mark_completed(entry: object, result: object) -> None:
        raise RuntimeError("terminal marking bug")

    monkeypatch.setattr(manager, "_mark_completed", broken_mark_completed)
    job = manager.submit(make_configuration(), instant_executor)
    failed = await recorder.wait_for_terminal(job.id)

    assert isinstance(failed, JobFailedEvent)
    assert failed.error.code == "separation_failed"
    assert manager.get(job.id).state is JobState.FAILED

    monkeypatch.undo()
    follow_up = manager.submit(make_configuration(), instant_executor)
    assert isinstance(await recorder.wait_for_terminal(follow_up.id), JobCompletedEvent)


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
    assert [e.type for e in received] == [
        "job_created",
        "job_started",
        "job_stage_changed",
        "job_completed",
    ]
    assert all(e.job_id == job.id for e in received)


async def test_remove_listener_stops_delivery(manager: JobManager, recorder: EventRecorder) -> None:
    removable = EventRecorder()
    manager.add_listener(removable)
    manager.remove_listener(removable)
    job = manager.submit(make_configuration(), instant_executor)
    await recorder.wait_for_terminal(job.id)
    assert removable.events == []


async def test_listener_removing_itself_does_not_skip_later_listeners(
    manager: JobManager,
) -> None:
    calls: list[str] = []
    later = EventRecorder()

    def self_removing(event: JobEvent) -> None:
        calls.append(event.type)
        manager.remove_listener(self_removing)

    manager.add_listener(self_removing)
    manager.add_listener(later)

    job = manager.submit(make_configuration(), instant_executor)
    await later.wait_for_terminal(job.id)

    # The later listener received every event, including the one during whose
    # dispatch the earlier listener removed itself.
    assert [e.type for e in later.for_job(job.id)] == [
        "job_created",
        "job_started",
        "job_stage_changed",
        "job_completed",
    ]
    assert calls == ["job_created"]


async def test_coroutine_listener_observes_events_in_emission_order(
    manager: JobManager,
) -> None:
    """A slow coroutine listener never sees later events before earlier ones."""
    received: list[str] = []
    release = asyncio.Event()
    done = asyncio.Event()

    async def slow_listener(event: JobEvent) -> None:
        if event.type == "job_created":
            await release.wait()  # hold the first event's delivery
        received.append(event.type)
        if isinstance(event, JobCompletedEvent):
            done.set()

    manager.add_listener(slow_listener)
    job = manager.submit(make_configuration(), instant_executor)

    # Let the job run to completion (emitting all remaining events) while the
    # first event's delivery is still gated.
    async def job_terminal() -> None:
        while not manager.get(job.id).state.is_terminal:
            await asyncio.sleep(0)

    await asyncio.wait_for(job_terminal(), timeout=WAIT_TIMEOUT)
    assert received == []  # strictly ordered: nothing may overtake the gate

    release.set()
    await asyncio.wait_for(done.wait(), timeout=WAIT_TIMEOUT)
    assert received == [
        "job_created",
        "job_started",
        "job_stage_changed",
        "job_completed",
    ]


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


async def test_nan_progress_report_is_ignored(manager: JobManager, recorder: EventRecorder) -> None:
    """A NaN progress report is dropped entirely — never stored in the job."""
    reported = asyncio.Event()
    gate = asyncio.Event()

    async def executor(job: Job, context: JobContext) -> SeparationResult:
        context.set_stage(JobState.SEPARATING)
        context.report_progress(0.25, 1, 4)
        context.report_progress(float("nan"), 2, 4)
        reported.set()
        await gate.wait()
        return make_result(job.id)

    job = manager.submit(make_configuration(), executor)
    await asyncio.wait_for(reported.wait(), timeout=WAIT_TIMEOUT)
    assert manager.get(job.id).progress == 0.25  # NaN never reached the record

    gate.set()
    await recorder.wait_for_terminal(job.id)
    progress_events = [e for e in recorder.for_job(job.id) if isinstance(e, JobProgressEvent)]
    assert [e.progress for e in progress_events] == [0.25]
    assert manager.get(job.id).state is JobState.COMPLETED


async def test_negative_audio_seconds_are_clamped_to_zero(
    manager: JobManager, recorder: EventRecorder
) -> None:
    async def executor(job: Job, context: JobContext) -> SeparationResult:
        context.set_stage(JobState.SEPARATING)
        context.report_progress(1.0, 4, 4, audio_processed_seconds=-1e-9, audio_total_seconds=-5.0)
        return make_result(job.id)

    job = manager.submit(make_configuration(), executor)
    completed = await recorder.wait_for_terminal(job.id)
    assert isinstance(completed, JobCompletedEvent)

    progress_events = [e for e in recorder.for_job(job.id) if isinstance(e, JobProgressEvent)]
    assert len(progress_events) == 1
    assert progress_events[0].audio_processed_seconds == 0.0
    assert progress_events[0].audio_total_seconds == 0.0


# -- state authority --------------------------------------------------------


async def test_running_job_is_preparing_before_first_set_stage(
    manager: JobManager, recorder: EventRecorder
) -> None:
    """The manager itself moves queued → preparing when the job starts."""
    started = asyncio.Event()
    gate = asyncio.Event()

    async def executor(job: Job, context: JobContext) -> SeparationResult:
        assert job.state is JobState.PREPARING  # snapshot already reflects it
        started.set()
        await gate.wait()
        return make_result(job.id)

    job = manager.submit(make_configuration(), executor)
    await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT)

    live = manager.get(job.id)
    assert live.state is JobState.PREPARING
    assert live.started_at is not None

    # Cancelling in this window is cooperative (token), not immediate.
    manager.cancel(job.id)
    assert manager.get(job.id).state is JobState.PREPARING
    gate.set()

    cancelled = await recorder.wait_for_terminal(job.id)
    assert isinstance(cancelled, JobCancelledEvent)
    assert cancelled.stage_at_cancellation is JobState.PREPARING


async def test_set_stage_to_current_stage_is_a_noop(
    manager: JobManager, recorder: EventRecorder
) -> None:
    async def executor(job: Job, context: JobContext) -> SeparationResult:
        context.set_stage(JobState.PREPARING)  # already preparing: no-op
        context.set_stage(JobState.SEPARATING)
        context.set_stage(JobState.SEPARATING)  # same stage again: no-op
        return make_result(job.id)

    job = manager.submit(make_configuration(), executor)
    await recorder.wait_for_terminal(job.id)

    stage_events = [e for e in recorder.for_job(job.id) if isinstance(e, JobStageChangedEvent)]
    assert [(e.previous_stage, e.stage) for e in stage_events] == [
        (JobState.QUEUED, JobState.PREPARING),
        (JobState.PREPARING, JobState.SEPARATING),
    ]
    assert manager.get(job.id).state is JobState.COMPLETED


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


async def test_cancel_after_aclose_raises_runtime_error(
    manager: JobManager, recorder: EventRecorder
) -> None:
    job = manager.submit(make_configuration(), instant_executor)
    await recorder.wait_for_terminal(job.id)
    await manager.aclose()

    with pytest.raises(RuntimeError):
        manager.cancel(job.id)
    # Reads remain available after close.
    assert manager.get(job.id).state is JobState.COMPLETED


# -- app wiring -------------------------------------------------------------


async def test_app_lifespan_starts_and_closes_the_job_manager() -> None:
    app = create_app()

    async with app.router.lifespan_context(app):
        manager = app.state.job_manager
        assert isinstance(manager, JobManager)
        request = Request({"type": "http", "app": app})
        assert get_job_manager(request) is manager

        recorder = EventRecorder()
        manager.add_listener(recorder)
        job = manager.submit(make_configuration(), instant_executor)
        completed = await recorder.wait_for_terminal(job.id)
        assert isinstance(completed, JobCompletedEvent)

    with pytest.raises(RuntimeError):
        manager.submit(make_configuration(), instant_executor)


def test_two_sequential_testclient_lifespans_on_one_app() -> None:
    """A second ``TestClient`` lifespan on the same app must not crash."""
    app = create_app()
    managers: list[JobManager] = []
    for _ in range(2):
        with TestClient(app):
            manager = app.state.job_manager
            assert isinstance(manager, JobManager)
            managers.append(manager)
    first, second = managers
    assert first is not second  # a fresh manager per lifespan cycle


async def test_second_lifespan_manager_still_processes_jobs() -> None:
    """Each lifespan cycle yields a manager that actually runs jobs."""
    app = create_app()
    for _ in range(2):
        async with app.router.lifespan_context(app):
            manager = app.state.job_manager
            assert isinstance(manager, JobManager)
            recorder = EventRecorder()
            manager.add_listener(recorder)
            job = manager.submit(make_configuration(), instant_executor)
            assert isinstance(await recorder.wait_for_terminal(job.id), JobCompletedEvent)
