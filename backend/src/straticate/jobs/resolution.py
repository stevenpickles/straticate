"""Resolving a job request's references: audio, model, compute device.

``POST /api/v1/jobs`` carries only IDs — an ``audio_id``, a ``mode_id`` + a
``quality_id``, and optionally a ``device_id``. Turning those into the concrete
things a job needs is three small, pure, individually testable functions, kept
out of the endpoint so the resolution rules (and the error codes they produce)
can be tested without an HTTP client:

- :func:`resolve_model` — mode + quality tier → the catalog
  :class:`~straticate.schemas.Model` behind them.
- :func:`resolve_audio` — audio ID → its record *and* its on-disk source path.
- :func:`resolve_device` — an explicit device ID, or the first detected device
  the chosen model can actually run on.

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

    Genuinely pure, as the module docstring promises: it reads the registry and
    asks :meth:`~straticate.audio.AudioStore.original_path` — the non-creating
    accessor — for a path, so probing for audio that is not there leaves
    nothing behind.

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


def resolve_device(
    detector: DeviceDetector, device_id: str | None, *, model: Model | None = None
) -> ComputeDevice:
    """Resolve the compute device a job should run on.

    ``None`` means "let the backend pick" (the contract's default): the
    detector's preferred device — the first CUDA device if any, else CPU.

    **``model.capabilities`` decides whether that pairing is legal.** A manifest
    declares which compute backends its weights can run on (ARCHITECTURE.md §9),
    and until feature 026 nothing read it: a CUDA-only model on a CPU-only host
    was accepted with ``201`` and then died mid-job as a generic
    ``separation_failed``, which tells a user nothing they can act on. The
    answer is known at create time, so it is given at create time.

    The two cases are treated differently on purpose:

    - **The client pinned a device.** It is honoured or refused —
      ``model_device_unsupported`` — never silently swapped for another one.
    - **The client pinned nothing.** "Let the backend pick" is a request to
      pick *well*, so the first detected device the model supports wins (still
      CUDA-before-CPU, since that is the detector's order). Only when no
      detected device can run the model at all is it an error.

    Passing ``model=None`` skips the check entirely, which is what a caller
    resolving a device for something other than a job wants.

    Raises:
        ApplicationError: ``device_not_found`` (404) when an explicit
            ``device_id`` names no detected device; ``model_device_unsupported``
            (409) when the model cannot run on the resolved device (or, with no
            device pinned, on any detected device).
    """
    if device_id is not None:
        device = detector.get_device(device_id)
        if model is not None and not _supports(model, device):
            raise _device_unsupported(model, device)
        return device
    if model is None:
        return detector.select_default_device()
    for candidate in detector.devices():
        if _supports(model, candidate):
            return candidate
    raise _device_unsupported(model, detector.select_default_device())


def _supports(model: Model, device: ComputeDevice) -> bool:
    """Whether ``model``'s manifest declares support for ``device``'s backend.

    Absence is refusal, not permission: ``capabilities`` is an open set of
    backend IDs, so a backend the manifest does not mention is one nobody has
    claimed the weights work on.
    """
    return model.capabilities.get(device.backend, False)


def _device_unsupported(model: Model, device: ComputeDevice) -> ApplicationError:
    """Build the 409 for a model that cannot run on the device a job resolved."""
    supported = sorted(backend for backend, allowed in model.capabilities.items() if allowed)
    return ApplicationError(
        "model_device_unsupported",
        (
            f"Model {model.id!r} does not support compute backend "
            f"{device.backend!r} (device {device.id!r})."
        ),
        status_code=409,
        detail={
            "model_id": model.id,
            "device_id": device.id,
            "device_backend": device.backend,
            "supported_backends": supported,
        },
    )


def _audio_not_found(audio_id: str) -> ApplicationError:
    """Build the standard 404 for audio that cannot be separated."""
    return ApplicationError(
        "audio_not_found",
        f"No uploaded audio with ID {audio_id!r}.",
        status_code=404,
        detail={"audio_id": audio_id},
    )


__all__ = ["resolve_audio", "resolve_device", "resolve_model"]
