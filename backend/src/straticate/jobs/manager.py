"""In-process asynchronous job manager: FIFO queue, states, cancellation.

The :class:`JobManager` is the job engine described in ARCHITECTURE.md §6:

- ``submit()`` creates a ``Job`` record (state ``queued``), enqueues it, and
  returns immediately — inference never runs inside a request handler.
- A single asyncio worker task processes jobs strictly first-in-first-out,
  **one active job at a time**. The worker is defensive: no executor outcome
  (including protocol violations) can kill it or stall the queue.
- The manager owns the ``queued → preparing`` transition: a job leaves the
  queue as ``preparing`` before its executor runs, so ``Job.state`` is the
  single source of truth for "is this job running".
- State transitions are validated by :func:`straticate.jobs.state.assert_transition`.
- Cancellation is cooperative via :class:`straticate.jobs.cancellation.CancellationToken`.
- Job records survive the process (feature 057). Given a
  :class:`~straticate.jobs.store.JobStore`, ``submit()`` and every terminal
  transition write ``{data_dir}/jobs/{job_id}/job.json``, and ``restore()``
  seeds the records a previous run left behind — without queueing them and
  without emitting a single event.
- ``remove()`` forgets a **terminal** job's entry (feature 058); a non-terminal
  one is refused (``job_active``, 409) rather than torn out from under a
  running executor. It is the in-memory half of ``DELETE /jobs/{job_id}`` —
  :mod:`straticate.api.jobs` deletes the job's directory (record, stems and
  exports together) once this call has confirmed it is safe to.
- Every lifecycle change is published to registered listeners as the typed
  event models from :mod:`straticate.schemas.events` (the WebSocket hub of
  feature 013 subscribes here; REST endpoints of feature 015 call the manager
  directly). Delivery is strictly ordered: a single dispatcher task consumes
  an internal FIFO event queue and awaits coroutine listeners before moving
  to the next event.

Concurrency contract: all ``JobManager`` methods (and ``JobContext`` calls)
must be made from the event loop the manager runs on — feature 015's endpoint
handlers must therefore be ``async def`` (so they run on the app's loop), not
sync handlers executed in the threadpool. The manager is single-loop,
in-process state; ``CancellationToken`` reads are the only thread-safe
exception. As a safety net, event dispatch detects off-loop calls after
``start()`` and marshals the event onto the manager's loop via
``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Protocol, cast

from fastapi import Request
from ulid import ULID

from straticate.errors import ApplicationError
from straticate.jobs.cancellation import CancellationToken, JobCancelled
from straticate.jobs.state import InvalidJobTransition, assert_transition
from straticate.jobs.store import JobStore
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

Listeners may be plain callables or coroutine functions. A single dispatcher
task delivers events strictly in emission order: for each event it calls every
listener in registration order and **awaits** coroutine listeners before
touching the next event. A listener raising is logged and never breaks job
processing or the delivery of the event to other listeners.
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
      failures (mapped to error code ``separation_failed``). Returning
      anything that is not a ``SeparationResult`` is a protocol violation and
      fails the job with code ``separation_failed``.
    """

    async def __call__(self, job: Job, context: JobContext) -> SeparationResult:
        """Run the separation for ``job``, using ``context`` for all plumbing."""
        ...


@dataclass
class _JobEntry:
    """Internal bookkeeping for one submitted job (the live record lives here).

    ``executor`` is ``None`` for an entry seeded by :meth:`JobManager.restore`
    — a job from a previous process, whose executor closure (a built separator,
    a decoded input path, a resolved device) cannot be reconstructed from a
    record. That is safe because a restored entry is always **terminal**: it is
    never put on the queue, so the worker never reaches for its executor, and
    :meth:`JobManager.cancel` is already a no-op on a terminal job.
    """

    job: Job
    executor: JobExecutor | None
    token: CancellationToken = field(default_factory=CancellationToken)
    started_monotonic: float | None = None
    last_progress_emit: float | None = None


_ChangeStage = Callable[[JobState], None]
_ReportProgress = Callable[[float, int, int, float | None, float | None], None]


def _sane_seconds(value: float | None) -> float:
    """Sanitize an optional seconds value to a non-negative float.

    ``None`` and ``NaN`` map to ``0.0`` ("unknown" per the event contract);
    negative values (e.g. tiny float-arithmetic undershoots) clamp to ``0.0``.
    """
    if value is None or math.isnan(value):
        return 0.0
    return max(value, 0.0)


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
        processing order are allowed (skipping stages is fine); setting the
        stage the job is already in is a no-op (no duplicate event) — the
        manager itself moves the job to ``preparing`` when it starts, so an
        executor's initial ``set_stage(PREPARING)`` simply does nothing.
        Terminal states are owned by the manager — return or raise instead.

        Raises:
            InvalidJobTransition: If ``stage`` is terminal or backward along
                the processing order.
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

        Inputs are sanitized before the job record is touched: a ``NaN``
        ``progress`` makes the whole report a logged no-op; ``progress`` is
        clamped to ``[0, 1]``; negative or ``NaN`` audio seconds are reported
        as ``0.0``. The event is validated before the record is mutated, so an
        invalid report (e.g. negative chunk counts) raises without corrupting
        the live job.

        Args:
            progress: Overall progress in ``[0, 1]`` (clamped; ``NaN`` ignores
                the report).
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
    shutdown. A closed manager cannot be restarted — the application lifespan
    creates a fresh instance per cycle. All returned ``Job`` objects are
    deep-copy **snapshots**; use :meth:`get` to re-read current state or
    :meth:`add_listener` for pushes.

    **Job records outlive the process** when a store is supplied (feature 057).
    The manager writes a record at exactly two points — when a job is submitted
    and when it reaches a terminal state — and reads none: loading is the
    application lifespan's job, which hands the results to :meth:`restore`.
    Everything in between (stage changes, four-per-second progress) stays in
    memory, because a job interrupted mid-run comes back ``failed`` whatever
    stage it had reached. Without a store the manager behaves exactly as it did
    before that feature, which is what the unit tests here rely on.

    Args:
        progress_min_interval: Minimum seconds between ``job_progress`` events
            per job (throttling; final ``progress >= 1.0`` always emits).
        store: Where durable job records are written, or ``None`` to keep the
            manager purely in memory.
    """

    def __init__(
        self,
        *,
        progress_min_interval: float = DEFAULT_PROGRESS_MIN_INTERVAL_SECONDS,
        store: JobStore | None = None,
    ) -> None:
        self._progress_min_interval = progress_min_interval
        self._store = store
        self._entries: dict[str, _JobEntry] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._events: asyncio.Queue[JobEvent | None] = asyncio.Queue()
        self._listeners: list[JobEventListener] = []
        self._worker_task: asyncio.Task[None] | None = None
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the worker and dispatcher tasks. Idempotent; requires a running loop.

        Raises:
            RuntimeError: If the manager was already closed.
        """
        if self._closed:
            raise RuntimeError("JobManager is closed")
        loop = asyncio.get_running_loop()
        self._loop = loop
        if self._worker_task is None:
            self._worker_task = loop.create_task(self._worker_loop(), name="straticate-job-worker")
        if self._dispatcher_task is None:
            self._dispatcher_task = loop.create_task(
                self._dispatcher_loop(), name="straticate-job-dispatcher"
            )

    async def aclose(self) -> None:
        """Stop the worker and release resources. Idempotent.

        A job running at shutdown receives ``asyncio.CancelledError`` and is
        marked ``cancelled`` (and, with a store, persisted as such — an orderly
        shutdown really did cancel it). Jobs still queued remain ``queued``,
        which is what makes them ``job_interrupted`` on the next boot rather
        than a cancellation nobody requested: see
        :mod:`straticate.jobs.store`. The internal event queue is drained:
        every already-emitted event (including the shutdown cancellation) is
        delivered to listeners before ``aclose()`` returns.
        """
        self._closed = True
        worker = self._worker_task
        self._worker_task = None
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        dispatcher = self._dispatcher_task
        self._dispatcher_task = None
        if dispatcher is not None:
            self._events.put_nowait(None)  # sentinel: exit after draining
            with contextlib.suppress(asyncio.CancelledError):
                await dispatcher

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

        **The record is written before the job is enqueued or announced**, so a
        job the API answered ``201`` for cannot be one the next process has
        never heard of — and, unlike the terminal transitions, a store that
        cannot write here **fails the submission**: nothing has run yet, the
        entry is removed, and the caller's error path answers the client
        honestly. Swallowing at submit would quietly re-open the exact "201
        then gone" window this feature closes (review finding); swallowing at
        the terminal transitions stays right, because a full disk must never
        turn work that really completed into a failure (see :meth:`_persist`).

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
        store = self._store
        if store is not None:
            # Deliberately not _persist(): submit is the one transition where
            # a write failure is recoverable by refusing, so it propagates.
            store.save(job)
        self._entries[job.id] = _JobEntry(job=job, executor=executor)
        self._queue.put_nowait(job.id)
        self._dispatch(
            JobCreatedEvent(type="job_created", job_id=job.id, job=job.model_copy(deep=True))
        )
        return job.model_copy(deep=True)

    def restore(self, jobs: Iterable[Job]) -> None:
        """Seed records from a previous process, without queueing or announcing them.

        Called once, by the application lifespan, with the records
        :meth:`straticate.jobs.store.JobStore.recover` loaded — before any job
        of this run is submitted, so ``GET /jobs`` still lists everything in
        ULID order.

        Two properties are the whole point:

        - **Nothing is enqueued.** A restored job is finished; there is nothing
          left to run, and re-running one would be the application repeating
          heavy work nobody asked for.
        - **No event is emitted.** The event stream is a *live* channel: a
          ``job_completed`` frame means "this job just completed", and there is
          no client connected at startup to receive one anyway. Replaying
          history through it would tell the first browser to connect that a job
          from last week had just finished, and would restart the telemetry
          sampler for it. REST reads are how a client learns about the past.

        Raises:
            RuntimeError: If the manager was already closed.
            ValueError: If any record is not in a terminal state. Only
                terminal records can be restored — a non-terminal one would sit
                in the manager forever, never queued and never finishing. The
                store normalizes those to ``failed`` before they get here.
        """
        if self._closed:
            raise RuntimeError("JobManager is closed")
        for job in jobs:
            if not job.state.is_terminal:
                raise ValueError(
                    f"cannot restore job {job.id!r} in non-terminal state {job.state.value!r}"
                )
            self._entries[job.id] = _JobEntry(job=job.model_copy(deep=True), executor=None)

    def get(self, job_id: str) -> Job:
        """Return a snapshot of the job, or raise ``job_not_found`` (404).

        Remains available after :meth:`aclose` (read-only).
        """
        return self._entry_or_404(job_id).job.model_copy(deep=True)

    def list_jobs(self) -> list[Job]:
        """Return snapshots of all jobs in submission order.

        Remains available after :meth:`aclose` (read-only).
        """
        return [entry.job.model_copy(deep=True) for entry in self._entries.values()]

    def remove(self, job_id: str) -> Job:
        """Forget a terminal job's entry; returns a snapshot of it as it was.

        This is the in-memory half of ``DELETE /jobs/{job_id}`` (feature 058):
        the caller is responsible for removing the job's directory from disk
        (record, stems and exports together) after this call succeeds — see
        :mod:`straticate.api.jobs`. Only the entry is dropped here; no event is
        emitted, symmetrically with :meth:`restore` (a deleted job is gone, not
        a lifecycle transition a listener should hear about).

        Deliberately available after :meth:`aclose`: unlike ``submit`` or
        ``cancel``, removing an entry touches no queue and no worker, so a
        deletion request arriving while the manager winds down is served the
        same as one arriving before it.

        This is a theoretical affordance rather than a reachable one: FastAPI
        stops routing new requests to a handler before the application
        lifespan runs :meth:`aclose`, so in practice ``DELETE /jobs/{job_id}``
        (:func:`straticate.api.jobs.delete_job`) never actually calls this
        post-shutdown — there would be no uncancellable
        :func:`asyncio.to_thread` writer left racing an ``rmtree`` underneath
        it, which is the scenario that would make ``remove`` unsafe to call
        that late. :func:`test_jobs_manager.test_remove_is_available_after_aclose`
        exists to pin *this method's* contract (that it does not itself raise
        or depend on the worker/dispatcher once closed), not to assert that
        route is reachable in the running application.

        Raises:
            ApplicationError: ``job_not_found`` (404) for an unknown id;
                ``job_active`` (409, with the job's current ``state`` in
                ``detail``) if the job has not reached a terminal state.
                Deleting a job whose executor is still writing into its
                directory is the corruption this feature exists to prevent,
                not a case it adds — the client must cancel first and wait for
                the terminal event.
        """
        entry = self._entry_or_404(job_id)
        job = entry.job
        if not job.state.is_terminal:
            raise ApplicationError(
                "job_active",
                f"Job {job_id!r} is still {job.state.value!r}; cancel it before deleting.",
                status_code=409,
                detail={"job_id": job_id, "state": job.state.value},
            )
        del self._entries[job_id]
        return job.model_copy(deep=True)

    def cancel(self, job_id: str) -> Job:
        """Request cancellation of a job; returns a post-request snapshot.

        - A ``queued`` job is cancelled immediately (it will never run) and a
          ``job_cancelled`` event is emitted.
        - A running job (any non-terminal state past ``queued`` — the manager
          moves a job to ``preparing`` before its executor runs) has its
          cancellation token set; the executor's next cooperative check ends
          it and the job then becomes ``cancelled``.
        - A job already in a terminal state is left untouched (idempotent).

        Raises:
            ApplicationError: ``job_not_found`` (404) for an unknown id.
            RuntimeError: If the manager was already closed (there is no
                worker or dispatcher left to act on the request).
        """
        if self._closed:
            raise RuntimeError("JobManager is closed")
        entry = self._entry_or_404(job_id)
        job = entry.job
        if not job.state.is_terminal:
            if job.state is JobState.QUEUED:
                self._mark_cancelled(entry)
            else:
                entry.token.cancel()
        return job.model_copy(deep=True)

    def add_listener(self, listener: JobEventListener) -> None:
        """Register a listener for every :data:`JobEvent` the manager emits.

        Listeners are invoked in registration order, in strict event order, on
        the manager's event loop by a single dispatcher task; a coroutine
        listener is awaited before the next event is delivered. Listener
        errors are logged and never affect job processing.
        """
        self._listeners.append(listener)

    def remove_listener(self, listener: JobEventListener) -> None:
        """Unregister a previously added listener (no-op if absent).

        Safe to call from inside a listener: dispatch iterates a snapshot, so
        the remaining listeners still receive the event being delivered.
        """
        with contextlib.suppress(ValueError):
            self._listeners.remove(listener)

    # -- internal: job execution -------------------------------------------

    async def _worker_loop(self) -> None:
        """Process queued jobs strictly FIFO, one at a time, forever.

        Defensive by design: no exception from a single job — executor bugs,
        protocol violations, even bugs in the manager's own terminal-marking —
        may kill this loop and stall the queue.
        """
        while True:
            job_id = await self._queue.get()
            try:
                entry = self._entries.get(job_id)
                if entry is None or entry.job.state is not JobState.QUEUED:
                    continue  # cancelled while queued — logically dequeued
                try:
                    await self._run_job(entry)
                except asyncio.CancelledError:
                    raise  # manager shutdown
                except Exception:
                    logger.exception(
                        "Internal job engine error while finishing job %s", entry.job.id
                    )
                    if not entry.job.state.is_terminal:
                        try:
                            self._mark_failed(
                                entry,
                                ErrorInfo(
                                    code="separation_failed",
                                    message="Internal job engine error.",
                                ),
                            )
                        except Exception:
                            logger.exception("Could not mark job %s as failed", entry.job.id)
            finally:
                self._queue.task_done()

    async def _run_job(self, entry: _JobEntry) -> None:
        """Run one job's executor and drive it to a terminal state."""
        job = entry.job
        executor = entry.executor
        if executor is None:
            # Unreachable by construction: only :meth:`submit` enqueues, and it
            # always supplies an executor; a restored entry (the only other
            # kind) is terminal and never queued. Raising rather than assuming
            # keeps that invariant checkable — the worker turns it into a
            # logged, failed job instead of a stalled queue.
            raise RuntimeError(f"job {job.id!r} was queued without an executor")
        job.started_at = datetime.now(UTC)
        entry.started_monotonic = time.monotonic()
        self._dispatch(
            JobStartedEvent(type="job_started", job_id=job.id, started_at=job.started_at)
        )
        # The manager owns leaving the queue: the job is `preparing` before
        # the executor runs, so Job.state alone answers "is this running".
        self._change_stage(entry, JobState.PREPARING)
        context = JobContext(
            token=entry.token,
            change_stage=partial(self._change_stage, entry),
            report_progress=partial(self._report_progress, entry),
        )
        try:
            result = await executor(job.model_copy(deep=True), context)
        except JobCancelled:
            if not job.state.is_terminal:
                self._mark_cancelled(entry)
        except asyncio.CancelledError:
            if self._closed:
                # Manager shutdown while this job was running.
                if not job.state.is_terminal:
                    self._mark_cancelled(entry)
                raise
            # An executor-internal CancelledError (not our shutdown) must not
            # kill the worker: treat it as an unexpected executor failure.
            logger.error(
                "Executor for job %s raised CancelledError outside manager shutdown", job.id
            )
            if not job.state.is_terminal:
                self._mark_failed(
                    entry,
                    ErrorInfo(
                        code="separation_failed",
                        message="Executor raised CancelledError outside manager shutdown.",
                    ),
                )
        except ApplicationError as exc:
            self._mark_failed(entry, exc.to_error_info())
        except Exception as exc:
            logger.exception("Executor for job %s raised an unexpected error", job.id)
            self._mark_failed(
                entry,
                ErrorInfo(code="separation_failed", message=str(exc) or type(exc).__name__),
            )
        else:
            # The annotation promises a SeparationResult; a real executor is
            # not type-checked at runtime, so verify it before trusting it.
            result_obj = cast(object, result)
            if entry.token.is_cancelled:
                # Cancellation was requested but the executor finished without
                # observing it; the user's cancellation wins.
                self._mark_cancelled(entry)
            elif isinstance(result_obj, SeparationResult):
                self._mark_completed(entry, result_obj)
            else:
                # Protocol violation: the executor "succeeded" but returned
                # something that is not a SeparationResult.
                logger.error(
                    "Executor for job %s returned %s instead of a SeparationResult",
                    job.id,
                    type(result_obj).__name__,
                )
                self._mark_failed(
                    entry,
                    ErrorInfo(
                        code="separation_failed",
                        message=(
                            "Executor violated the JobExecutor protocol: expected a "
                            f"SeparationResult, got {type(result_obj).__name__}."
                        ),
                    ),
                )

    def _change_stage(self, entry: _JobEntry, stage: JobState) -> None:
        job = entry.job
        previous = job.state
        if stage is previous:
            return  # same-state set_stage is a no-op (no duplicate event)
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
        if math.isnan(progress):
            logger.warning("Ignoring NaN progress report for job %s", job.id)
            return
        progress = min(max(progress, 0.0), 1.0)
        now = time.monotonic()
        is_final = progress >= 1.0
        if (
            not is_final
            and entry.last_progress_emit is not None
            and now - entry.last_progress_emit < self._progress_min_interval
        ):
            job.progress = progress
            return  # throttled (≤ 4 Hz per job); job.progress stays current
        elapsed = 0.0 if entry.started_monotonic is None else now - entry.started_monotonic
        # Build (and validate) the event first; mutate the record only on
        # success so an invalid report cannot corrupt the live job.
        event = JobProgressEvent(
            type="job_progress",
            job_id=job.id,
            stage=job.state,
            progress=progress,
            chunks_completed=chunks_completed,
            chunks_total=chunks_total,
            elapsed_seconds=elapsed,
            audio_processed_seconds=_sane_seconds(audio_processed_seconds),
            audio_total_seconds=_sane_seconds(audio_total_seconds),
        )
        job.progress = progress
        entry.last_progress_emit = now
        self._dispatch(event)

    # -- internal: terminal transitions ------------------------------------
    #
    # Each of the three persists the finished record *before* it dispatches, so
    # a client that receives a terminal event can trust that the record behind
    # it is already on disk. Nothing in between is persisted: see
    # :mod:`straticate.jobs.store`.

    def _mark_completed(self, entry: _JobEntry, result: SeparationResult) -> None:
        job = entry.job
        assert_transition(job.state, JobState.COMPLETED)
        job.state = JobState.COMPLETED
        job.progress = 1.0
        job.result = result
        job.finished_at = datetime.now(UTC)
        self._persist(job)
        self._dispatch(JobCompletedEvent(type="job_completed", job_id=job.id, result=result))

    def _mark_cancelled(self, entry: _JobEntry) -> None:
        job = entry.job
        stage = job.state
        assert_transition(stage, JobState.CANCELLED)
        job.state = JobState.CANCELLED
        job.finished_at = datetime.now(UTC)
        self._persist(job)
        self._dispatch(
            JobCancelledEvent(type="job_cancelled", job_id=job.id, stage_at_cancellation=stage)
        )

    def _mark_failed(self, entry: _JobEntry, error: ErrorInfo) -> None:
        job = entry.job
        assert_transition(job.state, JobState.FAILED)
        job.state = JobState.FAILED
        job.error = error
        job.finished_at = datetime.now(UTC)
        self._persist(job)
        self._dispatch(JobFailedEvent(type="job_failed", job_id=job.id, error=error))

    def _persist(self, job: Job) -> None:
        """Write ``job``'s durable record, if this manager has a store.

        **A failing store never fails a *running or finished* job.** The
        manager's whole defensive posture is that no single job can stall the
        queue or die half-way through a terminal transition, and a full disk
        is exactly the moment that matters: raising here would turn a
        separation that really did complete into ``separation_failed`` (the
        worker's own catch-all). The failure is logged with its traceback and
        the job carries on in memory. ``submit`` is deliberately different —
        it calls the store directly and lets a write failure refuse the
        submission, because at that point nothing has run and a refused POST
        is honest where an unpersisted 201 is the defect this feature exists
        to close.
        """
        store = self._store
        if store is None:
            return
        try:
            store.save(job)
        except Exception:
            logger.exception("Could not persist the record of job %s", job.id)

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
        """Enqueue ``event`` for strictly ordered delivery by the dispatcher.

        Normally called on the manager's loop; as a safety net, a call from
        another thread after :meth:`start` marshals the event onto the
        manager's loop via ``call_soon_threadsafe`` (delivery order is then
        only guaranteed relative to other off-loop calls from that thread).
        """
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            loop.call_soon_threadsafe(self._events.put_nowait, event)
        else:
            self._events.put_nowait(event)

    async def _dispatcher_loop(self) -> None:
        """Deliver queued events to listeners in strict emission order."""
        while True:
            event = await self._events.get()
            try:
                if event is None:
                    return  # aclose() sentinel: queue drained, we are done
                # Snapshot: a listener removing itself (or others) during
                # dispatch must not skip the remaining listeners.
                for listener in list(self._listeners):
                    try:
                        outcome = listener(event)
                        if inspect.isawaitable(outcome):
                            await outcome
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "Job event listener %r failed handling %r", listener, event.type
                        )
            finally:
                self._events.task_done()


def get_job_manager(request: Request) -> JobManager:
    """FastAPI dependency: the application's :class:`JobManager`.

    Usage (feature 015)::

        @router.post("/jobs")
        async def create_job(manager: Annotated[JobManager, Depends(get_job_manager)]): ...

    Endpoints using the manager must be ``async def`` so they run on the
    manager's event loop (see the concurrency contract above). The instance is
    created and started by the application lifespan (a fresh one per lifespan
    cycle) and stored on ``app.state.job_manager``.
    """
    return cast(JobManager, request.app.state.job_manager)
