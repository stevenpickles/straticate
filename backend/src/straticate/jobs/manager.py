"""In-process asynchronous job manager: FIFO queue, states, cancellation.

The :class:`JobManager` is the job engine described in ARCHITECTURE.md §6:

- ``submit()`` creates a ``Job`` record (state ``queued``), enqueues it, and
  returns immediately — inference never runs inside a request handler.
- A single asyncio worker task processes jobs strictly first-in-first-out,
  **one active job at a time**.
- State transitions are validated by :func:`straticate.jobs.state.assert_transition`.
- Cancellation is cooperative via :class:`straticate.jobs.cancellation.CancellationToken`.
- Every lifecycle change is published to registered listeners as the typed
  event models from :mod:`straticate.schemas.events` (the WebSocket hub of
  feature 013 subscribes here; REST endpoints of feature 015 call the manager
  directly).

Concurrency contract: all ``JobManager`` methods (and ``JobContext`` calls)
must be made from the event loop the manager runs on. The manager is
single-loop, in-process state — no cross-thread access, except for
``CancellationToken`` reads which are thread-safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Protocol, cast

from fastapi import Request
from ulid import ULID

from straticate.errors import ApplicationError
from straticate.jobs.cancellation import CancellationToken, JobCancelled
from straticate.jobs.state import InvalidJobTransition, assert_transition
from straticate.schemas.common import ErrorInfo
from straticate.schemas.events import (
    JobCancelledEvent,
    JobCompletedEvent,
    JobCreatedEvent,
    JobFailedEvent,
    JobProgressEvent,
    JobStageChangedEvent,
    JobStartedEvent,
)
from straticate.schemas.jobs import Job, JobState, SeparationConfiguration, SeparationResult

logger = logging.getLogger(__name__)

JobEvent = (
    JobCreatedEvent
    | JobStartedEvent
    | JobStageChangedEvent
    | JobProgressEvent
    | JobCompletedEvent
    | JobCancelledEvent
    | JobFailedEvent
)
"""Union of every event the job manager publishes to its listeners.

This is the job-lifecycle subset of the WebSocket contract
(``runtime_metrics`` is produced by the telemetry sampler, feature 019, not by
the job manager).
"""

JobEventListener = Callable[[JobEvent], Awaitable[None] | None]
"""A listener registered via :meth:`JobManager.add_listener`.

Listeners may be plain callables or coroutine functions. They are invoked in
event order on the manager's event loop; coroutine results are scheduled as
background tasks. A listener raising (or its task failing) is logged and never
breaks job processing.
"""

DEFAULT_PROGRESS_MIN_INTERVAL_SECONDS = 0.25
"""Minimum interval between ``job_progress`` events per job (≤ 4 Hz)."""


class JobExecutor(Protocol):
    """The seam between the job manager and the separation engine.

    Feature 014's ``Separator``-backed executor implements this protocol; the
    manager awaits it on the event loop, so long-running compute must be
    offloaded (e.g. to a worker thread) by the executor itself.

    The executor receives a **snapshot** of the job record plus a
    :class:`JobContext`. It must:

    - drive processing stages via ``context.set_stage(...)`` (forward-only;
      skipping stages that do not apply is fine),
    - report real progress via ``context.report_progress(...)``,
    - check ``context.cancellation`` cooperatively between units of work
      (``raise_if_cancelled()`` between chunks),
    - return a :class:`~straticate.schemas.jobs.SeparationResult` on success,
      or raise: :class:`~straticate.jobs.cancellation.JobCancelled` for
      cancellation, :class:`~straticate.errors.ApplicationError` for expected
      failures (its ``code`` is preserved), anything else for unexpected
      failures (mapped to error code ``separation_failed``).
    """

    async def __call__(self, job: Job, context: JobContext) -> SeparationResult:
        """Run the separation for ``job``, using ``context`` for all plumbing."""
        ...


@dataclass
class _JobEntry:
    """Internal bookkeeping for one submitted job (the live record lives here)."""

    job: Job
    executor: JobExecutor
    token: CancellationToken = field(default_factory=CancellationToken)
    running: bool = False
    started_monotonic: float | None = None
    last_progress_emit: float | None = None


_ChangeStage = Callable[[JobState], None]
_ReportProgress = Callable[[float, int, int, float | None, float | None], None]


class JobContext:
    """Facilities the job manager hands to a running executor.

    Constructed by the :class:`JobManager` — executors receive it, they never
    build it. All methods are synchronous, must be called on the manager's
    event loop, and drive job-record updates plus listener events. The context
    is the *only* legitimate way for an executor to mutate job state.
    """

    __slots__ = ("_change_stage", "_report_progress", "_token")

    def __init__(
        self,
        token: CancellationToken,
        change_stage: _ChangeStage,
        report_progress: _ReportProgress,
    ) -> None:
        self._token = token
        self._change_stage = change_stage
        self._report_progress = report_progress

    @property
    def cancellation(self) -> CancellationToken:
        """The job's cancellation token (thread-safe to read)."""
        return self._token

    def set_stage(self, stage: JobState) -> None:
        """Move the job to a later processing stage.

        Emits a ``job_stage_changed`` event. Only forward moves along the
        processing order are allowed (skipping stages is fine); terminal
        states are owned by the manager — return or raise instead.

        Raises:
            InvalidJobTransition: If ``stage`` is terminal, equal to the
                current stage, or backward along the processing order.
        """
        if stage.is_terminal:
            raise InvalidJobTransition(
                f"executors may not set terminal state {stage.value!r}; "
                "return a result or raise instead"
            )
        self._change_stage(stage)

    def report_progress(
        self,
        progress: float,
        chunks_completed: int,
        chunks_total: int,
        audio_processed_seconds: float | None = None,
        audio_total_seconds: float | None = None,
    ) -> None:
        """Report real work done (``chunks_completed / chunks_total``).

        Updates the job record's ``progress`` immediately; the corresponding
        ``job_progress`` event is throttled to at most one per
        ``progress_min_interval`` (default 0.25 s → ≤ 4 Hz) per job, except a
        report with ``progress >= 1.0`` which is always delivered.

        Args:
            progress: Overall progress in ``[0, 1]`` (clamped).
            chunks_completed: Chunks processed so far.
            chunks_total: Total chunks to process.
            audio_processed_seconds: Audio processed so far, in seconds
                (``0.0`` in the event when unknown).
            audio_total_seconds: Total audio duration, in seconds (``0.0`` in
                the event when unknown).
        """
        self._report_progress(
            progress,
            chunks_completed,
            chunks_total,
            audio_processed_seconds,
            audio_total_seconds,
        )


class JobManager:
    """The in-process asynchronous job engine (queue, states, cancellation).

    Lifecycle: construct, :meth:`start` on the running event loop (done by the
    FastAPI lifespan), submit/cancel/query while running, :meth:`aclose` on
    shutdown. All returned ``Job`` objects are deep-copy **snapshots**; use
    :meth:`get` to re-read current state or :meth:`add_listener` for pushes.

    Args:
        progress_min_interval: Minimum seconds between ``job_progress`` events
            per job (throttling; final ``progress >= 1.0`` always emits).
    """

    def __init__(
        self,
        *,
        progress_min_interval: float = DEFAULT_PROGRESS_MIN_INTERVAL_SECONDS,
    ) -> None:
        self._progress_min_interval = progress_min_interval
        self._entries: dict[str, _JobEntry] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._listeners: list[JobEventListener] = []
        self._listener_tasks: set[asyncio.Task[None]] = set()
        self._worker_task: asyncio.Task[None] | None = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the single worker task. Idempotent; requires a running loop.

        Raises:
            RuntimeError: If the manager was already closed.
        """
        if self._closed:
            raise RuntimeError("JobManager is closed")
        if self._worker_task is None:
            self._worker_task = asyncio.get_running_loop().create_task(
                self._worker_loop(), name="straticate-job-worker"
            )

    async def aclose(self) -> None:
        """Stop the worker and release resources. Idempotent.

        A job running at shutdown receives ``asyncio.CancelledError`` and is
        marked ``cancelled``; jobs still queued remain ``queued`` (this is an
        in-memory engine — nothing survives the process anyway). Outstanding
        coroutine-listener tasks are awaited.
        """
        self._closed = True
        task = self._worker_task
        self._worker_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._listener_tasks:
            await asyncio.gather(*self._listener_tasks, return_exceptions=True)

    # -- public API --------------------------------------------------------

    def submit(
        self,
        configuration: SeparationConfiguration,
        executor: JobExecutor,
        *,
        model_id: str = "",
    ) -> Job:
        """Create a job (state ``queued``), enqueue it, and return immediately.

        Emits ``job_created``. The returned ``Job`` is a snapshot.

        Args:
            configuration: The requested separation configuration.
            executor: The executor that will process the job when its turn
                comes (feature 014's separator-backed executor).
            model_id: The resolved model ID for the job record. Model
                resolution belongs to the caller (feature 015 / catalog);
                defaults to ``""`` until a resolver exists.

        Raises:
            RuntimeError: If the manager was already closed.
        """
        if self._closed:
            raise RuntimeError("JobManager is closed")
        job = Job(
            id=str(ULID()),
            audio_id=configuration.audio_id,
            configuration=configuration,
            model_id=model_id,
            state=JobState.QUEUED,
            progress=0.0,
            created_at=datetime.now(UTC),
            started_at=None,
            finished_at=None,
            error=None,
            result=None,
        )
        self._entries[job.id] = _JobEntry(job=job, executor=executor)
        self._queue.put_nowait(job.id)
        self._dispatch(
            JobCreatedEvent(type="job_created", job_id=job.id, job=job.model_copy(deep=True))
        )
        return job.model_copy(deep=True)

    def get(self, job_id: str) -> Job:
        """Return a snapshot of the job, or raise ``job_not_found`` (404)."""
        return self._entry_or_404(job_id).job.model_copy(deep=True)

    def list_jobs(self) -> list[Job]:
        """Return snapshots of all jobs in submission order."""
        return [entry.job.model_copy(deep=True) for entry in self._entries.values()]

    def cancel(self, job_id: str) -> Job:
        """Request cancellation of a job; returns a post-request snapshot.

        - A ``queued`` job is cancelled immediately (it will never run) and a
          ``job_cancelled`` event is emitted.
        - A running job has its cancellation token set; the executor's next
          cooperative check ends it and the job then becomes ``cancelled``.
        - A job already in a terminal state is left untouched (idempotent).

        Raises:
            ApplicationError: ``job_not_found`` (404) for an unknown id.
        """
        entry = self._entry_or_404(job_id)
        job = entry.job
        if not job.state.is_terminal:
            if entry.running:
                # A running job may still show state "queued" until its first
                # stage change — the running flag, not the state, decides.
                entry.token.cancel()
            else:
                self._mark_cancelled(entry)
        return job.model_copy(deep=True)

    def add_listener(self, listener: JobEventListener) -> None:
        """Register a listener for every :data:`JobEvent` the manager emits.

        Listeners are invoked in registration order, in event order, on the
        manager's event loop. A coroutine listener's awaitable is scheduled as
        a background task. Listener errors are logged and never affect job
        processing.
        """
        self._listeners.append(listener)

    def remove_listener(self, listener: JobEventListener) -> None:
        """Unregister a previously added listener (no-op if absent)."""
        with contextlib.suppress(ValueError):
            self._listeners.remove(listener)

    # -- internal: job execution -------------------------------------------

    async def _worker_loop(self) -> None:
        """Process queued jobs strictly FIFO, one at a time, forever."""
        while True:
            job_id = await self._queue.get()
            try:
                entry = self._entries.get(job_id)
                if entry is None or entry.job.state is not JobState.QUEUED:
                    continue  # cancelled while queued — logically dequeued
                await self._run_job(entry)
            finally:
                self._queue.task_done()

    async def _run_job(self, entry: _JobEntry) -> None:
        """Run one job's executor and drive it to a terminal state."""
        job = entry.job
        entry.running = True
        job.started_at = datetime.now(UTC)
        entry.started_monotonic = time.monotonic()
        self._dispatch(
            JobStartedEvent(type="job_started", job_id=job.id, started_at=job.started_at)
        )
        context = JobContext(
            token=entry.token,
            change_stage=partial(self._change_stage, entry),
            report_progress=partial(self._report_progress, entry),
        )
        try:
            result = await entry.executor(job.model_copy(deep=True), context)
        except JobCancelled:
            if not job.state.is_terminal:
                self._mark_cancelled(entry)
        except asyncio.CancelledError:
            # Manager shutdown while this job was running.
            if not job.state.is_terminal:
                self._mark_cancelled(entry)
            raise
        except ApplicationError as exc:
            self._mark_failed(
                entry, ErrorInfo(code=exc.code, message=exc.message, detail=exc.detail)
            )
        except Exception as exc:
            logger.exception("Executor for job %s raised an unexpected error", job.id)
            self._mark_failed(
                entry,
                ErrorInfo(code="separation_failed", message=str(exc) or type(exc).__name__),
            )
        else:
            if entry.token.is_cancelled:
                # Cancellation was requested but the executor finished without
                # observing it; the user's cancellation wins.
                self._mark_cancelled(entry)
            else:
                self._mark_completed(entry, result)
        finally:
            entry.running = False

    def _change_stage(self, entry: _JobEntry, stage: JobState) -> None:
        job = entry.job
        previous = job.state
        assert_transition(previous, stage)
        job.state = stage
        self._dispatch(
            JobStageChangedEvent(
                type="job_stage_changed", job_id=job.id, stage=stage, previous_stage=previous
            )
        )

    def _report_progress(
        self,
        entry: _JobEntry,
        progress: float,
        chunks_completed: int,
        chunks_total: int,
        audio_processed_seconds: float | None,
        audio_total_seconds: float | None,
    ) -> None:
        job = entry.job
        progress = min(max(progress, 0.0), 1.0)
        job.progress = progress
        now = time.monotonic()
        is_final = progress >= 1.0
        if (
            not is_final
            and entry.last_progress_emit is not None
            and now - entry.last_progress_emit < self._progress_min_interval
        ):
            return  # throttled (≤ 4 Hz per job); job.progress stays current
        entry.last_progress_emit = now
        elapsed = 0.0 if entry.started_monotonic is None else now - entry.started_monotonic
        self._dispatch(
            JobProgressEvent(
                type="job_progress",
                job_id=job.id,
                stage=job.state,
                progress=progress,
                chunks_completed=chunks_completed,
                chunks_total=chunks_total,
                elapsed_seconds=elapsed,
                audio_processed_seconds=audio_processed_seconds or 0.0,
                audio_total_seconds=audio_total_seconds or 0.0,
            )
        )

    # -- internal: terminal transitions ------------------------------------

    def _mark_completed(self, entry: _JobEntry, result: SeparationResult) -> None:
        job = entry.job
        assert_transition(job.state, JobState.COMPLETED)
        job.state = JobState.COMPLETED
        job.progress = 1.0
        job.result = result
        job.finished_at = datetime.now(UTC)
        self._dispatch(JobCompletedEvent(type="job_completed", job_id=job.id, result=result))

    def _mark_cancelled(self, entry: _JobEntry) -> None:
        job = entry.job
        stage = job.state
        assert_transition(stage, JobState.CANCELLED)
        job.state = JobState.CANCELLED
        job.finished_at = datetime.now(UTC)
        self._dispatch(
            JobCancelledEvent(type="job_cancelled", job_id=job.id, stage_at_cancellation=stage)
        )

    def _mark_failed(self, entry: _JobEntry, error: ErrorInfo) -> None:
        job = entry.job
        assert_transition(job.state, JobState.FAILED)
        job.state = JobState.FAILED
        job.error = error
        job.finished_at = datetime.now(UTC)
        self._dispatch(JobFailedEvent(type="job_failed", job_id=job.id, error=error))

    # -- internal: plumbing -------------------------------------------------

    def _entry_or_404(self, job_id: str) -> _JobEntry:
        entry = self._entries.get(job_id)
        if entry is None:
            raise ApplicationError(
                "job_not_found",
                f"No job with id {job_id!r}.",
                status_code=404,
                detail={"job_id": job_id},
            )
        return entry

    def _dispatch(self, event: JobEvent) -> None:
        """Deliver ``event`` to every listener; listener errors never propagate."""
        for listener in self._listeners:
            try:
                outcome = listener(event)
            except Exception:
                logger.exception("Job event listener %r failed handling %r", listener, event.type)
                continue
            if inspect.isawaitable(outcome):
                task = asyncio.get_running_loop().create_task(
                    self._await_listener(outcome, event.type)
                )
                self._listener_tasks.add(task)
                task.add_done_callback(self._listener_tasks.discard)

    @staticmethod
    async def _await_listener(outcome: Awaitable[None], event_type: str) -> None:
        try:
            await outcome
        except Exception:
            logger.exception("Async job event listener failed handling %r", event_type)


def get_job_manager(request: Request) -> JobManager:
    """FastAPI dependency: the application's :class:`JobManager`.

    Usage (feature 015)::

        @router.post("/jobs")
        async def create_job(manager: Annotated[JobManager, Depends(get_job_manager)]): ...

    The instance is created in ``create_app()`` and started/closed by the
    application lifespan.
    """
    return cast(JobManager, request.app.state.job_manager)
