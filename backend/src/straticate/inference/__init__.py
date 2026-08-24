"""The separation engine: the ``Separator`` seam and its implementations.

The machine-learning model is a replaceable inference backend
(ARCHITECTURE.md §1). Everything the rest of the application may depend on
lives here:

- :class:`Separator` — the protocol every inference backend implements, plus
  :class:`SeparatorInfo` (model descriptor), :class:`SeparationProgress` /
  :data:`ProgressCallback` (chunk-grained progress), :data:`StageCallback`,
  and :class:`SeparatorRuntimeStats` (the telemetry accessor for feature 019).
- :class:`FakeSeparator` — the CI/development backend of ARCHITECTURE.md §8:
  simulated chunk processing, deterministic progress, cooperative
  cancellation, fake GPU statistics and real playable placeholder stems.
- :class:`SeparatorJobExecutor` — the thin adapter that makes any separator a
  :class:`straticate.jobs.JobExecutor`.
- :class:`RoFormerSeparator` — the first real inference backend (feature 026):
  a Mel-Band RoFormer running vendored architecture code over weights feature
  025 installed, with real chunk-grained progress and real device telemetry.
- :class:`SeparatorRegistry` — the architecture-keyed seam that turns a catalog
  :class:`~straticate.schemas.Model` into a separator (feature 015 resolves a
  job's model through it; a real backend is built off the event loop through
  :meth:`SeparatorRegistry.aget`).
- :mod:`straticate.inference.layout` — where a job's stems are written.

Nothing in this package leaks PyTorch, tensors, or architecture-specific
parameters to its callers.
"""

from straticate.inference.base import (
    STEM_NAME_PATTERN,
    DeviceStats,
    ProcessingStats,
    ProgressCallback,
    SeparationProgress,
    Separator,
    SeparatorInfo,
    SeparatorRuntimeStats,
    StageCallback,
)
from straticate.inference.executor import SeparatorJobExecutor
from straticate.inference.fake import (
    FAKE_ARCHITECTURE,
    FAKE_SEPARATOR_INFOS,
    FAKE_STANDARD_INFO,
    FAKE_VOCALS_INFO,
    FakeDeviceProfile,
    FakeSeparator,
    fake_separator_info,
    fake_separator_info_for_mode,
)
from straticate.inference.layout import job_output_dir, job_stems_dir, stem_path
from straticate.inference.pcm import AudioDecodeError, PcmAudio, decode_to_pcm, write_wav
from straticate.inference.registry import (
    InferenceParameterSource,
    SeparatorBuilder,
    SeparatorRegistry,
    default_separator_builders,
    fake_separator_builder,
    roformer_separator_builder,
    separator_info_from_model,
)
from straticate.inference.roformer import (
    ROFORMER_ARCHITECTURE,
    RoFormerParameters,
    RoFormerSeparator,
)

__all__ = [
    "FAKE_ARCHITECTURE",
    "FAKE_SEPARATOR_INFOS",
    "FAKE_STANDARD_INFO",
    "FAKE_VOCALS_INFO",
    "ROFORMER_ARCHITECTURE",
    "STEM_NAME_PATTERN",
    "AudioDecodeError",
    "DeviceStats",
    "FakeDeviceProfile",
    "FakeSeparator",
    "InferenceParameterSource",
    "PcmAudio",
    "ProcessingStats",
    "ProgressCallback",
    "RoFormerParameters",
    "RoFormerSeparator",
    "SeparationProgress",
    "Separator",
    "SeparatorBuilder",
    "SeparatorInfo",
    "SeparatorJobExecutor",
    "SeparatorRegistry",
    "SeparatorRuntimeStats",
    "StageCallback",
    "decode_to_pcm",
    "default_separator_builders",
    "fake_separator_builder",
    "fake_separator_info",
    "fake_separator_info_for_mode",
    "job_output_dir",
    "job_stems_dir",
    "roformer_separator_builder",
    "separator_info_from_model",
    "stem_path",
    "write_wav",
]
