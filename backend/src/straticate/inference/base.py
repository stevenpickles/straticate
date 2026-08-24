"""The separation-engine seam: the :class:`Separator` abstraction.

This module is the boundary described in ARCHITECTURE.md §1/§7: *the machine
learning model is a replaceable inference backend*. Everything here is stated
in terms of model IDs, separation modes, stems, chunks, devices and results —
never in terms of PyTorch, tensors, segment sizes, FFT sizes or any particular
network architecture. Application code (the job manager, the API, telemetry)
depends only on what is defined here; concrete separators
(:mod:`straticate.inference.fake` today, RoFormer/MDX/MDXC/Demucs later) are
the only code allowed to know how the audio is actually produced.

The contract in one paragraph: a separator is constructed once (loading a model
is expensive), advertises itself through :attr:`Separator.info`, and runs one
separation at a time via :meth:`Separator.separate`. While it runs it announces
processing stages through a :data:`StageCallback`, reports **real work** —
``completed_chunks / total_chunks`` — through a :data:`ProgressCallback`,
checks the :class:`~straticate.jobs.cancellation.CancellationToken` between
chunks, and exposes live model/device statistics through
:meth:`Separator.runtime_stats`. It writes its stems into the output directory
it is given and returns a :class:`~straticate.schemas.jobs.SeparationResult`.

Implementing a new separator (feature 026 and beyond) means implementing
exactly this protocol; :class:`straticate.inference.executor.SeparatorJobExecutor`
then makes it a job executor without a single line of architecture-specific
code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from straticate.jobs.cancellation import CancellationToken
from straticate.schemas.events import GpuMetrics, ModelInfo, ProcessingMetrics
from straticate.schemas.jobs import JobState, SeparationConfiguration, SeparationResult
from straticate.schemas.stems import STEM_NAME_PATTERN

# ``STEM_NAME_PATTERN`` is re-exported from here (see ``__all__`` below) because
# :mod:`straticate.inference.layout` and the rest of the inference package have
# always imported it from this module. Its definition now lives in
# :mod:`straticate.schemas.stems`, next to the field constraint the API applies,
# so the separator seam and the shared contract cannot disagree about what a
# stem name is.


@dataclass(frozen=True, slots=True)
class SeparatorInfo:
    """What a separator advertises about the model it runs.

    This is the model descriptor the rest of the application uses: feature 019
    publishes it as the ``model`` block of ``runtime_metrics`` (see
    :meth:`to_model_info`), feature 015 records ``model_id`` on the job, and
    feature 021 needs :attr:`stems` to serve results. The fields mirror the
    catalog manifest (ARCHITECTURE.md §9) — a separator is always consistent
    with its ``models/catalog.json`` entry.

    Attributes:
        model_id: Stable logical model ID, e.g. ``"fake-vocals-001"``.
        display_name: Human-readable model name.
        architecture: Implementation family (open set) — ``"fake"``,
            ``"mel_band_roformer"``, ``"mdx"``, … Application code outside the
            inference package never branches on this.
        version: Model version string.
        separation_mode: Logical mode this model serves, e.g. ``"vocals"``.
        stems: Stem names the model produces, in output order.
        sample_rate: Native sample rate in Hz; separations decode to it.
    """

    model_id: str
    display_name: str
    architecture: str
    version: str
    separation_mode: str
    stems: tuple[str, ...]
    sample_rate: int

    def __post_init__(self) -> None:
        if len(self.stems) < 2:
            raise ValueError("a separator must produce at least two stems")
        if len(set(self.stems)) != len(self.stems):
            raise ValueError("stem names must be unique")
        for stem in self.stems:
            if not STEM_NAME_PATTERN.fullmatch(stem):
                raise ValueError(
                    f"invalid stem name {stem!r}: expected {STEM_NAME_PATTERN.pattern}"
                )
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")

    @property
    def stem_count(self) -> int:
        """Number of stems this model produces."""
        return len(self.stems)

    def to_model_info(self) -> ModelInfo:
        """Project onto the contract :class:`~straticate.schemas.events.ModelInfo`.

        Used by feature 019 to fill the ``model`` block of a
        ``runtime_metrics`` event.
        """
        return ModelInfo(
            id=self.model_id,
            display_name=self.display_name,
            architecture=self.architecture,
            version=self.version,
            separation_mode=self.separation_mode,
            stem_count=self.stem_count,
        )


@dataclass(frozen=True, slots=True)
class SeparationProgress:
    """One chunk-grained progress report from a running separation.

    The fields line up one-to-one with
    :meth:`straticate.jobs.JobContext.report_progress`, which is what keeps the
    executor adapter thin. Progress is **real work** (ARCHITECTURE.md §7): a
    separator only reports a chunk once the chunk has actually been processed,
    never on a timer.

    Attributes:
        chunks_completed: Chunks processed so far (``0`` for the opening
            report that merely announces ``chunks_total``).
        chunks_total: Total chunks this separation will process.
        audio_processed_seconds: Seconds of source audio processed so far.
        audio_total_seconds: Total duration of the source audio in seconds.
    """

    chunks_completed: int
    chunks_total: int
    audio_processed_seconds: float
    audio_total_seconds: float

    @property
    def fraction(self) -> float:
        """``chunks_completed / chunks_total`` clamped to ``[0, 1]`` (``0`` if unknown)."""
        if self.chunks_total <= 0:
            return 0.0
        return min(max(self.chunks_completed / self.chunks_total, 0.0), 1.0)


ProgressCallback = Callable[[SeparationProgress], None]
"""Called by a separator after every processed chunk.

Must not raise and must be cheap — a separator calls it inline. Throttling for
the wire is the job manager's business (``job_progress`` is capped at ~4 Hz),
so a separator reports every chunk and never filters.

A separator that offloads its chunk loop to a worker thread may invoke this
from that thread; :class:`straticate.inference.executor.SeparatorJobExecutor`
marshals such calls back onto the job manager's event loop.
"""

StageCallback = Callable[[JobState], None]
"""Called by a separator when it enters a new processing stage.

Only processing stages are legal (``decoding``, ``loading_model``,
``separating``, ``post_processing``, ``encoding``); terminal states belong to
the job manager. A separator announces only the stages it actually performs —
skipping forward is legal and honest. Same threading rules as
:data:`ProgressCallback`.
"""


@dataclass(frozen=True, slots=True)
class DeviceStats:
    """Compute-device statistics sampled while a separation runs.

    Mirrors :class:`~straticate.schemas.events.GpuMetrics`; a CPU-only
    separator reports ``None`` instead of this block. ``utilization`` and
    ``temperature_celsius`` are optional because NVML is optional
    (ARCHITECTURE.md §12) — basic operation never requires it.
    """

    device_id: str
    name: str
    backend: str
    memory_allocated_bytes: int
    memory_peak_bytes: int
    memory_total_bytes: int
    utilization: float | None = None
    temperature_celsius: float | None = None

    def to_gpu_metrics(self) -> GpuMetrics:
        """Project onto the contract :class:`~straticate.schemas.events.GpuMetrics`."""
        return GpuMetrics(
            device_id=self.device_id,
            name=self.name,
            backend=self.backend,
            memory_allocated_bytes=self.memory_allocated_bytes,
            memory_peak_bytes=self.memory_peak_bytes,
            memory_total_bytes=self.memory_total_bytes,
            utilization=self.utilization,
            temperature_celsius=self.temperature_celsius,
        )


@dataclass(frozen=True, slots=True)
class ProcessingStats:
    """Processing statistics of the current (or last) separation.

    ``realtime_factor`` is the project's standard performance metric
    (ARCHITECTURE.md §12): ``audio duration / processing duration``. While a
    separation runs it is computed from the audio processed so far, so it is
    meaningful long before the job finishes.
    """

    stage: JobState
    chunks_completed: int
    chunks_total: int
    elapsed_seconds: float
    audio_processed_seconds: float
    audio_total_seconds: float
    realtime_factor: float
    last_chunk_seconds: float | None = None
    mean_chunk_seconds: float | None = None

    def to_processing_metrics(self) -> ProcessingMetrics:
        """Project onto the contract :class:`~straticate.schemas.events.ProcessingMetrics`."""
        return ProcessingMetrics(
            stage=self.stage,
            chunks_completed=self.chunks_completed,
            chunks_total=self.chunks_total,
            elapsed_seconds=max(self.elapsed_seconds, 0.0),
            audio_processed_seconds=max(self.audio_processed_seconds, 0.0),
            realtime_factor=max(self.realtime_factor, 0.0),
        )


@dataclass(frozen=True, slots=True)
class SeparatorRuntimeStats:
    """A snapshot of everything a separator knows about its current run.

    This is the accessor feature 019's telemetry sampler polls (~1 Hz while a
    job is active). It deliberately carries *no* event type and publishes
    nothing itself: 019 owns telemetry publishing and 013 owns transport.
    Building the wire event is three projections::

        stats = separator.runtime_stats()
        RuntimeMetricsEvent(
            type="runtime_metrics",
            job_id=stats.job_id,
            model=stats.model.to_model_info(),
            gpu=None if stats.device is None else stats.device.to_gpu_metrics(),
            processing=stats.processing.to_processing_metrics(),
        )

    Attributes:
        job_id: The job this snapshot belongs to.
        model: The model descriptor of the separator.
        device: Device statistics, or ``None`` when running on CPU.
        processing: Chunk/timing statistics.
    """

    job_id: str
    model: SeparatorInfo
    device: DeviceStats | None
    processing: ProcessingStats


class Separator(Protocol):
    """A replaceable inference backend (ARCHITECTURE.md §7).

    Lifecycle and threading contract:

    - A separator instance is long-lived and may be reused across jobs;
      constructing one may be expensive (loading weights).
    - **One separation at a time per instance.** ``separate`` must raise
      ``RuntimeError`` if re-entered — the scheduler already guarantees one
      active job (ARCHITECTURE.md §6), and :meth:`runtime_stats` would
      otherwise be ambiguous.
    - ``separate`` is awaited on the job manager's event loop, so a separator
      doing real compute must offload it (worker thread/subprocess) and yield
      regularly.
    - :meth:`runtime_stats` is called from the event loop while ``separate``
      runs; it must be a cheap, non-blocking snapshot.

    Failure contract (mirrors :class:`straticate.jobs.JobExecutor`): raise
    :class:`~straticate.jobs.cancellation.JobCancelled` when cancellation is
    observed, :class:`~straticate.errors.ApplicationError` for expected
    failures (its ``code`` survives into the job record), anything else for
    unexpected ones.
    """

    @property
    def info(self) -> SeparatorInfo:
        """The model descriptor this separator advertises (never ``None``)."""
        ...

    def runtime_stats(self) -> SeparatorRuntimeStats | None:
        """Snapshot of the current run, or ``None`` if none has started yet.

        After a separation ends the snapshot of that run remains readable, so
        a telemetry sampler racing the end of a job still gets sane numbers.
        """
        ...

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
        """Separate ``input_path`` into stems written under ``output_dir``.

        Args:
            input_path: The source media file (any format FFmpeg decodes).
            configuration: The user's requested configuration. Its
                ``mode_id`` must match :attr:`SeparatorInfo.separation_mode`;
                model/quality/device resolution happens before this call.
            progress_callback: Called after every processed chunk.
            cancellation_token: Checked between chunks; observing cancellation
                raises :class:`~straticate.jobs.cancellation.JobCancelled`.
            job_id: The job this separation belongs to — echoed into the
                result and into :meth:`runtime_stats`.
            output_dir: Directory to write the stems into (created if needed).
                The caller owns the layout; see
                :mod:`straticate.inference.layout`.
            stage_callback: Optional; called when the separator enters a new
                processing stage. Separators announce only the stages they
                really perform.

        Returns:
            The completed :class:`~straticate.schemas.jobs.SeparationResult`
            with one :class:`~straticate.schemas.jobs.Stem` per name in
            :attr:`SeparatorInfo.stems` and populated performance metrics.

        Raises:
            JobCancelled: Cancellation was observed.
            ApplicationError: An expected failure (e.g. undecodable input).
            RuntimeError: The instance is already running a separation.
        """
        ...


__all__ = [
    "STEM_NAME_PATTERN",
    "DeviceStats",
    "ProcessingStats",
    "ProgressCallback",
    "SeparationProgress",
    "Separator",
    "SeparatorInfo",
    "SeparatorRuntimeStats",
    "StageCallback",
]
