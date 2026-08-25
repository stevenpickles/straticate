""":class:`DemucsSeparator` — real four-stem separation behind the ``Separator`` seam.

Feature 028. It runs the vendored Hybrid Transformer Demucs architecture
(``vendor/``, see its ``README.md``) over weights that feature 025 downloaded and
verified, on CUDA when the job resolved to a CUDA device and on CPU otherwise,
and produces the four stems the ``standard_stems`` mode advertises.

Everything architecture-specific stops here, exactly as it does for feature
026's Mel-Band RoFormer: PyTorch, tensors, STFT sizes, segment length, overlap,
the transformer's hyperparameters, the architecture's *name* — none of it
appears in :mod:`straticate.inference.base`, in the job manager, in the API or
in the frontend (ARCHITECTURE.md §1).

What is *not* here
------------------

Since feature 039, the run lifecycle is not. The comment this module used to
carry above its device helpers — "duplicated rather than shared … extracting one
``inference/torch_device.py`` for both backends is the obvious follow-up" — is
now spent: stages, decode plumbing, device placement, CUDA/NVML telemetry, the
PCM bridge, encoding, cleanup and RTF live in
:mod:`straticate.inference.torch_separator`,
:mod:`straticate.inference.torch_device` and
:mod:`straticate.inference.torch_audio`. What remains here is the Hybrid
Transformer Demucs *difference*: the catalog parameters, the checkpoint reader,
the stem mapping, and the two methods
:class:`~straticate.inference.torch_separator.TorchSeparator` leaves open —
:meth:`DemucsSeparator._run_chunks` and :meth:`DemucsSeparator._finish_stems`.

How a run is shaped
-------------------

Stages, all of them real work this separator actually performs:

``decoding``
    FFmpeg decodes the source to the model's native rate through
    :mod:`straticate.inference.pcm` — the same decoder every other separator
    uses, so format support is FFmpeg's, once, for everybody (ARCHITECTURE.md
    §5).
``loading_model``
    The network moves onto the compute device. Weights were read from disk when
    the separator was *constructed*, which the caller offloads (see
    :meth:`straticate.inference.registry.SeparatorRegistry.aget`).
``separating``
    The chunked overlap-add loop below, in a worker thread.
``post_processing``
    The network's outputs are matched to the *advertised* stem names, folded
    back into the source's channel layout and quantized to 16-bit.
``encoding``
    One WAV per stem, written ``.part``-then-renamed.

**Chunking is the progress.** The mixture is cut into fixed windows of
``window_samples`` advancing by ``(1 - overlap) * window_samples``, each faded
by a triangular envelope and summed into an accumulator that is finally divided
by the accumulated weight — the same overlap-add upstream's ``apply_model``
performs, with the same window, stride, triangular transition and centred
padding. Since feature 038 that accumulator is on the **host** and only the
window in flight is on the compute device, so peak VRAM is a function of the
window rather than of the length of the track. It is
reimplemented here rather than called so that it can report progress after every window,
check the cancellation token between windows, and run inside
:func:`asyncio.to_thread`. Every window is one forward pass through a
42-million-parameter hybrid transformer, so ``chunks_completed / chunks_total``
is a report of work genuinely done (AGENTS.md principle 3).

**Nothing positional decides which stem is which.** The network emits its
sources in the order the checkpoint was trained with — for ``htdemucs`` that is
``drums, bass, other, vocals`` — while the catalog advertises the mode's stem
order, which is ``vocals, drums, bass, other``. Those are *different orders*, so
zipping the two together would write drums into ``vocals.wav`` with nothing
anywhere reporting a problem. The mapping is by **name**, from the ``sources``
the catalog carries, and the checkpoint's own ``sources`` are checked against it
at load time; see :func:`stem_source_indices` and :func:`_check_sources`.
"""

from __future__ import annotations

import inspect
import logging
import math
import pickle
import time
import types
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from functools import cache
from pathlib import Path
from typing import Any, Final, Self, cast

import torch
from torch import Tensor

from straticate.audio.ffmpeg import DEFAULT_FFMPEG_TIMEOUT_SECONDS
from straticate.errors import ApplicationError
from straticate.inference.base import ProgressCallback, SeparatorInfo
from straticate.inference.demucs.architecture import DEMUCS_ARCHITECTURE
from straticate.inference.demucs.vendor import HTDemucs
from straticate.inference.model_errors import (
    parameters_invalid,
    positive_int,
    require_installed_weights,
    weights_not_loadable,
)
from straticate.inference.pcm import PcmAudio
from straticate.inference.torch_audio import pcm_to_tensor, tensor_to_pcm, to_source_channels
from straticate.inference.torch_overlap_add import HostOverlapAdd
from straticate.inference.torch_separator import RunState, TorchSeparator
from straticate.jobs.cancellation import CancellationToken

logger = logging.getLogger(__name__)

# ``DEMUCS_ARCHITECTURE`` is imported from
# :mod:`straticate.inference.demucs.architecture` and re-exported here (see
# ``__all__``), because the registry needs the *name* at import time and must
# not pull torch in to get it (feature 034).

DEFAULT_OVERLAP: Final = 0.25
"""Fraction of each window that the next window repeats; upstream's default."""

DEFAULT_TRANSITION_POWER: Final = 1.0
"""Exponent on the triangular cross-fade. ``1.0`` is a linear transition."""

NORMALIZATION_EPSILON: Final = 1e-8
"""Guard on the mixture's standard deviation, matching upstream's ``demucs.api``."""

_FRACTION_PARAMETER_NAMES: Final = frozenset({"segment"})
"""Model parameters a catalog entry states as ``[numerator, denominator]``.

``htdemucs`` was trained with ``segment = Fraction(39, 5)`` — 7.8 s — and
``HTDemucs.forward`` turns that into a sample count with
``int(self.segment * self.samplerate)``. That multiplication is exact for a
rational and *approximate* for a float, and ``int`` truncates, so the float
spelling of a fifth-of-a-second segment can come out **one sample short of the
training length** — a window the model then silently zero-pads on every forward
pass, for the whole track, with nothing reporting anything. It does not bite for
39/5 in particular (``int(7.8 * 44100)`` happens to round up to 343 980), but it
does for its immediate neighbours at the same rate: 7/5 gives 61 739 instead of
61 740, and 41/5 gives 361 619 instead of 361 620. Depending on which side of a
rounding boundary the next checkpoint's segment falls is not a thing to depend
on, so the catalog states the rational the checkpoint states and this module
reconstructs it. A plain number is still accepted, for a checkpoint whose
segment really is one.
"""


@dataclass(frozen=True, slots=True)
class DemucsParameters:
    """Everything the catalog says about *how* to run one Demucs model.

    This is the payload of the manifest's ``default_inference_parameters`` — the
    field ARCHITECTURE.md §9 provides precisely so that per-model tuning is
    **data**, not code. It never reaches the API (``models/catalog.py`` keeps it
    off :class:`~straticate.schemas.Model`) and no application code reads it;
    only this module knows what any of it means.

    Attributes:
        model: Constructor arguments for the architecture — the checkpoint's own
            hyperparameters, including the ``sources`` it emits and in what
            order. A checkpoint loads only into a network built with exactly
            these, which is why they are pinned per model rather than defaulted
            in code.
        chunk_samples: Samples per forward pass, or ``None`` to use the length
            the checkpoint was trained at (which is the sensible default and
            the only value that avoids padding every window).
        overlap: Fraction of a window the next window repeats. Windows advance
            by ``(1 - overlap) * window``.
        transition_power: Exponent applied to the triangular cross-fade between
            neighbouring windows. Must be ``>= 1``.
    """

    model: Mapping[str, Any]
    chunk_samples: int | None = None
    overlap: float = DEFAULT_OVERLAP
    transition_power: float = DEFAULT_TRANSITION_POWER

    @property
    def sources(self) -> tuple[str, ...]:
        """The stem names the network emits, **in the order it emits them**."""
        return tuple(cast("Sequence[str]", self.model["sources"]))

    @property
    def audio_channels(self) -> int:
        """Channels the network takes (2 for every published Demucs model)."""
        return int(self.model.get("audio_channels", 2))

    @property
    def sample_rate(self) -> int:
        """The rate the network was trained at, from the checkpoint's own kwargs."""
        return int(self.model.get("samplerate", 44100))

    @property
    def training_samples(self) -> int:
        """Samples in one training segment — the largest sensible window.

        ``HTDemucs`` with ``use_train_segment`` (the default, and what every
        published checkpoint was saved with) pads any shorter input up to this
        length on every forward pass and trims the result back, so a window
        longer than this runs the network off the distribution it was trained
        on and a shorter one simply wastes compute on padding.
        """
        segment = self.model.get("segment", 10)
        return int(Fraction(segment) * self.sample_rate)

    @property
    def window_samples(self) -> int:
        """Samples per forward pass, defaulted to :attr:`training_samples`.

        A smaller window is **not** a memory dial for this architecture, and
        that is worth knowing before someone reaches for it: because
        ``use_train_segment`` pads every window back up to the training length,
        the forward pass allocates the same working set whatever this is.
        Measured on a 60 s clip, an RTX 4060 and this checkpoint: 88 200 samples
        peaked at 660.6 MiB and 343 980 at 661.7 MiB — a 1 MiB difference for
        3.6x the wall clock (40 forward passes instead of 11). The length of the
        track used to move the peak instead; since feature 038 streamed the
        overlap-add onto the host, nothing much moves it at all.
        """
        return self.training_samples if self.chunk_samples is None else self.chunk_samples

    @property
    def stride_samples(self) -> int:
        """How far each window advances, at least one sample."""
        return max(int((1.0 - self.overlap) * self.window_samples), 1)

    @classmethod
    def from_catalog(cls, raw: Mapping[str, Any] | None, *, model_id: str) -> Self:
        """Build from a manifest's ``default_inference_parameters`` block.

        Shape::

            "default_inference_parameters": {
              "model":     { "sources": [...], "segment": [39, 5], … },
              "inference": { "chunk_size": 343980, "overlap": 0.25 }
            }

        Raises:
            ApplicationError: ``model_parameters_invalid`` (500) — the block is
                missing, malformed, or names a constructor argument the
                architecture does not take. A catalog that cannot be run is a
                deployment error, not a client error, and it says so loudly
                rather than falling back to defaults that would load the wrong
                network.
        """
        if not raw:
            raise parameters_invalid(model_id, "no default_inference_parameters block")
        model_block = raw.get("model")
        if not isinstance(model_block, Mapping):
            raise parameters_invalid(
                model_id, "default_inference_parameters.model must be an object"
            )
        declared = cast("Mapping[str, Any]", model_block)
        unknown = sorted(set(declared) - _architecture_parameter_names())
        if unknown:
            raise parameters_invalid(
                model_id, f"unknown architecture parameters: {', '.join(unknown)}"
            )
        normalized: dict[str, Any] = {
            key: _as_fraction(value, key, model_id) if key in _FRACTION_PARAMETER_NAMES else value
            for key, value in declared.items()
        }
        _validate_sources(normalized.get("sources"), model_id)

        inference_block = raw.get("inference")
        inference = cast(
            "Mapping[str, Any]",
            inference_block if isinstance(inference_block, Mapping) else {},
        )
        chunk_size = inference.get("chunk_size")
        chunk_samples = None if chunk_size is None else positive_int(chunk_size, model_id)
        overlap = _fraction_in_unit_interval(
            inference.get("overlap", DEFAULT_OVERLAP), "overlap", model_id
        )
        transition_power = _at_least_one(
            inference.get("transition_power", DEFAULT_TRANSITION_POWER),
            "transition_power",
            model_id,
        )
        parameters = cls(
            model=normalized,
            chunk_samples=chunk_samples,
            overlap=overlap,
            transition_power=transition_power,
        )
        if parameters.window_samples > parameters.training_samples:
            raise parameters_invalid(
                model_id,
                (
                    f"inference.chunk_size is {parameters.window_samples} samples, longer than "
                    f"the {parameters.training_samples} samples this checkpoint was trained at; "
                    f"the network would run off its training distribution"
                ),
            )
        return parameters


class DemucsSeparator(TorchSeparator):
    """A :class:`~straticate.inference.base.Separator` running Hybrid Transformer Demucs.

    The run lifecycle is
    :class:`~straticate.inference.torch_separator.TorchSeparator`'s; what this
    class adds is the two architecture-specific holes — the overlap-add chunk
    loop and the name-based stem mapping — plus the catalog parameters and the
    checkpoint reader.

    Construction is the expensive half: it reads the checkpoint off disk and
    builds a 42-million-parameter network. That is why
    :meth:`straticate.inference.registry.SeparatorRegistry.aget` exists — a build
    must never happen on the event loop — and why the network is built once and
    reused for every job of that model.

    Args:
        info: The model descriptor, projected from the catalog entry, so stems,
            sample rate, version and display name all come from
            ``models/catalog.json`` and never from a constant in this file.
        weights_file: Where feature 025 published the verified checkpoint —
            ``weights_path(settings.models_dir, model.id)``.
        parameters: The catalog's ``default_inference_parameters``.
        ffmpeg_timeout_seconds: Bound for the decode subprocesses, passed down
            from ``Settings.ffmpeg_timeout_seconds``.

    Raises:
        ApplicationError: ``model_weights_missing`` (409) when the checkpoint is
            not installed, ``model_weights_invalid`` (500) when it is installed
            but does not load into this architecture, or
            ``model_parameters_invalid`` (500) when the catalog entry's
            parameters are unusable — including when its stem list and the
            checkpoint's own ``sources`` cannot be reconciled.
    """

    def __init__(
        self,
        info: SeparatorInfo,
        *,
        weights_file: Path,
        parameters: DemucsParameters,
        ffmpeg_timeout_seconds: float = DEFAULT_FFMPEG_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(info, ffmpeg_timeout_seconds=ffmpeg_timeout_seconds)
        self._parameters = parameters
        # Weights first, deliberately. "Install this model" (409) is the answer a
        # user can act on, and every other check here is about a catalog entry a
        # user did not write; reporting a manifest fault instead of a missing
        # download would send them somewhere they cannot go.
        require_installed_weights(info, weights_file)
        _check_sample_rate(info, parameters)
        self._stem_sources = stem_source_indices(info, parameters)
        self._model = _load_model(info, weights_file, parameters)

    @property
    def parameters(self) -> DemucsParameters:
        """The catalog parameters this separator was built with (for tests/telemetry)."""
        return self._parameters

    @property
    def stem_sources(self) -> tuple[int, ...]:
        """For each advertised stem, the network output index it comes from."""
        return self._stem_sources

    # -- the architecture-specific halves -----------------------------------

    def _run_chunks(
        self,
        source: PcmAudio,
        run: RunState,
        progress_callback: ProgressCallback,
        cancellation_token: CancellationToken,
        device: torch.device,
    ) -> Tensor:
        """The chunked overlap-add loop, streamed. Runs in a worker thread.

        Returns the per-source estimates as a ``(sources, channels, samples)``
        float tensor on the CPU, in the **network's** source order — the
        advertised order is applied in :meth:`_finish_stems`.

        The mixture is normalized by its own mono reference before separation
        and the normalization is undone afterwards, which is what upstream's
        ``demucs.api.Separator.separate_tensor`` does and what the model expects
        to see.

        **Nothing whole-track is on the compute device** (feature 038), with one
        deliberate and bounded exception below. The decoded mixture stays on the
        host and one window at a time is copied across; the estimate comes
        straight back into a
        :class:`~straticate.inference.torch_overlap_add.HostOverlapAdd`, which
        holds the sum and the weight. Feature 028 had already cut the slope from
        5.85 to 1.85 MiB per second of audio by writing every whole-track step in
        place, and a ten-minute track's peak allocation from 4,023 MiB to
        1,662 MiB — but it could not make the peak *independent* of the track
        while the accumulator was device-resident. It is not any more, so the
        in-place discipline now applies to host tensors, where it costs RAM
        rather than VRAM, and the peak on the card is a function of the window.

        **The exception is the normalization statistics, and it is why they are
        computed the way they are.** ``shift`` and ``scale`` are reductions over
        the *whole* mono reference, and a reduction is exactly the operation that
        is not bit-identical between a CPU cascade sum and a CUDA tree reduction
        — measured here, ``reference.mean()`` differs by 2 ULP across the two.
        Feature 038's constraint is bit-identical output, so the reduction stays
        on the device and the mono reference is moved there for it: **four bytes
        per sample, transiently**, freed before the first forward pass. The
        mixdown that produces it (``mean`` over two channels — one addition and
        one halving per sample, in the only order there is) *is* element-wise and
        so is computed on the host. The window is then normalized on the device
        rather than the track, which is the same arithmetic per element and
        keeps the zero padding past the ends of the track zero, exactly as
        normalizing the track before cutting it did.

        See
        :meth:`straticate.inference.torch_separator.TorchSeparator._run_chunks`
        for the contract this keeps.
        """
        parameters = self._parameters
        window = parameters.window_samples
        stride = parameters.stride_samples

        mixture = pcm_to_tensor(source, parameters.audio_channels)
        channels, frames = mixture.shape[0], mixture.shape[-1]

        reference = mixture.mean(dim=0).to(device)
        shift = reference.mean()
        scale = reference.std() + NORMALIZATION_EPSILON
        del reference

        chunks_total = max(1, math.ceil(frames / stride))
        run.chunks_total = chunks_total
        run.audio_total_seconds = source.duration_seconds
        self._report(progress_callback, run)

        envelope = _transition_window(window, parameters.transition_power, device)
        sources = len(parameters.sources)
        accumulator = HostOverlapAdd((sources, channels, frames), frames)

        for index, offset in enumerate(range(0, frames, stride), start=1):
            cancellation_token.raise_if_cancelled()
            chunk_started = time.monotonic()
            length = min(window, frames - offset)
            part = _centred_window(mixture, offset, length, window, device, shift, scale)

            estimate = _center_trim(self._forward(part), length)

            faded = envelope[:length]
            accumulator.add(offset, estimate * faded, faded)

            run.last_chunk_seconds = time.monotonic() - chunk_started
            run.chunk_seconds_total += run.last_chunk_seconds
            run.chunks_completed = index
            covered = min(offset + length, frames)
            run.audio_processed_seconds = covered / source.sample_rate
            self._report(progress_callback, run)

        # Every sample is covered by at least one window and the envelope is
        # strictly positive, so the divisor is never zero; the clamp is
        # upstream's ``assert sum_weight.min() > 0`` restated as a guard rather
        # than as a crash.
        estimates = accumulator.resolve(minimum_weight=NORMALIZATION_EPSILON)
        estimates.mul_(scale.to("cpu")).add_(shift.to("cpu"))
        return estimates.detach().to("cpu", dtype=torch.float32)

    def _forward(self, part: Tensor) -> Tensor:
        """One forward pass, returned as ``(sources, channels, samples)``.

        No ``torch.autocast`` on CUDA, deliberately and unlike feature 026: this
        architecture's masking path runs on **complex** spectrograms
        (``torch.view_as_complex`` / ``torch.istft``), and complex half
        precision is not a working dtype here. Upstream runs this network in
        float32 too.
        """
        with torch.inference_mode():
            output = self._model(part.unsqueeze(0))
        return cast("Tensor", output)[0].to(torch.float32)

    def _finish_stems(self, estimates: Tensor, source: PcmAudio) -> list[PcmAudio]:
        """Match the network's outputs to the advertised stems, in advertised order.

        Real work, and the reason ``post_processing`` is announced: the network
        emits its sources in the order it was trained with and the catalog
        advertises them in the order the *mode* uses, so this is where the two
        are reconciled — by name, through :attr:`_stem_sources`, never by
        position. The list returned lines up with
        :attr:`SeparatorInfo.stems` index for index, which is what
        :meth:`~straticate.inference.torch_separator.TorchSeparator._encode`
        relies on when it zips the two together.
        """
        channels = source.channel_count
        return [
            tensor_to_pcm(to_source_channels(estimates[index], channels), source.sample_rate)
            for index in self._stem_sources
        ]


# --------------------------------------------------------------------------
# Reading a checkpoint without executing it
# --------------------------------------------------------------------------


class CheckpointArchitecture:
    """Stand-in for the architecture class a Demucs ``.th`` package pickles.

    A Demucs checkpoint is not a bare state dict: it is a pickled mapping
    ``{klass, args, kwargs, state, training_args, metrics}`` whose ``klass`` is
    a *reference to the class object* ``demucs.htdemucs.HTDemucs``. Upstream
    resolves it by importing the real class and calling it. Straticate does not,
    for two reasons — the module path does not exist here (the architecture is
    vendored under a different name), and the hyperparameters come from the
    catalog, where ARCHITECTURE.md §9 puts per-model tuning. So the reference is
    resolved to this inert placeholder and never called.
    """


CHECKPOINT_PICKLE_GLOBALS: Final = frozenset(
    {
        # ``segment``, the training window, stored as an exact rational.
        "fractions.Fraction",
        # NumPy scalars inside ``training_args`` and ``metrics``.
        "numpy.dtype",
        "numpy.ndarray",
        "numpy.core.multiarray.scalar",
        "numpy.core.multiarray._reconstruct",
        "numpy._core.multiarray.scalar",
        "numpy._core.multiarray._reconstruct",
    }
)
"""What a Demucs checkpoint may name **beyond** torch's own allowlist.

The first two spellings were read out of the real ``htdemucs`` package; the
remaining numpy entries are the other spellings NumPy's rebuild path uses across
versions (``numpy._core`` is NumPy 2's private module), listed so that a
different checkpoint of the same architecture does not need a code change to
load. Torch's own list already carries ``collections.OrderedDict`` and
``_codecs.encode``, which is why they are not repeated here.
"""

_FALLBACK_TORCH_PICKLE_GLOBALS: Final = frozenset(
    {
        "collections.OrderedDict",
        "_codecs.encode",
        "torch._utils._rebuild_tensor",
        "torch._utils._rebuild_tensor_v2",
        "torch._utils._rebuild_tensor_v3",
        "torch._utils._rebuild_parameter",
        "torch._utils._rebuild_meta_tensor_no_storage",
        "torch._utils._rebuild_sparse_tensor",
        "torch.Size",
        "torch.device",
        "torch.BFloat16Storage",
        "torch.BoolStorage",
        "torch.ByteStorage",
        "torch.CharStorage",
        "torch.DoubleStorage",
        "torch.FloatStorage",
        "torch.HalfStorage",
        "torch.IntStorage",
        "torch.LongStorage",
        "torch.ShortStorage",
        "torch.Storage",
    }
)
"""What to allow from torch if :func:`torch_pickle_globals` cannot ask torch.

Deliberately the *minimum* a published Demucs package is known to need — a real
``htdemucs`` file names exactly ``collections.OrderedDict``, ``_codecs.encode``
and ``torch._utils._rebuild_tensor_v2`` — plus the storage and shape types a
neighbouring checkpoint might. A build that falls back and then meets a
checkpoint torch would rebuild some other way refuses it loudly, which is the
right failure: refusing a valid file is recoverable, and widening an allowlist
by guessing is not.
"""


@cache
def torch_pickle_globals() -> frozenset[str]:
    """The ``module.name`` set torch's own ``weights_only`` loader permits.

    ``torch._weights_only_unpickler`` is where torch decides which globals are
    safe to resolve while rebuilding tensors, and its answer is 144 individually
    enumerated names: every ``_rebuild_*`` helper, every storage and tensor type,
    the dtype and layout singletons, ``collections.OrderedDict`` and
    ``_codecs.encode``. Crucially it does **not** contain ``torch.load``,
    ``torch.save``, or anything else that is an entry point rather than a data
    constructor.

    Asking torch is what makes "the same boundary torch's own ``weights_only``
    unpickler draws" a true statement rather than a claim. The list moves with
    every release, and reproducing it by hand would drift into either refusing
    valid checkpoints or -- the failure this exists to prevent -- permitting more
    than torch does.

    It is a private module, so the import is guarded, and a build where it has
    moved falls back to :data:`_FALLBACK_TORCH_PICKLE_GLOBALS` with a warning.
    Cached: it is asked once per checkpoint read and cannot change within a
    process.
    """
    try:
        from torch import _weights_only_unpickler

        # Private on purpose, and reached on purpose: this *is* torch's
        # allowlist, and duplicating it here is the mistake this call avoids.
        # The guarded import and the fallback below are what make depending on
        # a private name safe -- a torch that moves it degrades to refusing
        # unusual checkpoints, never to permitting more.
        resolve: Any = _weights_only_unpickler._get_allowed_globals  # pyright: ignore[reportPrivateUsage]
        allowed = frozenset(cast("Mapping[str, Any]", resolve()))
    except Exception:  # pragma: no cover - only on a torch that moved the module
        logger.warning(
            "torch._weights_only_unpickler._get_allowed_globals() is unavailable, so this "
            "build falls back to a minimal allowlist when reading a model checkpoint. A "
            "checkpoint needing a rebuild helper outside it is refused as "
            "model_weights_invalid rather than loaded."
        )
        return _FALLBACK_TORCH_PICKLE_GLOBALS
    return allowed or _FALLBACK_TORCH_PICKLE_GLOBALS


class RestrictedUnpickler(pickle.Unpickler):
    """A pickle reader that resolves only the names a checkpoint is allowed to name.

    Feature 025 verifies a SHA-256 before publishing an artifact, which proves
    the file is the file that was pinned -- it does not make a pickle safe to
    execute, and upstream's loader executes one. So this reader resolves the
    architecture reference to an inert placeholder that is never called, and
    checks every other name against **an enumeration**:
    :func:`torch_pickle_globals` (torch's own ``weights_only`` allowlist -- 144
    named data constructors, ``torch.load`` not among them) plus
    :data:`CHECKPOINT_PICKLE_GLOBALS` (the rational and the numpy scalars a
    Demucs package additionally carries). Anything else raises.

    **Why an enumeration and not "trust the torch module".** A pickle's
    ``GLOBAL`` opcode is a pair of raw strings and is not bound by any object's
    real ``__module__``. The first version of this class admitted anything whose
    module was ``torch``, so a hand-written ``c torch \n load \n ... R``
    resolved **and called** ``torch.load`` -- which on the ``torch>=2.4`` this
    project allows defaults to ``weights_only=False``, that is, a full unpickling
    of a second file of the attacker's choosing. Code review found it; the
    ``GLOBAL``-opcode path is now pinned by
    ``tests/test_demucs_separator.py::test_the_reader_refuses_a_hand_written_global_opcode``.

    Defence in depth rather than a security boundary: the digest is the
    boundary. What this removes is the class of accident where a file that
    passed the digest -- because the digest itself was wrong, or the artifact was
    hand-placed -- still gets to run code at load time.
    """

    def find_class(self, module: str, name: str) -> Any:
        """Resolve ``module.name``, or refuse.

        Raises:
            pickle.UnpicklingError: The name is neither the architecture class
                nor one of the enumerated data constructors.
        """
        if module == "demucs" or module.startswith("demucs."):
            return CheckpointArchitecture
        reference = f"{module}.{name}"
        if reference in CHECKPOINT_PICKLE_GLOBALS or reference in torch_pickle_globals():
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"a model checkpoint may not reference {reference}")


def _restricted_pickle_module() -> Any:
    """A stand-in for :mod:`pickle` whose ``Unpickler`` is the restricted one.

    ``torch.load`` takes a ``pickle_module`` and subclasses its ``Unpickler`` to
    add its own storage handling, so handing it a module object with everything
    :mod:`pickle` has except a stricter reader is the supported way in.
    """
    module = types.ModuleType("straticate_restricted_pickle")
    module.__dict__.update(pickle.__dict__)
    module.Unpickler = RestrictedUnpickler  # pyright: ignore[reportAttributeAccessIssue]
    return module


def load_checkpoint_package(path: Path, *, model_id: str) -> Mapping[str, Any]:
    """Read a Demucs ``.th`` package from disk without executing anything in it.

    Args:
        path: The installed checkpoint.
        model_id: For the error envelope.

    Returns:
        The package mapping — ``state`` (the tensors), ``kwargs`` (the
        hyperparameters the checkpoint was built with) and the training metadata
        upstream stores alongside them.

    Raises:
        ApplicationError: ``model_weights_invalid`` (500) when the file is not a
            readable Demucs package.
    """
    try:
        package = torch.load(
            path,
            map_location="cpu",
            # ``weights_only=True`` cannot read this file: it is a package, not
            # a state dict, and torch's own allowlist has no way to resolve a
            # class from a module that is not installed. The restricted reader
            # below is stricter in the way that matters — it resolves the class
            # to something inert instead of importing it.
            weights_only=False,
            pickle_module=_restricted_pickle_module(),
        )
    except Exception as exc:
        logger.exception("Reading the checkpoint for model %s failed", model_id)
        raise ApplicationError(
            "model_weights_invalid",
            f"The installed weights for model {model_id!r} are not a readable checkpoint.",
            status_code=500,
            detail={"model_id": model_id, "reason": type(exc).__name__},
        ) from exc
    if not isinstance(package, Mapping) or "state" not in package:
        raise ApplicationError(
            "model_weights_invalid",
            (
                f"The installed weights for model {model_id!r} are not a Demucs checkpoint "
                f"package (no 'state' entry)."
            ),
            status_code=500,
            detail={"model_id": model_id, "reason": "not_a_demucs_package"},
        )
    return cast("Mapping[str, Any]", package)


# --------------------------------------------------------------------------
# Construction helpers
# --------------------------------------------------------------------------


def _architecture_parameter_names() -> frozenset[str]:
    """Constructor arguments a catalog entry may set on the architecture.

    Read off the vendored class rather than transcribed, so the whitelist cannot
    drift from the code it guards. It is a whitelist that **rejects** rather than
    one that silently drops: upstream's own loader drops unknown keys with a
    warning because it reads community training configs, but a key in
    ``models/catalog.json`` is something a maintainer typed, and a typo that is
    quietly ignored is a model running with the wrong hyperparameters.
    """
    signature = inspect.signature(cast("Any", HTDemucs).__init__)
    return frozenset(signature.parameters) - {"self"}


def _check_sample_rate(info: SeparatorInfo, parameters: DemucsParameters) -> None:
    """Refuse a catalog entry whose two sample rates disagree.

    The manifest states the rate twice, and both are load-bearing in different
    places: the entry's top-level ``sample_rate`` is what FFmpeg resamples the
    source to
    (:meth:`straticate.inference.torch_separator.TorchSeparator._decode`, via
    :attr:`SeparatorInfo.sample_rate`), while
    ``default_inference_parameters.model.samplerate`` is what the architecture is
    built with and what sizes the training window. They are two edits apart, and
    if they diverge the network is fed audio at one rate while its window is
    sized for another — degraded output, from a data edit, with nothing anywhere
    reporting a problem.

    That is the same failure mode :func:`_check_sources` exists to prevent, so
    the guard belongs in the same place: at construction, loudly.

    Raises:
        ApplicationError: ``model_parameters_invalid`` (500).
    """
    if info.sample_rate != parameters.sample_rate:
        raise parameters_invalid(
            info.model_id,
            (
                f"the catalog entry decodes audio at {info.sample_rate} Hz (its sample_rate) "
                f"but builds the network at {parameters.sample_rate} Hz "
                f"(default_inference_parameters.model.samplerate); the two must agree"
            ),
        )


def _load_model(info: SeparatorInfo, weights_file: Path, parameters: DemucsParameters) -> Any:
    """Build the architecture and load the installed checkpoint into it.

    ``strict=True`` is the whole point: a checkpoint that does not match the
    vendored architecture exactly must fail here, loudly, rather than load
    partially and produce plausible-sounding nonsense. The published weights are
    stored in ``float16``; ``load_state_dict`` casts them into the network's
    ``float32`` parameters, which is what upstream does too.
    """
    require_installed_weights(info, weights_file)
    package = load_checkpoint_package(weights_file, model_id=info.model_id)
    _check_sources(package, parameters, model_id=info.model_id)
    try:
        model = HTDemucs(**parameters.model)
    except (TypeError, ValueError, AssertionError) as exc:
        raise parameters_invalid(info.model_id, str(exc)) from exc
    try:
        model.load_state_dict(package["state"], strict=True)
    except ApplicationError:
        raise
    except Exception as exc:
        logger.exception("Loading weights for model %s failed", info.model_id)
        raise weights_not_loadable(info.model_id, type(exc).__name__) from exc
    model.eval()
    for parameter in cast("Iterable[Tensor]", model.parameters()):
        parameter.requires_grad_(False)
    return model


def _check_sources(
    package: Mapping[str, Any], parameters: DemucsParameters, *, model_id: str
) -> None:
    """Refuse a catalog entry whose ``sources`` disagree with the checkpoint's.

    This is the guard that decides stem **order**, and it is the only one that
    can. :func:`stem_source_indices` maps advertised stem names onto the
    catalog's ``sources`` list, which is correct given that list — but it can
    only check that the two are the same *set*, so a catalog that transposes two
    of the network's sources passes it, and every shape assertion and the strict
    ``load_state_dict`` pass too, while two stems' **audio** is swapped. The only
    authority on the order is the file the tensors came from, which records the
    ``sources`` it was trained with.

    So this refuses **both** a disagreement and an inability to check. Returning
    quietly when a package carries no usable ``sources`` was the original
    version, and code review was right that it defeated the guard: a
    ``htdemucs``-family checkpoint saved without ``kwargs``, plus a transposed
    catalog entry, would have written the drums into ``bass.wav`` with nothing
    reporting a problem — the exact outcome this feature's acceptance criteria
    say is impossible. A checkpoint whose order cannot be verified is a
    checkpoint this backend will not run.

    ``sources`` is read leniently on *type* and strictly on *content*: a
    ``list``, a ``tuple`` and a ``numpy.ndarray`` of names are all fine (torch
    checkpoints carry all three in practice), a string is not a list of names,
    and anything whose elements are not strings is not a source list.

    Raises:
        ApplicationError: ``model_weights_invalid`` (500) when the checkpoint
            records no usable ``sources``, so the catalog's order cannot be
            checked against it; ``model_parameters_invalid`` (500) when it does
            and the two disagree.
    """
    declared = package.get("kwargs")
    recorded: Any = (
        cast("Mapping[str, Any]", declared).get("sources")
        if isinstance(declared, Mapping)
        else None
    )
    actual = _source_names(recorded)
    if actual is None:
        raise ApplicationError(
            "model_weights_invalid",
            (
                f"The installed weights for model {model_id!r} record no source list, so the "
                f"order the catalog claims the network emits cannot be verified against them."
            ),
            status_code=500,
            detail={"model_id": model_id, "reason": "no_recorded_sources"},
        )
    if actual != parameters.sources:
        raise parameters_invalid(
            model_id,
            (
                f"the catalog says the network emits {list(parameters.sources)} but the "
                f"installed checkpoint was trained to emit {list(actual)}"
            ),
        )


def _source_names(recorded: Any) -> tuple[str, ...] | None:
    """The checkpoint's ``sources`` as a tuple of names, or ``None`` if unusable.

    Deliberately not an ``isinstance(..., Sequence)`` test: ``numpy.ndarray`` is
    not registered as one, and silently treating a numpy array of names as "no
    source list" is how the caller's guard came to be skippable.
    """
    if recorded is None or isinstance(recorded, str | bytes):
        return None
    try:
        names = list(cast("Iterable[Any]", recorded))
    except TypeError:
        return None
    if not names or not all(isinstance(name, str) for name in names):
        return None
    return tuple(cast("list[str]", names))


def stem_source_indices(info: SeparatorInfo, parameters: DemucsParameters) -> tuple[int, ...]:
    """For each advertised stem, the index of the network output it comes from.

    **Position decides nothing.** The network emits ``drums, bass, other,
    vocals`` — the order ``htdemucs`` was trained in — while the catalog
    advertises the ``standard_stems`` order, ``vocals, drums, bass, other``.
    Feature 026 learned this the expensive way with its residual stem: the
    manifest schema imposes no order on ``stems``, the whole promise of a
    replaceable backend is that another checkpoint is a pure data edit, and
    silently wrong audio is the worst thing this module can produce. So the
    network's own order is *stated* in
    ``default_inference_parameters.model.sources`` and each advertised stem is
    looked up in it by name.

    **What this function cannot decide is whether that stated order is right.**
    It compares the two lists as *sets*, so a catalog that transposes two of the
    network's sources passes here and every later assertion about shapes and
    counts as well. Checking the order is :func:`_check_sources`'s job, against
    the ``sources`` the checkpoint itself records, and it is mandatory for
    exactly that reason.

    Returns:
        One index per advertised stem, in advertised order.

    Raises:
        ApplicationError: ``model_parameters_invalid`` (500) when the advertised
            stems and the network's sources are not the same set — a stem the
            network does not produce, or a source the catalog does not
            advertise, either of which would otherwise be a stem file nobody can
            account for.
    """
    sources = parameters.sources
    advertised = info.stems
    if sorted(sources) != sorted(advertised):
        missing = sorted(set(advertised) - set(sources))
        extra = sorted(set(sources) - set(advertised))
        raise parameters_invalid(
            info.model_id,
            (
                f"the catalog advertises stems {list(advertised)} but the network emits "
                f"{list(sources)}"
                + (f"; not produced: {missing}" if missing else "")
                + (f"; not advertised: {extra}" if extra else "")
            ),
        )
    return tuple(sources.index(name) for name in advertised)


def _validate_sources(value: Any, model_id: str) -> None:
    """The one model parameter this backend cannot run without."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise parameters_invalid(
            model_id, "default_inference_parameters.model.sources must be a list of stem names"
        )
    names = cast("Sequence[Any]", value)
    if not names or not all(isinstance(name, str) for name in names):
        raise parameters_invalid(
            model_id, "default_inference_parameters.model.sources must be a list of stem names"
        )
    if len(set(cast("Sequence[str]", names))) != len(names):
        raise parameters_invalid(model_id, "model.sources contains a duplicate name")


def _as_fraction(value: Any, key: str, model_id: str) -> Any:
    """``[39, 5]`` → ``Fraction(39, 5)``; a bare number is passed through.

    See :data:`_FRACTION_PARAMETER_NAMES` for why this exists.
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value
    if isinstance(value, list) and len(cast("list[Any]", value)) == 2:
        pair = cast("list[Any]", value)
        if all(isinstance(part, int) and not isinstance(part, bool) for part in pair):
            numerator, denominator = cast("list[int]", pair)
            if denominator > 0:
                return Fraction(numerator, denominator)
    raise parameters_invalid(
        model_id,
        f"model.{key} must be a number or a [numerator, denominator] pair, got {value!r}",
    )


def _fraction_in_unit_interval(value: Any, key: str, model_id: str) -> float:
    """Coerce a catalog number to a fraction in ``[0, 1)``, or fail loudly."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0.0 <= value < 1.0:
        raise parameters_invalid(
            model_id, f"inference.{key} must be a number in [0, 1), got {value!r}"
        )
    return float(value)


def _at_least_one(value: Any, key: str, model_id: str) -> float:
    """Coerce a catalog number to a float ``>= 1``, or fail loudly."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 1.0:
        raise parameters_invalid(model_id, f"inference.{key} must be a number >= 1, got {value!r}")
    return float(value)


# --------------------------------------------------------------------------
# Windowing
# --------------------------------------------------------------------------


def _transition_window(window: int, power: float, device: torch.device) -> Tensor:
    """The per-window envelope: a triangle, normalized, raised to ``power``.

    Upstream's shape exactly (``apply_model``): rising 1..⌈w/2⌉ then falling
    ⌊w/2⌋..1, divided by its own maximum. With ``overlap <= 0.5`` and
    ``power == 1`` this makes the cross-fade between neighbouring windows
    linear; a larger power sharpens the transition.
    """
    half = window // 2
    rising = torch.arange(1, half + 1, dtype=torch.float32, device=device)
    falling = torch.arange(window - half, 0, -1, dtype=torch.float32, device=device)
    envelope = torch.cat([rising, falling])
    return (envelope / envelope.max()) ** power


def _centred_window(
    mixture: Tensor,
    offset: int,
    length: int,
    target: int,
    device: torch.device,
    shift: Tensor,
    scale: Tensor,
) -> Tensor:
    """``target`` normalized samples covering ``mixture[..., offset : offset + length]``.

    The extra context is taken **from the surrounding audio** where there is any,
    centred on the requested span, and zero-padded only past the ends of the
    track — which is upstream's ``TensorChunk.padded``. It matters for the final
    window of a track: filling the tail with real preceding audio rather than
    silence is what keeps the model from hearing an artificial edge there.

    ``mixture`` is the **raw** decoded audio on the host (feature 038); this is
    where a window of it crosses onto the compute device, and the only place
    that happens. The three steps are then in the order the whole-track version
    used, which is what makes them bit-identical to it: cut, normalize, pad. Cut
    and pad only move values about. ``(x - shift) / scale`` is element-wise, so
    applying it to a window gives that window's samples exactly the values
    applying it to the track gave them — and doing it *before* the padding is
    what keeps the samples past the ends of the track at zero rather than at
    ``-shift / scale``.

    The normalization is written out of place on purpose: ``Tensor.to`` returns
    **self** when the tensor is already on the target device, so on a CPU run an
    in-place ``sub_`` here would rewrite the caller's decoded mixture — and every
    later window would then be normalized twice.
    """
    total = int(mixture.shape[-1])
    delta = target - length
    start = offset - delta // 2
    end = start + target
    first = max(0, start)
    last = min(total, end)
    part = mixture[..., first:last].to(device).sub(shift).div(scale)
    return torch.nn.functional.pad(part, (first - start, end - last))


def _center_trim(tensor: Tensor, length: int) -> Tensor:
    """Trim ``tensor`` to ``length`` about its centre (upstream's ``center_trim``)."""
    delta = int(tensor.shape[-1]) - length
    if delta < 0:
        raise ValueError(f"cannot trim {tensor.shape[-1]} samples up to {length}")
    if delta == 0:
        return tensor
    return tensor[..., delta // 2 : -(delta - delta // 2)]


__all__ = [
    "CHECKPOINT_PICKLE_GLOBALS",
    "DEFAULT_OVERLAP",
    "DEFAULT_TRANSITION_POWER",
    "DEMUCS_ARCHITECTURE",
    "CheckpointArchitecture",
    "DemucsParameters",
    "DemucsSeparator",
    "RestrictedUnpickler",
    "load_checkpoint_package",
    "stem_source_indices",
    "torch_pickle_globals",
]
