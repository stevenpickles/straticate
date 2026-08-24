"""Runtime telemetry: sampling a running separator into ``runtime_metrics``.

Public surface:

- :class:`TelemetrySampler` — samples the active job's separator on an
  interval and publishes :class:`~straticate.schemas.events.RuntimeMetricsEvent`
  through the WebSocket event hub (ARCHITECTURE.md §12).
- :func:`get_telemetry_sampler` — FastAPI dependency accessor.
- :data:`DEFAULT_SAMPLE_INTERVAL_SECONDS` — the ~1 Hz sampling interval.
- :data:`FINISHED_JOB_MEMORY` — how many finished job IDs are remembered so a
  late registration is refused rather than stored forever.
"""

from straticate.telemetry.sampler import (
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    FINISHED_JOB_MEMORY,
    TERMINAL_EVENT_TYPES,
    TelemetrySampler,
    get_telemetry_sampler,
)

__all__ = [
    "DEFAULT_SAMPLE_INTERVAL_SECONDS",
    "FINISHED_JOB_MEMORY",
    "TERMINAL_EVENT_TYPES",
    "TelemetrySampler",
    "get_telemetry_sampler",
]
