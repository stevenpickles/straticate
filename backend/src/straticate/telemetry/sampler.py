"""Runtime telemetry sampler: ``runtime_metrics`` while a job runs.

This module is the only thing that connects the two halves of ARCHITECTURE.md
§12. The **separator** is the authority on its own statistics
(:meth:`~straticate.inference.base.Separator.runtime_stats`), the **event hub**
is pure transport (feature 013), and :class:`TelemetrySampler` is the thin
strap between them: while a job is active it reads the running separator's
snapshot on an interval, projects it onto the wire contract, and publishes a
:class:`~straticate.schemas.events.RuntimeMetricsEvent`.

Everything it does is shaped by four rules:

- **Publish, don't await.** :meth:`~straticate.jobs.EventHub.publish` is
  synchronous and must be called on the application's event loop; reading
  ``runtime_stats()`` is a cheap snapshot read, so the sampling loop is an
  ordinary task on that loop and never offloads anything.
- **The separator's device stats are published verbatim.** ``gpu`` is
  ``stats.device.to_gpu_metrics()``, or ``null`` when the separator reports no
  device. Nothing is substituted from the device detector — see
  ``docs/features/019-telemetry-sampler.md``.
- **Skip work when nobody is listening.** With no WebSocket client connected
  the tick builds nothing at all.
- **Telemetry may never break a job.** Every listener call and every sampling
  tick swallows and logs its own errors; one bad tick neither kills the
  sampling task nor propagates into the job manager's ordered dispatcher.

Wiring (see :func:`straticate.main.lifespan`)::

    sampler = TelemetrySampler(hub)
    manager.add_listener(sampler.on_job_event)   # starts/stops sampling
    ...
    sampler.register(job.id, executor.separator) # from the create-job endpoint

The sampler deliberately knows nothing about the API layer: the create-job
endpoint pushes a separator in, the job manager's events drive the lifecycle,
and the hub takes the result.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import cast

from fastapi import Request

from straticate.inference.base import Separator
from straticate.jobs.hub import EventHub
from straticate.jobs.manager import JobEvent
from straticate.schemas.events import RuntimeMetricsEvent

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_INTERVAL_SECONDS = 1.0
"""Seconds between telemetry samples (~1 Hz, ARCHITECTURE.md §12).

Telemetry is a live readout, not a recording: a human reads it at roughly this
rate, and every sample is a frame a slow client may have to shed
(``runtime_metrics`` is evictable in the hub's backpressure policy).
"""

TERMINAL_EVENT_TYPES = frozenset({"job_completed", "job_cancelled", "job_failed"})
"""Event types after which a job is over and sampling must stop."""


class TelemetrySampler:
    """Samples the active separator and publishes ``runtime_metrics`` events.

    Lifecycle: construct with the application's :class:`EventHub`, register
    :meth:`on_job_event` with the :class:`~straticate.jobs.JobManager`,
    :meth:`register` each job's separator as the job is submitted, and
    :meth:`aclose` on shutdown. The application lifespan creates a fresh
    instance per cycle; a closed sampler starts nothing and holds nothing.

    Only one job runs at a time (ARCHITECTURE.md §6), so at most one sampling
    task exists at any moment; several jobs may nevertheless be *registered*
    while they sit in the queue.

    Args:
        hub: The event hub to publish through.
        interval_seconds: Seconds between samples.

    Raises:
        ValueError: If ``interval_seconds`` is not positive.
    """

    __slots__ = (
        "_active_job_id",
        "_awaiting_registration",
        "_closed",
        "_hub",
        "_interval_seconds",
        "_separators",
        "_task",
    )

    def __init__(
        self,
        hub: EventHub,
        *,
        interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._hub = hub
        self._interval_seconds = interval_seconds
        self._separators: dict[str, Separator] = {}
        self._task: asyncio.Task[None] | None = None
        self._active_job_id: str | None = None
        self._awaiting_registration: str | None = None
        self._closed = False

    # -- introspection -----------------------------------------------------

    @property
    def interval_seconds(self) -> float:
        """Seconds between samples of the active job."""
        return self._interval_seconds

    @property
    def registered_job_ids(self) -> frozenset[str]:
        """Jobs whose separator is currently registered.

        Entries are dropped on a job's terminal event and by :meth:`aclose`,
        so this never grows without bound on a long-running server.
        """
        return frozenset(self._separators)

    @property
    def active_job_id(self) -> str | None:
        """The job currently being sampled, or ``None`` when idle."""
        return self._active_job_id

    @property
    def is_closed(self) -> bool:
        """Whether :meth:`aclose` has been called."""
        return self._closed

    # -- registration ------------------------------------------------------

    def register(self, job_id: str, separator: Separator) -> None:
        """Tell the sampler which separator will run ``job_id``.

        Called by the create-job endpoint immediately after
        :meth:`~straticate.jobs.JobManager.submit` returns — synchronously,
        with no ``await`` in between, so the job's ``job_started`` cannot be
        dispatched before the separator is known. As a belt-and-braces measure
        for any other caller, a registration that *does* arrive after the job
        already started begins sampling right away instead of losing the job's
        telemetry entirely.

        Registering on a closed sampler is a no-op: nothing will ever sample
        it, and keeping the entry would leak.
        """
        if self._closed:
            return
        self._separators[job_id] = separator
        if self._awaiting_registration == job_id:
            self._awaiting_registration = None
            self._start(job_id)

    # -- job manager listener ----------------------------------------------

    def on_job_event(self, event: JobEvent) -> None:
        """Start sampling when a job starts; stop at its terminal event.

        Registered with :meth:`~straticate.jobs.JobManager.add_listener`. Per
        the listener contract (feature 012) it is synchronous, never blocks and
        **never raises**: an unexpected failure here would be dispatched into
        the manager's ordered event loop, so it is logged and swallowed.
        """
        try:
            if event.type == "job_started":
                self._start(event.job_id)
            elif event.type in TERMINAL_EVENT_TYPES:
                self._stop(event.job_id)
        except Exception:  # telemetry must never break job processing
            logger.exception("Telemetry sampler failed handling %r", event.type)

    # -- sampling ----------------------------------------------------------

    def sample_once(self, job_id: str) -> RuntimeMetricsEvent | None:
        """Build one ``runtime_metrics`` event for ``job_id``, or ``None``.

        The event is exactly the three documented projections of the
        separator's :class:`~straticate.inference.base.SeparatorRuntimeStats`
        and nothing else — in particular ``gpu`` is the separator's own
        :class:`~straticate.inference.base.DeviceStats` verbatim, and ``null``
        when it reports no device.

        Returns ``None`` — publishing nothing — when the job has no registered
        separator, when the separator has no statistics yet, or when its
        snapshot belongs to a *different* job. That last case is real: a
        separator instance is reused across jobs and keeps the previous run's
        snapshot readable, so a sample taken before the new run starts would
        otherwise publish stale numbers under the wrong job ID.
        """
        separator = self._separators.get(job_id)
        if separator is None:
            return None
        stats = separator.runtime_stats()
        if stats is None or stats.job_id != job_id:
            return None
        return RuntimeMetricsEvent(
            type="runtime_metrics",
            job_id=stats.job_id,
            model=stats.model.to_model_info(),
            gpu=None if stats.device is None else stats.device.to_gpu_metrics(),
            processing=stats.processing.to_processing_metrics(),
        )

    # -- shutdown ----------------------------------------------------------

    async def aclose(self) -> None:
        """Stop sampling, drop every registration, and refuse to start again.

        Idempotent, and safe to call twice. The application lifespan closes the
        sampler **before** the job manager and the hub, so no sample can be
        published into a hub that is already tearing its connections down.
        """
        self._closed = True
        self._separators.clear()
        self._awaiting_registration = None
        self._active_job_id = None
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # -- internal ----------------------------------------------------------

    def _start(self, job_id: str) -> None:
        """Begin sampling ``job_id``, replacing any task still running."""
        if self._closed:
            return
        self._cancel_task()
        if job_id not in self._separators:
            # The job started before its separator was registered; sampling
            # begins the moment `register` arrives.
            self._awaiting_registration = job_id
            return
        self._active_job_id = job_id
        self._task = asyncio.get_running_loop().create_task(
            self._sample_loop(job_id), name="straticate-telemetry-sampler"
        )

    def _stop(self, job_id: str) -> None:
        """Stop sampling ``job_id`` and forget its separator."""
        self._separators.pop(job_id, None)
        if self._awaiting_registration == job_id:
            self._awaiting_registration = None
        if self._active_job_id != job_id:
            return
        self._active_job_id = None
        self._cancel_task()

    def _cancel_task(self) -> None:
        """Cancel the sampling task, if any.

        Cancellation is requested synchronously and not awaited: this runs
        inside the job manager's dispatcher, which may not block. The task can
        only ever be suspended at its interval sleep, so it is torn down
        without taking another sample — nothing is published after a job's
        terminal event.
        """
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()

    async def _sample_loop(self, job_id: str) -> None:
        """Sample ``job_id`` every :attr:`interval_seconds` until cancelled."""
        while True:
            self._tick(job_id)
            await asyncio.sleep(self._interval_seconds)

    def _tick(self, job_id: str) -> None:
        """Take and publish one sample; a failure is logged, never raised.

        Swallowing here (rather than around the whole loop) is what keeps the
        sampling task alive across a bad tick: a separator whose statistics
        accessor throws once must not silence telemetry for the rest of the
        job, and must certainly not affect the job itself.
        """
        try:
            if self._hub.connection_count == 0:
                return  # nobody is listening: build nothing, publish nothing
            event = self.sample_once(job_id)
            if event is not None:
                self._hub.publish(event)
        except Exception:
            logger.exception("Telemetry sample failed for job %s", job_id)


def get_telemetry_sampler(request: Request) -> TelemetrySampler:
    """FastAPI dependency: the application's :class:`TelemetrySampler`.

    Usage (feature 015's create-job endpoint)::

        @router.post("/jobs")
        async def create_job(sampler: Annotated[TelemetrySampler, Depends(get_telemetry_sampler)]):
            ...

    Endpoints using it must be ``async def`` so they run on the application's
    event loop — :meth:`TelemetrySampler.register` may start a sampling task,
    and the hub's ``publish`` is single-loop by contract. The instance is
    created, wired to the job manager, and closed by the application lifespan
    (a fresh one per cycle) and lives on ``app.state.telemetry_sampler``.
    """
    return cast(TelemetrySampler, request.app.state.telemetry_sampler)


__all__ = [
    "DEFAULT_SAMPLE_INTERVAL_SECONDS",
    "TERMINAL_EVENT_TYPES",
    "TelemetrySampler",
    "get_telemetry_sampler",
]
