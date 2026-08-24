"""Asynchronous job engine: FIFO queue, state machine, cooperative cancellation.

Public surface (consumed by features 013/014/015):

- :class:`JobManager` — submit/get/list/cancel, worker lifecycle, listeners.
- :class:`JobExecutor` / :class:`JobContext` — the seam the separation engine
  (feature 014) plugs into.
- :data:`JobEvent` / :data:`JobEventListener` — the typed listener contract
  the WebSocket hub (feature 013) subscribes to.
- :class:`EventHub` — the WebSocket fan-out (feature 013) that broadcasts
  those events to connected browsers, plus :func:`get_event_hub`.
- :class:`CancellationToken` / :class:`JobCancelled` — cooperative
  cancellation primitives.
- :func:`assert_transition` / :class:`InvalidJobTransition` — state-machine
  validation.
- :func:`resolve_audio` / :func:`resolve_model` / :func:`resolve_device` — the
  pure resolvers that turn a create-job request's IDs into the audio file,
  catalog model and compute device the job runs with (feature 015).
- :func:`get_job_manager` — FastAPI dependency accessor.
"""

from straticate.jobs.cancellation import CancellationToken, JobCancelled
from straticate.jobs.hub import (
    DEFAULT_CLIENT_QUEUE_SIZE,
    EVICTABLE_EVENT_TYPES,
    EventHub,
    EventSocket,
    get_event_hub,
)
from straticate.jobs.manager import (
    DEFAULT_PROGRESS_MIN_INTERVAL_SECONDS,
    JobContext,
    JobEvent,
    JobEventListener,
    JobExecutor,
    JobManager,
    get_job_manager,
)
from straticate.jobs.resolution import resolve_audio, resolve_device, resolve_model
from straticate.jobs.state import InvalidJobTransition, assert_transition

__all__ = [
    "DEFAULT_CLIENT_QUEUE_SIZE",
    "DEFAULT_PROGRESS_MIN_INTERVAL_SECONDS",
    "EVICTABLE_EVENT_TYPES",
    "CancellationToken",
    "EventHub",
    "EventSocket",
    "InvalidJobTransition",
    "JobCancelled",
    "JobContext",
    "JobEvent",
    "JobEventListener",
    "JobExecutor",
    "JobManager",
    "assert_transition",
    "get_event_hub",
    "get_job_manager",
    "resolve_audio",
    "resolve_device",
    "resolve_model",
]
