"""Runtime telemetry: sampling a running separator into ``runtime_metrics``.

Public surface:

- :class:`TelemetrySampler` — samples the active job's separator on an
  interval and publishes :class:`~straticate.schemas.events.RuntimeMetricsEvent`
  through the WebSocket event hub (ARCHITECTURE.md §12).
- :func:`get_telemetry_sampler` — FastAPI dependency accessor.
- :data:`DEFAULT_SAMPLE_INTERVAL_SECONDS` — the ~1 Hz sampling interval.
"""

from straticate.telemetry.sampler import (
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    TERMINAL_EVENT_TYPES,
    TelemetrySampler,
    get_telemetry_sampler,
)

__all__ = [
    "DEFAULT_SAMPLE_INTERVAL_SECONDS",
    "TERMINAL_EVENT_TYPES",
    "TelemetrySampler",
    "get_telemetry_sampler",
]
