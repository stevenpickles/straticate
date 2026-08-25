"""The catalog model → :class:`~straticate.inference.base.Separator` seam.

Feature 015 has to turn "the model behind mode *vocals*, tier *balanced*" into
something that can actually separate audio. That mapping is the one place where
a model *architecture* is allowed to select an implementation — and it is
deliberately keyed **by architecture, not by model ID**:

- adding another model of an already-implemented architecture to
  ``models/catalog.json`` is a data edit, never a code change;
- a new inference backend (feature 026 and beyond) registers exactly one
  builder under its own architecture name and needs to touch nothing else;
- the API layer never names an architecture, a mode or a stem — it hands the
  registry a catalog :class:`~straticate.schemas.Model` and gets a separator
  (AGENTS.md principles 1 and 6).

The descriptor a separator advertises is built **from the catalog entry**
(:func:`separator_info_from_model`), so the catalog stays the single source of
truth for stems, sample rate, version and display name. Built-in constants such
as :data:`~straticate.inference.fake.FAKE_VOCALS_INFO` exist only so the fake
engine and the catalog file can be asserted consistent — they are never
consulted on the resolution path.

Instances are cached one per model ID: constructing a separator is expensive
for a real backend (it loads weights), the job manager runs one job at a time
(ARCHITECTURE.md §6), and a separator's own contract is one separation at a
time (feature 014) — so a shared, lazily created instance per model is exactly
right.

**Building one must not happen on the event loop.** For the fake engine a cache
miss costs microseconds; for a real backend it is hundreds of megabytes read off
disk and a 228-million-parameter network assembled, and doing that inside an
``async def`` handler would stall the job worker, the event dispatcher, every
other HTTP request and all WebSocket delivery for the duration. Hence
:meth:`SeparatorRegistry.aget` — the accessor request handlers use — which
offloads the build to a worker thread and serializes concurrent misses for the
same model behind one lock, so two simultaneous job submissions load the weights
once. :meth:`SeparatorRegistry.get` remains for synchronous callers (tests, and
anything already off the loop) and is documented as blocking.

**A builder's implementation module is imported on first use, not on ours.**
This module is reached from :mod:`straticate.main`, so importing a backend here
would make that backend's dependencies dependencies of *starting the
application*: feature 026's RoFormer separator imports torch at module scope,
which is how PyTorch — an *optional* runtime probe by feature 018's design —
became mandatory for a process that only ever wanted the fake engine. Feature
034 puts that back: the builder each architecture registers imports its
implementation inside :func:`build`, which runs in the worker thread
:meth:`SeparatorRegistry.aget` dispatches to, and an implementation that cannot
be imported is reported as :func:`_separator_unavailable` — the same
``separator_unavailable`` (501) this registry has always raised for a model it
cannot run.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from straticate.audio.ffmpeg import DEFAULT_FFMPEG_TIMEOUT_SECONDS
from straticate.errors import ApplicationError
from straticate.inference.base import Separator, SeparatorInfo
from straticate.inference.fake import (
    DEFAULT_CHUNK_DELAY_SECONDS,
    DEFAULT_CHUNK_SECONDS,
    DEFAULT_FADE_SECONDS,
    DEFAULT_FAKE_DEVICE,
    DEFAULT_MODEL_LOAD_SECONDS,
    FAKE_ARCHITECTURE,
    FakeDeviceProfile,
    FakeSeparator,
)
from straticate.inference.roformer.architecture import ROFORMER_ARCHITECTURE
from straticate.models.layout import weights_path
from straticate.schemas.models import Model

logger = logging.getLogger(__name__)

SeparatorBuilder = Callable[[Model], Separator]
"""Constructs the separator that runs one catalog model.

Called at most once per model ID (:meth:`SeparatorRegistry.get` caches the
result). Implementations must not assume anything about the model beyond what
:class:`~straticate.schemas.Model` carries.
"""


def _separator_unavailable(model: Model) -> ApplicationError:
    """The ``separator_unavailable`` (501) raised when this build cannot run ``model``.

    One function so that every way of reaching that conclusion produces the
    *identical* envelope — code, message and ``detail`` — because from a
    client's point of view they are one fact: the model is catalogued, and this
    build has no implementation able to run it. That is true whether no builder
    was ever registered for the architecture (a catalog naming ``"demucs"``) or
    the registered builder's implementation module cannot be imported (a build
    installed without the ``torch`` extra). Feature 034 deliberately did **not**
    add a second error code for the latter: the wire contract in
    ``docs/contracts/rest-api.md`` is unchanged, and *why* the implementation is
    unavailable is a deployment fact for the server's log, not for the browser
    (see :func:`roformer_separator_builder`).
    """
    return ApplicationError(
        "separator_unavailable",
        f"No separator implementation is available for model {model.id!r} "
        f"(architecture {model.architecture!r}).",
        status_code=501,
        detail={"model_id": model.id, "architecture": model.architecture},
    )


def _torch_is_installed() -> bool:
    """Whether ``torch`` can be *located*, without importing it.

    :func:`importlib.util.find_spec` walks the same finders an import would but
    stops before executing the module. That is what makes this safe to ask on a
    failure path: it costs microseconds (importing torch itself is seconds),
    and it cannot fail the way *executing* a broken installation does — which
    is the very situation it is asked about.
    """
    try:
        return importlib.util.find_spec("torch") is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        # A parent package that cannot be imported, or an entry in
        # ``sys.modules`` with no ``__spec__``. Either way torch is not usable.
        return False


def _log_backend_import_failure(model: Model, exc: ImportError) -> None:
    """Report *why* an architecture's implementation could not be imported.

    Two genuinely different deployment faults reach this point, and telling an
    operator the wrong one wastes their time:

    - **the optional extra was never installed** — the ordinary case, and the
      fix is one documented command;
    - **the extra is installed and the import still failed** — a broken or
      incompatible installation (a mismatched ``einops`` or
      ``rotary-embedding-torch`` inside the vendored architecture, a corrupted
      wheel), or a rename inside ``roformer/separator.py``. Advising a
      reinstall here would send someone to redo something they have already
      done; what they need is the underlying ``ImportError`` and to know that
      PyTorch itself is present.

    ``except ImportError`` stays deliberately wide — every one of these means
    "this build cannot run that model", which is the one thing the client is
    told (:func:`_separator_unavailable`). The distinction is drawn *here*,
    in the log, where an operator can act on it.
    """
    if _torch_is_installed():
        logger.warning(
            "Model %r needs the %r architecture, whose implementation is installed but "
            "failed to import: %s. PyTorch itself is present, so this is a broken or "
            "incompatible installation rather than a missing one -- check the versions of "
            "torch and of the vendored architecture's dependencies (numpy, einops, "
            "rotary-embedding-torch, beartype).",
            model.id,
            model.architecture,
            exc,
        )
    else:
        logger.warning(
            "Model %r needs the %r architecture, whose implementation requires PyTorch, "
            "which is not installed (%s). Install the optional dependencies "
            "('uv sync --extra torch') to enable it.",
            model.id,
            model.architecture,
            exc,
        )


def separator_info_from_model(model: Model) -> SeparatorInfo:
    """Project a catalog model onto the descriptor its separator advertises.

    The catalog is authoritative: stems, sample rate, version, display name and
    separation mode all come from the manifest entry, never from a constant
    baked into an implementation module.
    """
    return SeparatorInfo(
        model_id=model.id,
        display_name=model.display_name,
        architecture=model.architecture,
        version=model.version,
        separation_mode=model.separation_mode,
        stems=tuple(model.stems),
        sample_rate=model.sample_rate,
    )


def fake_separator_builder(
    *,
    chunk_seconds: float = DEFAULT_CHUNK_SECONDS,
    chunk_delay_seconds: float = DEFAULT_CHUNK_DELAY_SECONDS,
    model_load_seconds: float = DEFAULT_MODEL_LOAD_SECONDS,
    fade_seconds: float = DEFAULT_FADE_SECONDS,
    device: FakeDeviceProfile | None = DEFAULT_FAKE_DEVICE,
    ffmpeg_timeout_seconds: float = DEFAULT_FFMPEG_TIMEOUT_SECONDS,
) -> SeparatorBuilder:
    """Build the :data:`SeparatorBuilder` for the ``fake`` architecture.

    Every catalog model whose ``architecture`` is
    :data:`~straticate.inference.fake.FAKE_ARCHITECTURE` is served by a
    :class:`~straticate.inference.fake.FakeSeparator` configured from the
    catalog entry, so a third fake model needs no code change at all.

    The tuning arguments are passed straight through to ``FakeSeparator``.
    Production keeps its defaults — the per-chunk delay is what makes the M1
    demo's progress visibly real-time — while tests inject a builder with
    ``chunk_delay_seconds=0.0`` and ``model_load_seconds=0.0`` so a full job
    runs as fast as the machine allows.
    """

    def build(model: Model) -> Separator:
        return FakeSeparator(
            separator_info_from_model(model),
            chunk_seconds=chunk_seconds,
            chunk_delay_seconds=chunk_delay_seconds,
            model_load_seconds=model_load_seconds,
            fade_seconds=fade_seconds,
            device=device,
            ffmpeg_timeout_seconds=ffmpeg_timeout_seconds,
        )

    return build


InferenceParameterSource = Callable[[str], Mapping[str, Any] | None]
"""Resolves a model ID to its manifest's ``default_inference_parameters``.

A deliberately narrow seam. A real backend needs the checkpoint's own
hyperparameters, which ARCHITECTURE.md §9 keeps in the catalog *as data* and
``models/catalog.py`` keeps off the public :class:`~straticate.schemas.Model` —
so the builder needs a way back to the catalog entry. Passing a one-argument
lookup rather than the whole :class:`~straticate.models.ModelCatalog` keeps this
module from importing the catalog service at all, and lets a test supply a dict.
"""


def roformer_separator_builder(
    *,
    models_dir: Path | None,
    inference_parameters: InferenceParameterSource | None = None,
    ffmpeg_timeout_seconds: float = DEFAULT_FFMPEG_TIMEOUT_SECONDS,
) -> SeparatorBuilder:
    """Build the :data:`SeparatorBuilder` for the Mel-Band RoFormer architecture.

    Every catalog model whose ``architecture`` is
    :data:`~straticate.inference.roformer.ROFORMER_ARCHITECTURE` is served by a
    :class:`~straticate.inference.roformer.RoFormerSeparator` configured from the
    catalog entry — its descriptor from the public
    :class:`~straticate.schemas.Model`, its hyperparameters and chunking from the
    entry's ``default_inference_parameters``. **Adding a second RoFormer
    checkpoint is therefore a pure data edit.**

    Weights are located, never fetched: ``{models_dir}/weights/{model_id}/…``,
    exactly where feature 025's installer publishes them after verifying their
    SHA-256. A model whose weights are absent fails with
    ``model_weights_missing`` (409) at construction — which, because
    :meth:`SeparatorRegistry.aget` is awaited inside ``POST /jobs``, is the
    status that request answers with.

    **The implementation is imported inside** :func:`build`, not at this
    module's scope, and that is the whole of feature 034.
    :class:`~straticate.inference.roformer.RoFormerSeparator` imports torch;
    importing it here made PyTorch a dependency of importing
    :mod:`straticate.main`, so a fake-separator-only deployment (CI's ``e2e``
    tier, a frontend developer, a contributor without the disk) had to install
    ~183 MiB it would never execute. Now ``uv sync`` gives a working
    application and ``uv sync --extra torch`` is what enables this backend.

    A build without that extra raises ``ImportError`` here, which becomes
    :func:`_separator_unavailable` — the **same** 501 envelope a catalogued but
    unimplemented architecture has always produced, because it is the same fact
    about this build. The diagnosis goes to a ``WARNING`` log record instead,
    where :func:`_log_backend_import_failure` separates "the optional extra is
    not installed" from "the extra is installed and the import still failed" —
    an operator debugging a broken installation must not be sent to reinstall
    something they already have. The browser is told only that the server
    cannot run this model, which is all it can act on.

    Args:
        models_dir: ``Settings.models_dir``. ``None`` means this process was not
            told where weights live, which is a wiring error and is reported as
            ``separator_unavailable`` rather than guessed at.
        inference_parameters: Lookup for the model's
            ``default_inference_parameters``.
        ffmpeg_timeout_seconds: Bound for the separator's decode subprocesses.
    """

    def build(model: Model) -> Separator:
        if models_dir is None or inference_parameters is None:
            raise ApplicationError(
                "separator_unavailable",
                (
                    f"Model {model.id!r} needs installed weights, but this application "
                    f"was built without a models directory."
                ),
                status_code=501,
                detail={"model_id": model.id, "architecture": model.architecture},
            )
        try:
            from straticate.inference.roformer import RoFormerParameters, RoFormerSeparator
        except ImportError as exc:
            _log_backend_import_failure(model, exc)
            raise _separator_unavailable(model) from exc
        return RoFormerSeparator(
            separator_info_from_model(model),
            weights_file=weights_path(models_dir, model.id),
            parameters=RoFormerParameters.from_catalog(
                inference_parameters(model.id), model_id=model.id
            ),
            ffmpeg_timeout_seconds=ffmpeg_timeout_seconds,
        )

    return build


def default_separator_builders(
    *,
    ffmpeg_timeout_seconds: float = DEFAULT_FFMPEG_TIMEOUT_SECONDS,
    models_dir: Path | None = None,
    inference_parameters: InferenceParameterSource | None = None,
) -> Mapping[str, SeparatorBuilder]:
    """The builders a :class:`SeparatorRegistry` starts with.

    Two architectures: the fake engine (feature 014) and Mel-Band RoFormer
    (feature 026). Both are always registered — **registering a builder imports
    nothing** (feature 034), so this map is the same map whether or not the
    ``torch`` extra is installed, and a catalog naming anything else still gets
    ``separator_unavailable`` (501). :attr:`SeparatorRegistry.architectures` is
    therefore a statement of the architectures this build *implements*; whether
    an implementation's dependencies are actually installed is settled when the
    first model of that architecture is built, and reported as the same 501.
    The alternative — probing every backend's imports at startup so the set
    could be filtered — would pay torch's multi-second import on every start
    for a fact only ``POST /jobs`` ever needs, which is the cost this feature
    exists to stop paying.

    ``ffmpeg_timeout_seconds``, ``models_dir`` and ``inference_parameters`` are
    the application facts a separator needs. :func:`straticate.main.create_app`
    passes all three, so an application built with explicit settings really
    governs its subprocesses and really reads weights from *its* models
    directory. They are *builder* arguments rather than registry ones so the
    registry keeps knowing nothing about how any particular engine decodes audio
    or finds its weights.
    """
    return MappingProxyType(
        {
            FAKE_ARCHITECTURE: fake_separator_builder(
                ffmpeg_timeout_seconds=ffmpeg_timeout_seconds
            ),
            ROFORMER_ARCHITECTURE: roformer_separator_builder(
                models_dir=models_dir,
                inference_parameters=inference_parameters,
                ffmpeg_timeout_seconds=ffmpeg_timeout_seconds,
            ),
        }
    )


class SeparatorRegistry:
    """Resolves catalog models to separator instances, caching one per model.

    Args:
        builders: Architecture → builder mapping. Defaults to
            :func:`default_separator_builders`; tests inject fast (zero-delay)
            builders through this argument.
    """

    __slots__ = ("_builders", "_instances", "_locks", "_registry_lock")

    def __init__(self, builders: Mapping[str, SeparatorBuilder] | None = None) -> None:
        source = default_separator_builders() if builders is None else builders
        self._builders: dict[str, SeparatorBuilder] = dict(source)
        self._instances: dict[str, Separator] = {}
        # ``threading`` locks rather than ``asyncio`` ones, for two reasons.
        # An ``asyncio.Lock`` binds to whichever loop first awaits it, and a
        # registry outlives any one loop — an application object driven by an
        # async client and then by a synchronous ``TestClient``, or two
        # ``asyncio.run`` calls, would meet ``RuntimeError: ... is bound to a
        # different event loop`` on the next cache miss instead of building.
        # And the build itself already runs in a worker thread, so a thread
        # lock is what actually guards it; nothing here is ever held across an
        # ``await``.
        self._locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    @property
    def architectures(self) -> frozenset[str]:
        """The architectures this registry can build a separator for."""
        return frozenset(self._builders)

    def register(self, architecture: str, builder: SeparatorBuilder) -> None:
        """Register (or replace) the builder for ``architecture``.

        Replacing a builder does not evict separators already created from the
        previous one — construct a fresh registry if that matters.
        """
        self._builders[architecture] = builder

    def get(self, model: Model) -> Separator:
        """Return the separator for ``model``, creating it on first use.

        **Blocking.** A cache miss runs the architecture's builder inline, which
        for a real backend reads weights off disk. Never call this from a
        coroutine running on the application's event loop — use :meth:`aget`.

        Raises:
            ApplicationError: ``separator_unavailable`` (501) when no builder
                is registered for the model's architecture, **or** when the
                registered builder's implementation cannot be imported because
                this build was installed without its optional dependencies
                (feature 034). Both mean the same thing — the model is
                catalogued but this build has no implementation able to run it
                — and both produce the identical envelope
                (:func:`_separator_unavailable`). A builder may raise its own
                ``ApplicationError`` (for instance ``model_weights_missing``),
                which propagates unchanged.
        """
        cached = self._instances.get(model.id)
        if cached is not None:
            return cached
        return self._build_once(model)

    async def aget(self, model: Model) -> Separator:
        """Return the separator for ``model``, building it **off the event loop**.

        This is what request handlers call. A cache hit returns without ever
        suspending; a miss takes a per-model lock, re-checks the cache, and runs
        the builder in a worker thread through :func:`asyncio.to_thread`, so the
        loop keeps serving other requests, dispatching job events and pushing
        WebSocket frames while several hundred megabytes of weights load.

        The lock is a **thread** lock taken inside that worker (see
        :meth:`_lock_for`), not an ``asyncio`` one: nothing is held across an
        ``await``, and a registry outlives any single event loop.

        Raises:
            ApplicationError: As :meth:`get`.
        """
        cached = self._instances.get(model.id)
        if cached is not None:
            return cached
        return await asyncio.to_thread(self._build_once, model)

    def _build_once(self, model: Model) -> Separator:
        """Build and cache ``model``'s separator, at most once, under its lock.

        Runs in whichever thread called it — the worker thread for
        :meth:`aget`, the caller's own for :meth:`get`. Everything that touches
        the instance cache happens here, so both accessors share one definition
        of "build once".
        """
        with self._lock_for(model.id):
            cached = self._instances.get(model.id)
            if cached is not None:
                return cached
            separator = self._build(model)
            self._instances[model.id] = separator
            return separator

    def _lock_for(self, model_id: str) -> threading.Lock:
        """The lock guarding one model's construction, created on first need.

        Per model ID rather than global, so two jobs for *different* models may
        load in parallel while two racing submissions for the *same* model load
        it once — which for a 228-million-parameter network is a wasted gigabyte
        rather than a wasted millisecond. The registry-wide lock exists only to
        make this mapping thread-safe, and is never held while a build runs.
        """
        with self._registry_lock:
            lock = self._locks.get(model_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[model_id] = lock
            return lock

    def _build(self, model: Model) -> Separator:
        """Run the builder registered for ``model``'s architecture."""
        builder = self._builders.get(model.architecture)
        if builder is None:
            raise _separator_unavailable(model)
        return builder(model)


__all__ = [
    "InferenceParameterSource",
    "SeparatorBuilder",
    "SeparatorRegistry",
    "default_separator_builders",
    "fake_separator_builder",
    "roformer_separator_builder",
    "separator_info_from_model",
]
