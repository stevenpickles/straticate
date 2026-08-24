"""Resolving a job request's references: audio, model, compute device.

``POST /api/v1/jobs`` carries only IDs — an ``audio_id``, a ``mode_id`` + a
``quality_id``, and optionally a ``device_id``. Turning those into the concrete
things a job needs is three small, pure, individually testable functions, kept
out of the endpoint so the resolution rules (and the error codes they produce)
can be tested without an HTTP client:

- :func:`resolve_model` — mode + quality tier → the catalog
  :class:`~straticate.schemas.Model` behind them.
- :func:`resolve_audio` — audio ID → its record *and* its on-disk source path.
- :func:`resolve_device` — an explicit device ID, or the detector's default.

Every failure is an :class:`~straticate.errors.ApplicationError` with an error
code documented in ``docs/contracts/rest-api.md``; nothing here imports FastAPI.

Import direction: :mod:`straticate.inference` already imports
:mod:`straticate.jobs`, so this module deliberately knows nothing about
separators — turning a resolved model into a
:class:`~straticate.inference.base.Separator` is
:class:`straticate.inference.registry.SeparatorRegistry`'s job.
"""

from __future__ import annotations

from pathlib import Path

from straticate.audio import AudioStore
from straticate.errors import ApplicationError
from straticate.models import ModelCatalog
from straticate.schemas import AudioFile, ComputeDevice, Model
from straticate.system import DeviceDetector


def resolve_model(catalog: ModelCatalog, mode_id: str, quality_id: str) -> Model:
    """Resolve the model backing ``quality_id`` within separation mode ``mode_id``.

    Modes and their quality options are derived from the catalog (feature 010),
    so this is a pure lookup: no mode, tier or model ID is hardcoded anywhere.

    Raises:
        ApplicationError: ``separation_mode_not_found`` (404) when no derived
            mode has that ID; ``quality_option_not_found`` (404) when the mode
            offers no such tier; ``model_not_found`` (404) when the option
            names a model the catalog does not contain.
    """
    mode = next((m for m in catalog.list_separation_modes() if m.id == mode_id), None)
    if mode is None:
        raise ApplicationError(
            "separation_mode_not_found",
            f"No separation mode with ID {mode_id!r}.",
            status_code=404,
            detail={"mode_id": mode_id},
        )
    option = next((o for o in mode.quality_options if o.id == quality_id), None)
    if option is None:
        raise ApplicationError(
            "quality_option_not_found",
            f"Separation mode {mode_id!r} has no quality option {quality_id!r}.",
            status_code=404,
            detail={"mode_id": mode_id, "quality_id": quality_id},
        )
    return catalog.get_model(option.model_id)


def resolve_audio(store: AudioStore, audio_id: str) -> tuple[AudioFile, Path]:
    """Resolve an uploaded audio ID to its record and its source file.

    A registered record whose file has disappeared from disk is reported as
    ``audio_not_found`` as well: from a job's point of view there is nothing to
    separate either way, and inventing a second error code for it would only
    give clients a second thing to handle.

    Returns:
        The :class:`~straticate.schemas.AudioFile` record and the path of the
        stored original media.

    Raises:
        ApplicationError: ``audio_not_found`` (404).
    """
    record = store.get(audio_id)
    if record is None:
        raise _audio_not_found(audio_id)
    source = store.original_path(audio_id, record.filename)
    if not source.is_file():
        raise _audio_not_found(audio_id)
    return record, source


def resolve_device(detector: DeviceDetector, device_id: str | None) -> ComputeDevice:
    """Resolve the compute device a job should run on.

    ``None`` means "let the backend pick" (the contract's default), which is
    the detector's preferred device — the first CUDA device if any, else CPU.

    Raises:
        ApplicationError: ``device_not_found`` (404) when an explicit
            ``device_id`` names no detected device.
    """
    if device_id is None:
        return detector.select_default_device()
    return detector.get_device(device_id)


def _audio_not_found(audio_id: str) -> ApplicationError:
    """Build the standard 404 for audio that cannot be separated."""
    return ApplicationError(
        "audio_not_found",
        f"No uploaded audio with ID {audio_id!r}.",
        status_code=404,
        detail={"audio_id": audio_id},
    )


__all__ = ["resolve_audio", "resolve_device", "resolve_model"]
