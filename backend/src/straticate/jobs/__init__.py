"""Asynchronous job engine: FIFO queue, state machine, cooperative cancellation.

Public surface (consumed by features 013/014/015):

- :class:`JobManager` — submit/get/list/cancel, worker lifecycle, listeners.
- :class:`JobExecutor` / :class:`JobContext` — the seam the separation engine
  (feature 014) plugs into.
- :data:`JobEvent` / :data:`JobEventListener` — the typed listener contract
  the WebSocket hub (feature 013) subscribes to.
- :class:`CancellationToken` / :class:`JobCancelled` — cooperative
  cancellation primitives.
- :func:`assert_transition` / :class:`InvalidJobTransition` — state-machine
  validation.
- :func:`get_job_manager` — FastAPI dependency accessor.
"""

from straticate.jobs.cancellation import CancellationToken, JobCancelled
from straticate.jobs.manager import (
    DEFAULT_PROGRESS_MIN_INTERVAL_SECONDS,
    JobContext,
    JobEvent,
    JobEventListener,
    JobExecutor,
    JobManager,
    get_job_manager,
)
from straticate.jobs.state import InvalidJobTransition, assert_transition

__all__ = [
    "DEFAULT_PROGRESS_MIN_INTERVAL_SECONDS",
    "CancellationToken",
    "InvalidJobTransition",
    "JobCancelled",
    "JobContext",
    "JobEvent",
    "JobEventListener",
    "JobExecutor",
    "JobManager",
    "assert_transition",
    "get_job_manager",
]
